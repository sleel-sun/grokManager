"""ChatGPT web-backed GPT image generation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncGenerator
from uuid import uuid4

import aiohttp
import orjson

from app.control.account.commands import AccountPatch, ListAccountsQuery
from app.control.account.runtime import get_account_repository
from app.platform.config.snapshot import get_config
from app.platform.errors import AppError, RateLimitError, UpstreamError, ValidationError
from app.platform.logging.logger import logger
from app.platform.paths import data_path
from app.platform.storage import save_local_image

from ._format import make_chat_response, make_response_id, make_stream_chunk, make_thinking_chunk

BASE_URL = "https://chatgpt.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
CLIENT_BUILD_NUMBER = "5955942"
CLIENT_VERSION = "prod-be885abbfcfe7b1f511e88b3003d9ee44757fbad"
TIMEZONE = "America/Los_Angeles"
TIMEZONE_OFFSET_MIN = -480
MAX_POW_ATTEMPTS = 500000
_TRANSIENT_STATUSES = {429, 502, 503, 504}
_FILE_ID_RE = re.compile(r"(file-service://|sediment://)([A-Za-z0-9_-]+)")
_DATA_BUILD_RE = re.compile(r'data-build="([^"]*)"', re.I)
_SCRIPT_RE = re.compile(r'<script[^>]+src="([^"]+)"', re.I)
_DEFAULT_GENERATION_TIMEOUT_S = 60.0
_INVALID_CREDENTIAL_MARKERS = (
    "invalid token",
    "expired token",
    "unauthorized",
    "authentication",
    "not authenticated",
    "login required",
    "access token",
    "invalid_api_key",
)


@dataclass(slots=True)
class GPTImageAccount:
    record_token: str
    access_token: str
    is_free: bool = False
    status_key: str = "gpt_image_status"
    error_key: str = "gpt_image_error"


@dataclass(slots=True)
class _ChatGPTContext:
    access_token: str
    device_id: str
    script: str
    dpl: str


@dataclass(slots=True)
class _GeneratedImage:
    b64_json: str
    mime_type: str = "image/png"


def _app_url() -> str:
    return get_config().get_str("app.app_url", "").rstrip("/")


def _generation_timeout_s() -> float:
    try:
        value = get_config().get_float("gpt_image.timeout", _DEFAULT_GENERATION_TIMEOUT_S)
    except Exception:
        value = _DEFAULT_GENERATION_TIMEOUT_S
    return max(5.0, float(value or _DEFAULT_GENERATION_TIMEOUT_S))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _mask(value: str) -> str:
    value = str(value or "")
    return f"{value[:8]}...{value[-8:]}" if len(value) > 20 else value


def _local_image_url(file_id: str) -> str:
    path = f"/v1/files/image?id={file_id}"
    app_url = _app_url()
    return f"{app_url}{path}" if app_url else path


def _image_value(image: _GeneratedImage, response_format: str) -> dict[str, str]:
    fmt = (response_format or "url").strip().lower()
    if fmt == "b64_json":
        return {"b64_json": image.b64_json}
    if fmt != "url":
        raise ValidationError(
            "response_format must be one of ['url', 'b64_json']",
            param="response_format",
        )
    raw = base64.b64decode(image.b64_json)
    file_id = hashlib.sha1(raw).hexdigest()[:32]
    saved_id = save_local_image(raw, image.mime_type, file_id)
    return {"url": _local_image_url(saved_id)}


def _markdown_value(image: _GeneratedImage, response_format: str) -> str:
    value = _image_value(image, response_format)
    if "url" in value:
        return f"![image]({value['url']})"
    return f"![image](data:{image.mime_type};base64,{value['b64_json']})"


def _browser_headers(device_id: str) -> dict[str, str]:
    return {
        "user-agent": USER_AGENT,
        "accept-language": "en-US,en;q=0.9",
        "origin": BASE_URL,
        "referer": f"{BASE_URL}/",
        "accept": "*/*",
        "sec-ch-ua": '"Microsoft Edge";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "oai-device-id": device_id,
    }


def _conversation_headers(
    context: _ChatGPTContext,
    chat_token: str,
    proof_token: str | None,
) -> dict[str, str]:
    headers = {
        **_browser_headers(context.device_id),
        "authorization": f"Bearer {context.access_token}",
        "accept": "text/event-stream",
        "content-type": "application/json",
        "oai-language": "zh-CN",
        "oai-client-build-number": CLIENT_BUILD_NUMBER,
        "oai-client-version": CLIENT_VERSION,
        "openai-sentinel-chat-requirements-token": chat_token,
    }
    if proof_token:
        headers["openai-sentinel-proof-token"] = proof_token
    return headers


async def _response_error(response: aiohttp.ClientResponse, prefix: str) -> UpstreamError:
    try:
        body = await response.text()
    except Exception:
        body = ""
    detail = body[:500]
    message = f"{prefix}: upstream returned {response.status}"
    if detail:
        message = f"{message}: {detail}"
    return UpstreamError(message, status=response.status, body=detail)


async def _request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    data: bytes | None = None,
    timeout_s: float = 30.0,
    retries: int = 4,
    retry_statuses: set[int] | None = None,
) -> aiohttp.ClientResponse:
    retry_statuses = retry_statuses or set()
    last_exc: BaseException | None = None
    for attempt in range(max(1, retries)):
        try:
            response = await session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                data=data,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            )
            if response.status in retry_statuses and attempt < retries - 1:
                response.release()
                await asyncio.sleep(2 * (attempt + 1))
                continue
            return response
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                await asyncio.sleep(2 * (attempt + 1))
                continue
    raise UpstreamError(f"ChatGPT image request failed: {last_exc}") from last_exc


async def _bootstrap(session: aiohttp.ClientSession, access_token: str) -> _ChatGPTContext:
    response = await _request(
        session,
        "GET",
        f"{BASE_URL}/",
        headers={
            "user-agent": USER_AGENT,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
        },
        timeout_s=30.0,
    )
    if not response.ok:
        raise await _response_error(response, "ChatGPT bootstrap failed")
    html = await response.text()
    device_id = response.cookies.get("oai-did")
    script_match = _SCRIPT_RE.search(html)
    build_match = _DATA_BUILD_RE.search(html)
    return _ChatGPTContext(
        access_token=access_token,
        device_id=(device_id.value if device_id else str(uuid4())),
        script=script_match.group(1) if script_match else f"{BASE_URL}/backend-api/sentinel/sdk.js",
        dpl=build_match.group(1) if build_match else "",
    )


def _pow_config(context: _ChatGPTContext) -> list[Any]:
    return [
        random.choice([3000, 4000, 6000]),
        time.strftime("%a %b %d %Y %H:%M:%S GMT-0500 (Eastern Standard Time)"),
        4294705152,
        0,
        USER_AGENT,
        context.script,
        context.dpl,
        "en-US",
        "en-US,es-US,en,es",
        0,
        random.choice(
            [
                "webdriver-false",
                "vendor-Google Inc.",
                "cookieEnabled-true",
                "pdfViewerEnabled-true",
                "hardwareConcurrency-32",
            ]
        ),
        random.choice(["location", "_reactListeningo743lnnpvdg"]),
        random.choice(["innerWidth", "innerHeight", "devicePixelRatio", "screen", "chrome"]),
        int(time.time() * 1000) % 100000,
        str(uuid4()),
        "",
        random.choice([8, 16, 24, 32]),
        int(time.time() * 1000),
    ]


def _json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _generate_answer(seed: str, difficulty: str, config: list[Any]) -> str:
    target = bytes.fromhex(difficulty)
    seed_bytes = seed.encode("utf-8")
    part1 = f"{_json_compact(config[:3])[:-1]},".encode("utf-8")
    part2 = f",{_json_compact(config[4:9])[1:-1]},".encode("utf-8")
    part3 = f",{_json_compact(config[10:])[1:]}".encode("utf-8")
    for attempt in range(MAX_POW_ATTEMPTS):
        encoded = base64.b64encode(
            b"".join(
                [
                    part1,
                    str(attempt).encode(),
                    part2,
                    str(attempt >> 1).encode(),
                    part3,
                ]
            )
        ).decode("ascii")
        digest = hashlib.sha3_512(seed_bytes + encoded.encode("utf-8")).digest()
        if digest[: len(target)] <= target:
            return encoded
    return base64.b64encode(json.dumps(seed).encode("utf-8")).decode("ascii")


def _requirements_token(context: _ChatGPTContext) -> str:
    return f"gAAAAAC{_generate_answer(str(random.random()), '0fffff', _pow_config(context))}"


def _proof_token(context: _ChatGPTContext, proof_info: dict[str, Any] | None) -> str | None:
    if not proof_info or not proof_info.get("required"):
        return None
    seed = str(proof_info.get("seed") or "")
    difficulty = str(proof_info.get("difficulty") or "")
    if not seed or not difficulty:
        return None
    return f"gAAAAAB{_generate_answer(seed, difficulty, _pow_config(context))}"


async def _chat_requirements(
    session: aiohttp.ClientSession,
    context: _ChatGPTContext,
) -> tuple[str, dict[str, Any] | None]:
    response = await _request(
        session,
        "POST",
        f"{BASE_URL}/backend-api/sentinel/chat-requirements",
        headers={
            **_browser_headers(context.device_id),
            "authorization": f"Bearer {context.access_token}",
            "accept": "application/json",
            "content-type": "application/json",
        },
        json_body={"p": _requirements_token(context)},
        timeout_s=30.0,
        retry_statuses=_TRANSIENT_STATUSES,
    )
    if not response.ok:
        raise await _response_error(response, "ChatGPT chat-requirements failed")
    payload = await response.json(content_type=None)
    return str(payload.get("token") or ""), payload.get("proofofwork") or None


def _client_contextual_info() -> dict[str, Any]:
    return {
        "is_dark_mode": False,
        "time_since_loaded": random.randint(50, 500),
        "page_height": random.randint(500, 1000),
        "page_width": random.randint(1000, 2000),
        "pixel_ratio": 1.2,
        "screen_height": random.randint(800, 1200),
        "screen_width": random.randint(1200, 2200),
    }


def _upstream_model(requested_model: str, is_free: bool) -> str:
    model = (requested_model or "gpt-image-1").strip()
    if model == "gpt-image-1":
        return "auto"
    if model == "gpt-image-2":
        return "auto" if is_free else "gpt-5-3"
    return model or "gpt-4o"


async def _send_conversation(
    session: aiohttp.ClientSession,
    context: _ChatGPTContext,
    *,
    prompt: str,
    requested_model: str,
    is_free: bool,
) -> str:
    chat_token, proof_info = await _chat_requirements(session, context)
    response = await _request(
        session,
        "POST",
        f"{BASE_URL}/backend-api/conversation",
        headers=_conversation_headers(context, chat_token, _proof_token(context, proof_info)),
        json_body={
            "action": "next",
            "messages": [
                {
                    "id": str(uuid4()),
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": [prompt]},
                    "metadata": {"attachments": []},
                }
            ],
            "parent_message_id": str(uuid4()),
            "model": _upstream_model(requested_model, is_free),
            "history_and_training_disabled": False,
            "timezone_offset_min": TIMEZONE_OFFSET_MIN,
            "timezone": TIMEZONE,
            "conversation_mode": {"kind": "primary_assistant"},
            "conversation_origin": None,
            "force_paragen": False,
            "force_paragen_model_slug": "",
            "force_rate_limit": False,
            "force_use_sse": True,
            "paragen_cot_summary_display_override": "allow",
            "paragen_stream_type_override": None,
            "reset_rate_limits": False,
            "suggestions": [],
            "supported_encodings": [],
            "system_hints": ["picture_v2"],
            "variant_purpose": "comparison_implicit",
            "websocket_request_id": str(uuid4()),
            "client_contextual_info": _client_contextual_info(),
        },
        timeout_s=180.0,
        retry_statuses=_TRANSIENT_STATUSES,
    )
    if not response.ok:
        raise await _response_error(response, "ChatGPT image conversation failed")
    return await response.text()


def _parse_sse(raw_text: str) -> tuple[str, list[str], str]:
    conversation_id = ""
    file_ids: list[str] = []
    text_parts: list[str] = []
    for raw_line in str(raw_text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            break
        for match in _FILE_ID_RE.finditer(payload):
            prefix, file_id = match.groups()
            value = f"sed:{file_id}" if prefix == "sediment://" else file_id
            if value not in file_ids:
                file_ids.append(value)
        try:
            obj = orjson.loads(payload)
        except Exception:
            continue
        if isinstance(obj, dict):
            conversation_id = str(obj.get("conversation_id") or conversation_id)
            nested = obj.get("v")
            if isinstance(nested, dict):
                conversation_id = str(nested.get("conversation_id") or conversation_id)
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), dict) else {}
            if content.get("content_type") == "text":
                parts = content.get("parts")
                if isinstance(parts, list) and parts:
                    text_parts.append(str(parts[0]))
    return conversation_id, file_ids, "".join(text_parts)


def _extract_image_ids(mapping: dict[str, Any]) -> list[str]:
    file_ids: list[str] = []
    for node in (mapping or {}).values():
        if not isinstance(node, dict):
            continue
        message = node.get("message") if isinstance(node.get("message"), dict) else {}
        author = message.get("author") if isinstance(message.get("author"), dict) else {}
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), dict) else {}
        if author.get("role") != "tool" or metadata.get("async_task_type") != "image_gen":
            continue
        if content.get("content_type") != "multimodal_text":
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            pointer = str(part.get("asset_pointer") or "")
            if pointer.startswith("file-service://"):
                value = pointer.replace("file-service://", "")
            elif pointer.startswith("sediment://"):
                value = f"sed:{pointer.replace('sediment://', '')}"
            else:
                continue
            if value not in file_ids:
                file_ids.append(value)
    return file_ids


async def _poll_image_ids(
    session: aiohttp.ClientSession,
    context: _ChatGPTContext,
    conversation_id: str,
) -> list[str]:
    deadline = time.monotonic() + 180.0
    while time.monotonic() < deadline:
        response = await _request(
            session,
            "GET",
            f"{BASE_URL}/backend-api/conversation/{conversation_id}",
            headers={
                **_browser_headers(context.device_id),
                "authorization": f"Bearer {context.access_token}",
                "accept": "*/*",
            },
            timeout_s=30.0,
            retries=2,
            retry_statuses=_TRANSIENT_STATUSES,
        )
        if response.ok:
            try:
                payload = await response.json(content_type=None)
                file_ids = _extract_image_ids(payload.get("mapping") or {})
                if file_ids:
                    return file_ids
            except Exception:
                pass
        await asyncio.sleep(3)
    return []


async def _fetch_download_url(
    session: aiohttp.ClientSession,
    context: _ChatGPTContext,
    conversation_id: str,
    file_id: str,
) -> str:
    is_sediment = file_id.startswith("sed:")
    raw_id = file_id[4:] if is_sediment else file_id
    endpoint = (
        f"{BASE_URL}/backend-api/conversation/{conversation_id}/attachment/{raw_id}/download"
        if is_sediment
        else f"{BASE_URL}/backend-api/files/{raw_id}/download"
    )
    response = await _request(
        session,
        "GET",
        endpoint,
        headers={
            **_browser_headers(context.device_id),
            "authorization": f"Bearer {context.access_token}",
        },
        timeout_s=30.0,
        retries=2,
    )
    if not response.ok:
        return ""
    try:
        payload = await response.json(content_type=None)
    except Exception:
        return ""
    return str(payload.get("download_url") or "")


async def _download_base64(
    session: aiohttp.ClientSession,
    context: _ChatGPTContext,
    download_url: str,
) -> _GeneratedImage:
    same_origin = download_url.startswith(BASE_URL)
    headers = (
        {
            **_browser_headers(context.device_id),
            "authorization": f"Bearer {context.access_token}",
            "accept": "*/*",
        }
        if same_origin
        else {
            "user-agent": USER_AGENT,
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
        }
    )
    response = await _request(
        session,
        "GET",
        download_url,
        headers=headers,
        timeout_s=60.0,
        retries=2,
    )
    if not response.ok:
        raise await _response_error(response, "ChatGPT image download failed")
    raw = await response.read()
    if not raw:
        raise UpstreamError("ChatGPT image download returned empty body", status=502)
    content_type = (response.headers.get("content-type") or "image/png").split(";", 1)[0]
    return _GeneratedImage(
        b64_json=base64.b64encode(raw).decode("ascii"),
        mime_type=content_type or "image/png",
    )


async def _generate_one_inner(account: GPTImageAccount, prompt: str, model: str) -> _GeneratedImage:
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        context = await _bootstrap(session, account.access_token)
        sse_text = await _send_conversation(
            session,
            context,
            prompt=prompt,
            requested_model=model,
            is_free=account.is_free,
        )
        conversation_id, file_ids, text = _parse_sse(sse_text)
        if conversation_id and not file_ids:
            file_ids = await _poll_image_ids(session, context, conversation_id)
        if not file_ids:
            raise UpstreamError(text or "ChatGPT image generation returned no images", status=502)
        download_url = await _fetch_download_url(session, context, conversation_id, file_ids[0])
        if not download_url:
            raise UpstreamError("ChatGPT image generation returned no download URL", status=502)
        return await _download_base64(session, context, download_url)


async def _generate_one(account: GPTImageAccount, prompt: str, model: str) -> _GeneratedImage:
    timeout_s = _generation_timeout_s()
    try:
        return await asyncio.wait_for(
            _generate_one_inner(account, prompt, model),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError as exc:
        raise UpstreamError(
            f"ChatGPT image generation timed out after {timeout_s:g}s",
            status=504,
            body="timeout",
        ) from exc


async def _validate_access_token(access_token: str) -> None:
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        context = await _bootstrap(session, access_token)
        chat_token, _proof_info = await _chat_requirements(session, context)
        if not chat_token:
            raise UpstreamError("ChatGPT validation returned no requirements token", status=502)


async def _gpt_image_accounts() -> list[GPTImageAccount]:
    repo = get_account_repository()
    if repo is None:
        raise RateLimitError("Account repository not initialised")

    accounts: list[GPTImageAccount] = []
    page_num = 1
    while True:
        page = await repo.list_accounts(
            ListAccountsQuery(
                page=page_num,
                page_size=2000,
                include_deleted=False,
                sort_by="updated_at",
                sort_desc=True,
            )
        )
        for record in page.items:
            account = await _account_from_record(record)
            if account is not None:
                accounts.append(account)
        if page_num * 2000 >= page.total:
            break
        page_num += 1
    return accounts


def _maintainer_web_config_path() -> str:
    return str(data_path("maintainer", "web", "maintainer.config.json"))


def _is_gpt_credential_record(record: Any) -> bool:
    ext = record.ext or {}
    tags = set(record.tags or [])
    return (
        bool(ext.get("gpt_image"))
        or bool(ext.get("gpt"))
        or "gpt-image" in tags
        or "gpt" in tags
    )


def _record_access_token(ext: dict[str, Any]) -> str:
    return str(
        ext.get("gpt_image_access_token")
        or ext.get("gpt_access_token")
        or ""
    ).strip()


def _record_credentials(ext: dict[str, Any]) -> tuple[str, str, str]:
    email = str(ext.get("gpt_image_email") or ext.get("gpt_email") or "").strip()
    password = str(ext.get("gpt_image_password") or ext.get("gpt_password") or "").strip()
    mail_token = str(ext.get("gpt_image_mail_token") or ext.get("gpt_mail_token") or "").strip()
    return email, password, mail_token


def _record_is_free(ext: dict[str, Any]) -> bool:
    if ext.get("gpt_image"):
        return bool(ext.get("gpt_image_is_free"))
    plan = str(ext.get("gpt_plan_type") or "").strip().lower()
    return plan in {"", "free", "basic"}


def _record_patch_keys(ext: dict[str, Any]) -> tuple[str, str, str, str]:
    if ext.get("gpt_image"):
        return (
            "gpt_image_access_token",
            "gpt_image_status",
            "gpt_image_error",
            "gpt_image_login_attempt_at",
        )
    return (
        "gpt_access_token",
        "gpt_status",
        "gpt_registration_error",
        "gpt_login_attempt_at",
    )


def _login_cooldown_active(ext: dict[str, Any]) -> bool:
    *_unused, attempt_key = _record_patch_keys(ext)
    last_attempt = int(ext.get(attempt_key) or 0)
    cooldown_s = get_config().get_float("gpt_image.login_retry_cooldown_s", 300.0)
    return bool(last_attempt and time.time() - (last_attempt / 1000) < cooldown_s)


def _usable_access_token_status(status: str) -> bool:
    return status not in {"invalid", "login_failed"}


async def _login_gpt_credentials_async(
    *,
    email: str,
    password: str,
    mail_token: str,
) -> str:
    from app.maintainer.gpt import login_gpt_credentials

    return await asyncio.to_thread(
        login_gpt_credentials,
        email=email,
        password=password,
        mail_token=mail_token,
        config_path=_maintainer_web_config_path(),
    )


async def _account_from_record(record: Any) -> GPTImageAccount | None:
    if not _is_gpt_credential_record(record):
        return None
    ext = record.ext or {}
    access_key, status_key, error_key, attempt_key = _record_patch_keys(ext)
    access_token = _record_access_token(ext)
    if access_token:
        status = str(ext.get(status_key) or "unchecked")
        if not _usable_access_token_status(status):
            return None
        return GPTImageAccount(
            record_token=record.token,
            access_token=access_token,
            is_free=_record_is_free(ext),
            status_key=status_key,
            error_key=error_key,
        )

    email, password, mail_token = _record_credentials(ext)
    if not (email and password and mail_token):
        return None
    if not get_config().get_bool("gpt_image.auto_login_credentials", True):
        return None
    if _login_cooldown_active(ext):
        return None

    repo = get_account_repository()
    if repo is None:
        return None
    now = _now_ms()
    try:
        access_token = await _login_gpt_credentials_async(
            email=email,
            password=password,
            mail_token=mail_token,
        )
        await repo.patch_accounts(
            [
                AccountPatch(
                    token=record.token,
                    ext_merge={
                        access_key: access_token,
                        status_key: "available",
                        error_key: None,
                        attempt_key: now,
                    },
                )
            ]
        )
        return GPTImageAccount(
            record_token=record.token,
            access_token=access_token,
            is_free=_record_is_free(ext),
            status_key=status_key,
            error_key=error_key,
        )
    except Exception as exc:
        await repo.patch_accounts(
            [
                AccountPatch(
                    token=record.token,
                    last_fail_at=now,
                    last_fail_reason=str(exc)[:500],
                    ext_merge={
                        status_key: "login_failed",
                        error_key: str(exc)[:500],
                        attempt_key: now,
                    },
                )
            ]
        )
        logger.warning(
            "gpt image credential login failed: record={} error={}",
            record.token,
            exc,
        )
        return None


async def _patch_account_failure(
    repo: Any,
    account: GPTImageAccount,
    exc: BaseException,
    *,
    status: str | None = None,
    ext_merge_extra: dict[str, Any] | None = None,
) -> tuple[str, str]:
    now = _now_ms()
    message = _failure_message(exc)
    resolved_status = status or _capability_failure_status(exc)
    ext_merge = {
        account.status_key: resolved_status,
        account.error_key: message,
        _last_checked_key(account.status_key): now,
    }
    if ext_merge_extra:
        ext_merge.update(ext_merge_extra)
    await repo.patch_accounts(
        [
            AccountPatch(
                token=account.record_token,
                last_fail_at=now,
                last_fail_reason=message,
                ext_merge=ext_merge,
            )
        ]
    )
    return resolved_status, message


async def _mark_account_failure(account: GPTImageAccount, exc: BaseException) -> None:
    repo = get_account_repository()
    if repo is None:
        return
    try:
        await _patch_account_failure(repo, account, exc)
    except Exception as patch_exc:
        logger.debug("gpt image account failure patch failed: error={}", patch_exc)


async def _patch_account_success(
    repo: Any,
    account: GPTImageAccount,
    *,
    ext_merge_extra: dict[str, Any] | None = None,
) -> None:
    now = _now_ms()
    ext_merge = {
        account.status_key: "available",
        account.error_key: None,
        _last_checked_key(account.status_key): now,
    }
    if ext_merge_extra:
        ext_merge.update(ext_merge_extra)
    await repo.patch_accounts(
        [
            AccountPatch(
                token=account.record_token,
                last_use_at=now,
                ext_merge=ext_merge,
            )
        ]
    )


async def _mark_account_success(account: GPTImageAccount) -> None:
    repo = get_account_repository()
    if repo is None:
        return
    try:
        await _patch_account_success(repo, account)
    except Exception as patch_exc:
        logger.debug("gpt image account success patch failed: error={}", patch_exc)


async def test_gpt_account_record(record: Any, *, repo: Any | None = None) -> dict[str, Any]:
    """Validate a stored GPT/GPT-image record and persist the outcome."""
    if not _is_gpt_credential_record(record):
        raise ValidationError("Not a GPT account record", param="account")
    repo = repo or get_account_repository()
    if repo is None:
        raise RateLimitError("Account repository not initialised")

    ext = record.ext or {}
    access_key, status_key, error_key, attempt_key = _record_patch_keys(ext)
    access_token = _record_access_token(ext)
    account = GPTImageAccount(
        record_token=record.token,
        access_token=access_token,
        is_free=_record_is_free(ext),
        status_key=status_key,
        error_key=error_key,
    )
    extra: dict[str, Any] = {}

    if not access_token:
        email, password, mail_token = _record_credentials(ext)
        if not (email and password and mail_token):
            message = "No access token or login credentials configured"
            await _patch_account_failure(
                repo,
                account,
                ValidationError(message, param="account"),
                status="login_required",
            )
            return _test_result(record, ok=False, status="login_required", error=message)
        attempt_at = _now_ms()
        try:
            access_token = await _login_gpt_credentials_async(
                email=email,
                password=password,
                mail_token=mail_token,
            )
            account.access_token = access_token
            extra = {
                access_key: access_token,
                attempt_key: attempt_at,
            }
        except Exception as exc:
            status, message = await _patch_account_failure(
                repo,
                account,
                exc,
                status="login_failed",
                ext_merge_extra={attempt_key: attempt_at},
            )
            return _test_result(record, ok=False, status=status, error=message)

    try:
        await _validate_access_token(access_token)
        await _patch_account_success(repo, account, ext_merge_extra=extra)
        return _test_result(record, ok=True, status="available", access_token=access_token)
    except Exception as exc:
        status, message = await _patch_account_failure(
            repo,
            account,
            exc,
            ext_merge_extra=extra or None,
        )
        return _test_result(record, ok=False, status=status, error=message, access_token=access_token)


def _test_result(
    record: Any,
    *,
    ok: bool,
    status: str,
    error: str = "",
    access_token: str = "",
) -> dict[str, Any]:
    ext = record.ext or {}
    token = access_token or _record_access_token(ext)
    return {
        "id": record.token,
        "ok": ok,
        "kind": "gpt_image" if ext.get("gpt_image") else "gpt",
        "email": ext.get("gpt_image_email") or ext.get("gpt_email"),
        "alias": ext.get("gpt_image_alias") or ext.get("gpt_alias"),
        "capability_status": status,
        "capability_error": error or None,
        "has_access_token": bool(token),
        "access_token_masked": _mask(token),
    }


def _last_checked_key(status_key: str) -> str:
    if status_key.endswith("_status"):
        return f"{status_key[:-7]}_last_checked_at"
    return f"{status_key}_last_checked_at"


def _error_text(exc: BaseException) -> str:
    parts = [str(exc)]
    if isinstance(exc, UpstreamError):
        body = str(exc.details.get("body") or "")
        if body:
            parts.append(body)
    return " ".join(parts).lower()


def _failure_message(exc: BaseException) -> str:
    message = str(exc)
    if isinstance(exc, UpstreamError):
        body = str(exc.details.get("body") or "").strip()
        if body and body not in message:
            message = f"{message}: {body}"
    return message[:500]


def _capability_failure_status(exc: BaseException) -> str:
    if isinstance(exc, UpstreamError):
        text = _error_text(exc)
        if exc.status == 401 or (
            exc.status == 403 and any(marker in text for marker in _INVALID_CREDENTIAL_MARKERS)
        ):
            return "invalid"
        if exc.status == 429:
            return "rate_limited"
        if exc.status == 504 or "timed out" in text or "timeout" in text:
            return "timeout"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    return "failed"


async def _run_generation(prompt: str, model: str, n: int) -> list[_GeneratedImage]:
    accounts = await _gpt_image_accounts()
    if not accounts:
        raise RateLimitError("No GPT image accounts configured")

    images: list[_GeneratedImage] = []
    failures: list[str] = []
    last_exc: BaseException | None = None
    account_index = 0
    for _ in range(n):
        generated = False
        for _attempt in range(len(accounts)):
            account = accounts[account_index % len(accounts)]
            account_index += 1
            try:
                images.append(await _generate_one(account, prompt, model))
                await _mark_account_success(account)
                generated = True
                break
            except Exception as exc:
                last_exc = exc
                failures.append(str(exc))
                await _mark_account_failure(account, exc)
                logger.warning(
                    "gpt image account attempt failed: account={} error={}",
                    account.record_token,
                    exc,
                )
        if not generated:
            break
    if not images:
        if isinstance(last_exc, AppError) and last_exc.status not in {401, 403, 429}:
            raise last_exc
        detail = failures[-1] if failures else "no account attempts were made"
        raise RateLimitError(f"GPT image generation failed across configured accounts: {detail}")
    return images


async def generate(
    *,
    model: str,
    prompt: str,
    n: int = 1,
    response_format: str = "url",
    stream: bool = False,
    chat_format: bool = False,
) -> dict | AsyncGenerator[str, None]:
    """Generate images with ChatGPT GPT-image models."""
    if not prompt.strip():
        raise ValidationError("prompt is required", param="prompt")
    if not (1 <= n <= 4):
        raise ValidationError("n must be between 1 and 4 for GPT image models", param="n")

    response_id = make_response_id()

    if stream:
        async def _sse() -> AsyncGenerator[str, None]:
            if chat_format:
                yield f"data: {orjson.dumps(make_thinking_chunk(response_id, model, 'GPT image generation started')).decode()}\n\n"
            images = await _run_generation(prompt, model, n)
            for image in images:
                chunk = make_stream_chunk(
                    response_id,
                    model,
                    _markdown_value(image, response_format) if chat_format else json.dumps(_image_value(image, response_format)),
                )
                yield f"data: {orjson.dumps(chunk).decode()}\n\n"
            final = make_stream_chunk(response_id, model, "", is_final=True)
            yield f"data: {orjson.dumps(final).decode()}\n\n"
            yield "data: [DONE]\n\n"

        return _sse()

    images = await _run_generation(prompt, model, n)
    if chat_format:
        content = "\n\n".join(_markdown_value(image, response_format) for image in images)
        return make_chat_response(
            model,
            content,
            prompt_content=prompt,
            response_id=response_id,
            reasoning_content="GPT image generation completed",
        )
    return {
        "created": int(time.time()),
        "data": [_image_value(image, response_format) for image in images],
    }


__all__ = ["GPTImageAccount", "generate", "test_gpt_account_record"]
