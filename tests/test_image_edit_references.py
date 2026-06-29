import asyncio
import base64
import importlib
import json
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from app.control.account.enums import FeedbackKind
from app.platform.errors import UpstreamError, ValidationError
from app.products.openai.images import _prepare_edit_reference, _prepare_edit_references
from app.products.openai.schemas import ImageGenerationRequest


class _FakeConfig:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, key: str, default=None):
        return self._values.get(key, default)

    def get_str(self, key: str, default: str = "") -> str:
        return self._values.get(key, default)

    def get_bool(self, key: str, default: bool = False) -> bool:
        return bool(self._values.get(key, default))

    def get_float(self, key: str, default: float = 0.0) -> float:
        return float(self._values.get(key, default))

    def get_int(self, key: str, default: int = 0) -> int:
        return int(self._values.get(key, default))


class _FakeLease:
    def __init__(self, token: str) -> None:
        self.token = token


class _FakeImageDirectory:
    def __init__(self) -> None:
        self.leases = [_FakeLease("bad-token"), _FakeLease("good-token")]
        self.reserved: list[tuple[str, ...]] = []
        self.released: list[str] = []
        self.feedback_calls: list[tuple[str, FeedbackKind, int]] = []

    async def reserve(self, *, exclude_tokens=None, **_kwargs):
        excluded = tuple(exclude_tokens or ())
        self.reserved.append(excluded)
        for lease in self.leases:
            if lease.token not in excluded:
                return lease
        return None

    async def reserve_any(self, *, exclude_tokens=None, **kwargs):
        return await self.reserve(exclude_tokens=exclude_tokens, **kwargs)

    async def release(self, lease) -> None:
        self.released.append(lease.token)

    async def feedback(self, token, kind, mode_id, **_kwargs) -> None:
        self.feedback_calls.append((token, kind, mode_id))


class _FakeUpload:
    def __init__(self, raw: bytes, *, filename: str = "image.png", content_type: str = "image/png") -> None:
        self._raw = raw
        self.filename = filename
        self.content_type = content_type

    async def read(self) -> bytes:
        return self._raw


def _png_bytes(image: Image.Image) -> bytes:
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


class ImageEditReferenceTests(unittest.TestCase):
    def test_uploads_to_data_uris_composes_mask_alpha(self) -> None:
        from app.products.openai.router import _uploads_to_data_uris

        image_raw = _png_bytes(Image.new("RGB", (2, 1), (255, 0, 0)))
        mask_raw = _png_bytes(Image.frombytes("L", (2, 1), bytes([0, 255])))

        [data_uri] = asyncio.run(
            _uploads_to_data_uris([_FakeUpload(image_raw)], mask=_FakeUpload(mask_raw))
        )

        prefix, payload = data_uri.split(",", 1)
        decoded = base64.b64decode(payload)
        with Image.open(BytesIO(decoded)) as composed:
            composed = composed.convert("RGBA")
            self.assertEqual(prefix, "data:image/png;base64")
            self.assertEqual(composed.getpixel((0, 0))[3], 0)
            self.assertEqual(composed.getpixel((1, 0))[3], 255)

    def test_upstream_asset_content_url_skips_reupload(self) -> None:
        url = "https://assets.grok.com/users/user-1/asset-1/content"

        with patch(
            "app.products.openai.images.upload_from_input",
            side_effect=AssertionError("should not re-upload upstream asset content URL"),
        ):
            resolved = asyncio.run(_prepare_edit_reference("token", url, 0))

        self.assertEqual(resolved, url)

    def test_prepare_edit_references_unwraps_taskgroup_upstream_error(self) -> None:
        async def fake_prepare_reference(token: str, image_input: str, index: int) -> str:
            if index == 1:
                raise UpstreamError("reference 2 upload failed", status=403)
            return f"resolved-{image_input}"

        with patch(
            "app.products.openai.images._prepare_edit_reference",
            side_effect=fake_prepare_reference,
        ):
            with self.assertRaises(UpstreamError) as ctx:
                asyncio.run(_prepare_edit_references("token", ["img-a", "img-b"]))

        self.assertEqual(ctx.exception.message, "reference 2 upload failed")
        self.assertEqual(ctx.exception.status, 403)


class ImageGenerationErrorTests(unittest.TestCase):
    def test_cloudflare_403_error_mentions_clearance_configuration(self) -> None:
        from app.products.openai import images

        message = images._image_generation_upstream_error_message(
            403,
            '<!DOCTYPE html><title>Just a moment...</title><p>Cloudflare</p>',
        )

        self.assertIn("Cloudflare challenge", message)
        self.assertIn("proxy.clearance", message)

    def test_non_cloudflare_403_error_keeps_plain_status(self) -> None:
        from app.products.openai import images

        message = images._image_generation_upstream_error_message(403, "")

        self.assertEqual(message, "Image-generation upstream returned 403")


class ImageGenerationOutputTests(unittest.TestCase):
    def test_standalone_generation_defaults_to_b64_json(self) -> None:
        router_module = importlib.import_module("app.products.openai.router")

        captured: dict[str, object] = {}

        async def fake_generate(**kwargs):
            captured.update(kwargs)
            return {"created": 1, "data": [{"b64_json": "aW1hZ2U="}]}

        req = ImageGenerationRequest(model="gpt-image-2", prompt="draw a cat")
        with patch("app.products.openai.images.generate", side_effect=fake_generate):
            response = asyncio.run(router_module.image_generations(req))

        body = json.loads(response.body)
        self.assertEqual(captured["response_format"], "b64_json")
        self.assertEqual(body["data"][0]["b64_json"], "aW1hZ2U=")

    def test_standalone_generation_preserves_explicit_url_format(self) -> None:
        router_module = importlib.import_module("app.products.openai.router")

        captured: dict[str, object] = {}

        async def fake_generate(**kwargs):
            captured.update(kwargs)
            return {"created": 1, "data": [{"url": "/v1/files/image?id=abc"}]}

        req = ImageGenerationRequest(
            model="gpt-image-2",
            prompt="draw a cat",
            response_format="url",
        )
        with patch("app.products.openai.images.generate", side_effect=fake_generate):
            response = asyncio.run(router_module.image_generations(req))

        body = json.loads(response.body)
        self.assertEqual(captured["response_format"], "url")
        self.assertEqual(body["data"][0]["url"], "/v1/files/image?id=abc")

    def test_standalone_generation_accepts_camel_case_response_format(self) -> None:
        req = ImageGenerationRequest.model_validate(
            {
                "model": "gpt-image-2",
                "prompt": "draw a cat",
                "responseFormat": "b64_json",
            }
        )

        self.assertEqual(req.response_format, "b64_json")

    def test_content_asset_url_file_id_falls_back_to_route_safe_hash(self) -> None:
        from app.products.openai import images

        file_id = images._extract_image_file_id(
            "https://assets.grok.com/users/user-1/asset-1/content"
        )

        self.assertRegex(file_id, r"^[0-9a-f]{32}$")
        self.assertNotEqual(file_id, "content")

    def test_grok_url_format_does_not_download_even_when_app_url_is_configured(self) -> None:
        from app.products.openai import images

        url = "https://assets.grok.com/users/user-1/image-1/content"
        cfg = _FakeConfig(
            {
                "app.app_url": "http://127.0.0.1:8000",
                "features.image_format": "grok_url",
            }
        )

        with patch("app.products.openai.images.get_config", return_value=cfg), patch(
            "app.products.openai.images._download_image_bytes",
            side_effect=AssertionError("grok_url must not download assets"),
        ):
            output = asyncio.run(
                images._resolve_image_output(
                    token="token",
                    url=url,
                    response_format="url",
                )
            )

        self.assertEqual(output.api_value, url)
        self.assertEqual(output.markdown_value, f"![image]({url})")

    def test_local_url_format_saves_with_relative_url_when_app_url_is_empty(self) -> None:
        from app.products.openai import images

        url = "https://assets.grok.com/users/user-1/asset-1/content"
        cfg = _FakeConfig(
            {
                "app.app_url": "",
                "features.image_format": "local_url",
            }
        )

        saved_ids: list[str] = []

        def fake_save(_raw: bytes, _mime: str, file_id: str) -> str:
            saved_ids.append(file_id)
            return file_id

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        with patch("app.products.openai.images.get_config", return_value=cfg), patch(
            "app.products.openai.images._download_image_bytes",
            new=AsyncMock(return_value=(b"image-bytes", "image/jpeg")),
        ), patch(
            "app.products.openai.images.asyncio.to_thread",
            side_effect=fake_to_thread,
        ), patch("app.products.openai.images._save_image", side_effect=fake_save):
            output = asyncio.run(
                images._resolve_image_output(
                    token="token",
                    url=url,
                    response_format="url",
                )
            )

        self.assertEqual(saved_ids, [images._extract_image_file_id(url)])
        self.assertEqual(output.api_value, f"/v1/files/image?id={saved_ids[0]}")
        self.assertEqual(output.markdown_value, f"![image](/v1/files/image?id={saved_ids[0]})")

    def test_local_url_format_falls_back_to_upstream_url_when_download_fails(self) -> None:
        from app.products.openai import images

        url = "https://assets.grok.com/users/user-1/image-1/content"
        cfg = _FakeConfig(
            {
                "app.app_url": "http://127.0.0.1:8000",
                "features.image_format": "local_url",
            }
        )

        with patch("app.products.openai.images.get_config", return_value=cfg), patch(
            "app.products.openai.images._download_image_bytes",
            side_effect=UpstreamError("download returned 403", status=403),
        ):
            output = asyncio.run(
                images._resolve_image_output(
                    token="token",
                    url=url,
                    response_format="url",
                )
            )

        self.assertEqual(output.api_value, url)
        self.assertEqual(output.markdown_value, f"![image]({url})")


class ImageGenerationRetryTests(unittest.TestCase):
    def test_image_retry_codes_include_cloudflare_524_timeout(self) -> None:
        from app.products.openai import images

        retry_codes = images._image_retry_codes(_FakeConfig({"retry.on_codes": ""}))

        self.assertIn(504, retry_codes)
        self.assertIn(524, retry_codes)
        self.assertEqual(images._normalized_upstream_status(524), 504)
        self.assertIn("Cloudflare 524", images._image_generation_upstream_error_message(524, ""))

    def test_media_pool_candidates_include_all_pools_with_rotation(self) -> None:
        from app.control.model.registry import get as get_model
        from app.products.openai import images

        spec = get_model("grok-imagine-image")
        self.assertIsNotNone(spec)

        candidates = images._image_pool_candidates(spec)

        self.assertEqual(candidates, (1, 2, 0))
        self.assertEqual(images._rotate_pool_candidates(candidates, 0), (1, 2, 0))
        self.assertEqual(images._rotate_pool_candidates(candidates, 1), (2, 0, 1))
        self.assertEqual(images._rotate_pool_candidates(candidates, 2), (0, 1, 2))

    def test_lite_generation_retries_next_account_after_403(self) -> None:
        from app.dataplane import account as account_module
        from app.control.model.registry import get as get_model
        from app.products.openai import images

        directory = _FakeImageDirectory()
        spec = get_model("grok-imagine-image-lite")
        self.assertIsNotNone(spec)

        async def fake_stream_lite(token: str, *_args, **_kwargs):
            if token == "bad-token":
                raise UpstreamError("Image-generation upstream returned 403", status=403)
            yield 'data: {"ok":true}'

        class FakeStreamAdapter:
            def feed(self, _data: str):
                return [SimpleNamespace(kind="image", content="https://assets.grok.com/image.jpg")]

        async def fake_resolve(**_kwargs):
            return images._ImageOutput("https://assets.grok.com/image.jpg", "![image](ok)")

        async def run():
            with (
                patch.object(account_module, "_directory", directory),
                patch.object(images, "selection_max_retries", return_value=1),
                patch.object(images, "get_config", return_value=_FakeConfig({"retry.on_codes": "429"})),
                patch.object(images, "_stream_lite_generate", side_effect=fake_stream_lite),
                patch.object(images, "StreamAdapter", FakeStreamAdapter),
                patch.object(images, "_resolve_image_output", new=AsyncMock(side_effect=fake_resolve)),
                patch.object(images, "_quota_sync", new=AsyncMock(return_value=None)),
                patch.object(images, "_image_fail_sync", new=AsyncMock(return_value=None)),
            ):
                return await images._run_lite_request(
                    spec=spec,
                    prompt="draw a cat",
                    timeout_s=1,
                    response_format="url",
                )

        output = asyncio.run(run())

        self.assertEqual(output.api_value, "https://assets.grok.com/image.jpg")
        self.assertEqual(directory.reserved, [(), ("bad-token",)])
        self.assertEqual(directory.released, ["bad-token", "good-token"])
        self.assertEqual(directory.feedback_calls[0][1], FeedbackKind.RATE_LIMITED)
        self.assertEqual(directory.feedback_calls[1][1], FeedbackKind.SUCCESS)

    def test_lite_generation_uses_adapter_collected_image_urls(self) -> None:
        from app.dataplane import account as account_module
        from app.control.model.registry import get as get_model
        from app.products.openai import images

        directory = _FakeImageDirectory()
        spec = get_model("grok-imagine-image-lite")
        self.assertIsNotNone(spec)
        collected_url = "https://imgen.x.ai/generated/image-content?token=abc"

        async def fake_stream_lite(*_args, **_kwargs):
            yield 'data: {"ok":true}'
            yield "data: [DONE]"

        class FakeStreamAdapter:
            def __init__(self) -> None:
                self.text_buf = []
                self.image_urls = []

            def feed(self, _data: str):
                self.text_buf.append(f"Here is the image: ![image]({collected_url})")
                self.image_urls.append((collected_url, "ig_123"))
                return []

            def extract_generated_images_from_text(self, text: str) -> str:
                return text

        async def fake_resolve(**kwargs):
            self.assertEqual(kwargs["url"], collected_url)
            return images._ImageOutput(collected_url, f"![image]({collected_url})")

        async def run():
            with (
                patch.object(account_module, "_directory", directory),
                patch.object(images, "selection_max_retries", return_value=0),
                patch.object(images, "get_config", return_value=_FakeConfig({"retry.on_codes": ""})),
                patch.object(images, "_stream_lite_generate", side_effect=fake_stream_lite),
                patch.object(images, "StreamAdapter", FakeStreamAdapter),
                patch.object(images, "_resolve_image_output", new=AsyncMock(side_effect=fake_resolve)),
                patch.object(images, "_quota_sync", new=AsyncMock(return_value=None)),
                patch.object(images, "_image_fail_sync", new=AsyncMock(return_value=None)),
            ):
                return await images._run_lite_request(
                    spec=spec,
                    prompt="draw a cat",
                    timeout_s=1,
                    response_format="url",
                )

        output = asyncio.run(run())

        self.assertEqual(output.api_value, collected_url)
        self.assertEqual(directory.released, ["bad-token"])
        self.assertEqual(directory.feedback_calls[0][1], FeedbackKind.SUCCESS)

    def test_lite_stream_cancellation_drains_background_batch_task(self) -> None:
        from app.control.model.registry import get as get_model
        from app.products.openai import images

        spec = get_model("grok-imagine-image-lite")
        self.assertIsNotNone(spec)

        async def run() -> bool:
            started = asyncio.Event()
            cancelled = asyncio.Event()

            async def fake_run_lite_batch(**_kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            with (
                patch.object(images, "get_config", return_value=_FakeConfig({"chat.timeout": "1"})),
                patch.object(images, "_run_lite_batch", side_effect=fake_run_lite_batch),
            ):
                stream = await images._generate_lite(
                    spec=spec,
                    prompt="draw a cat",
                    n=1,
                    response_format="url",
                    stream=True,
                    chat_format=True,
                )
                next_chunk = asyncio.create_task(stream.__anext__())
                await asyncio.wait_for(started.wait(), timeout=1)
                next_chunk.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await next_chunk
                await asyncio.wait_for(cancelled.wait(), timeout=1)
                return cancelled.is_set()

        self.assertTrue(asyncio.run(run()))

    def test_ws_generation_retries_next_account_after_rate_limit_event(self) -> None:
        from app.dataplane import account as account_module
        from app.control.model.registry import get as get_model
        from app.products.openai import images

        directory = _FakeImageDirectory()
        spec = get_model("grok-imagine-image")
        self.assertIsNotNone(spec)
        cfg = _FakeConfig(
            {
                "features.enable_nsfw": True,
                "features.image_format": "grok_url",
                "retry.on_codes": "429",
            }
        )

        async def fake_stream_images(token: str, *_args, **_kwargs):
            if token == "bad-token":
                yield {
                    "type": "error",
                    "error_code": "rate_limit_exceeded",
                    "error": "Image rate limit exceeded",
                }
                return
            yield {
                "type": "image",
                "is_final": True,
                "url": "https://assets.grok.com/users/user-1/image-1/content",
                "image_id": "image-1",
            }

        fail_sync = AsyncMock(return_value=None)

        async def run():
            with (
                patch.object(account_module, "_directory", directory),
                patch.object(images, "selection_max_retries", return_value=1),
                patch.object(images, "get_config", return_value=cfg),
                patch.object(images, "resolve_model", return_value=spec),
                patch.object(images, "stream_images", side_effect=fake_stream_images),
                patch.object(images, "_quota_sync", new=AsyncMock(return_value=None)),
                patch.object(images, "_image_fail_sync", new=fail_sync),
                patch.object(
                    images,
                    "_resolve_image_output",
                    new=AsyncMock(return_value=images._ImageOutput("ok-url", "![image](ok-url)")),
                ),
            ):
                return await images.generate(
                    model="grok-imagine-image",
                    prompt="draw a cat",
                    n=1,
                    stream=False,
                )

        payload = asyncio.run(run())

        self.assertEqual(payload["data"], [{"url": "ok-url"}])
        self.assertEqual(directory.reserved, [(), ("bad-token",)])
        self.assertEqual(directory.released, ["bad-token", "good-token"])
        self.assertEqual(directory.feedback_calls[0][1], FeedbackKind.RATE_LIMITED)
        self.assertEqual(directory.feedback_calls[1][1], FeedbackKind.SUCCESS)
        fail_sync.assert_called_once()
        self.assertEqual(fail_sync.call_args.args[0], "bad-token")
        self.assertEqual(fail_sync.call_args.args[2].status, 429)

    def test_ws_generation_retries_next_account_after_transport_502(self) -> None:
        from app.dataplane import account as account_module
        from app.control.model.registry import get as get_model
        from app.products.openai import images

        directory = _FakeImageDirectory()
        spec = get_model("grok-imagine-image")
        self.assertIsNotNone(spec)
        cfg = _FakeConfig(
            {
                "features.enable_nsfw": True,
                "features.image_format": "grok_url",
                "retry.on_codes": "429",
            }
        )

        async def fake_stream_images(token: str, *_args, **_kwargs):
            if token == "bad-token":
                raise UpstreamError("Cannot connect to host grok.com:443", status=502)
            yield {
                "type": "image",
                "is_final": True,
                "url": "https://assets.grok.com/users/user-1/image-1/content",
                "image_id": "image-1",
            }

        fail_sync = AsyncMock(return_value=None)

        async def run():
            with (
                patch.object(account_module, "_directory", directory),
                patch.object(images, "selection_max_retries", return_value=1),
                patch.object(images, "get_config", return_value=cfg),
                patch.object(images, "resolve_model", return_value=spec),
                patch.object(images, "stream_images", side_effect=fake_stream_images),
                patch.object(images, "_quota_sync", new=AsyncMock(return_value=None)),
                patch.object(images, "_image_fail_sync", new=fail_sync),
                patch.object(
                    images,
                    "_resolve_image_output",
                    new=AsyncMock(return_value=images._ImageOutput("ok-url", "![image](ok-url)")),
                ),
            ):
                return await images.generate(
                    model="grok-imagine-image",
                    prompt="draw a cat",
                    n=1,
                    stream=False,
                )

        payload = asyncio.run(run())

        self.assertEqual(payload["data"], [{"url": "ok-url"}])
        self.assertEqual(directory.reserved, [(), ("bad-token",)])
        self.assertEqual(directory.feedback_calls[0][1], FeedbackKind.SERVER_ERROR)
        fail_sync.assert_called_once()
        self.assertEqual(fail_sync.call_args.args[2].status, 502)

    def test_ws_generation_empty_final_result_is_not_success(self) -> None:
        from app.dataplane import account as account_module
        from app.control.model.registry import get as get_model
        from app.products.openai import images

        directory = _FakeImageDirectory()
        spec = get_model("grok-imagine-image")
        self.assertIsNotNone(spec)
        cfg = _FakeConfig(
            {
                "features.enable_nsfw": True,
                "features.image_format": "grok_url",
                "image.max_retries": 0,
                "image.account_retry_min_retries": 0,
                "retry.on_codes": "",
            }
        )

        async def fake_stream_images(*_args, **_kwargs):
            if False:
                yield {}

        fail_sync = AsyncMock(return_value=None)

        async def run():
            with (
                patch.object(account_module, "_directory", directory),
                patch.object(images, "get_config", return_value=cfg),
                patch.object(images, "resolve_model", return_value=spec),
                patch.object(images, "stream_images", side_effect=fake_stream_images),
                patch.object(images, "_quota_sync", new=AsyncMock(return_value=None)),
                patch.object(images, "_image_fail_sync", new=fail_sync),
            ):
                return await images.generate(
                    model="grok-imagine-image",
                    prompt="draw a cat",
                    n=1,
                    stream=False,
                )

        with self.assertRaises(UpstreamError) as ctx:
            asyncio.run(run())

        self.assertEqual(ctx.exception.message, "Image generation returned no images")
        self.assertEqual(directory.feedback_calls[0][1], FeedbackKind.SERVER_ERROR)
        fail_sync.assert_called_once()

    def test_ws_stream_empty_final_result_emits_sse_error(self) -> None:
        from app.dataplane import account as account_module
        from app.control.model.registry import get as get_model
        from app.products.openai import images
        from app.products.openai.router import _safe_sse

        directory = _FakeImageDirectory()
        spec = get_model("grok-imagine-image")
        self.assertIsNotNone(spec)
        cfg = _FakeConfig(
            {
                "features.enable_nsfw": True,
                "features.image_format": "grok_url",
                "image.max_retries": 0,
                "image.account_retry_min_retries": 0,
                "retry.on_codes": "",
            }
        )

        async def fake_stream_images(*_args, **_kwargs):
            if False:
                yield {}

        async def run():
            with (
                patch.object(account_module, "_directory", directory),
                patch.object(images, "get_config", return_value=cfg),
                patch.object(images, "resolve_model", return_value=spec),
                patch.object(images, "stream_images", side_effect=fake_stream_images),
                patch.object(images, "_quota_sync", new=AsyncMock(return_value=None)),
                patch.object(images, "_image_fail_sync", new=AsyncMock(return_value=None)),
            ):
                stream = await images.generate(
                    model="grok-imagine-image",
                    prompt="draw a cat",
                    n=1,
                    stream=True,
                    chat_format=True,
                )
                return [chunk async for chunk in _safe_sse(stream)]

        chunks = asyncio.run(run())

        self.assertTrue(chunks[0].startswith("event: error\n"))
        self.assertIn("Image generation returned no images", chunks[0])
        self.assertEqual(chunks[-1], "data: [DONE]\n\n")
        self.assertEqual(directory.feedback_calls[0][1], FeedbackKind.SERVER_ERROR)

    def test_image_failure_sync_does_not_refresh_chat_quotas(self) -> None:
        from app.products.openai import images

        class FakeRefreshService:
            def __init__(self) -> None:
                self.recorded: list[tuple[str, int, BaseException | None]] = []
                self.refresh_called = False

            async def record_failure_async(self, token, mode_id, exc=None):
                self.recorded.append((token, mode_id, exc))

            async def refresh_on_demand(self):
                self.refresh_called = True
                raise AssertionError("image failures must not refresh chat quotas")

        svc = FakeRefreshService()

        with patch.object(images, "get_refresh_service", return_value=svc):
            asyncio.run(
                images._image_fail_sync(
                    "bad-token",
                    0,
                    UpstreamError("Image upstream returned 429", status=429),
                )
            )

        self.assertFalse(svc.refresh_called)
        self.assertEqual(len(svc.recorded), 1)
        self.assertEqual(svc.recorded[0][0], "bad-token")

    def test_image_account_forbidden_persists_as_quota_exhausted(self) -> None:
        from app.products.openai import images

        class FakeRefreshService:
            def __init__(self) -> None:
                self.recorded: list[tuple[str, int, BaseException | None]] = []

            async def record_failure_async(self, token, mode_id, exc=None):
                self.recorded.append((token, mode_id, exc))

        svc = FakeRefreshService()

        with patch.object(images, "get_refresh_service", return_value=svc):
            asyncio.run(
                images._image_fail_sync(
                    "bad-token",
                    0,
                    UpstreamError("Image-generation upstream returned 403", status=403),
                )
            )

        self.assertEqual(len(svc.recorded), 1)
        persisted_exc = svc.recorded[0][2]
        self.assertIsInstance(persisted_exc, UpstreamError)
        self.assertEqual(persisted_exc.status, 429)

    def test_image_edit_retries_next_account_after_reference_upload_403(self) -> None:
        from app.dataplane import account as account_module
        from app.control.model.registry import get as get_model
        from app.products.openai import images

        directory = _FakeImageDirectory()
        spec = get_model("grok-imagine-image-edit")
        self.assertIsNotNone(spec)
        cfg = _FakeConfig(
            {
                "features.image_format": "grok_url",
                "retry.on_codes": "429",
            }
        )

        async def fake_prepare_references(token: str, _image_inputs: list[str]):
            if token == "bad-token":
                raise UpstreamError("Asset upload returned 403", status=403)
            return ["https://assets.grok.com/users/user-1/asset-1/content"]

        async def fake_create_post(*_args, **_kwargs):
            return {"post": {"id": "post-1"}}

        async def fake_collect_images(**_kwargs):
            return [images._ImageOutput("ok-url", "![image](ok-url)")]

        fail_sync = AsyncMock(return_value=None)

        async def run():
            with (
                patch.object(account_module, "_directory", directory),
                patch.object(images, "selection_max_retries", return_value=1),
                patch.object(images, "get_config", return_value=cfg),
                patch.object(images, "resolve_model", return_value=spec),
                patch.object(images, "_prepare_edit_references", side_effect=fake_prepare_references),
                patch.object(images, "create_media_post", side_effect=fake_create_post),
                patch.object(images, "_collect_edit_images", side_effect=fake_collect_images),
                patch.object(images, "_quota_sync", new=AsyncMock(return_value=None)),
                patch.object(images, "_image_fail_sync", new=fail_sync),
            ):
                return await images.edit(
                    model="grok-imagine-image-edit",
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "make it brighter"},
                                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
                            ],
                        }
                    ],
                    n=1,
                    stream=False,
                )

        payload = asyncio.run(run())

        self.assertEqual(payload["data"], [{"url": "ok-url"}])
        self.assertEqual(directory.reserved, [(), ("bad-token",)])
        self.assertEqual(directory.released, ["bad-token", "good-token"])
        self.assertEqual(directory.feedback_calls[0][1], FeedbackKind.RATE_LIMITED)
        self.assertEqual(directory.feedback_calls[1][1], FeedbackKind.SUCCESS)
        fail_sync.assert_called_once()
        self.assertEqual(fail_sync.call_args.args[0], "bad-token")


class ImageChatPromptExtractionTests(unittest.TestCase):
    def test_extracts_prompt_from_openai_text_content_blocks(self) -> None:
        from app.products.openai.router import _last_user_text_prompt
        from app.products.openai.schemas import ChatCompletionRequest

        req = ChatCompletionRequest.model_validate(
            {
                "model": "grok-imagine-image",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "draw a red fox"},
                            {"type": "image_url", "image_url": {"url": "ignored"}},
                        ],
                    }
                ],
            }
        )

        self.assertEqual(_last_user_text_prompt(req.messages), "draw a red fox")

    def test_empty_image_prompt_validation_error_is_explicit(self) -> None:
        from app.products.openai.router import chat_completions_endpoint
        from app.products.openai.schemas import ChatCompletionRequest

        req = ChatCompletionRequest.model_validate(
            {
                "model": "grok-imagine-image",
                "stream": False,
                "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "ignored"}}]}],
            }
        )

        with self.assertRaises(ValidationError) as ctx:
            asyncio.run(chat_completions_endpoint(req))

        self.assertEqual(ctx.exception.message, "Image generation requires a non-empty text prompt")
        self.assertEqual(ctx.exception.param, "messages")


if __name__ == "__main__":
    unittest.main()
