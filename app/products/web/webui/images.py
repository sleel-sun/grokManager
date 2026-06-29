"""WebUI image studio API routes."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.control.model import registry as model_registry
from app.platform.auth.middleware import WebUIUser, verify_webui_key
from app.platform.config.snapshot import get_config
from app.platform.errors import ValidationError
from app.platform.paths import data_path
from app.platform.storage import image_files_dir
from app.products.openai.router import (
    _coalesce_uploads,
    _image_bytes_to_data_uri,
    _uploads_to_data_uris,
    _validate_image_edit_n,
    _validate_image_n,
)

router = APIRouter(prefix="/webui/api", tags=["WebUI - Images"])

_HISTORY_LIMIT = 80
_GPT_WORKSPACE_MODELS = {"gpt-image-1", "gpt-image-2", "codex-gpt-image-2"}
_IMAGE_EDIT_SIZE = "1024x1024"
_IMAGE_EDIT_REFERENCE_LIMIT = 5
_LOCAL_IMAGE_ID_RE = re.compile(r"^[0-9a-fA-F\-]{16,36}$")
_QUALITY_VALUES = {"1k", "2k", "4k"}
_QUALITY_RANK = {"1k": 1, "2k": 2, "4k": 3}


class WebUIImageGenerationRequest(BaseModel):
    model: str
    prompt: str
    n: int | None = Field(1, ge=1, le=10)
    size: str | None = "1024x1024"
    quality: str | None = "1k"
    response_format: str | None = "url"
    session_id: str | None = ""


class WebUIImageUrlCacheRequest(BaseModel):
    url: str
    prompt: str = ""
    model: str = "cached-url"
    mode: str = "cache"
    size: str = "1024x1024"
    quality: str = "1k"


def _model_payload(spec) -> dict:
    if spec.is_image():
        capability = "image"
    elif spec.is_image_edit():
        capability = "image_edit"
    else:
        capability = "unknown"
    return {
        "id": spec.model_name,
        "name": spec.public_name,
        "capability": capability,
        "upstream_profile": spec.upstream_profile,
        "gpt_workspace": spec.model_name in _GPT_WORKSPACE_MODELS,
    }


def _premium_usernames() -> set[str]:
    raw = get_config("app.webui_premium_users", [])
    if isinstance(raw, str):
        return {part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()}
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    if isinstance(raw, dict):
        values: set[str] = set()
        for key, value in raw.items():
            enabled = value
            if isinstance(value, dict):
                enabled = value.get("enabled", True)
            if enabled:
                values.add(str(key).strip())
        return {item for item in values if item}
    return set()


def _user_is_premium(user: WebUIUser) -> bool:
    if _QUALITY_RANK.get(_user_max_quality(user), 1) > 1:
        return True
    return False


def _user_max_quality(user: WebUIUser) -> str:
    if user.legacy:
        return "4k"
    quality = str(getattr(user, "gpt_image_quality", "1k") or "1k").strip().lower()
    if quality not in _QUALITY_VALUES:
        quality = "1k"
    if quality == "1k":
        premium = _premium_usernames()
        if user.username in premium or user.id in premium:
            return "4k"
    return quality


def _allowed_gpt_models(user: WebUIUser) -> set[str]:
    if user.legacy or user.anonymous:
        return set(_GPT_WORKSPACE_MODELS)
    models = getattr(user, "gpt_models", ()) or ()
    allowed = {str(model).strip() for model in models if str(model).strip()}
    return allowed & _GPT_WORKSPACE_MODELS


def _ensure_gpt_model_access(model: str, user: WebUIUser) -> None:
    if model in _GPT_WORKSPACE_MODELS and model not in _allowed_gpt_models(user):
        raise ValidationError(
            f"WebUI user {user.username!r} does not have access to GPT image model {model!r}",
            param="model",
            code="model_not_allowed",
        )


def _quality_for_user(value: str | None, user: WebUIUser) -> str:
    quality = str(value or "1k").strip().lower()
    if quality not in _QUALITY_VALUES:
        raise ValidationError("quality must be one of ['1k', '2k', '4k']", param="quality")
    max_quality = _user_max_quality(user)
    if _QUALITY_RANK[quality] > _QUALITY_RANK[max_quality]:
        return max_quality
    return quality


def _quality_payload(user: WebUIUser) -> dict[str, Any]:
    max_quality = _user_max_quality(user)
    return {
        "premium": _QUALITY_RANK[max_quality] > 1,
        "default": "1k",
        "max": max_quality,
        "options": [
            {"id": "1k", "label": "1K", "enabled": True},
            {"id": "2k", "label": "2K", "enabled": _QUALITY_RANK[max_quality] >= 2},
            {"id": "4k", "label": "4K", "enabled": _QUALITY_RANK[max_quality] >= 3},
        ],
    }


def _quality_prompt(prompt: str, quality: str) -> str:
    clean = str(prompt or "").strip()
    if quality == "1k":
        return clean
    return f"{clean}\n\nOutput target: {quality.upper()} high-detail image."


def _history_path_for_user(user: WebUIUser) -> Path:
    if user.legacy or user.anonymous:
        return data_path("webui", "image_studio", "history.json")
    return data_path("webui", "users", user.id, "image_studio_history.json")


def _read_history_sync(path: Path) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return []
    sessions = parsed.get("sessions") if isinstance(parsed, dict) else parsed
    return sessions if isinstance(sessions, list) else []


def _write_history_sync(path: Path, sessions: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": int(time.time()),
        "sessions": sessions[:_HISTORY_LIMIT],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def _load_history(user: WebUIUser) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_read_history_sync, _history_path_for_user(user))


async def _save_history(user: WebUIUser, sessions: list[dict[str, Any]]) -> None:
    await asyncio.to_thread(_write_history_sync, _history_path_for_user(user), sessions)


def _image_url_from_item(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    url = item.get("url")
    if isinstance(url, str) and url:
        return url
    b64_json = item.get("b64_json")
    if isinstance(b64_json, str) and b64_json:
        return f"data:image/png;base64,{b64_json}"
    return ""


def _images_from_payload(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    images: list[dict[str, str]] = []
    for item in data:
        url = _image_url_from_item(item)
        if url:
            images.append({"url": url})
    return images


def _coalesce_reference_urls(*groups: list[str] | str | None) -> list[str]:
    urls: list[str] = []
    for group in groups:
        if not group:
            continue
        if isinstance(group, str):
            group = [group]
        urls.extend(str(item or "").strip() for item in group if str(item or "").strip())
    return urls


def _local_image_id_from_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise ValidationError("reference_url must be a generated image URL", param="reference_url") from exc
    if parsed.path != "/v1/files/image":
        raise ValidationError("reference_url must be a generated image URL", param="reference_url")
    file_id = (parse_qs(parsed.query).get("id") or [""])[0].strip()
    if not _LOCAL_IMAGE_ID_RE.fullmatch(file_id):
        raise ValidationError("Invalid generated image ID", param="reference_url")
    return file_id.lower()


def _local_image_bytes(file_id: str) -> tuple[bytes, str]:
    mime_by_ext = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }
    matches: list[tuple[Path, str]] = []
    img_dir = image_files_dir()
    for ext, mime in mime_by_ext.items():
        path = img_dir / f"{file_id}{ext}"
        if path.exists():
            matches.append((path, mime))
    if not matches:
        raise ValidationError(f"Generated image {file_id!r} not found", param="reference_url")
    path, mime = max(matches, key=lambda item: item[0].stat().st_mtime_ns)
    return path.read_bytes(), mime


async def _reference_url_to_data_uri(url: str, index: int) -> str:
    if url.startswith("data:image/"):
        return url
    file_id = _local_image_id_from_url(url)
    raw, mime = await asyncio.to_thread(_local_image_bytes, file_id)
    return _image_bytes_to_data_uri(raw, mime, param=f"reference_url.{index}")


def _reference_name_from_url(url: str, index: int) -> str:
    if url.startswith("data:image/"):
        return f"history-reference-{index + 1}"
    try:
        file_id = _local_image_id_from_url(url)
    except ValidationError:
        return f"history-reference-{index + 1}"
    return f"generated-{file_id[:8]}"


async def _append_history_session(
    user: WebUIUser,
    *,
    prompt: str,
    model: str,
    mode: str,
    size: str,
    quality: str,
    images: list[dict[str, str]],
    reference_names: list[str] | None = None,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    if not images:
        return None
    now = int(time.time())
    turn = {
        "id": uuid.uuid4().hex,
        "created_at": now,
        "prompt": str(prompt or "").strip(),
        "model": model,
        "mode": mode,
        "size": size,
        "quality": quality,
        "images": images,
        "reference_names": reference_names or [],
    }
    sessions = await _load_history(user)
    target_id = str(session_id or "").strip()
    session = next(
        (
            item
            for item in sessions
            if isinstance(item, dict) and str(item.get("id") or "") == target_id
        ),
        None,
    )
    if session is not None:
        turns = _history_session_turns(session)
        session["turns"] = [*turns, turn]
        session["updated_at"] = now
        session["title"] = str(session.get("title") or session.get("prompt") or turn["prompt"])
        # Keep top-level fields as the latest turn for older clients that do not
        # understand the turns array yet.
        session.update(
            {
                "prompt": turn["prompt"],
                "model": model,
                "mode": mode,
                "size": size,
                "quality": quality,
                "images": images,
                "reference_names": reference_names or [],
            }
        )
        sessions = [session, *[item for item in sessions if item is not session]]
    else:
        session = {
            "id": uuid.uuid4().hex,
            "title": turn["prompt"],
            "created_at": now,
            "updated_at": now,
            "prompt": turn["prompt"],
            "model": model,
            "mode": mode,
            "size": size,
            "quality": quality,
            "images": images,
            "reference_names": reference_names or [],
            "turns": [turn],
        }
        sessions = [session, *sessions]
    await _save_history(user, sessions)
    return session


def _history_session_turns(session: dict[str, Any]) -> list[dict[str, Any]]:
    raw_turns = session.get("turns")
    if isinstance(raw_turns, list):
        turns = [
            item
            for item in raw_turns
            if isinstance(item, dict) and isinstance(item.get("images"), list) and item.get("images")
        ]
        if turns:
            return turns
    images = session.get("images")
    if not isinstance(images, list) or not images:
        return []
    return [
        {
            "id": uuid.uuid4().hex,
            "created_at": int(session.get("created_at") or time.time()),
            "prompt": str(session.get("prompt") or ""),
            "model": str(session.get("model") or ""),
            "mode": str(session.get("mode") or "generate"),
            "size": str(session.get("size") or "1024x1024"),
            "quality": str(session.get("quality") or "1k"),
            "images": images,
            "reference_names": session.get("reference_names") if isinstance(session.get("reference_names"), list) else [],
        }
    ]


@router.get("/images/models")
async def list_webui_image_models(user: WebUIUser = Depends(verify_webui_key)):
    generation = [
        _model_payload(spec)
        for spec in model_registry.list_enabled()
        if spec.is_image()
        and (
            spec.model_name not in _GPT_WORKSPACE_MODELS
            or spec.model_name in _allowed_gpt_models(user)
        )
    ]
    edits = [
        _model_payload(spec)
        for spec in model_registry.list_enabled()
        if spec.is_image_edit()
        and spec.model_name in _GPT_WORKSPACE_MODELS
        and spec.model_name in _allowed_gpt_models(user)
    ]
    return JSONResponse(
        {
            "object": "list",
            "generation": generation,
            "edits": edits,
            "workspace": {
                "gpt_models": sorted(_allowed_gpt_models(user)),
                "quality": _quality_payload(user),
                "history_limit": _HISTORY_LIMIT,
            },
        }
    )


@router.post("/images/generations")
async def webui_image_generations(
    req: WebUIImageGenerationRequest,
    user: WebUIUser = Depends(verify_webui_key),
):
    spec = model_registry.get(req.model)
    if spec is None or not spec.enabled or not spec.is_image():
        raise ValidationError(f"Model {req.model!r} is not an image model", param="model")
    _ensure_gpt_model_access(req.model, user)
    _validate_image_n(req.model, req.n or 1, param="n")
    quality = _quality_for_user(getattr(req, "quality", None), user)
    prompt = _quality_prompt(req.prompt, quality) if req.model in _GPT_WORKSPACE_MODELS else req.prompt

    from app.products.openai.images import generate as image_generate

    result = await image_generate(
        model=req.model,
        prompt=prompt,
        n=req.n or 1,
        size=req.size or "1024x1024",
        response_format=req.response_format or "url",
        stream=False,
        chat_format=False,
    )
    session = await _append_history_session(
        user,
        prompt=req.prompt,
        model=req.model,
        mode="generate",
        size=req.size or "1024x1024",
        quality=quality,
        images=_images_from_payload(result),
        session_id=getattr(req, "session_id", None),
    )
    if isinstance(result, dict):
        result["studio_session"] = session
        result["quality"] = quality
    return JSONResponse(result)


@router.post("/images/edits")
async def webui_image_edits(
    prompt: Annotated[str, Form(...)],
    model: Annotated[str, Form()] = "gpt-image-2",
    image: Annotated[list[UploadFile] | None, File(alias="image")] = None,
    image_array: Annotated[list[UploadFile] | None, File(alias="image[]")] = None,
    reference_url: Annotated[list[str] | None, Form(alias="reference_url")] = None,
    reference_url_array: Annotated[list[str] | None, Form(alias="reference_url[]")] = None,
    mask: Annotated[UploadFile | None, File()] = None,
    n: Annotated[int, Form()] = 1,
    size: Annotated[str, Form()] = "1024x1024",
    quality: Annotated[str, Form()] = "1k",
    response_format: Annotated[str, Form()] = "url",
    session_id: Annotated[str, Form()] = "",
    user: WebUIUser = Depends(verify_webui_key),
):
    spec = model_registry.get(model)
    if spec is None or not spec.enabled or not spec.is_image_edit():
        raise ValidationError(f"Model {model!r} is not an image-edit model", param="model")
    _ensure_gpt_model_access(model, user)
    _validate_image_edit_n(n, param="n")
    quality_value = _quality_for_user(quality, user)
    edit_size = _IMAGE_EDIT_SIZE

    image_uploads = _coalesce_uploads(image, image_array)
    reference_urls = _coalesce_reference_urls(reference_url, reference_url_array)
    if not image_uploads and not reference_urls:
        raise ValidationError("image is required", param="image")
    if len(image_uploads) + len(reference_urls) > _IMAGE_EDIT_REFERENCE_LIMIT:
        raise ValidationError(
            f"image edit supports up to {_IMAGE_EDIT_REFERENCE_LIMIT} reference images",
            param="image",
        )
    reference_names = [
        upload.filename or f"reference-{idx + 1}"
        for idx, upload in enumerate(image_uploads)
    ]
    reference_names.extend(
        _reference_name_from_url(url, idx)
        for idx, url in enumerate(reference_urls)
    )

    from app.products.openai.images import edit as image_edit

    image_inputs = await _uploads_to_data_uris(image_uploads, mask=mask) if image_uploads else []
    image_inputs.extend(
        [
            await _reference_url_to_data_uri(url, idx)
            for idx, url in enumerate(reference_urls)
        ]
    )
    content = [{"type": "text", "text": prompt}]
    content.extend(
        {"type": "image_url", "image_url": {"url": image_input}}
        for image_input in image_inputs
    )
    result = await image_edit(
        model=model,
        messages=[{"role": "user", "content": content}],
        n=n,
        size=edit_size,
        response_format=response_format or "url",
        stream=False,
        chat_format=False,
    )
    session = await _append_history_session(
        user,
        prompt=prompt,
        model=model,
        mode="edit",
        size=edit_size,
        quality=quality_value,
        images=_images_from_payload(result),
        reference_names=reference_names,
        session_id=session_id,
    )
    if isinstance(result, dict):
        result["studio_session"] = session
        result["quality"] = quality_value
    return JSONResponse(result)


@router.get("/images/history")
async def webui_image_history(user: WebUIUser = Depends(verify_webui_key)):
    sessions = await _load_history(user)
    return JSONResponse({"object": "list", "data": sessions[:_HISTORY_LIMIT]})


@router.post("/images/history/cache-url")
async def webui_image_cache_url(
    req: WebUIImageUrlCacheRequest,
    user: WebUIUser = Depends(verify_webui_key),
):
    url = req.url.strip()
    if not url:
        raise ValidationError("url is required", param="url")
    quality = _quality_for_user(req.quality, user)
    session = await _append_history_session(
        user,
        prompt=req.prompt,
        model=req.model,
        mode=req.mode,
        size=req.size,
        quality=quality,
        images=[{"url": url}],
    )
    return JSONResponse({"ok": True, "session": session})


@router.delete("/images/history/{session_id}")
async def delete_webui_image_history_session(
    session_id: str,
    user: WebUIUser = Depends(verify_webui_key),
):
    sessions = await _load_history(user)
    remaining = [item for item in sessions if str(item.get("id") or "") != session_id]
    await _save_history(user, remaining)
    return JSONResponse({"ok": True, "deleted": len(sessions) - len(remaining)})


@router.delete("/images/history")
async def clear_webui_image_history(user: WebUIUser = Depends(verify_webui_key)):
    await _save_history(user, [])
    return JSONResponse({"ok": True})


__all__ = ["router"]
