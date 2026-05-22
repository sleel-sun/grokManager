import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from app.platform.errors import UpstreamError


class UploadFieldCompatibilityTests(unittest.TestCase):
    def test_coalesce_uploads_accepts_standard_and_bracketed_fields(self) -> None:
        from app.products.openai.router import _coalesce_uploads

        standard = object()
        bracketed = object()

        self.assertEqual(_coalesce_uploads([standard], [bracketed]), [standard, bracketed])
        self.assertEqual(_coalesce_uploads(None, [bracketed]), [bracketed])
        self.assertEqual(_coalesce_uploads([standard], None), [standard])
        self.assertEqual(_coalesce_uploads(None, None), [])


class _FakeAccount:
    token = "token"


class _FakeDirectory:
    async def reserve(self, **_kwargs):
        return _FakeAccount()

    async def release(self, _account) -> None:
        return None

    async def feedback(self, *_args, **_kwargs) -> None:
        return None


class VideoJobCompatibilityTests(unittest.TestCase):
    def test_completed_video_job_payload_exposes_video_urls(self) -> None:
        from app.products.openai.video import _VideoJob

        job = _VideoJob(
            id="video_test",
            model="grok-imagine-video",
            prompt="prompt",
            seconds="6",
            size="720x1280",
            quality="standard",
            created_at=0,
            status="completed",
            progress=100,
            completed_at=1,
            video_url="https://assets.grok.com/users/user-1/video.mp4",
            content_path="/tmp/video_test.mp4",
        )

        payload = job.to_dict()

        self.assertEqual(payload["video_url"], job.video_url)
        self.assertEqual(payload["url"], job.video_url)
        self.assertEqual(payload["content_url"], "/v1/videos/video_test/content")

    def test_video_job_completes_with_upstream_url_when_local_download_fails(self) -> None:
        from app.products.openai import video

        job = video._VideoJob(
            id="video_test",
            model="grok-imagine-video",
            prompt="prompt",
            seconds="6",
            size="720x1280",
            quality="standard",
            created_at=int(time.time()),
        )
        artifact = video._VideoArtifact(
            video_url="https://assets.grok.com/users/user-1/video.mp4",
            video_post_id="post-1",
            asset_id="asset-1",
            thumbnail_url="",
        )

        with patch("app.dataplane.account._directory", _FakeDirectory()), patch(
            "app.products.openai.video._generate_video_with_token",
            new=AsyncMock(return_value=artifact),
        ), patch(
            "app.products.openai.video._download_video_bytes",
            new=AsyncMock(side_effect=UpstreamError("download returned 403", status=403)),
        ), patch(
            "app.products.openai.video._quota_sync",
            new=AsyncMock(return_value=None),
        ), patch(
            "app.products.openai.video._fail_sync",
            new=AsyncMock(return_value=None),
        ):
            asyncio.run(
                video._run_video_job(
                    job,
                    size="720x1280",
                    resolution_name=None,
                    prompt="prompt",
                    seconds=6,
                    preset=None,
                )
            )

        self.assertEqual(job.status, "completed")
        self.assertEqual(job.progress, 100)
        self.assertEqual(job.video_url, artifact.video_url)
        self.assertEqual(job.content_path, "")
        self.assertIsNone(job.error)


if __name__ == "__main__":
    unittest.main()
