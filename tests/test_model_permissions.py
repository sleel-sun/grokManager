import pytest

from app.control.account.enums import AccountStatus
from app.control.account.models import AccountRecord, RuntimeSnapshot
from app.control.model.registry import get as get_model
from app.platform.errors import UpstreamError
from app.products.web.admin import model_permissions


class _Repo:
    def __init__(self, records: list[AccountRecord]) -> None:
        self._records = records

    async def runtime_snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(revision=1, items=self._records)


def _acct(token: str, pool: str = "basic") -> AccountRecord:
    return AccountRecord(token=token, pool=pool, status=AccountStatus.ACTIVE)


async def _ok_probe(*args, **kwargs) -> model_permissions.ProbeOutcome:
    return model_permissions.ProbeOutcome(status="supported", message="ok")


def test_default_model_permission_scope_includes_image_generation_models() -> None:
    specs = model_permissions._normalize_models([])
    names = {spec.model_name for spec in specs}

    assert "grok-imagine-image-lite" in names
    assert "grok-imagine-image" in names
    assert "grok-imagine-image-pro" in names
    assert "grok-imagine-image-edit" not in names
    assert "grok-imagine-video" not in names


@pytest.mark.anyio
async def test_model_permission_reports_no_accounts_without_probe() -> None:
    calls = 0

    async def _probe(*args, **kwargs) -> model_permissions.ProbeOutcome:
        nonlocal calls
        calls += 1
        return model_permissions.ProbeOutcome(status="supported")

    result = await model_permissions.detect_model_permissions(
        _Repo([]),
        models=["grok-4.3"],
        pools=["basic"],
        probe_func=_probe,
    )

    item = result["results"][0]
    assert calls == 0
    assert item["status"] == "no_accounts"
    assert item["accounts_checked"] == 0


@pytest.mark.anyio
async def test_model_permission_reports_pool_not_routed_without_probe() -> None:
    calls = 0

    async def _probe(*args, **kwargs) -> model_permissions.ProbeOutcome:
        nonlocal calls
        calls += 1
        return model_permissions.ProbeOutcome(status="supported")

    result = await model_permissions.detect_model_permissions(
        _Repo([_acct("tok-basic-000000000000", "basic")]),
        models=["grok-4.20-heavy"],
        pools=["basic"],
        probe_func=_probe,
    )

    item = result["results"][0]
    assert calls == 0
    assert item["status"] == "pool_not_routed"
    assert item["required_pools"] == ["heavy"]


@pytest.mark.anyio
async def test_model_permission_reports_supported_on_first_success() -> None:
    result = await model_permissions.detect_model_permissions(
        _Repo([_acct("tok-basic-000000000000", "basic")]),
        models=["grok-4.3"],
        pools=["basic"],
        probe_func=_ok_probe,
    )

    item = result["results"][0]
    assert item["status"] == "supported"
    assert item["accounts_checked"] == 1
    assert item["status_code"] == 200


@pytest.mark.anyio
async def test_model_permission_probes_explicit_image_models() -> None:
    result = await model_permissions.detect_model_permissions(
        _Repo([_acct("tok-basic-000000000000", "basic")]),
        models=["grok-imagine-image-lite"],
        pools=["basic"],
        probe_func=_ok_probe,
    )

    item = result["results"][0]
    assert item["capability"] == "image"
    assert item["status"] == "supported"
    assert item["accounts_checked"] == 1


@pytest.mark.anyio
async def test_lite_image_probe_accepts_adapter_collected_image_urls(monkeypatch) -> None:
    spec = get_model("grok-imagine-image-lite")
    assert spec is not None

    async def fake_stream_lite(*args, **kwargs):
        yield 'data: {"ok":true}'
        yield "data: [DONE]"

    class FakeStreamAdapter:
        def __init__(self) -> None:
            self.text_buf = []
            self.image_urls = []

        def feed(self, data: str):
            self.text_buf.append("![image](https://imgen.x.ai/generated/image-content?token=abc)")
            self.image_urls.append(("https://imgen.x.ai/generated/image-content?token=abc", "ig_123"))
            return []

        def extract_generated_images_from_text(self, text: str) -> str:
            return text

    monkeypatch.setattr(
        "app.products.openai.images._stream_lite_generate",
        fake_stream_lite,
    )
    monkeypatch.setattr(
        "app.dataplane.reverse.protocol.xai_chat.StreamAdapter",
        FakeStreamAdapter,
    )

    outcome = await model_permissions._probe_image_model("tok", spec, 1)

    assert outcome.status == "supported"
    assert outcome.status_code == 200


@pytest.mark.anyio
async def test_model_permission_aggregates_account_entitlement_failures() -> None:
    async def _probe(*args, **kwargs) -> model_permissions.ProbeOutcome:
        raise UpstreamError("forbidden", status=403, body="")

    result = await model_permissions.detect_model_permissions(
        _Repo([
            _acct("tok-basic-000000000000", "basic"),
            _acct("tok-basic-111111111111", "basic"),
        ]),
        models=["grok-4.20-0309"],
        pools=["basic"],
        sample_size=2,
        probe_func=_probe,
    )

    item = result["results"][0]
    assert item["status"] == "no_quota_or_entitlement"
    assert item["accounts_checked"] == 2
    assert item["status_code"] == 403


@pytest.mark.anyio
async def test_model_permission_reports_console_model_not_exposed() -> None:
    async def _probe(*args, **kwargs) -> model_permissions.ProbeOutcome:
        raise UpstreamError("not found", status=404, body="")

    result = await model_permissions.detect_model_permissions(
        _Repo([_acct("tok-basic-000000000000", "basic")]),
        models=["grok-4.3-high"],
        pools=["basic"],
        probe_func=_probe,
    )

    item = result["results"][0]
    assert item["status"] == "upstream_model_not_found"
    assert item["status_code"] == 404
