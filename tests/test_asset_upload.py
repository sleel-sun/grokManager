import asyncio
import base64
from unittest.mock import AsyncMock, patch

import orjson

from app.dataplane.reverse.transport import asset_upload
from app.products.openai import chat


class _FakeResponse:
    def __init__(self, status_code: int, data: dict | bytes = b"", headers: dict | None = None):
        self.status_code = status_code
        self.content = orjson.dumps(data) if isinstance(data, dict) else data
        self.headers = headers or {}


class _FakeProxy:
    def __init__(self) -> None:
        self.feedbacks = []

    async def acquire(self):
        return None

    async def feedback(self, lease, feedback) -> None:
        self.feedbacks.append((lease, feedback))


class _FakeUploadV2Session:
    requests: list[tuple] = []

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def post(self, url, *, headers=None, data=None, timeout=None):
        payload = orjson.loads(data) if data else {}
        self.requests.append(("POST", url, payload, headers, timeout))
        if url.endswith("/init"):
            return _FakeResponse(
                200,
                {
                    "uploadId": "upload-1",
                    "assetId": "asset-1",
                    "uploadMethod": "UPLOAD_METHOD_SINGLE_PUT",
                    "singlePut": {
                        "url": "https://upload.example.test/put",
                        "requiredHeaders": {"x-upload": "yes"},
                    },
                },
            )
        if url.endswith("/complete"):
            assert payload == {
                "presigned": {"uploadId": "upload-1", "completedParts": []}
            }
            return _FakeResponse(
                200,
                {
                    "fileMetadata": {
                        "fileMetadataId": "file-1",
                        "fileUri": "asset://file-1",
                    }
                },
            )
        raise AssertionError(f"unexpected POST {url}")

    async def put(self, url, *, headers=None, data=None, timeout=None):
        self.requests.append(("PUT", url, data, headers, timeout))
        assert url == "https://upload.example.test/put"
        assert data == b"abc"
        assert headers == {"x-upload": "yes"}
        return _FakeResponse(200, b"", headers={"ETag": '"etag-1"'})

    async def get(self, *_args, **_kwargs):
        raise AssertionError("single-put inline completion should not poll status")


def test_upload_file_prefers_v2_for_document() -> None:
    b64 = base64.b64encode(b"document").decode()

    with patch.object(
        asset_upload,
        "_upload_file_v2_inner",
        new=AsyncMock(return_value=("file-1", "uri-1")),
    ) as upload_v2, patch.object(
        asset_upload,
        "_upload_file_inner",
        new=AsyncMock(side_effect=AssertionError("legacy should not be used")),
    ):
        result = asyncio.run(
            asset_upload.upload_file("token", "report.pdf", "application/pdf", b64)
        )

    assert result == ("file-1", "uri-1")
    upload_v2.assert_awaited_once_with(
        "token", "report.pdf", "application/pdf", b"document"
    )


def test_upload_v2_single_put_flow() -> None:
    proxy = _FakeProxy()
    _FakeUploadV2Session.requests = []

    async def fake_proxy():
        return proxy

    with patch.object(
        asset_upload, "get_proxy_runtime", side_effect=fake_proxy
    ), patch.object(asset_upload, "ResettableSession", _FakeUploadV2Session):
        result = asyncio.run(
            asset_upload._upload_file_v2_inner(
                "token",
                "report.pdf",
                "application/pdf",
                b"abc",
            )
        )

    assert result == ("file-1", "asset://file-1")
    assert [request[0] for request in _FakeUploadV2Session.requests] == [
        "POST",
        "PUT",
        "POST",
    ]
    assert proxy.feedbacks


def test_parse_data_uri_uses_supplied_filename_for_mime() -> None:
    filename, b64, mime = asset_upload.parse_data_uri(
        "data:application/octet-stream;base64,AA==",
        filename="report.pdf",
    )

    assert filename == "report.pdf"
    assert b64 == "AA=="
    assert mime == "application/pdf"


def test_prepare_file_attachments_passes_filename() -> None:
    captured = {}

    async def fake_upload_from_input(token, file_input, *, filename=None, mime=None):
        captured.update(
            {
                "token": token,
                "file_input": file_input,
                "filename": filename,
                "mime": mime,
            }
        )
        return "file-1", "uri-1"

    with patch.object(chat, "upload_from_input", side_effect=fake_upload_from_input):
        result = asyncio.run(
            chat._prepare_file_attachments(
                "token",
                [{"data": "data:application/pdf;base64,AA==", "filename": "report.pdf"}],
            )
        )

    assert result == ["file-1"]
    assert captured == {
        "token": "token",
        "file_input": "data:application/pdf;base64,AA==",
        "filename": "report.pdf",
        "mime": None,
    }
