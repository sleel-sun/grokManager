import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.control.account.enums import FeedbackKind
from app.products.web.webui.imagine import (
    _acquire_token,
    _image_event_has_displayable_output,
    _image_event_error_payload,
    imagine_ws,
    _no_webui_image_accounts_message,
    _resolve_webui_nsfw,
)
from app.platform.auth.middleware import WebUIUser


class WebuiImagineErrorPayloadTests(unittest.TestCase):
    def test_normalizes_stream_error_fields_for_masonry_frontend(self) -> None:
        payload = _image_event_error_payload(
            {
                "type": "error",
                "error_code": "rate_limit_exceeded",
                "error": "Image rate limit exceeded",
            },
            "run-1",
        )

        self.assertEqual(payload["type"], "error")
        self.assertEqual(payload["message"], "Image rate limit exceeded")
        self.assertEqual(payload["code"], "rate_limit_exceeded")
        self.assertEqual(payload["run_id"], "run-1")

    def test_no_webui_image_accounts_message_mentions_required_pool(self) -> None:
        message = _no_webui_image_accounts_message()

        self.assertIn("Super", message)
        self.assertIn("Heavy", message)

    def test_displayable_output_accepts_url_variants_and_blob(self) -> None:
        self.assertTrue(_image_event_has_displayable_output({"type": "image", "url": "/images/a.jpg"}))
        self.assertTrue(_image_event_has_displayable_output({"type": "image", "imageUrl": "/images/a.jpg"}))
        self.assertTrue(_image_event_has_displayable_output({"type": "image", "blob": "AA=="}))
        self.assertFalse(_image_event_has_displayable_output({"type": "progress", "progress": 50}))
        self.assertFalse(_image_event_has_displayable_output({"type": "image"}))


class _CaptureDirectory:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def reserve(self, **kwargs):
        self.calls.append(kwargs)
        return None


class WebuiImagineAccountSelectionTests(unittest.TestCase):
    def test_masonry_uses_super_heavy_pools_without_basic_fallback(self) -> None:
        from app.dataplane import account as account_module

        directory = _CaptureDirectory()

        async def run():
            with patch.object(account_module, "_directory", directory):
                return await _acquire_token(attempt=0)

        token, lease = asyncio.run(run())

        self.assertIsNone(token)
        self.assertIsNone(lease)
        self.assertEqual(directory.calls[0]["pool_candidates"], (1, 2))


class WebuiImagineNsfwPermissionTests(unittest.TestCase):
    def test_per_user_nsfw_permission_restricts_requested_and_default_nsfw(self) -> None:
        from app.products.web.webui import imagine

        allowed = WebUIUser(id="alice", username="alice", allow_nsfw=True)
        blocked = WebUIUser(id="bob", username="bob", allow_nsfw=False)
        enabled_config = _FakeConfig({"features.enable_nsfw": True})
        disabled_config = _FakeConfig({"features.enable_nsfw": False})

        with patch.object(imagine, "get_config", return_value=enabled_config):
            self.assertTrue(_resolve_webui_nsfw(allowed, None))
            self.assertTrue(_resolve_webui_nsfw(allowed, True))
            self.assertFalse(_resolve_webui_nsfw(allowed, False))
            self.assertFalse(_resolve_webui_nsfw(blocked, None))
            self.assertFalse(_resolve_webui_nsfw(blocked, True))

        with patch.object(imagine, "get_config", return_value=disabled_config):
            self.assertFalse(_resolve_webui_nsfw(allowed, True))


class _FakeConfig:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, key: str, default=None):
        return self._values.get(key, default)

    def get_bool(self, key: str, default: bool = False) -> bool:
        return bool(self._values.get(key, default))

    def get_int(self, key: str, default: int = 0) -> int:
        return int(self._values.get(key, default))


class _FakeWebSocket:
    def __init__(self, first_message: dict) -> None:
        self.headers = {}
        self.query_params = {}
        self.client_state = WebSocketState.CONNECTED
        self.sent: list[dict] = []
        self.closed: list[tuple[int, str]] = []
        self._first_message = json.dumps(first_message)
        self._received_first = False
        self._error_sent = asyncio.Event()

    async def accept(self) -> None:
        pass

    async def receive_text(self) -> str:
        if not self._received_first:
            self._received_first = True
            return self._first_message
        await asyncio.wait_for(self._error_sent.wait(), timeout=1)
        raise WebSocketDisconnect()

    async def send_text(self, payload: str) -> None:
        data = json.loads(payload)
        self.sent.append(data)
        if data.get("type") == "error":
            self._error_sent.set()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


class _LeaseDirectory:
    def __init__(self) -> None:
        self.lease = SimpleNamespace(token="token-1", mode_id=9)
        self.released: list[str] = []
        self.feedback_calls: list[tuple[str, FeedbackKind, int]] = []

    async def reserve(self, **_kwargs):
        return self.lease

    async def release(self, lease) -> None:
        self.released.append(lease.token)

    async def feedback(self, token, kind, mode_id, **_kwargs) -> None:
        self.feedback_calls.append((token, kind, mode_id))


class WebuiImagineWebSocketTests(unittest.TestCase):
    def test_empty_generation_stream_sends_error_instead_of_completed(self) -> None:
        from app.dataplane import account as account_module
        from app.dataplane.reverse.transport import imagine_ws as imagine_transport
        from app.products.web.webui import imagine

        directory = _LeaseDirectory()
        config = _FakeConfig(
            {
                "features.enable_nsfw": True,
                "image.max_retries": 0,
                "image.account_retry_min_retries": 0,
                "retry.on_codes": "",
            }
        )
        websocket = _FakeWebSocket(
            {
                "type": "start",
                "prompt": "draw a cat",
                "aspect_ratio": "2:3",
                "count": 1,
                "quality": "speed",
            }
        )

        async def fake_stream_images(*_args, **_kwargs):
            yield {"type": "progress", "image_id": "img-1", "progress": 80}

        async def run():
            with (
                patch.object(imagine, "_is_allowed", return_value=True),
                patch.object(imagine, "_webui_user", return_value=WebUIUser(id="alice", username="alice")),
                patch.object(imagine, "get_config", return_value=config),
                patch.object(account_module, "_directory", directory),
                patch.object(imagine_transport, "stream_images", side_effect=fake_stream_images),
                patch.object(imagine, "_schedule_account_sync", return_value=None),
            ):
                await imagine_ws(websocket)

        asyncio.run(run())

        error_payloads = [item for item in websocket.sent if item.get("type") == "error"]
        completed = [item for item in websocket.sent if item.get("type") == "status" and item.get("status") == "completed"]

        self.assertEqual(error_payloads[-1]["message"], "Image generation returned no images")
        self.assertEqual(error_payloads[-1]["code"], "upstream_error")
        self.assertEqual(completed, [])
        self.assertEqual(directory.released, ["token-1"])
        self.assertEqual(directory.feedback_calls[0][1], FeedbackKind.SERVER_ERROR)


if __name__ == "__main__":
    unittest.main()
