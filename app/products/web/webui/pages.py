"""Static page routes for the statics-based WebUI."""

from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse

from app.platform.auth.middleware import is_webui_enabled
from ..static_html import serve_static_html

router = APIRouter(include_in_schema=False)

STATIC_DIR = Path(__file__).resolve().parents[3] / "statics" / "webui"


def _serve(filename: str) -> FileResponse:
    path = STATIC_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Page not found")
    return FileResponse(path)


def _serve_html(filename: str):
    return serve_static_html(STATIC_DIR / filename)


def _validate_redirect_url(value: str) -> str:
    target = str(value or "").strip()
    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Invalid redirect URL")
    return target


@router.get("/webui/chat")
async def webui_chat_page():
    if not is_webui_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return _serve_html("chat.html")


@router.get("/webui/redirect")
async def webui_redirect(url: str = Query(..., min_length=1)):
    if not is_webui_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return RedirectResponse(_validate_redirect_url(url), status_code=302)


@router.get("/webui/code-preview")
async def webui_code_preview_page():
    if not is_webui_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return _serve_html("code-preview.html")


@router.get("/webui/chatkit")
async def webui_chatkit_page():
    if not is_webui_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return _serve_html("chatkit.html")


@router.get("/webui/masonry")
async def webui_masonry_page():
    if not is_webui_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return _serve_html("masonry.html")


@router.get("/webui/image-studio")
async def webui_image_studio_page():
    if not is_webui_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return _serve_html("image-studio.html")


__all__ = ["router"]
