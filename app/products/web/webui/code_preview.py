"""Shareable WebUI code preview storage."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.platform.auth.middleware import is_webui_enabled, verify_webui_key
from app.platform.paths import data_path


router = APIRouter(tags=["WebUI - Code Preview"])

_STORE_DIR = data_path("webui", "code_previews")
_MAX_SRCDOC_CHARS = 1_000_000
_MAX_PREVIEWS = 200
_PREVIEW_TTL_SECONDS = 7 * 24 * 60 * 60


class CodePreviewCreateRequest(BaseModel):
    srcdoc: str = Field(..., min_length=1, max_length=_MAX_SRCDOC_CHARS)
    title: str = Field("Code preview", max_length=120)


def _preview_id() -> str:
    return secrets.token_urlsafe(18).rstrip("=")


def _preview_path(preview_id: str) -> Path:
    raw_id = str(preview_id or "")
    cleaned = "".join(ch for ch in raw_id if ch.isalnum() or ch in {"_", "-"})
    if not cleaned or cleaned != raw_id:
        raise HTTPException(status_code=404, detail="Preview not found")
    return _STORE_DIR / f"{cleaned}.json"


def _write_preview_sync(payload: dict[str, Any]) -> dict[str, Any]:
    _STORE_DIR.mkdir(parents=True, exist_ok=True)
    preview_id = _preview_id()
    created_at = int(time.time())
    record = {
        "id": preview_id,
        "title": str(payload.get("title") or "Code preview")[:120],
        "srcdoc": str(payload.get("srcdoc") or ""),
        "created_at": created_at,
    }
    path = _preview_path(preview_id)
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    _prune_previews_sync(now=created_at)
    return record


def _delete_preview_sync(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _read_preview_sync(preview_id: str) -> dict[str, Any] | None:
    path = _preview_path(preview_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        record = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(record, dict):
        return None
    srcdoc = record.get("srcdoc")
    if not isinstance(srcdoc, str) or not srcdoc.strip():
        return None
    try:
        created_at = int(record.get("created_at") or 0)
    except (TypeError, ValueError):
        created_at = 0
    expires_from = created_at
    if not expires_from:
        try:
            expires_from = int(path.stat().st_mtime)
        except OSError:
            expires_from = 0
    if expires_from and int(time.time()) - expires_from > _PREVIEW_TTL_SECONDS:
        _delete_preview_sync(path)
        return None
    return {
        "id": str(record.get("id") or preview_id),
        "title": str(record.get("title") or "Code preview"),
        "srcdoc": srcdoc,
        "created_at": created_at,
    }


def _prune_previews_sync(*, now: int | None = None) -> None:
    if not _STORE_DIR.exists():
        return
    now = int(now or time.time())
    entries: list[tuple[int, Path]] = []
    for path in _STORE_DIR.glob("*.json"):
        try:
            stat = path.stat()
        except OSError:
            continue
        age = now - int(stat.st_mtime)
        if age > _PREVIEW_TTL_SECONDS:
            _delete_preview_sync(path)
            continue
        entries.append((int(stat.st_mtime), path))

    entries.sort(reverse=True)
    for _mtime, path in entries[_MAX_PREVIEWS:]:
        _delete_preview_sync(path)


@router.post(
    "/webui/api/code-previews",
    dependencies=[Depends(verify_webui_key)],
)
async def create_code_preview(req: CodePreviewCreateRequest):
    if not is_webui_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    record = await asyncio.to_thread(_write_preview_sync, req.model_dump())
    return JSONResponse(
        {
            "id": record["id"],
            "url": f"/webui/code-preview?id={record['id']}",
            "created_at": record["created_at"],
        }
    )


@router.get("/webui/api/code-previews/{preview_id}")
async def get_code_preview(preview_id: str):
    if not is_webui_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    record = await asyncio.to_thread(_read_preview_sync, preview_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Preview not found")
    return JSONResponse(record, headers={"Cache-Control": "no-store"})


__all__ = [
    "router",
    "_read_preview_sync",
    "_write_preview_sync",
]
