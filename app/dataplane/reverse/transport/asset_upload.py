"""Asset upload transport for Grok chat attachments.

Supports the legacy base64 endpoint and the current v2 presigned-upload flow,
returning the file metadata ID used as a chat file attachment reference.
"""

import asyncio
import base64
import mimetypes
import re
from urllib.parse import urlparse

import orjson

from app.platform.logging.logger import logger
from app.platform.config.snapshot import get_config
from app.platform.errors import UpstreamError, ValidationError
from app.dataplane.proxy import get_proxy_runtime
from app.dataplane.proxy.adapters.headers import build_sso_cookie
from app.dataplane.proxy.adapters.headers import build_http_headers
from app.dataplane.proxy.adapters.session import ResettableSession, build_session_kwargs
from app.dataplane.reverse.protocol.xai_assets import resolve_asset_reference
from app.control.proxy.feedback import build_feedback
from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind

_UPLOAD_URL = "https://grok.com/rest/app-chat/upload-file"
_UPLOAD_V2_INIT_URL = "https://grok.com/rest/app-chat/upload-file-v2/init"
_UPLOAD_V2_COMPLETE_URL = "https://grok.com/rest/app-chat/upload-file-v2/complete"
_UPLOAD_V2_STATUS_URL = "https://grok.com/rest/app-chat/upload-file-v2/status"
_X_USER_ID_RE = re.compile(r"(?:^|;\s*)x-userid=([^;]+)")
_CLOUDFLARE_MARKERS = (
    "just a moment",
    "cf-challenge",
    "cf-mitigated",
    "cloudflare",
    "request was blocked",
    "you have been blocked",
)
_UPLOAD_V2_SUCCESS_STATUSES = {
    "success",
    "upload_job_status_success",
    "upload-job-status-success",
}
_UPLOAD_V2_ERROR_STATUSES = {
    "error",
    "failed",
    "upload_job_status_error",
    "upload-job-status-error",
}

# Global semaphore — limits concurrent upload_file() calls across all requests.
# Initialised lazily on first call so the event loop is guaranteed to be running.
_upload_sem: asyncio.Semaphore | None = None

def _get_upload_sem() -> asyncio.Semaphore:
    global _upload_sem
    if _upload_sem is None:
        n = max(1, int(get_config("batch.asset_upload_concurrency", 10)))
        _upload_sem = asyncio.Semaphore(n)
    return _upload_sem


# ---------------------------------------------------------------------------
# File-input parsing
# ---------------------------------------------------------------------------

def _is_url(value: str) -> bool:
    try:
        p = urlparse(value)
        return bool(p.scheme in {"http", "https"} and p.netloc)
    except Exception:
        return False


def _mime_from_name(filename: str, fallback: str = "application/octet-stream") -> str:
    mime, _ = mimetypes.guess_type(filename)
    return mime or fallback


def _safe_filename(filename: str | None) -> str:
    raw = (filename or "").strip()
    if not raw:
        return ""
    raw = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return raw or ""


def _filename_for_mime(mime: str) -> str:
    ext = mimetypes.guess_extension(mime or "") or ""
    if not ext and "/" in mime:
        ext = "." + mime.split("/", 1)[1].split("+", 1)[0].replace(".", "-")
    if not ext:
        ext = ".bin"
    return f"file{ext}"


def parse_data_uri(data_uri: str, *, filename: str | None = None) -> tuple[str, str, str]:
    """Split a data URI into (filename, base64_content, mime_type).

    Raises ``ValidationError`` on invalid input.
    """
    if not data_uri.startswith("data:"):
        raise ValidationError("File input must be a URL or data URI", param="content")

    try:
        header, b64 = data_uri.split(",", 1)
    except ValueError:
        raise ValidationError("Malformed data URI: missing comma separator", param="content")

    if ";base64" not in header:
        raise ValidationError("Data URI must be base64-encoded", param="content")

    mime = header[5:].split(";", 1)[0].strip() or "application/octet-stream"
    b64  = re.sub(r"\s+", "", b64)
    if not b64:
        raise ValidationError("Data URI has empty payload", param="content")

    name = _safe_filename(filename) or _filename_for_mime(mime)
    if filename and mime == "application/octet-stream":
        mime = _mime_from_name(name, mime)
    return name, b64, mime


def _decode_b64_content(b64: str) -> bytes:
    try:
        return base64.b64decode(re.sub(r"\s+", "", b64), validate=True)
    except Exception as exc:
        raise ValidationError("File content is not valid base64", param="content") from exc


def _is_document_like_upload(mime: str) -> bool:
    lowered = (mime or "").lower()
    if lowered.startswith(("image/", "audio/")):
        return False
    return True


def _is_cloudflare_body(body: str) -> bool:
    haystack = (body or "").lower()
    return any(marker in haystack for marker in _CLOUDFLARE_MARKERS)


# ---------------------------------------------------------------------------
# Core upload function
# ---------------------------------------------------------------------------

async def upload_file(
    token:    str,
    filename: str,
    mime:     str,
    b64:      str,
) -> tuple[str, str]:
    """Upload base64-encoded file content to Grok.

    Args:
        token:    SSO session token.
        filename: Original file name (used for content-type inference).
        mime:     MIME type string (e.g. ``"image/png"``).
        b64:      Base64-encoded file content (no data-URI prefix).

    Returns:
        ``(file_id, file_uri)`` — file_id is used as a file attachment ref.

    Raises:
        ``UpstreamError`` on HTTP failure.
    """
    async with _get_upload_sem():
        raw = _decode_b64_content(b64)
        b64 = base64.b64encode(raw).decode()
        filename = _safe_filename(filename) or _filename_for_mime(mime)
        if not mime or mime == "application/octet-stream":
            mime = _mime_from_name(filename, mime or "application/octet-stream")

        if _is_document_like_upload(mime):
            try:
                return await _upload_file_v2_inner(token, filename, mime, raw)
            except UpstreamError as exc:
                # Older deployments may not expose v2 yet; keep the legacy path
                # as a compatibility fallback only for endpoint availability.
                if exc.status not in {404, 405, 501}:
                    raise
                logger.warning(
                    "asset upload v2 unavailable, falling back to legacy: status={} filename={!r}",
                    exc.status,
                    filename,
                )

        try:
            return await _upload_file_inner(token, filename, mime, b64)
        except UpstreamError as exc:
            body = str(exc.details.get("body", "") or "")
            if exc.status in {403, 413, 415} and not _is_cloudflare_body(body):
                logger.warning(
                    "legacy asset upload failed, retrying via v2: status={} filename={!r}",
                    exc.status,
                    filename,
                )
                return await _upload_file_v2_inner(token, filename, mime, raw)
            raise


async def _upload_file_inner(
    token:    str,
    filename: str,
    mime:     str,
    b64:      str,
) -> tuple[str, str]:
    cfg       = get_config()
    timeout_s = cfg.get_float("asset.upload_timeout", 60.0)

    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()

    payload = orjson.dumps({
        "fileName":     filename,
        "fileMimeType": mime,
        "content":      b64,
    })
    headers = build_http_headers(token, lease=lease)
    kwargs  = build_session_kwargs(lease=lease)

    try:
        async with ResettableSession(**kwargs) as session:
            response = await session.post(
                _UPLOAD_URL,
                headers = headers,
                data    = payload,
                timeout = timeout_s,
            )

        body_bytes = response.content
        if response.status_code != 200:
            body_text = body_bytes.decode("utf-8", "replace")[:300]
            logger.error(
                "asset upload request failed: status={} body={}",
                response.status_code, body_text,
            )
            is_cloudflare = _is_cloudflare_body(body_text)
            await proxy.feedback(
                lease,
                build_feedback(response.status_code, is_cloudflare=is_cloudflare),
            )
            raise UpstreamError(
                f"Asset upload returned {response.status_code}",
                status = response.status_code,
                body   = body_text,
            )

        await proxy.feedback(
            lease,
            ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200),
        )

        result   = orjson.loads(body_bytes)
        file_id  = result.get("fileMetadataId") or result.get("fileId", "")
        file_uri = result.get("fileUri", "")
        logger.info("asset upload completed: filename={!r} file_id={}", filename, file_id)
        return file_id, file_uri

    except UpstreamError:
        raise
    except Exception as exc:
        await proxy.feedback(
            lease,
            ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR),
        )
        raise UpstreamError(f"Asset upload transport error: {exc}") from exc


async def _grok_json_request(
    session: ResettableSession,
    *,
    method: str,
    url: str,
    token: str,
    lease,
    timeout_s: float,
    payload: dict | None = None,
    params: dict | None = None,
    phase: str,
) -> dict:
    headers = build_http_headers(token, lease=lease)
    data = orjson.dumps(payload) if payload is not None else None
    if method == "GET":
        response = await session.get(
            url,
            headers=headers,
            params=params,
            timeout=timeout_s,
        )
    else:
        response = await session.post(
            url,
            headers=headers,
            data=data,
            timeout=timeout_s,
        )

    body_bytes = response.content
    if response.status_code not in (200, 201, 204):
        body_text = body_bytes.decode("utf-8", "replace")[:400]
        logger.error(
            "asset upload v2 {} failed: status={} body={}",
            phase,
            response.status_code,
            body_text,
        )
        raise UpstreamError(
            f"Asset upload v2 {phase} returned {response.status_code}",
            status=response.status_code,
            body=body_text,
        )
    return orjson.loads(body_bytes) if body_bytes.strip() else {}


def _upload_metadata_tuple(data: dict) -> tuple[str, str]:
    metadata = data.get("fileMetadata")
    if isinstance(metadata, dict):
        source = metadata
    else:
        source = data
    file_id = (
        source.get("fileMetadataId")
        or source.get("fileId")
        or source.get("assetId")
        or ""
    )
    file_uri = source.get("fileUri") or source.get("uri") or ""
    return str(file_id or ""), str(file_uri or "")


def _normalise_upload_status(status: object) -> str:
    return str(status or "").strip().lower()


async def _put_presigned_part(
    session: ResettableSession,
    *,
    url: str,
    body: bytes,
    headers: dict[str, str] | None,
    timeout_s: float,
    require_etag: bool = False,
) -> str:
    safe_headers = {str(k): str(v) for k, v in (headers or {}).items()}
    last_exc: BaseException | None = None
    for attempt in range(2):
        response = await session.put(
            url,
            headers=safe_headers,
            data=body,
            timeout=timeout_s,
        )
        if 200 <= response.status_code < 300:
            etag = response.headers.get("ETag") or response.headers.get("etag") or ""
            if require_etag and not etag:
                raise UpstreamError("Asset upload v2 multipart part missing ETag")
            return str(etag)

        body_text = response.content.decode("utf-8", "replace")[:400]
        last_exc = UpstreamError(
            f"Asset upload v2 presigned PUT returned {response.status_code}",
            status=response.status_code,
            body=body_text,
        )
        if response.status_code not in {408, 429} and response.status_code < 500:
            break
        if attempt == 0:
            await asyncio.sleep(1.0)
    assert last_exc is not None
    raise last_exc


async def _upload_v2_single_put(
    session: ResettableSession,
    init: dict,
    raw: bytes,
    timeout_s: float,
) -> list[dict]:
    single_put = init.get("singlePut")
    if not isinstance(single_put, dict):
        raise UpstreamError("Asset upload v2 init response missing singlePut")
    url = str(single_put.get("url") or "")
    if not url:
        raise UpstreamError("Asset upload v2 init response missing singlePut URL")
    headers = single_put.get("requiredHeaders")
    if not isinstance(headers, dict):
        headers = {}
    await _put_presigned_part(
        session,
        url=url,
        body=raw,
        headers=headers,
        timeout_s=timeout_s,
    )
    return []


async def _upload_v2_multipart(
    session: ResettableSession,
    init: dict,
    raw: bytes,
    timeout_s: float,
) -> list[dict]:
    multipart = init.get("multipart")
    if not isinstance(multipart, dict):
        raise UpstreamError("Asset upload v2 init response missing multipart")
    parts = multipart.get("parts")
    part_size = int(multipart.get("partSize") or 0)
    if not isinstance(parts, list) or not parts or part_size <= 0:
        raise UpstreamError("Asset upload v2 init response has invalid multipart data")

    completed: list[dict] = []
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            raise UpstreamError("Asset upload v2 multipart part is invalid")
        url = str(part.get("url") or "")
        part_number = part.get("partNumber")
        if not url or part_number is None:
            raise UpstreamError("Asset upload v2 multipart part missing URL or number")
        start = index * part_size
        end = min(start + part_size, len(raw))
        etag = await _put_presigned_part(
            session,
            url=url,
            body=raw[start:end],
            headers=None,
            timeout_s=timeout_s,
            require_etag=True,
        )
        completed.append({"partNumber": part_number, "etag": etag})
    return completed


async def _poll_upload_v2_status(
    session: ResettableSession,
    *,
    token: str,
    lease,
    upload_id: str,
    timeout_s: float,
    poll_timeout_s: float,
) -> tuple[str, str]:
    deadline = asyncio.get_running_loop().time() + poll_timeout_s
    delay = 0.15
    while True:
        data = await _grok_json_request(
            session,
            method="GET",
            url=_UPLOAD_V2_STATUS_URL,
            token=token,
            lease=lease,
            timeout_s=timeout_s,
            params={"uploadId": upload_id},
            phase="status",
        )
        status = _normalise_upload_status(data.get("status"))
        if status in _UPLOAD_V2_SUCCESS_STATUSES:
            file_id, file_uri = _upload_metadata_tuple(data)
            if file_id:
                return file_id, file_uri
            asset_id = data.get("assetId")
            if asset_id:
                return str(asset_id), ""
        if status in _UPLOAD_V2_ERROR_STATUSES:
            message = str(data.get("errorMessage") or "processing failed")
            raise UpstreamError(f"Asset upload v2 processing failed: {message}")
        if asyncio.get_running_loop().time() >= deadline:
            raise UpstreamError("Asset upload v2 processing timed out", status=504)
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 3.0)


async def _upload_file_v2_inner(
    token: str,
    filename: str,
    mime: str,
    raw: bytes,
) -> tuple[str, str]:
    cfg = get_config()
    timeout_s = cfg.get_float("asset.upload_timeout", 60.0)
    poll_timeout_s = cfg.get_float("asset.upload_processing_timeout", 600.0)

    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()
    kwargs = build_session_kwargs(lease=lease)

    try:
        async with ResettableSession(**kwargs) as session:
            init = await _grok_json_request(
                session,
                method="POST",
                url=_UPLOAD_V2_INIT_URL,
                token=token,
                lease=lease,
                timeout_s=timeout_s,
                payload={
                    "fileName": filename,
                    "fileMimeType": mime,
                    "sizeBytes": str(len(raw)),
                    "multipartSupported": True,
                },
                phase="init",
            )
            upload_id = str(init.get("uploadId") or "")
            if not upload_id:
                raise UpstreamError("Asset upload v2 init response missing uploadId")

            method = str(init.get("uploadMethod") or "UPLOAD_METHOD_SINGLE_PUT")
            if method in {"UPLOAD_METHOD_UNSPECIFIED", "UPLOAD_METHOD_SINGLE_PUT"}:
                completed_parts = await _upload_v2_single_put(
                    session,
                    init,
                    raw,
                    timeout_s,
                )
            elif method == "UPLOAD_METHOD_MULTIPART":
                completed_parts = await _upload_v2_multipart(
                    session,
                    init,
                    raw,
                    timeout_s,
                )
            else:
                raise UpstreamError(f"Asset upload v2 unsupported upload method: {method}")

            complete = await _grok_json_request(
                session,
                method="POST",
                url=_UPLOAD_V2_COMPLETE_URL,
                token=token,
                lease=lease,
                timeout_s=timeout_s,
                payload={
                    "presigned": {
                        "uploadId": upload_id,
                        "completedParts": completed_parts,
                    }
                },
                phase="complete",
            )
            file_id, file_uri = _upload_metadata_tuple(complete)
            if file_id:
                await proxy.feedback(
                    lease,
                    ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200),
                )
                logger.info(
                    "asset upload v2 completed inline: filename={!r} file_id={}",
                    filename,
                    file_id,
                )
                return file_id, file_uri

            next_upload_id = str(complete.get("uploadId") or upload_id)
            file_id, file_uri = await _poll_upload_v2_status(
                session,
                token=token,
                lease=lease,
                upload_id=next_upload_id,
                timeout_s=timeout_s,
                poll_timeout_s=poll_timeout_s,
            )
            await proxy.feedback(
                lease,
                ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200),
            )
            logger.info("asset upload v2 completed: filename={!r} file_id={}", filename, file_id)
            return file_id, file_uri

    except UpstreamError as exc:
        body = str(exc.details.get("body", "") or "")
        await proxy.feedback(
            lease,
            build_feedback(exc.status, is_cloudflare=_is_cloudflare_body(body)),
        )
        raise
    except Exception as exc:
        await proxy.feedback(
            lease,
            ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR),
        )
        raise UpstreamError(f"Asset upload v2 transport error: {exc}") from exc


async def upload_from_input(
    token: str,
    file_input: str,
    *,
    filename: str | None = None,
    mime: str | None = None,
) -> tuple[str, str]:
    """High-level helper: parse *file_input* (URL or data URI) and upload.

    Returns ``(file_id, file_uri)``.
    """
    requested_filename = _safe_filename(filename)
    if _is_url(file_input):
        # Fetch the remote URL and re-upload as base64.
        proxy = await get_proxy_runtime()
        lease = await proxy.acquire()
        try:
            headers = build_http_headers(token, lease=lease)
            kwargs  = build_session_kwargs(lease=lease)
            async with ResettableSession(**kwargs) as session:
                resp = await session.get(file_input, headers=headers, timeout=30.0)
            raw  = resp.content
            if resp.status_code != 200:
                await proxy.feedback(
                    lease,
                    ProxyFeedback(
                        kind        = ProxyFeedbackKind.UPSTREAM_5XX if resp.status_code >= 500
                                      else ProxyFeedbackKind.FORBIDDEN,
                        status_code = resp.status_code,
                    ),
                )
                raise UpstreamError(
                    f"Failed to fetch input URL: {resp.status_code}",
                    status = resp.status_code,
                )
            detected_mime = (resp.headers.get("content-type", "").split(";")[0].strip()
                             or "application/octet-stream")
            mime = mime or detected_mime
            filename = requested_filename or file_input.split("/")[-1].split("?")[0] or "download"
            filename = _safe_filename(filename) or _filename_for_mime(mime)
            b64      = base64.b64encode(raw).decode()
        except UpstreamError:
            raise
        except Exception as exc:
            await proxy.feedback(lease, ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR))
            raise UpstreamError(f"Asset fetch transport error: {exc}") from exc

        await proxy.feedback(lease, ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS))
        return await upload_file(token, filename, mime, b64)

    # Data URI
    filename, b64, mime = parse_data_uri(file_input, filename=requested_filename)
    if mime == "application/octet-stream" and filename:
        mime = _mime_from_name(filename, mime)
    return await upload_file(token, filename, mime, b64)


def resolve_uploaded_asset_reference(token: str, file_id: str, file_uri: str) -> str:
    """Resolve an uploaded asset to the content URL required by image-edit."""
    user_id = _extract_user_id(token)
    url = resolve_asset_reference(file_id, file_uri, user_id=user_id)
    if url:
        return url
    raise UpstreamError("Could not resolve uploaded asset reference URL")


def _extract_user_id(token: str) -> str | None:
    cookie = build_sso_cookie(token)
    match = _X_USER_ID_RE.search(cookie)
    if match:
        return match.group(1)
    return None


__all__ = [
    "upload_file",
    "upload_from_input",
    "parse_data_uri",
    "resolve_uploaded_asset_reference",
]
