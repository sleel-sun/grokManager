import pytest

from app.control.proxy.models import ProxyFeedbackKind
from app.platform.errors import UpstreamError
from app.platform.net.grpc import GrpcStatus
from app.dataplane.reverse.protocol import xai_auth


class _FakeSession:
    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeProxy:
    def __init__(self) -> None:
        self.lease = object()
        self.feedbacks = []

    async def acquire(self, **_: object):
        return self.lease

    async def feedback(self, lease, feedback) -> None:
        assert lease is self.lease
        self.feedbacks.append(feedback)


@pytest.mark.anyio
async def test_nsfw_sequence_continues_when_accept_tos_transiently_fails(monkeypatch) -> None:
    proxy = _FakeProxy()
    calls = []

    async def _accept_tos(token: str) -> GrpcStatus:
        assert token == "token-a"
        raise UpstreamError("cloudflare 520", status=520)

    async def _set_birth_date(token: str, *, session=None, lease=None) -> dict:
        calls.append(("birth", token, session is not None, lease is proxy.lease))
        return {"ok": True}

    async def _grpc_call(url: str, token: str, payload: bytes, **kwargs: object) -> GrpcStatus:
        calls.append(("grpc", token, kwargs.get("label"), kwargs.get("lease") is proxy.lease))
        return GrpcStatus(0)

    monkeypatch.setattr(xai_auth, "accept_tos", _accept_tos)
    monkeypatch.setattr(xai_auth, "set_birth_date", _set_birth_date)
    monkeypatch.setattr(xai_auth, "_grpc_call", _grpc_call)

    async def _get_proxy_runtime() -> _FakeProxy:
        return proxy

    monkeypatch.setattr(xai_auth, "get_proxy_runtime", _get_proxy_runtime)
    monkeypatch.setattr(xai_auth, "build_session_kwargs", lambda **_: {})
    monkeypatch.setattr(xai_auth, "ResettableSession", _FakeSession)

    await xai_auth.nsfw_sequence("token-a")

    assert calls == [
        ("birth", "token-a", True, True),
        ("grpc", "token-a", "enable_nsfw", True),
    ]
    assert proxy.feedbacks[-1].kind == ProxyFeedbackKind.SUCCESS


@pytest.mark.anyio
async def test_set_birth_date_retries_transient_429(monkeypatch) -> None:
    proxy = _FakeProxy()
    attempts = 0

    async def _post_json(*args: object, **kwargs: object) -> dict:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise UpstreamError("rate limited", status=429)
        return {"ok": True}

    async def _sleep(_: float) -> None:
        return None

    async def _get_proxy_runtime() -> _FakeProxy:
        return proxy

    monkeypatch.setattr(xai_auth, "get_proxy_runtime", _get_proxy_runtime)
    monkeypatch.setattr(xai_auth, "post_json", _post_json)
    monkeypatch.setattr(xai_auth.asyncio, "sleep", _sleep)
    monkeypatch.setattr(xai_auth, "build_set_birth_payload", lambda: {"birthDate": "2000-01-01T00:00:00.000Z"})

    result = await xai_auth.set_birth_date("token-b")

    assert result == {"ok": True}
    assert attempts == 2
    assert proxy.feedbacks[-1].kind == ProxyFeedbackKind.SUCCESS


@pytest.mark.anyio
async def test_grpc_call_retries_transient_grpc_status(monkeypatch) -> None:
    proxy = _FakeProxy()
    attempts = 0

    async def _get_proxy_runtime() -> _FakeProxy:
        return proxy

    async def _post_grpc_web(*args: object, **kwargs: object) -> tuple[list[bytes], dict[str, str]]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return [], {"grpc-status": "14", "grpc-message": "unavailable"}
        return [], {"grpc-status": "0"}

    async def _sleep(_: float) -> None:
        return None

    monkeypatch.setattr(xai_auth, "get_proxy_runtime", _get_proxy_runtime)
    monkeypatch.setattr(xai_auth, "post_grpc_web", _post_grpc_web)
    monkeypatch.setattr(xai_auth.asyncio, "sleep", _sleep)

    result = await xai_auth._grpc_call(
        xai_auth.NSFW_MGMT_URL,
        "token-c",
        xai_auth.build_nsfw_mgmt_payload(),
        label="enable_nsfw",
    )

    assert result.ok
    assert attempts == 2
    assert proxy.feedbacks[-1].kind == ProxyFeedbackKind.SUCCESS


@pytest.mark.anyio
async def test_nsfw_sequence_retries_grok_steps_with_fresh_lease(monkeypatch) -> None:
    calls = []

    async def _accept_tos(token: str) -> GrpcStatus:
        calls.append(("accept", token))
        return GrpcStatus(0)

    async def _run_once(token: str) -> None:
        calls.append(("grok", token))
        if len([call for call in calls if call[0] == "grok"]) == 1:
            raise UpstreamError("challenge", status=403)

    async def _sleep(_: float) -> None:
        return None

    monkeypatch.setattr(xai_auth, "accept_tos", _accept_tos)
    monkeypatch.setattr(xai_auth, "_nsfw_grok_sequence_once", _run_once)
    monkeypatch.setattr(xai_auth.asyncio, "sleep", _sleep)

    await xai_auth.nsfw_sequence("token-d")

    assert calls == [
        ("accept", "token-d"),
        ("grok", "token-d"),
        ("grok", "token-d"),
    ]
