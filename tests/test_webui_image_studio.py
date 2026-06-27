from __future__ import annotations

import asyncio
import orjson
from pathlib import Path

from app.platform.auth.middleware import WebUIUser
from app.products.openai.schemas import ImageGenerationRequest
from app.products.web.webui import images as image_studio_api


ROOT = Path(__file__).resolve().parent.parent


def test_image_studio_page_route_and_header_entry_exist() -> None:
    pages = (ROOT / "app" / "products" / "web" / "webui" / "pages.py").read_text(encoding="utf-8")
    package = (ROOT / "app" / "products" / "web" / "webui" / "__init__.py").read_text(encoding="utf-8")
    header = (ROOT / "app" / "statics" / "webui" / "header.html").read_text(encoding="utf-8")
    html = (ROOT / "app" / "statics" / "webui" / "image-studio.html").read_text(encoding="utf-8")
    js = (ROOT / "app" / "statics" / "js" / "webui" / "image-studio.js").read_text(encoding="utf-8")

    assert '@router.get("/webui/image-studio")' in pages
    assert 'return _serve_html("image-studio.html")' in pages
    assert "from .images import router as images_router" in package
    assert "router.include_router(images_router)" in package
    assert 'href="/webui/image-studio"' in header
    assert 'id="studioForm"' in html
    assert "/static/js/auth.js?v={{APP_VERSION}}-usernsfw1" in html
    assert "fetch(GENERATE_ENDPOINT" in js
    assert "fetch(EDIT_ENDPOINT" in js


def test_webui_image_generation_uses_webui_wrapper(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_generate(**kwargs):
        calls.append(kwargs)
        return {"created": 1, "data": [{"url": "/v1/files/image?id=abc"}]}

    monkeypatch.setattr("app.products.openai.images.generate", fake_generate)

    response = asyncio.run(
        image_studio_api.webui_image_generations(
            ImageGenerationRequest(
                model="gpt-image-2",
                prompt="draw a cat",
                n=2,
                size="1024x1024",
            ),
            _user=WebUIUser(id="alice", username="alice"),
        )
    )
    body = orjson.loads(response.body)

    assert body["data"][0]["url"] == "/v1/files/image?id=abc"
    assert calls == [
        {
            "model": "gpt-image-2",
            "prompt": "draw a cat",
            "n": 2,
            "size": "1024x1024",
            "response_format": "url",
            "stream": False,
            "chat_format": False,
        }
    ]
