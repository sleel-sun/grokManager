from __future__ import annotations

import asyncio
import orjson
from pathlib import Path

from app.platform.auth.middleware import WebUIUser
from app.platform.errors import ValidationError
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
    assert 'data-i18n="webui.header.draw"' in header
    assert 'id="studioForm"' in html
    assert 'id="studioSessionList"' in html
    assert 'id="studioComposer"' in html
    assert "/static/js/auth.js?v={{APP_VERSION}}-usernsfw1" in html
    assert 'id="studioQuality"' in html
    assert 'id="studioReferencePreview"' in html
    assert "/static/js/webui/image-studio.js?v={{APP_VERSION}}-studio4" in html
    assert "fetch(GENERATE_ENDPOINT" in js
    assert "fetch(EDIT_ENDPOINT" in js
    assert "HISTORY_ENDPOINT" in js
    assert "|| 'gpt-image-2'" in js
    assert "payload.edits.filter((item) => GPT_MODELS.has" in js
    assert "data-reference-image" in js
    assert "reference_url" in js
    assert "const EDIT_SIZE = '1024x1024';" in js
    assert "const size = EDIT_SIZE;" in js
    assert "PENDING_REFERENCE_KEY = 'grokmanager.image_studio.pending_reference.v1'" in js
    assert "function consumePendingReference()" in js
    assert "function clearPromptInput()" in js
    assert "clearPromptInput();" in js
    assert "restorePromptInput(prompt);" in js
    assert "session_id: sessionId || undefined" in js
    assert "body.set('session_id', sessionId);" in js
    assert "turns.map((turn, index) => renderTurn(session, turn, index)).join('')" in js


def test_webui_image_generation_uses_webui_wrapper(monkeypatch, tmp_path) -> None:
    calls: list[dict] = []

    async def fake_generate(**kwargs):
        calls.append(kwargs)
        return {"created": 1, "data": [{"url": "/v1/files/image?id=abc"}]}

    monkeypatch.setattr("app.products.openai.images.generate", fake_generate)
    monkeypatch.setattr("app.products.web.webui.images.data_path", lambda *parts: tmp_path.joinpath(*parts))

    response = asyncio.run(
        image_studio_api.webui_image_generations(
            ImageGenerationRequest(
                model="gpt-image-2",
                prompt="draw a cat",
                n=2,
                size="1024x1024",
            ),
            user=WebUIUser(id="alice", username="alice", gpt_models=("gpt-image-2",)),
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


def test_webui_image_generation_locks_quality_for_basic_user(monkeypatch, tmp_path) -> None:
    calls: list[dict] = []

    async def fake_generate(**kwargs):
        calls.append(kwargs)
        return {"created": 1, "data": [{"url": "/v1/files/image?id=abc"}]}

    monkeypatch.setattr("app.products.openai.images.generate", fake_generate)
    monkeypatch.setattr("app.products.web.webui.images.data_path", lambda *parts: tmp_path.joinpath(*parts))

    response = asyncio.run(
        image_studio_api.webui_image_generations(
            image_studio_api.WebUIImageGenerationRequest(
                model="gpt-image-2",
                prompt="draw a cat",
                n=1,
                size="1024x1024",
                quality="4k",
            ),
            user=WebUIUser(id="alice", username="alice", gpt_models=("gpt-image-2",)),
        )
    )
    body = orjson.loads(response.body)

    assert body["quality"] == "1k"
    assert body["studio_session"]["quality"] == "1k"
    assert calls[0]["prompt"] == "draw a cat"


def test_webui_image_generation_allows_premium_quality(monkeypatch, tmp_path) -> None:
    calls: list[dict] = []

    async def fake_generate(**kwargs):
        calls.append(kwargs)
        return {"created": 1, "data": [{"url": "/v1/files/image?id=abc"}]}

    monkeypatch.setattr("app.products.openai.images.generate", fake_generate)
    monkeypatch.setattr("app.products.web.webui.images.data_path", lambda *parts: tmp_path.joinpath(*parts))
    monkeypatch.setattr("app.products.web.webui.images._premium_usernames", lambda: {"alice"})

    response = asyncio.run(
        image_studio_api.webui_image_generations(
            image_studio_api.WebUIImageGenerationRequest(
                model="codex-gpt-image-2",
                prompt="draw a cat",
                n=1,
                size="1024x1024",
                quality="4k",
            ),
            user=WebUIUser(id="alice", username="alice", gpt_models=("codex-gpt-image-2",)),
        )
    )
    body = orjson.loads(response.body)

    assert body["quality"] == "4k"
    assert "Output target: 4K high-detail image." in calls[0]["prompt"]


def test_webui_image_generation_appends_to_existing_studio_session(monkeypatch, tmp_path) -> None:
    async def fake_generate(**kwargs):
        return {"created": 1, "data": [{"url": f"/v1/files/image?id={kwargs['prompt'].replace(' ', '-')}"}]}

    monkeypatch.setattr("app.products.openai.images.generate", fake_generate)
    monkeypatch.setattr("app.products.web.webui.images.data_path", lambda *parts: tmp_path.joinpath(*parts))

    user = WebUIUser(id="alice", username="alice", gpt_models=("gpt-image-2",))
    first = asyncio.run(
        image_studio_api.webui_image_generations(
            image_studio_api.WebUIImageGenerationRequest(
                model="gpt-image-2",
                prompt="first cat",
                n=1,
                size="1024x1024",
            ),
            user=user,
        )
    )
    first_body = orjson.loads(first.body)
    session_id = first_body["studio_session"]["id"]

    second = asyncio.run(
        image_studio_api.webui_image_generations(
            image_studio_api.WebUIImageGenerationRequest(
                model="gpt-image-2",
                prompt="second dog",
                n=1,
                size="1024x1024",
                session_id=session_id,
            ),
            user=user,
        )
    )
    second_body = orjson.loads(second.body)
    listed = asyncio.run(image_studio_api.webui_image_history(user=user))
    history = orjson.loads(listed.body)["data"]

    assert second_body["studio_session"]["id"] == session_id
    assert len(history) == 1
    assert [turn["prompt"] for turn in history[0]["turns"]] == ["first cat", "second dog"]
    assert history[0]["title"] == "first cat"
    assert history[0]["prompt"] == "second dog"


def test_webui_image_models_are_filtered_by_user_gpt_permissions() -> None:
    response = asyncio.run(
        image_studio_api.list_webui_image_models(
            user=WebUIUser(
                id="alice",
                username="alice",
                gpt_models=("codex-gpt-image-2",),
                gpt_image_quality="2k",
            ),
        )
    )
    body = orjson.loads(response.body)
    ids = {item["id"] for item in body["generation"]}
    edit_ids = {item["id"] for item in body["edits"]}
    generation_by_id = {item["id"]: item for item in body["generation"]}
    edits_by_id = {item["id"]: item for item in body["edits"]}

    assert "codex-gpt-image-2" in ids
    assert "gpt-image-2" not in ids
    assert "codex-gpt-image-2" in edit_ids
    assert "gpt-image-2" not in edit_ids
    assert "grok-imagine-image-edit" not in edit_ids
    assert generation_by_id["codex-gpt-image-2"]["capability"] == "image"
    assert edits_by_id["codex-gpt-image-2"]["capability"] == "image"
    assert body["workspace"]["gpt_models"] == ["codex-gpt-image-2"]
    assert body["workspace"]["quality"]["max"] == "2k"


def test_webui_image_generation_rejects_disallowed_gpt_model(monkeypatch, tmp_path) -> None:
    async def fake_generate(**kwargs):
        raise AssertionError("disallowed model should fail before upstream")

    monkeypatch.setattr("app.products.openai.images.generate", fake_generate)
    monkeypatch.setattr("app.products.web.webui.images.data_path", lambda *parts: tmp_path.joinpath(*parts))

    try:
        asyncio.run(
            image_studio_api.webui_image_generations(
                image_studio_api.WebUIImageGenerationRequest(
                    model="gpt-image-2",
                    prompt="draw a cat",
                    n=1,
                ),
                user=WebUIUser(
                    id="alice",
                    username="alice",
                    gpt_models=("codex-gpt-image-2",),
                ),
            )
        )
    except ValidationError as exc:
        assert exc.code == "model_not_allowed"
    else:
        raise AssertionError("expected ValidationError")


def test_webui_image_edit_defaults_to_gpt_image_2(monkeypatch, tmp_path) -> None:
    calls: list[dict] = []

    async def fake_edit(**kwargs):
        calls.append(kwargs)
        return {"created": 1, "data": [{"url": "/v1/files/image?id=edited"}]}

    async def fake_uploads_to_data_uris(_uploads, *, mask=None):
        return ["data:image/png;base64,aW1hZ2U="]

    async def fake_append_history_session(_user, **kwargs):
        return {"model": kwargs["model"]}

    monkeypatch.setattr("app.products.openai.images.edit", fake_edit)
    monkeypatch.setattr("app.products.web.webui.images._uploads_to_data_uris", fake_uploads_to_data_uris)
    monkeypatch.setattr("app.products.web.webui.images._append_history_session", fake_append_history_session)
    monkeypatch.setattr("app.products.web.webui.images.data_path", lambda *parts: tmp_path.joinpath(*parts))

    response = asyncio.run(
        image_studio_api.webui_image_edits(
            prompt="make it blue",
            size="720x1280",
            image=[type("Upload", (), {"filename": "ref.png"})()],
            user=WebUIUser(
                id="alice",
                username="alice",
                gpt_models=("gpt-image-2",),
            ),
        )
    )
    body = orjson.loads(response.body)

    assert body["data"][0]["url"] == "/v1/files/image?id=edited"
    assert calls == [
        {
            "model": "gpt-image-2",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "make it blue"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
                        },
                    ],
                }
            ],
            "n": 1,
            "size": "1024x1024",
            "response_format": "url",
            "stream": False,
            "chat_format": False,
        }
    ]
    assert body["studio_session"]["model"] == "gpt-image-2"


def test_webui_image_edit_accepts_generated_reference_url(monkeypatch, tmp_path) -> None:
    calls: list[dict] = []
    history_calls: list[dict] = []
    file_id = "1234567890abcdef1234567890abcdef"
    image_dir = tmp_path / "files" / "images"
    image_dir.mkdir(parents=True)
    (image_dir / f"{file_id}.png").write_bytes(b"ref")

    async def fake_edit(**kwargs):
        calls.append(kwargs)
        return {"created": 1, "data": [{"url": "/v1/files/image?id=edited"}]}

    async def fake_append_history_session(_user, **kwargs):
        history_calls.append(kwargs)
        return {"model": kwargs["model"], "reference_names": kwargs["reference_names"]}

    monkeypatch.setattr("app.products.openai.images.edit", fake_edit)
    monkeypatch.setattr("app.products.web.webui.images._append_history_session", fake_append_history_session)
    monkeypatch.setattr("app.products.web.webui.images.image_files_dir", lambda: image_dir)
    monkeypatch.setattr("app.products.web.webui.images.data_path", lambda *parts: tmp_path.joinpath(*parts))

    response = asyncio.run(
        image_studio_api.webui_image_edits(
            prompt="make it cinematic",
            reference_url=[f"/v1/files/image?id={file_id}"],
            user=WebUIUser(
                id="alice",
                username="alice",
                gpt_models=("gpt-image-2",),
            ),
        )
    )
    body = orjson.loads(response.body)

    assert body["data"][0]["url"] == "/v1/files/image?id=edited"
    content = calls[0]["messages"][0]["content"]
    assert content == [
        {"type": "text", "text": "make it cinematic"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,cmVm"},
        },
    ]
    assert calls[0]["size"] == "1024x1024"
    assert history_calls[0]["reference_names"] == ["generated-12345678"]


def test_webui_image_history_delete_and_clear(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("app.products.web.webui.images.data_path", lambda *parts: tmp_path.joinpath(*parts))
    user = WebUIUser(id="alice", username="alice")

    cached = asyncio.run(
        image_studio_api.webui_image_cache_url(
            image_studio_api.WebUIImageUrlCacheRequest(
                url="/v1/files/image?id=abc",
                prompt="cached",
            ),
            user=user,
        )
    )
    session_id = orjson.loads(cached.body)["session"]["id"]
    listed = asyncio.run(image_studio_api.webui_image_history(user=user))
    assert len(orjson.loads(listed.body)["data"]) == 1

    deleted = asyncio.run(image_studio_api.delete_webui_image_history_session(session_id, user=user))
    assert orjson.loads(deleted.body)["deleted"] == 1

    asyncio.run(
        image_studio_api.webui_image_cache_url(
            image_studio_api.WebUIImageUrlCacheRequest(url="/v1/files/image?id=def"),
            user=user,
        )
    )
    cleared = asyncio.run(image_studio_api.clear_webui_image_history(user=user))
    assert orjson.loads(cleared.body)["ok"] is True
    listed_after = asyncio.run(image_studio_api.webui_image_history(user=user))
    assert orjson.loads(listed_after.body)["data"] == []
