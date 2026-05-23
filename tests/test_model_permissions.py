import pytest

from app.control.account.enums import AccountStatus
from app.control.account.models import AccountRecord, RuntimeSnapshot
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
