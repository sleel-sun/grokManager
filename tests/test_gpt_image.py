from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.control.account.commands import AccountPatch, AccountUpsert
from app.control.account.enums import AccountStatus
from app.control.account.models import AccountRecord
from app.control.model.registry import resolve
from app.platform.errors import RateLimitError, UpstreamError
from app.products.openai import gpt_image
from app.products.web.admin.gpt_accounts import gpt_account_credential_record_token
from app.products.web.admin.gpt_image_accounts import (
    GPTImageAccountItem,
    account_credential_record_token,
    _ext_for_item,
    _export_record,
    _summary,
    account_record_token,
)

_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9s"
    "AAAAASUVORK5CYII="
)


def _tiny_png_bytes() -> bytes:
    return base64.b64decode(_TINY_PNG_B64)


def _tiny_png_data_uri() -> str:
    return f"data:image/png;base64,{_TINY_PNG_B64}"


def test_gpt_image_models_are_registered_as_image_models() -> None:
    one = resolve("gpt-image-1")
    two = resolve("gpt-image-2")
    codex = resolve("codex-gpt-image-2")

    assert one.is_image()
    assert two.is_image()
    assert codex.is_image()
    assert one.is_image_edit()
    assert two.is_image_edit()
    assert codex.is_image_edit()
    assert one.upstream_profile == "chatgpt_image"
    assert one.upstream_model_name() == "gpt-image-2"
    assert two.upstream_model_name() == "gpt-image-2"
    assert codex.upstream_model_name() == "codex-gpt-image-2"


def test_gpt_image_account_record_token_is_stable_and_non_secret() -> None:
    first = account_record_token("token-a")
    second = account_record_token("token-a")

    assert first == second
    assert first.startswith("gpt_")
    assert "token-a" not in first


def test_gpt_image_account_ext_uses_unified_gpt_account_shape() -> None:
    item = GPTImageAccountItem(
        access_token="Bearer abc123",
        email="user@example.test",
        alias="image user",
        is_free=True,
    )

    ext = _ext_for_item(item)

    assert item.access_token == "abc123"
    assert ext["gpt"] is True
    assert ext["gpt_access_token"] == "abc123"
    assert ext["gpt_plan_type"] == "free"
    assert ext["gpt_image_is_free"] is True
    assert ext["gpt_status"] == "unchecked"


def test_gpt_image_account_credentials_map_to_unified_gpt_record() -> None:
    item = GPTImageAccountItem(
        email=" Image@Example.test ",
        password="chat-pass",
        mail_token="mail-token",
        email_provider="DuckMail",
    )

    ext = _ext_for_item(item)
    record_token = account_credential_record_token(item.email or "")

    assert record_token.startswith("gptcred_")
    assert "Image@Example" not in record_token
    assert ext["gpt_access_token"] is None
    assert ext["gpt_email"] == "Image@Example.test"
    assert ext["gpt_password"] == "chat-pass"
    assert ext["gpt_mail_token"] == "mail-token"
    assert ext["gpt_email_provider"] == "DuckMail"
    assert ext["gpt_status"] == "login_required"


def test_gpt_image_account_summary_and_secret_export() -> None:
    record = AccountRecord(
        token="gpt_123",
        tags=["gpt"],
        ext={
            "gpt": True,
            "gpt_access_token": "image-access-secret",
            "gpt_email": "image@example.test",
            "gpt_password": "password-secret",
            "gpt_mail_token": "mail-secret",
            "gpt_plan_type": "free",
            "gpt_image_is_free": True,
            "gpt_status": "available",
        },
    )

    summary = _summary([record])
    safe_export = _export_record(record, include_secrets=False)
    secret_export = _export_record(record, include_secrets=True)

    assert summary["total"] == 1
    assert summary["available"] == 1
    assert summary["types"]["free"] == 1
    assert summary["with_access_token"] == 1
    assert summary["with_credentials"] == 1
    assert "access_token" not in safe_export
    assert secret_export["access_token"] == "image-access-secret"
    assert secret_export["password"] == "password-secret"
    assert secret_export["mail_token"] == "mail-secret"


def test_gpt_image_parse_sse_extracts_conversation_and_file_ids() -> None:
    raw = "\n".join(
        [
            'data: {"conversation_id":"conv_1"}',
            'data: {"message":{"content":{"content_type":"text","parts":["working"]}}}',
            "data: file-service://file_123",
            "data: sediment://sed_456",
            "data: [DONE]",
        ]
    )

    conversation_id, file_ids, text = gpt_image._parse_sse(raw)

    assert conversation_id == "conv_1"
    assert file_ids == ["file_123", "sed:sed_456"]
    assert text == "working"


def test_gpt_image_extracts_assistant_asset_pointer_without_tool_metadata() -> None:
    mapping = {
        "node_1": {
            "message": {
                "author": {"role": "assistant"},
                "content": {
                    "content_type": "multimodal_text",
                    "parts": [
                        {
                            "content_type": "image_asset_pointer",
                            "asset_pointer": "file-service://file_success_123",
                        }
                    ],
                },
            }
        }
    }

    assert gpt_image._extract_image_ids(mapping) == ["file_success_123"]


def test_gpt_image_extracts_nested_sediment_asset_pointer() -> None:
    mapping = {
        "node_1": {
            "message": {
                "author": {"role": "assistant"},
                "metadata": {
                    "attachments": [
                        {
                            "type": "image",
                            "asset_pointer": "sediment://sed_success_456",
                        }
                    ]
                },
                "content": {"content_type": "text", "parts": ["done"]},
            }
        }
    }

    assert gpt_image._extract_image_ids(mapping) == ["sed:sed_success_456"]


def test_gpt_image_recursive_asset_extraction_skips_user_prompt() -> None:
    mapping = {
        "node_1": {
            "message": {
                "author": {"role": "user"},
                "content": {
                    "content_type": "text",
                    "parts": ["literal file-service://not_generated in prompt"],
                },
            }
        }
    }

    assert gpt_image._extract_image_ids(mapping) == []


def test_gpt_image_prompt_forces_generation_not_search() -> None:
    prompt = gpt_image._image_generation_prompt("马斯克直播图")

    assert "Create exactly one original image" in prompt
    assert "Do not search the web" in prompt
    assert "image_group" in prompt
    assert "马斯克直播图" in prompt


def test_gpt_image_no_image_error_sanitizes_image_group_text() -> None:
    message = gpt_image._no_image_error('马斯克直播图image_group{"query":["Elon Musk"]}')

    assert message == "ChatGPT returned image search results instead of a generated image"


def test_gpt_image_no_image_error_sanitizes_processing_queue_text() -> None:
    message = gpt_image._no_image_error("正在处理图片\n\n目前有很多人在创建图片，因此可能需要一点时间。")

    assert message == "ChatGPT image generation is still queued upstream; retry later"


def test_gpt_image_upstream_model_uses_gpt_image_2_directly() -> None:
    assert gpt_image._normalize_image_model("gpt-image-1") == "gpt-image-2"
    assert gpt_image._normalize_image_model("gpt-image-2") == "gpt-image-2"
    assert gpt_image._normalize_image_model("codex-gpt-image-2") == "codex-gpt-image-2"
    assert gpt_image._upstream_model("gpt-image-1", is_free=True) == "gpt-image-2"
    assert gpt_image._upstream_model("gpt-image-1", is_free=False) == "gpt-image-2"
    assert gpt_image._upstream_model("gpt-image-2", is_free=True) == "gpt-image-2"
    assert gpt_image._upstream_model("gpt-image-2", is_free=False) == "gpt-image-2"
    assert gpt_image._upstream_model("codex-gpt-image-2", is_free=True) == "codex-gpt-image-2"


def test_gpt_image_send_conversation_uses_direct_backend_api(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeContent:
        async def iter_chunked(self, _size: int):
            yield b'data: {"conversation_id":"conv_1"}\n'
            yield b"data: sediment://file_123\n"

    class FakeResponse:
        ok = True
        content = FakeContent()

        def release(self) -> None:
            captured["released"] = True

    async def fake_chat_requirements(_session, _context):
        return "chat-token", None

    async def fake_request(_session, method: str, url: str, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr(gpt_image, "_chat_requirements", fake_chat_requirements)
    monkeypatch.setattr(gpt_image, "_request", fake_request)
    context = gpt_image._ChatGPTContext(
        access_token="access-token",
        device_id="device-id",
        script="sdk.js",
        dpl="build",
    )

    conversation_id, file_ids, _text = asyncio.run(
        gpt_image._send_conversation(
            None,
            context,
            prompt="draw a cube",
            requested_model="gpt-image-2",
            is_free=True,
        )
    )

    payload = captured["json_body"]
    headers = captured["headers"]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://chatgpt.com/backend-api/conversation"
    assert "x-conduit-token" not in headers
    assert "x-openai-target-path" not in headers
    assert "Create exactly one original image" in payload["messages"][0]["content"]["parts"][0]
    assert "draw a cube" in payload["messages"][0]["content"]["parts"][0]
    assert payload["messages"][0]["metadata"] == {"attachments": []}
    assert payload["model"] == "gpt-image-2"
    assert payload["force_use_sse"] is True
    assert payload["system_hints"] == ["picture_v2"]
    assert conversation_id == "conv_1"
    assert file_ids == ["sed:file_123"]
    assert captured["released"] is True


def test_gpt_image_edit_reference_upload_adds_azure_blob_header(monkeypatch) -> None:
    requests: list[dict[str, object]] = []

    class FakeResponse:
        ok = True
        status = 200

        def __init__(self, payload: dict[str, object] | None = None) -> None:
            self._payload = payload or {}

        async def json(self, content_type=None):
            return self._payload

        def release(self) -> None:
            pass

    async def fake_request(_session, method: str, url: str, **kwargs):
        requests.append({
            "method": method,
            "url": url,
            **kwargs,
            "headers": dict(kwargs.get("headers") or {}),
        })
        if method == "POST" and url.endswith("/backend-api/files"):
            return FakeResponse(
                {
                    "file_id": "file_ref_1",
                    "upload_url": (
                        "https://account.blob.core.windows.net/container/ref.png?sig=test"
                    ),
                    "requiredHeaders": {"x-ms-meta-origin": "chatgpt"},
                }
            )
        if method == "PUT":
            return FakeResponse()
        if method == "POST" and url.endswith("/backend-api/files/file_ref_1/uploaded"):
            return FakeResponse()
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(gpt_image, "_request", fake_request)
    context = gpt_image._ChatGPTContext(
        access_token="access-token",
        device_id="device-id",
        script="sdk.js",
        dpl="build",
    )

    reference = asyncio.run(
        gpt_image._upload_edit_reference(
            None,
            context,
            _tiny_png_data_uri(),
            0,
        )
    )

    put_request = next(request for request in requests if request["method"] == "PUT")
    headers = put_request["headers"]
    assert reference.file_id == "file_ref_1"
    assert reference.width == 1
    assert reference.height == 1
    assert put_request["data"] == _tiny_png_bytes()
    assert headers["content-type"] == "image/png"
    assert headers["x-ms-blob-type"] == "BlockBlob"
    assert headers["x-ms-meta-origin"] == "chatgpt"


def test_gpt_image_edit_reference_upload_headers_handle_custom_azure_sas_url() -> None:
    headers = gpt_image._edit_reference_upload_headers(
        {"requiredHeaders": {"x-ms-blob-type": ""}},
        "https://uploads.example.test/ref.png?sv=2024-11-04&sr=b&sp=w&sig=test",
        "image/png",
    )

    assert headers["x-ms-blob-type"] == "BlockBlob"


def test_gpt_image_edit_reference_upload_retries_missing_blob_type(monkeypatch) -> None:
    requests: list[dict[str, object]] = []

    class FakeResponse:
        status = 200

        def __init__(
            self,
            payload: dict[str, object] | None = None,
            *,
            ok: bool = True,
            status: int = 200,
            body: str = "",
        ) -> None:
            self._payload = payload or {}
            self.ok = ok
            self.status = status
            self._body = body

        async def json(self, content_type=None):
            return self._payload

        async def text(self):
            return self._body

        def release(self) -> None:
            pass

    async def fake_request(_session, method: str, url: str, **kwargs):
        requests.append({
            "method": method,
            "url": url,
            **kwargs,
            "headers": dict(kwargs.get("headers") or {}),
        })
        if method == "POST" and url.endswith("/backend-api/files"):
            return FakeResponse(
                {
                    "file_id": "file_ref_1",
                    "upload_url": "https://uploads.example.test/container/ref.png",
                }
            )
        if method == "PUT" and len([item for item in requests if item["method"] == "PUT"]) == 1:
            return FakeResponse(
                ok=False,
                status=400,
                body=(
                    "<Error><Code>MissingRequiredHeader</Code>"
                    "<HeaderName>x-ms-blob-type</HeaderName></Error>"
                ),
            )
        if method == "PUT":
            assert kwargs["headers"]["x-ms-blob-type"] == "BlockBlob"
            return FakeResponse()
        if method == "POST" and url.endswith("/backend-api/files/file_ref_1/uploaded"):
            return FakeResponse()
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(gpt_image, "_request", fake_request)
    context = gpt_image._ChatGPTContext(
        access_token="access-token",
        device_id="device-id",
        script="sdk.js",
        dpl="build",
    )

    reference = asyncio.run(
        gpt_image._upload_edit_reference(
            None,
            context,
            _tiny_png_data_uri(),
            0,
        )
    )

    put_requests = [request for request in requests if request["method"] == "PUT"]
    assert reference.file_id == "file_ref_1"
    assert len(put_requests) == 2
    assert "x-ms-blob-type" not in put_requests[0]["headers"]
    assert put_requests[1]["headers"]["x-ms-blob-type"] == "BlockBlob"


def test_gpt_image_send_edit_conversation_uses_picture_f_conversation(monkeypatch) -> None:
    requests: list[dict[str, object]] = []
    released: dict[str, bool] = {}

    class FakeContent:
        async def iter_chunked(self, _size: int):
            yield (
                b'data: {"conversation_id":"conv_edit","message":{"author":{"role":"user"},'
                b'"content":{"content_type":"multimodal_text","parts":[{"asset_pointer":'
                b'"file-service://file_ref_0"}]}}}\n'
            )
            yield (
                b'data: {"conversation_id":"conv_edit","message":{"author":{"role":"tool"},'
                b'"metadata":{"async_task_type":"image_gen"},"content":{"content_type":'
                b'"multimodal_text","parts":[{"asset_pointer":"file-service://file_result"}]}}}\n'
            )

    class FakeResponse:
        def __init__(self, payload: dict[str, object] | None = None) -> None:
            self._payload = payload or {}

        ok = True
        content = FakeContent()

        async def json(self, content_type=None):
            return self._payload

        def release(self) -> None:
            released["stream"] = True

    async def fake_upload(_session, _context, _image_input, index: int):
        return gpt_image._EditReference(
            file_id=f"file_ref_{index}",
            name=f"reference-{index}.png",
            mime_type="image/png",
            size=5,
            width=1,
            height=1,
        )

    async def fake_chat_requirements(_session, _context):
        return "chat-token", None

    async def fake_request(_session, method: str, url: str, **kwargs):
        requests.append({"method": method, "url": url, **kwargs})
        if url.endswith("/backend-api/f/conversation/prepare"):
            return FakeResponse({"conduit_token": "conduit-token"})
        if url.endswith("/backend-api/f/conversation"):
            return FakeResponse()
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(gpt_image, "_upload_edit_reference", fake_upload)
    monkeypatch.setattr(gpt_image, "_chat_requirements", fake_chat_requirements)
    monkeypatch.setattr(gpt_image, "_request", fake_request)
    context = gpt_image._ChatGPTContext(
        access_token="access-token",
        device_id="device-id",
        script="sdk.js",
        dpl="build",
    )

    conversation_id, file_ids, _text = asyncio.run(
        gpt_image._send_edit_conversation(
            None,
            context,
            prompt="make it blue",
            image_inputs=[_tiny_png_data_uri()],
            requested_model="gpt-image-1",
            is_free=True,
        )
    )

    prepare_request = requests[0]
    conversation_request = requests[1]
    prepare_payload = prepare_request["json_body"]
    payload = conversation_request["json_body"]
    message = payload["messages"][0]
    parts = message["content"]["parts"]
    assert prepare_request["url"] == "https://chatgpt.com/backend-api/f/conversation/prepare"
    assert prepare_payload["model"] == "gpt-5-3"
    assert "make it blue" in prepare_payload["partial_query"]["content"]["parts"][0]
    assert conversation_request["url"] == "https://chatgpt.com/backend-api/f/conversation"
    assert conversation_request["headers"]["x-conduit-token"] == "conduit-token"
    assert message["content"]["content_type"] == "multimodal_text"
    assert parts[0]["content_type"] == "image_asset_pointer"
    assert parts[0]["asset_pointer"] == "file-service://file_ref_0"
    assert parts[0]["width"] == 1
    assert "Edit the provided reference image" in parts[1]
    assert "make it blue" in parts[1]
    assert message["metadata"]["attachments"][0]["id"] == "file_ref_0"
    assert message["metadata"]["attachments"][0]["mimeType"] == "image/png"
    assert message["metadata"]["attachments"][0]["width"] == 1
    assert payload["model"] == "gpt-5-3"
    assert payload["system_hints"] == ["picture_v2"]
    assert conversation_id == "conv_edit"
    assert file_ids == ["file_result"]
    assert released["stream"] is True


def test_gpt_image_generate_preserves_requested_gpt_image_model(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run_generation(prompt: str, model: str, n: int):
        captured.update({"prompt": prompt, "model": model, "n": n})
        return [
            gpt_image._GeneratedImage(
                b64_json=base64.b64encode(b"image").decode("ascii"),
            )
        ]

    monkeypatch.setattr(gpt_image, "_run_generation", fake_run_generation)

    result = asyncio.run(
        gpt_image.generate(
            model="gpt-image-1",
            prompt="draw a cube",
            response_format="b64_json",
        )
    )

    assert captured == {"prompt": "draw a cube", "model": "gpt-image-2", "n": 1}
    assert result["data"][0]["b64_json"] == base64.b64encode(b"image").decode("ascii")


def test_gpt_image_edit_preserves_requested_gpt_image_model(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run_edit(prompt: str, image_inputs: list[str], model: str, n: int):
        captured.update({"prompt": prompt, "image_inputs": image_inputs, "model": model, "n": n})
        return [
            gpt_image._GeneratedImage(
                b64_json=base64.b64encode(b"image").decode("ascii"),
            )
        ]

    monkeypatch.setattr(gpt_image, "_run_edit", fake_run_edit)

    result = asyncio.run(
        gpt_image.edit(
            model="gpt-image-1",
            prompt="edit a cube",
            image_inputs=["data:image/png;base64,aW1hZ2U="],
            response_format="b64_json",
        )
    )

    assert captured == {
        "prompt": "edit a cube",
        "image_inputs": ["data:image/png;base64,aW1hZ2U="],
        "model": "gpt-image-2",
        "n": 1,
    }
    assert result["data"][0]["b64_json"] == base64.b64encode(b"image").decode("ascii")


def test_gpt_image_account_import_uses_disabled_status_patch_shape() -> None:
    access_token = "access-token"
    upsert = AccountUpsert(
        token=account_record_token(access_token),
        pool="basic",
        tags=["gpt"],
        ext=_ext_for_item(GPTImageAccountItem(access_token=access_token)),
    )
    patch = AccountPatch(
        token=upsert.token,
        status=AccountStatus.DISABLED,
        state_reason="GPT account record; excluded from Grok SSO pool",
    )

    assert upsert.ext["gpt_access_token"] == access_token
    assert patch.status == AccountStatus.DISABLED


def test_gpt_image_accounts_accepts_ordinary_gpt_access_token() -> None:
    record = AccountRecord(
        token="gpt_123",
        tags=["gpt"],
        ext={
            "gpt": True,
            "gpt_access_token": "ordinary-access-token",
            "gpt_plan_type": "free",
            "gpt_status": "available",
        },
    )

    account = asyncio.run(gpt_image._account_from_record(record))

    assert account is not None
    assert account.record_token == "gpt_123"
    assert account.access_token == "ordinary-access-token"
    assert account.is_free is True


def test_gpt_image_accounts_accepts_unchecked_access_token() -> None:
    record = AccountRecord(
        token="gptimg_123",
        tags=["gpt-image"],
        ext={
            "gpt_image": True,
            "gpt_image_access_token": "unchecked-access-token",
            "gpt_image_status": "unchecked",
        },
    )

    account = asyncio.run(gpt_image._account_from_record(record))

    assert account is not None
    assert account.access_token == "unchecked-access-token"
    assert account.status_key == "gpt_image_status"
    assert account.error_key == "gpt_image_error"


def test_gpt_image_accounts_prefer_unified_fields_on_migrated_record() -> None:
    record = AccountRecord(
        token="gptimg_123",
        tags=["gpt-image", "gpt"],
        ext={
            "gpt_image": True,
            "gpt_image_access_token": "legacy-access-token",
            "gpt_image_status": "invalid",
            "gpt": True,
            "gpt_access_token": "unified-access-token",
            "gpt_status": "available",
            "gpt_plan_type": "plus",
        },
    )

    account = asyncio.run(gpt_image._account_from_record(record))

    assert account is not None
    assert account.access_token == "unified-access-token"
    assert account.status_key == "gpt_status"
    assert account.error_key == "gpt_registration_error"
    assert account.is_free is False


def test_gpt_image_accounts_skip_invalid_access_token() -> None:
    record = AccountRecord(
        token="gptimg_123",
        tags=["gpt-image"],
        ext={
            "gpt_image": True,
            "gpt_image_access_token": "invalid-access-token",
            "gpt_image_status": "invalid",
        },
    )

    account = asyncio.run(gpt_image._account_from_record(record))

    assert account is None


def test_gpt_image_accounts_skip_timeout_during_cooldown() -> None:
    record = AccountRecord(
        token="gptimg_123",
        tags=["gpt-image"],
        ext={
            "gpt_image": True,
            "gpt_image_access_token": "timeout-access-token",
            "gpt_image_status": "timeout",
            "gpt_image_cooldown_until": gpt_image._now_ms() + 60_000,
        },
    )

    account = asyncio.run(gpt_image._account_from_record(record))

    assert account is None


def test_gpt_image_accounts_skip_recent_generation_timeout_even_if_status_available() -> None:
    record = AccountRecord(
        token="gpt_123",
        tags=["gpt"],
        last_fail_at=gpt_image._now_ms(),
        last_fail_reason="ChatGPT image generation timed out after 180s: timeout",
        ext={
            "gpt": True,
            "gpt_access_token": "ordinary-access-token",
            "gpt_status": "available",
        },
    )

    account = asyncio.run(gpt_image._account_from_record(record))

    assert account is None


def test_gpt_image_accounts_skip_recent_image_search_failure() -> None:
    record = AccountRecord(
        token="gptimg_123",
        tags=["gpt-image"],
        last_fail_at=gpt_image._now_ms(),
        last_fail_reason="ChatGPT returned image search results instead of a generated image",
        ext={
            "gpt_image": True,
            "gpt_image_access_token": "search-routed-token",
            "gpt_image_status": "failed",
        },
    )

    account = asyncio.run(gpt_image._account_from_record(record))

    assert account is None


def test_gpt_image_accounts_skip_recent_free_plan_limit() -> None:
    record = AccountRecord(
        token="gpt_123",
        tags=["gpt"],
        last_fail_at=gpt_image._now_ms(),
        last_fail_reason=(
            "You've hit the Free plan limit for image generations requests. "
            "You can create more images when the limit resets in 7 hours and 48 minutes."
        ),
        ext={
            "gpt": True,
            "gpt_access_token": "free-plan-token",
            "gpt_status": "available",
        },
    )

    account = asyncio.run(gpt_image._account_from_record(record))

    assert account is None


def test_gpt_image_accounts_skip_active_generation_cooldown() -> None:
    record = AccountRecord(
        token="gpt_123",
        tags=["gpt"],
        ext={
            "gpt": True,
            "gpt_access_token": "cooldown-token",
            "gpt_status": "available",
            "gpt_cooldown_until": gpt_image._now_ms() + 60_000,
        },
    )

    account = asyncio.run(gpt_image._account_from_record(record))

    assert account is None


def test_gpt_image_accounts_retry_rate_limited_after_cooldown() -> None:
    record = AccountRecord(
        token="gpt_123",
        tags=["gpt"],
        last_fail_at=gpt_image._now_ms() - 2 * 3600 * 1000,
        last_fail_reason=(
            "You've hit the Free plan limit for image generations requests. "
            "You can create more images when the limit resets in 1 hour."
        ),
        ext={
            "gpt": True,
            "gpt_access_token": "retry-token",
            "gpt_status": "rate_limited",
            "gpt_cooldown_until": gpt_image._now_ms() - 60_000,
        },
    )

    account = asyncio.run(gpt_image._account_from_record(record))

    assert account is not None
    assert account.access_token == "retry-token"


def test_gpt_image_duplicate_retryable_failure_does_not_block_after_cooldown() -> None:
    access_token = "shared-token"
    old_failure = gpt_image._now_ms() - 2 * 3600 * 1000
    blocked = AccountRecord(
        token="gpt_image_123",
        tags=["gpt-image"],
        last_fail_at=old_failure,
        last_fail_reason="ChatGPT image generation timed out after 60s: timeout",
        ext={
            "gpt_image": True,
            "gpt_image_access_token": access_token,
            "gpt_image_status": "timeout",
            "gpt_image_cooldown_until": gpt_image._now_ms() - 60_000,
        },
    )

    assert gpt_image._record_blocked_access_token(blocked) == ""


def test_repair_timed_out_gpt_image_accounts_clears_stale_failure(monkeypatch) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.patches: list[AccountPatch] = []

        async def list_accounts(self, query):
            assert query.include_deleted is False
            return SimpleNamespace(
                items=[
                    AccountRecord(
                        token="gpt_123",
                        tags=["gpt"],
                        status=AccountStatus.DISABLED,
                        state_reason="GPT account record; excluded from Grok SSO pool",
                        last_fail_at=gpt_image._now_ms() - 3600 * 1000,
                        last_fail_reason="ChatGPT image generation timed out after 600s: timeout",
                        ext={
                            "gpt": True,
                            "gpt_access_token": "timeout-token",
                            "gpt_status": "timeout",
                            "gpt_registration_error": "timeout",
                            "gpt_cooldown_until": gpt_image._now_ms() - 60_000,
                        },
                    )
                ],
                total=1,
            )

        async def patch_accounts(self, patches):
            self.patches.extend(patches)
            return SimpleNamespace(patched=len(patches))

    class Config:
        def get_bool(self, *_args, **_kwargs):
            return True

        def get_float(self, key, default):
            if key == "gpt_image.timeout_repair_after_s":
                return 1800.0
            return default

    monkeypatch.setattr(gpt_image, "get_config", lambda: Config())
    repo = FakeRepo()

    repaired = asyncio.run(gpt_image.repair_timed_out_gpt_image_accounts(repo))

    assert repaired == 1
    patch = repo.patches[0]
    assert patch.token == "gpt_123"
    assert patch.status is None
    assert patch.clear_last_failure is True
    assert patch.clear_failures is False
    assert patch.ext_merge["gpt_status"] == "available"
    assert patch.ext_merge["gpt_registration_error"] is None
    assert patch.ext_merge["gpt_cooldown_until"] == 0


def test_repair_timed_out_gpt_image_accounts_keeps_recent_timeout(monkeypatch) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.patches: list[AccountPatch] = []

        async def list_accounts(self, _query):
            return SimpleNamespace(
                items=[
                    AccountRecord(
                        token="gpt_123",
                        tags=["gpt"],
                        last_fail_at=gpt_image._now_ms(),
                        last_fail_reason="ChatGPT image generation timed out after 600s: timeout",
                        ext={
                            "gpt": True,
                            "gpt_access_token": "timeout-token",
                            "gpt_status": "timeout",
                        },
                    )
                ],
                total=1,
            )

        async def patch_accounts(self, patches):
            self.patches.extend(patches)
            return SimpleNamespace(patched=len(patches))

    class Config:
        def get_bool(self, *_args, **_kwargs):
            return True

        def get_float(self, key, default):
            if key == "gpt_image.timeout_repair_after_s":
                return 1800.0
            return default

    monkeypatch.setattr(gpt_image, "get_config", lambda: Config())
    repo = FakeRepo()

    repaired = asyncio.run(gpt_image.repair_timed_out_gpt_image_accounts(repo))

    assert repaired == 0
    assert repo.patches == []


def test_gpt_image_failure_patch_sets_reset_cooldown() -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.patches: list[AccountPatch] = []

        async def patch_accounts(self, patches):
            self.patches.extend(patches)

    repo = FakeRepo()
    account = gpt_image.GPTImageAccount(
        record_token="gpt_123",
        access_token="free-plan-token",
        status_key="gpt_status",
        error_key="gpt_registration_error",
    )
    exc = UpstreamError(
        "You've hit the Free plan limit for image generations requests. "
        "You can create more images when the limit resets in 7 hours and 48 minutes.",
        status=429,
    )

    status, _message = asyncio.run(gpt_image._patch_account_failure(repo, account, exc))

    assert status == "rate_limited"
    cooldown_until = repo.patches[0].ext_merge["gpt_cooldown_until"]
    assert cooldown_until > gpt_image._now_ms() + 7 * 3600 * 1000


def test_gpt_image_524_is_retryable_timeout() -> None:
    exc = UpstreamError("ChatGPT image edit upstream returned 524", status=524)

    assert 524 in gpt_image._TRANSIENT_STATUSES
    assert gpt_image._capability_failure_status(exc) == "timeout"


def test_gpt_image_response_error_normalizes_cloudflare_524() -> None:
    class FakeResponse:
        status = 524

        async def text(self):
            return "cloudflare timeout"

    exc = asyncio.run(gpt_image._response_error(FakeResponse(), "ChatGPT image-edit conversation failed"))

    assert exc.status == 504
    assert "Cloudflare 524" in exc.message
    assert "cloudflare timeout" in exc.message


def test_gpt_image_accounts_dedupe_tokens_and_prioritize_unified_gpt_records(monkeypatch) -> None:
    class FakeRepo:
        async def list_accounts(self, query):
            return SimpleNamespace(
                total=3,
                items=[
                    AccountRecord(
                        token="gpt_1",
                        tags=["gpt"],
                        ext={
                            "gpt": True,
                            "gpt_access_token": "shared-token",
                            "gpt_status": "available",
                        },
                    ),
                    AccountRecord(
                        token="gptimg_1",
                        tags=["gpt-image"],
                        ext={
                            "gpt_image": True,
                            "gpt_image_access_token": "image-token",
                            "gpt_image_status": "available",
                        },
                    ),
                    AccountRecord(
                        token="gptimg_2",
                        tags=["gpt-image"],
                        ext={
                            "gpt_image": True,
                            "gpt_image_access_token": "shared-token",
                            "gpt_image_status": "available",
                        },
                    ),
                ],
            )

    monkeypatch.setattr(gpt_image, "get_account_repository", lambda: FakeRepo())

    accounts = asyncio.run(gpt_image._gpt_image_accounts())

    assert [item.record_token for item in accounts] == ["gpt_1", "gptimg_1"]


def test_gpt_image_accounts_block_duplicate_token_after_image_timeout(monkeypatch) -> None:
    shared_token = "shared-timeout-token"

    class FakeRepo:
        async def list_accounts(self, query):
            return SimpleNamespace(
                total=2,
                items=[
                    AccountRecord(
                        token="gptimg_1",
                        tags=["gpt-image"],
                        last_fail_at=gpt_image._now_ms(),
                        last_fail_reason="ChatGPT image generation timed out after 180s: timeout",
                        ext={
                            "gpt_image": True,
                            "gpt_image_access_token": shared_token,
                            "gpt_image_status": "timeout",
                        },
                    ),
                    AccountRecord(
                        token="gpt_1",
                        tags=["gpt"],
                        ext={
                            "gpt": True,
                            "gpt_access_token": shared_token,
                            "gpt_status": "available",
                        },
                    ),
                ],
            )

    monkeypatch.setattr(gpt_image, "get_account_repository", lambda: FakeRepo())

    accounts = asyncio.run(gpt_image._gpt_image_accounts())

    assert accounts == []


def test_gpt_image_accounts_login_ordinary_gpt_credentials(monkeypatch) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.patches = []

        async def patch_accounts(self, patches):
            self.patches.extend(patches)

    repo = FakeRepo()
    record = AccountRecord(
        token=gpt_account_credential_record_token("user@example.test"),
        tags=["gpt"],
        ext={
            "gpt": True,
            "gpt_email": "user@example.test",
            "gpt_password": "chat-pass",
            "gpt_mail_token": "mail-token",
            "gpt_plan_type": "free",
            "gpt_status": "login_required",
        },
    )

    monkeypatch.setattr(gpt_image, "get_account_repository", lambda: repo)

    async def fake_login(**kwargs):
        return "fresh-session-token"

    monkeypatch.setattr(gpt_image, "_login_gpt_credentials_async", fake_login)

    account = asyncio.run(gpt_image._account_from_record(record))

    assert account is not None
    assert account.access_token == "fresh-session-token"
    assert repo.patches
    patch = repo.patches[0]
    assert patch.ext_merge["gpt_access_token"] == "fresh-session-token"
    assert patch.ext_merge["gpt_status"] == "available"


def test_gpt_image_mark_success_sets_available(monkeypatch) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.patches = []

        async def patch_accounts(self, patches):
            self.patches.extend(patches)

    repo = FakeRepo()
    monkeypatch.setattr(gpt_image, "get_account_repository", lambda: repo)
    account = gpt_image.GPTImageAccount(
        record_token="gptimg_123",
        access_token="token",
        status_key="gpt_image_status",
        error_key="gpt_image_error",
    )

    asyncio.run(gpt_image._mark_account_success(account))

    patch = repo.patches[0]
    assert patch.last_use_at is not None
    assert patch.ext_merge["gpt_image_status"] == "available"
    assert patch.ext_merge["gpt_image_error"] is None
    assert "gpt_image_last_checked_at" in patch.ext_merge


def test_gpt_image_mark_failure_marks_invalid_token(monkeypatch) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.patches = []

        async def patch_accounts(self, patches):
            self.patches.extend(patches)

    repo = FakeRepo()
    monkeypatch.setattr(gpt_image, "get_account_repository", lambda: repo)
    account = gpt_image.GPTImageAccount(
        record_token="gptimg_123",
        access_token="token",
        status_key="gpt_image_status",
        error_key="gpt_image_error",
    )
    exc = UpstreamError("ChatGPT chat-requirements failed", status=401, body="unauthorized")

    asyncio.run(gpt_image._mark_account_failure(account, exc))

    patch = repo.patches[0]
    assert patch.last_fail_at is not None
    assert patch.ext_merge["gpt_image_status"] == "invalid"
    assert patch.ext_merge["gpt_image_error"] == (
        "ChatGPT access token is invalid or revoked; re-login or replace this GPT account"
    )


def test_gpt_image_mark_failure_skips_cloudflare_524_cooldown(monkeypatch) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.patches = []

        async def patch_accounts(self, patches):
            self.patches.extend(patches)

    repo = FakeRepo()
    monkeypatch.setattr(gpt_image, "get_account_repository", lambda: repo)
    account = gpt_image.GPTImageAccount(
        record_token="gptimg_123",
        access_token="token",
        status_key="gpt_image_status",
        error_key="gpt_image_error",
    )
    exc = UpstreamError(
        "ChatGPT image-edit conversation failed: upstream timed out (Cloudflare 524)",
        status=504,
        body="cloudflare timeout",
    )

    asyncio.run(gpt_image._mark_account_failure(account, exc))

    assert repo.patches == []


def test_gpt_image_test_account_success_marks_available(monkeypatch) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.patches = []

        async def patch_accounts(self, patches):
            self.patches.extend(patches)

    async def validate(access_token: str) -> None:
        assert access_token == "valid-token"

    repo = FakeRepo()
    monkeypatch.setattr(gpt_image, "_validate_access_token", validate)
    record = AccountRecord(
        token="gptimg_123",
        tags=["gpt-image"],
        ext={
            "gpt_image": True,
            "gpt_image_access_token": "valid-token",
            "gpt_image_status": "unchecked",
        },
    )

    result = asyncio.run(gpt_image.test_gpt_account_record(record, repo=repo))

    assert result["ok"] is True
    assert result["capability_status"] == "available"
    patch = repo.patches[0]
    assert patch.ext_merge["gpt_image_status"] == "available"
    assert patch.ext_merge["gpt_image_error"] is None
    assert "gpt_image_last_checked_at" in patch.ext_merge


def test_gpt_image_test_account_failure_marks_invalid(monkeypatch) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.patches = []

        async def patch_accounts(self, patches):
            self.patches.extend(patches)

    async def validate(access_token: str) -> None:
        raise UpstreamError("ChatGPT chat-requirements failed", status=401, body="unauthorized")

    repo = FakeRepo()
    monkeypatch.setattr(gpt_image, "_validate_access_token", validate)
    record = AccountRecord(
        token="gpt_123",
        tags=["gpt"],
        ext={
            "gpt": True,
            "gpt_access_token": "invalid-token",
            "gpt_status": "unchecked",
        },
    )

    result = asyncio.run(gpt_image.test_gpt_account_record(record, repo=repo))

    assert result["ok"] is False
    assert result["kind"] == "gpt"
    assert result["capability_status"] == "invalid"
    patch = repo.patches[0]
    assert patch.ext_merge["gpt_status"] == "invalid"
    assert patch.ext_merge["gpt_registration_error"] == (
        "ChatGPT access token is invalid or revoked; re-login or replace this GPT account"
    )


def test_gpt_image_generate_one_has_hard_timeout(monkeypatch) -> None:
    async def slow_generate(*args, **kwargs):
        await asyncio.sleep(1)
        raise AssertionError("timeout should cancel first")

    monkeypatch.setattr(gpt_image, "_generate_one_inner", slow_generate)
    monkeypatch.setattr(gpt_image, "_generation_timeout_s", lambda: 0.01)
    account = gpt_image.GPTImageAccount(
        record_token="gptimg_123",
        access_token="token",
    )

    with pytest.raises(UpstreamError) as excinfo:
        asyncio.run(gpt_image._generate_one(account, "draw", "gpt-image-2"))

    assert excinfo.value.status == 504
    assert "timed out" in str(excinfo.value)


def test_gpt_image_generate_one_passes_timeout_budget_to_inner(monkeypatch) -> None:
    captured: list[float | None] = []

    async def fake_generate(_account, _prompt, _model, *, timeout_s=None):
        captured.append(timeout_s)
        return gpt_image._GeneratedImage(b64_json="aW1hZ2U=")

    monkeypatch.setattr(gpt_image, "_generate_one_inner", fake_generate)
    account = gpt_image.GPTImageAccount(
        record_token="gptimg_123",
        access_token="token",
    )

    image = asyncio.run(gpt_image._generate_one(account, "draw", "gpt-image-2", timeout_s=321))

    assert image.b64_json == "aW1hZ2U="
    assert captured == [321]


def test_gpt_image_run_generation_defaults_to_four_account_attempts(monkeypatch) -> None:
    attempts: list[str] = []
    accounts = [
        gpt_image.GPTImageAccount(record_token="gptimg_1", access_token="token-1"),
        gpt_image.GPTImageAccount(record_token="gptimg_2", access_token="token-2"),
        gpt_image.GPTImageAccount(record_token="gptimg_3", access_token="token-3"),
        gpt_image.GPTImageAccount(record_token="gptimg_4", access_token="token-4"),
        gpt_image.GPTImageAccount(record_token="gptimg_5", access_token="token-5"),
    ]

    async def fake_accounts():
        return accounts

    async def fail_generate(account, prompt, model, *, timeout_s=None):
        attempts.append(account.record_token)
        raise UpstreamError("ChatGPT image generation timed out after 180s", status=504)

    async def mark_failure(account, exc):
        return None

    monkeypatch.setattr(gpt_image, "_gpt_image_accounts", fake_accounts)
    monkeypatch.setattr(gpt_image, "_generate_one", fail_generate)
    monkeypatch.setattr(gpt_image, "_mark_account_failure", mark_failure)

    with pytest.raises(UpstreamError):
        asyncio.run(gpt_image._run_generation("draw", "gpt-image-2", 1))

    assert attempts == ["gptimg_1", "gptimg_2", "gptimg_3", "gptimg_4"]


def test_gpt_image_max_account_attempts_falls_back_to_four(monkeypatch) -> None:
    class BrokenConfig:
        def get_int(self, *_args, **_kwargs):
            raise RuntimeError("config unavailable")

    monkeypatch.setattr(gpt_image, "get_config", lambda: BrokenConfig())

    assert gpt_image._max_account_attempts_per_image(10) == 4


def test_gpt_image_run_generation_uses_single_request_timeout_budget(monkeypatch) -> None:
    accounts = [
        gpt_image.GPTImageAccount(record_token="gptimg_1", access_token="token-1"),
        gpt_image.GPTImageAccount(record_token="gptimg_2", access_token="token-2"),
    ]
    attempts: list[tuple[str, float | None]] = []

    async def fake_accounts():
        return accounts

    async def slow_fail_generate(account, prompt, model, *, timeout_s=None):
        attempts.append((account.record_token, timeout_s))
        await asyncio.sleep(0.25)
        raise UpstreamError("You've hit the Free plan limit for image generations requests.", status=429)

    async def mark_failure(account, exc):
        return None

    monkeypatch.setattr(gpt_image, "_gpt_image_accounts", fake_accounts)
    monkeypatch.setattr(gpt_image, "_generation_timeout_s", lambda: 1.1)
    monkeypatch.setattr(gpt_image, "_max_account_attempts_per_image", lambda count: 2)
    monkeypatch.setattr(gpt_image, "_generate_one", slow_fail_generate)
    monkeypatch.setattr(gpt_image, "_mark_account_failure", mark_failure)

    with pytest.raises(UpstreamError) as excinfo:
        asyncio.run(gpt_image._run_generation("draw", "gpt-image-2", 1))

    assert excinfo.value.status == 504
    assert [item[0] for item in attempts] == ["gptimg_1"]
    assert attempts[0][1] is not None
    assert attempts[0][1] <= 1.1


def test_gpt_image_run_generation_prefers_quota_failure_detail(monkeypatch) -> None:
    accounts = [
        gpt_image.GPTImageAccount(record_token="gpt_1", access_token="token-1"),
        gpt_image.GPTImageAccount(record_token="gpt_2", access_token="token-2"),
    ]
    attempts: list[str] = []

    async def fake_accounts():
        return accounts

    async def fake_generate(account, prompt, model, *, timeout_s=None):
        attempts.append(account.record_token)
        if account.record_token == "gpt_1":
            raise UpstreamError("You've hit the Free plan limit for image generations requests.", status=429)
        raise UpstreamError('ChatGPT image generation failed: upstream returned 401: {"detail":"Unauthorized"}', status=401)

    async def mark_failure(account, exc):
        return None

    monkeypatch.setattr(gpt_image, "_gpt_image_accounts", fake_accounts)
    monkeypatch.setattr(gpt_image, "_max_account_attempts_per_image", lambda count: 2)
    monkeypatch.setattr(gpt_image, "_generate_one", fake_generate)
    monkeypatch.setattr(gpt_image, "_mark_account_failure", mark_failure)

    with pytest.raises(RateLimitError) as excinfo:
        asyncio.run(gpt_image._run_generation("draw", "gpt-image-2", 1))

    assert attempts == ["gpt_1", "gpt_2"]
    assert "Free plan limit" in str(excinfo.value)


def test_gpt_image_run_generation_sanitizes_revoked_token_failure(monkeypatch) -> None:
    accounts = [
        gpt_image.GPTImageAccount(record_token="gpt_1", access_token="token-1"),
    ]

    async def fake_accounts():
        return accounts

    async def fake_generate(account, prompt, model, *, timeout_s=None):
        raise UpstreamError(
            'ChatGPT chat-requirements failed: upstream returned 401: {"error":{"code":"token_revoked"}}',
            status=401,
            body='{"error":{"code":"token_revoked"}}',
        )

    async def mark_failure(account, exc):
        return None

    monkeypatch.setattr(gpt_image, "_gpt_image_accounts", fake_accounts)
    monkeypatch.setattr(gpt_image, "_generate_one", fake_generate)
    monkeypatch.setattr(gpt_image, "_mark_account_failure", mark_failure)

    with pytest.raises(RateLimitError) as excinfo:
        asyncio.run(gpt_image._run_generation("draw", "gpt-image-2", 1))

    message = str(excinfo.value)
    assert "invalid or revoked" in message
    assert "chat-requirements" not in message
    assert "token_revoked" not in message


def test_account_admin_page_uses_unified_gptchat_account_panel() -> None:
    html = (
        Path(__file__).resolve().parent.parent
        / "app"
        / "statics"
        / "admin"
        / "account.html"
    ).read_text(encoding="utf-8")

    assert "GPTChat 账号池" in html
    assert 'id="gpt-account-tbody"' in html
    assert 'id="modal-gpt-account-add"' in html
    assert "_api('GET', '/gpt/accounts')" in html
    assert "_api('POST', '/gpt/accounts'" in html
    assert "_api('POST', '/gpt/accounts/test'" in html
    assert "_api('DELETE', '/gpt/accounts'" in html
    assert 'id="modal-gpt-image"' not in html
    assert 'id="gpt-image-tbody"' not in html
    assert "/gpt-image/accounts" not in html


def test_maintainer_page_uses_unified_gptchat_registration_panel() -> None:
    html = (
        Path(__file__).resolve().parent.parent
        / "app"
        / "statics"
        / "admin"
        / "maintainer.html"
    ).read_text(encoding="utf-8")

    assert "GPTChat 账号批量注册" in html
    assert 'id="gpt-account-form"' in html
    assert 'id="gpt-account-oauth-tokens"' in html
    assert 'id="gpt-account-bulk-credentials"' in html
    assert 'id="gpt-account-test-btn"' in html
    assert "parseGPTAccountBulkCredentials(" in html
    assert "api('POST', '/gpt/accounts'" in html
    assert "runGPTAccountTest('/gpt/accounts/test'" in html
    assert 'id="gpt-image-form"' not in html
    assert "readGPTImagePayload()" not in html
    assert "/gpt-image/accounts" not in html
