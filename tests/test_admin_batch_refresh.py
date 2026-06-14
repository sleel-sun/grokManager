import orjson
import pytest

from app.control.account.enums import AccountStatus
from app.control.account.models import AccountRecord
from app.products.web.admin.batch import _concurrency, _dispatch_sync


class _Repo:
    async def get_accounts(self, tokens: list[str]) -> list[AccountRecord]:
        records: list[AccountRecord] = []
        for token in tokens:
            status = AccountStatus.EXPIRED if token == "expired-token" else AccountStatus.ACTIVE
            records.append(AccountRecord(token=token, status=status))
        return records


@pytest.mark.anyio
async def test_dispatch_sync_reports_expired_and_transient_failures() -> None:
    async def _handler(token: str) -> dict:
        if token == "ok-token":
            return {"refreshed": 1}
        raise RuntimeError("refresh failed")

    response = await _dispatch_sync(
        ["ok-token", "expired-token", "transient-token"],
        _handler,
        concurrency=10,
        repo=_Repo(),
    )

    body = orjson.loads(response.body)
    assert body["summary"] == {
        "total": 3,
        "ok": 1,
        "fail": 2,
        "expired": 1,
        "transient": 1,
    }


def test_batch_concurrency_is_capped_at_80() -> None:
    assert _concurrency(999, "batch.refresh_concurrency") == 80
