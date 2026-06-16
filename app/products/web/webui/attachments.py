"""WebUI attachment download helpers."""

from __future__ import annotations

import re
import urllib.parse
from pathlib import PurePosixPath
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.dataplane.shared.enums import PoolId
from app.platform.auth.middleware import verify_webui_key
from app.platform.errors import RateLimitError, UpstreamError, ValidationError
from app.products._account_selection import selection_max_retries


router = APIRouter(
    prefix="/webui/api",
    dependencies=[Depends(verify_webui_key)],
    tags=["WebUI - Attachments"],
)

_ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {
        "assets.grok.com",
        "grok.x.ai",
        "imgen.x.ai",
        "imagine-public.x.ai",
    }
)
_FILENAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def _validate_download_reference(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValidationError("Attachment URL is required", param="url")

    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"}:
            raise ValidationError("Unsupported attachment URL scheme", param="url")
        if parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS:
            raise ValidationError("Unsupported attachment download host", param="url")
        return raw

    if raw.startswith("//"):
        raise ValidationError("Unsupported attachment URL", param="url")
    return raw


def _safe_filename(filename: str, url: str) -> str:
    raw = str(filename or "").strip()
    if not raw:
        parsed = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed.path or url)
        raw = PurePosixPath(path).name
    raw = raw.replace("\\", "/").rsplit("/", 1)[-1].strip().strip(".")
    cleaned = _FILENAME_UNSAFE_RE.sub("_", raw)[:160].strip(" ._")
    return cleaned or "attachment"


def _content_disposition(filename: str) -> str:
    quoted = urllib.parse.quote(filename, safe="")
    fallback = filename.encode("ascii", "ignore").decode("ascii") or "attachment"
    fallback = fallback.replace("\\", "_").replace('"', "_")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quoted}'


async def _download_with_release(
    stream: AsyncGenerator[bytes, None],
    directory,
    lease,
) -> AsyncGenerator[bytes, None]:
    try:
        async for chunk in stream:
            yield chunk
    finally:
        await directory.release(lease)


@router.get("/attachments/download")
async def download_attachment(
    url: str = Query(..., description="Grok asset URL or relative asset path"),
    filename: str = Query("", description="Suggested download filename"),
):
    """Download a Grok-hosted attachment through the server with WebUI auth."""
    from app.dataplane.account import _directory
    from app.dataplane.reverse.transport.assets import download_asset

    if _directory is None:
        raise RateLimitError("Account directory not initialised")

    file_ref = _validate_download_reference(url)
    safe_name = _safe_filename(filename, file_ref)
    pools = (int(PoolId.BASIC), int(PoolId.SUPER), int(PoolId.HEAVY))
    excluded: list[str] = []
    last_exc: BaseException | None = None

    for _attempt in range(selection_max_retries() + 1):
        lease = await _directory.reserve_any(pools, exclude_tokens=excluded or None)
        if lease is None:
            break
        try:
            stream, content_type = await download_asset(lease.token, file_ref)
        except Exception as exc:
            await _directory.release(lease)
            excluded.append(lease.token)
            last_exc = exc
            continue

        return StreamingResponse(
            _download_with_release(stream, _directory, lease),
            media_type=content_type or "application/octet-stream",
            headers={
                "Content-Disposition": _content_disposition(safe_name),
                "Cache-Control": "no-store",
            },
        )

    if last_exc is not None:
        raise UpstreamError(f"Attachment download failed: {last_exc}") from last_exc
    raise RateLimitError("No available account for attachment download")


__all__ = [
    "router",
    "_content_disposition",
    "_safe_filename",
    "_validate_download_reference",
]
