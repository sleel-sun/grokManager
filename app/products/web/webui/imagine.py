"""WebUI imagine endpoint backed by Grok Imagine WebSocket only."""

import asyncio
import hmac
import uuid
from typing import Optional

import orjson
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.platform.auth.middleware import get_webui_key, is_webui_enabled
from app.platform.config.snapshot import get_config
from app.platform.errors import UpstreamError
from app.platform.logging.logger import logger
from app.platform.runtime.clock import now_s
from app.control.account.enums import FeedbackKind
from app.products.openai.images import (
    _image_feedback_kind,
    _image_max_retries,
    _image_retry_codes,
    _image_stream_error_to_upstream_error,
    _rotate_pool_candidates,
    _schedule_account_sync,
    _should_retry_image_upstream,
    resolve_aspect_ratio,
)

router = APIRouter()


def _image_event_error_payload(event: dict, run_id: str) -> dict:
    """Normalize upstream WS errors to the WebUI error contract."""
    message = str(
        event.get("message")
        or event.get("error")
        or event.get("detail")
        or "Image generation failed"
    ).strip()
    code = str(event.get("code") or event.get("error_code") or "upstream_error").strip()
    return {
        "type": "error",
        "message": message or "Image generation failed",
        "code": code or "upstream_error",
        "run_id": run_id,
    }


def _empty_webui_image_error() -> UpstreamError:
    return UpstreamError("Image generation returned no images")


def _image_event_has_displayable_output(event: dict) -> bool:
    if not isinstance(event, dict) or event.get("type") != "image":
        return False
    image_url = event.get("image_url")
    if isinstance(image_url, dict):
        image_url = image_url.get("url")
    for value in (
        event.get("url"),
        image_url,
        event.get("imageUrl"),
        event.get("src"),
        event.get("blob"),
        event.get("b64_json"),
        event.get("base64"),
    ):
        if str(value or "").strip():
            return True
    return False


async def _acquire_token(
    exclude_tokens: list[str] | None = None,
    *,
    attempt: int = 0,
):
    from app.dataplane.account import _directory as _acct_dir
    if _acct_dir is None:
        return None, None
    from app.control.model.registry import get as get_model
    spec = get_model("grok-imagine-image")
    if spec is None:
        return None, None
    # Masonry uses Grok Imagine WebSocket models, which require Super/Heavy
    # image access. Falling back to basic accounts only burns retries and
    # produces upstream "Image rate limit exceeded" for every attempt.
    acct = await _acct_dir.reserve(
        pool_candidates=_rotate_pool_candidates(spec.pool_candidates(), attempt),
        mode_id=int(spec.mode_id),
        exclude_tokens=exclude_tokens or None,
        now_s_override=now_s(),
    )
    if acct is None:
        return None, None
    return acct.token, acct


def _no_webui_image_accounts_message(excluded_count: int = 0) -> str:
    if excluded_count > 0:
        return (
            "All available Super/Heavy image accounts are currently rate-limited. "
            "Wait for image quota reset or import more Super/Heavy accounts."
        )
    return (
        "Masonry image generation requires Super or Heavy accounts. "
        "Import Super/Heavy accounts before using this page."
    )


def _webui_image_exhausted_message() -> str:
    return (
        "All available Super/Heavy image accounts are currently rate-limited. "
        "Wait for image quota reset or import more Super/Heavy accounts."
    )


def _extract_token(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    scheme, _, token = raw.partition(" ")
    if scheme.lower() == "bearer" and token:
        return token.strip()
    return raw


def _is_allowed(token: str) -> bool:
    webui_key = get_webui_key()
    if not webui_key:
        return is_webui_enabled()
    return bool(token) and hmac.compare_digest(token, webui_key)


def _websocket_token(websocket: WebSocket) -> str:
    return (
        _extract_token(websocket.headers.get("authorization"))
        or str(websocket.query_params.get("access_token") or "").strip()
    )


@router.websocket("/imagine/ws")
async def imagine_ws(websocket: WebSocket):
    if not _is_allowed(_websocket_token(websocket)):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    stop_event = asyncio.Event()
    run_task: Optional[asyncio.Task] = None

    async def _send(payload: dict) -> bool:
        try:
            await websocket.send_text(orjson.dumps(payload).decode())
            return True
        except Exception:
            return False

    async def _stop_run():
        nonlocal run_task
        stop_event.set()
        if run_task and not run_task.done():
            run_task.cancel()
            try:
                await run_task
            except Exception:
                pass
        run_task = None
        stop_event.clear()

    async def _run(
        prompt: str,
        aspect_ratio: str,
        nsfw: Optional[bool],
        count: int,
        quality: str,
    ):
        from app.dataplane.account import _directory as _acct_dir
        from app.dataplane.reverse.transport.imagine_ws import stream_images

        run_id = uuid.uuid4().hex
        enable_pro = quality == "quality"
        await _send({
            "type": "status",
            "status": "running",
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "run_id": run_id,
            "count": count,
            "quality": quality,
        })

        enable_nsfw = nsfw if nsfw is not None else get_config().get_bool("features.enable_nsfw", True)
        cfg = get_config()
        max_retries = _image_max_retries(cfg)
        retry_codes = _image_retry_codes(cfg)
        excluded: list[str] = []
        last_exc = None
        try:
            for attempt in range(max_retries + 1):
                token, acct = await _acquire_token(excluded or None, attempt=attempt)
                if not token:
                    exhausted = last_exc is not None or bool(excluded)
                    await _send({
                        "type": "error",
                        "message": _webui_image_exhausted_message()
                        if exhausted
                        else _no_webui_image_accounts_message(len(excluded)),
                        "code": "rate_limit_exceeded" if exhausted else "image_pool_unavailable",
                    })
                    return

                success = False
                should_retry = False
                fail_exc = None
                delivered_images = 0
                try:
                    async for event in stream_images(
                        token,
                        prompt,
                        aspect_ratio=aspect_ratio,
                        n=count,
                        enable_nsfw=enable_nsfw,
                        enable_pro=enable_pro,
                    ):
                        if stop_event.is_set():
                            return
                        if not isinstance(event, dict) or event.get("type") == "_meta":
                            continue
                        if event.get("type") == "error":
                            exc = _image_stream_error_to_upstream_error(
                                event,
                                prefix="Image generation failed",
                            )
                            fail_exc = exc
                            last_exc = exc
                            should_retry = (
                                attempt < max_retries
                                and _should_retry_image_upstream(exc, retry_codes)
                            )
                            if should_retry:
                                logger.warning(
                                    "webui imagine retry scheduled: attempt={}/{} status={} token={}...",
                                    attempt + 1,
                                    max_retries,
                                    exc.status,
                                    token[:8],
                                )
                                break
                            await _send(_image_event_error_payload(event, run_id))
                            return

                        if _image_event_has_displayable_output(event):
                            delivered_images += 1
                        event.setdefault("run_id", run_id)
                        await _send(event)

                    if should_retry:
                        excluded.append(token)
                        continue

                    if delivered_images <= 0:
                        exc = _empty_webui_image_error()
                        fail_exc = exc
                        last_exc = exc
                        should_retry = (
                            attempt < max_retries
                            and _should_retry_image_upstream(exc, retry_codes)
                        )
                        if should_retry:
                            logger.warning(
                                "webui imagine empty result retry scheduled: attempt={}/{} status={} token={}...",
                                attempt + 1,
                                max_retries,
                                exc.status,
                                token[:8],
                            )
                            excluded.append(token)
                            continue
                        await _send({
                            "type": "error",
                            "message": exc.message,
                            "code": exc.code,
                            "run_id": run_id,
                        })
                        return

                    success = True
                    if not stop_event.is_set():
                        await _send({
                            "type": "status",
                            "status": "completed",
                            "run_id": run_id,
                            "count": count,
                        })
                    return
                finally:
                    if acct and _acct_dir:
                        await _acct_dir.release(acct)
                        if not stop_event.is_set():
                            kind = (
                                FeedbackKind.SUCCESS
                                if success
                                else _image_feedback_kind(fail_exc)
                            )
                            mode_id = int(getattr(acct, "mode_id", 0))
                            await _acct_dir.feedback(
                                token,
                                kind,
                                mode_id,
                                now_s_val=now_s(),
                            )
                            _schedule_account_sync(
                                token,
                                mode_id,
                                success=success,
                                fail_exc=fail_exc,
                            )

            await _send({
                "type": "error",
                "message": _webui_image_exhausted_message()
                if last_exc or excluded
                else _no_webui_image_accounts_message(len(excluded)),
                "code": "rate_limit_exceeded" if last_exc or excluded else "image_pool_unavailable",
            })
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(
                "webui imagine run failed: error_type={} error={}",
                type(exc).__name__,
                exc,
            )
            await _send({
                "type": "error",
                "message": str(exc),
                "code": "internal_error",
            })
        finally:
            if stop_event.is_set():
                await _send({"type": "status", "status": "stopped", "run_id": run_id})

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except (RuntimeError, WebSocketDisconnect):
                break

            try:
                payload = orjson.loads(raw)
            except Exception:
                await _send({
                    "type": "error",
                    "message": "Invalid message format.",
                    "code": "invalid_payload",
                })
                continue

            action = payload.get("type")
            if action == "start":
                prompt = str(payload.get("prompt") or "").strip()
                if not prompt:
                    await _send({
                        "type": "error",
                        "message": "Prompt cannot be empty.",
                        "code": "invalid_prompt",
                    })
                    continue
                aspect_ratio = resolve_aspect_ratio(str(payload.get("aspect_ratio") or "2:3").strip() or "2:3")
                quality = str(payload.get("quality") or "speed").strip().lower()
                if quality not in {"speed", "quality"}:
                    quality = "speed"
                nsfw = payload.get("nsfw")
                if nsfw is not None:
                    if isinstance(nsfw, str):
                        nsfw = nsfw.strip().lower() in {"1", "true", "yes", "on"}
                    else:
                        nsfw = bool(nsfw)
                try:
                    count = int(payload.get("count") or 6)
                except (TypeError, ValueError):
                    count = 6
                count = max(1, min(count, 6))
                await _stop_run()
                run_task = asyncio.create_task(_run(prompt, aspect_ratio, nsfw, count, quality))
                continue

            if action == "stop":
                await _stop_run()
                continue

            await _send({
                "type": "error",
                "message": "Unknown action.",
                "code": "invalid_action",
            })
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.error(
            "webui imagine websocket handler failed: error_type={} error={}",
            type(exc).__name__,
            exc,
        )
    finally:
        await _stop_run()
        try:
            from starlette.websockets import WebSocketState
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close(code=1000, reason="Server closing connection")
        except Exception:
            pass
