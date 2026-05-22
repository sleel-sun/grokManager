"""Voice token endpoint — LiveKit token acquisition."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.control.account.enums import FeedbackKind
from app.control.account.invalid_credentials import feedback_kind_for_error
from app.dataplane.reverse.protocol.xai_usage import is_invalid_credentials_error
from app.platform.config.snapshot import get_config
from app.platform.errors import AppError, RateLimitError, UpstreamError
from app.platform.logging.logger import logger
from app.platform.runtime.clock import now_s
from app.platform.auth.middleware import verify_webui_key
from app.products._account_selection import selection_max_retries

router = APIRouter(prefix="/webui/api", dependencies=[Depends(verify_webui_key)], tags=["WebUI - Voice"])


class VoiceTokenResponse(BaseModel):
    token: str
    url: str
    participant_name: str = ""
    room_name: str = ""


class VoiceTokenRequest(BaseModel):
    voice: str = "ara"
    personality: str = "assistant"
    speed: float = 1.0
    instruction: str = ""


def _payload_candidates(data: dict) -> list[dict]:
    candidates = [data]
    for key in ("data", "result", "livekit", "session"):
        value = data.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    return candidates


def _first_str(data: dict, *keys: str) -> str:
    for payload in _payload_candidates(data):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _normalize_livekit_token_response(data: dict) -> VoiceTokenResponse:
    token = _first_str(data, "token", "accessToken", "access_token", "jwt")
    if not token:
        raise UpstreamError("Upstream returned no voice token", body=str(data)[:400])

    return VoiceTokenResponse(
        token=token,
        url=_first_str(
            data,
            "livekitUrl",
            "livekit_url",
            "serverUrl",
            "server_url",
            "url",
        ) or "wss://livekit.grok.com",
        participant_name=_first_str(
            data,
            "participantName",
            "participant_name",
            "participantIdentity",
            "participant_identity",
            "identity",
        ),
        room_name=_first_str(data, "roomName", "room_name", "room"),
    )


def _parse_retry_codes(raw) -> frozenset[int]:
    if raw is None:
        return frozenset()
    if isinstance(raw, (list, tuple, set)):
        parts = raw
    else:
        parts = str(raw).split(",")
    codes: set[int] = set()
    for part in parts:
        text = str(part).strip()
        if not text:
            continue
        try:
            codes.add(int(text))
        except ValueError:
            logger.warning("invalid retry status code ignored: {}", text)
    return frozenset(codes)


def _voice_retry_codes() -> frozenset[int]:
    cfg = get_config()
    raw = cfg.get("retry.on_codes")
    if raw is None:
        raw = cfg.get("retry.retry_status_codes", "429,401,503")
    # LiveKit token negotiation often fails through transport/proxy layers as a
    # local 502, so voice retries that status even when the global app retry
    # list keeps chat retries narrower.
    return _parse_retry_codes(raw) | frozenset({502})


def _should_retry_voice(exc: UpstreamError, retry_codes: frozenset[int]) -> bool:
    return exc.status in retry_codes or is_invalid_credentials_error(exc)


def _feedback_kind(exc: BaseException | None) -> FeedbackKind:
    return feedback_kind_for_error(exc)


@router.post("/voice/token", response_model=VoiceTokenResponse)
async def voice_token(request: VoiceTokenRequest):
    """Acquire a LiveKit voice session token."""
    from app.dataplane.account import _directory as _acct_dir
    if _acct_dir is None:
        raise RateLimitError("Account directory not initialised")
    directory = _acct_dir

    # Voice uses super/basic pools → try super first, then basic, then heavy.
    from app.control.model.enums import ModeId

    mode_id = int(ModeId.AUTO)
    max_retries = selection_max_retries()
    retry_codes = _voice_retry_codes()
    excluded: list[str] = []
    last_exc: UpstreamError | None = None

    from app.dataplane.reverse.transport.livekit import fetch_livekit_token

    for attempt in range(max_retries + 1):
        acct = await directory.reserve(
            pool_candidates=(1, 0, 2),
            mode_id=mode_id,
            exclude_tokens=excluded or None,
            now_s_override=now_s(),
        )
        if acct is None:
            if last_exc is not None:
                raise last_exc
            raise RateLimitError("No available tokens for voice mode")

        token = acct.token
        success = False
        fail_exc: BaseException | None = None
        should_retry = False
        try:
            data = await fetch_livekit_token(
                token,
                voice=request.voice,
                personality=request.personality,
                speed=request.speed,
                custom_instruction=request.instruction.strip(),
            )
            response = _normalize_livekit_token_response(data)
            success = True
            return response
        except UpstreamError as exc:
            fail_exc = exc
            last_exc = exc
            should_retry = (
                attempt < max_retries and _should_retry_voice(exc, retry_codes)
            )
            if not should_retry:
                raise
            logger.warning(
                "voice token retry scheduled: attempt={}/{} status={} token={}...",
                attempt + 1,
                max_retries,
                exc.status,
                token[:8],
            )
        except AppError:
            raise
        except Exception as exc:
            wrapped = UpstreamError(f"Voice token error: {exc}", body=str(exc)[:400])
            fail_exc = wrapped
            last_exc = wrapped
            should_retry = (
                attempt < max_retries and _should_retry_voice(wrapped, retry_codes)
            )
            if not should_retry:
                raise wrapped from exc
            logger.warning(
                "voice token retry scheduled after transport error: attempt={}/{} token={}... error={}",
                attempt + 1,
                max_retries,
                token[:8],
                exc,
            )
        finally:
            await directory.release(acct)
            kind = (
                FeedbackKind.SUCCESS
                if success
                else _feedback_kind(fail_exc)
                if fail_exc
                else FeedbackKind.SERVER_ERROR
            )
            await directory.feedback(token, kind, mode_id, now_s_val=now_s())

        if should_retry:
            excluded.append(token)
            continue

    if last_exc is not None:
        raise last_exc
    raise RateLimitError("No available tokens for voice mode")
