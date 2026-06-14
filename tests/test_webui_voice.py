import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.control.account.enums import FeedbackKind
from app.platform.errors import UpstreamError
from app.products.web.webui.voice import VoiceTokenRequest, _normalize_livekit_token_response


class VoiceTokenNormalizationTests(unittest.TestCase):
    def test_accepts_livekit_access_token_aliases(self) -> None:
        response = _normalize_livekit_token_response(
            {
                "accessToken": "lk-token",
                "serverUrl": "wss://voice.example.test",
                "participantIdentity": "participant-1",
                "room": "room-1",
            }
        )

        self.assertEqual(response.token, "lk-token")
        self.assertEqual(response.url, "wss://voice.example.test")
        self.assertEqual(response.participant_name, "participant-1")
        self.assertEqual(response.room_name, "room-1")


class _FakeLease:
    def __init__(self, token: str) -> None:
        self.token = token


class _FakeDirectory:
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

    async def release(self, lease) -> None:
        self.released.append(lease.token)

    async def feedback(self, token, kind, mode_id, **_kwargs) -> None:
        self.feedback_calls.append((token, kind, mode_id))


class VoiceTokenEndpointTests(unittest.TestCase):
    def test_retries_with_next_account_after_livekit_upstream_error(self) -> None:
        from app.dataplane import account as account_module
        from app.products.web.webui import voice as voice_module

        directory = _FakeDirectory()

        async def fake_fetch(token, **_kwargs):
            if token == "bad-token":
                raise UpstreamError("LiveKit temporary failure", status=503)
            return {"token": "lk-good-token", "livekitUrl": "wss://livekit.grok.com"}

        async def run():
            with (
                patch.object(account_module, "_directory", directory),
                patch(
                    "app.dataplane.reverse.transport.livekit.fetch_livekit_token",
                    new=AsyncMock(side_effect=fake_fetch),
                ),
                patch.object(voice_module, "selection_max_retries", return_value=1),
            ):
                return await voice_module.voice_token(VoiceTokenRequest())

        response = asyncio.run(run())

        self.assertEqual(response.token, "lk-good-token")
        self.assertEqual(directory.reserved, [(), ("bad-token",)])
        self.assertEqual(directory.released, ["bad-token", "good-token"])
        self.assertEqual(directory.feedback_calls[0][0], "bad-token")
        self.assertEqual(directory.feedback_calls[0][1], FeedbackKind.SERVER_ERROR)
        self.assertEqual(directory.feedback_calls[1][0], "good-token")
        self.assertEqual(directory.feedback_calls[1][1], FeedbackKind.SUCCESS)


if __name__ == "__main__":
    unittest.main()
