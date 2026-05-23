import pytest

from app.control.account.enums import AccountStatus, QuotaSource
from app.control.account.models import (
    AccountQuotaSet,
    AccountRecord,
    QuotaWindow,
    RuntimeSnapshot,
)
from app.dataplane.account import selector
from app.dataplane.account.sync import bootstrap


class _Repo:
    def __init__(self, records: list[AccountRecord]) -> None:
        self._records = records

    async def runtime_snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(revision=1, items=self._records)


def _window(remaining: int, source: QuotaSource) -> QuotaWindow:
    synced_at = 1_700_000_000_000 if source != QuotaSource.DEFAULT else None
    return QuotaWindow(
        remaining=remaining,
        total=max(remaining, 1),
        window_seconds=7200,
        reset_at=None,
        synced_at=synced_at,
        source=source,
    )


def _record(token: str, source: QuotaSource, remaining: int) -> AccountRecord:
    qs = AccountQuotaSet(
        auto=_window(remaining, source),
        fast=_window(remaining, source),
        expert=_window(remaining, source),
    )
    return AccountRecord(
        token=token,
        pool="basic",
        status=AccountStatus.ACTIVE,
        quota=qs.to_dict(),
        last_sync_at=1_700_000_000_000 if source != QuotaSource.DEFAULT else None,
    )


@pytest.fixture(autouse=True)
def _restore_strategy():
    previous = selector.current_strategy()
    selector.set_strategy("quota")
    try:
        yield
    finally:
        selector.set_strategy(previous)


@pytest.mark.anyio
async def test_quota_selector_prefers_real_quota_over_larger_default_quota() -> None:
    table = await bootstrap(
        _Repo(
            [
                _record("default-token", QuotaSource.DEFAULT, 150),
                _record("real-token", QuotaSource.REAL, 1),
            ]
        )
    )

    idx = selector.select(table, 0, 0, now_s=1)

    assert idx is not None
    assert table.get_token(idx) == "real-token"

    assert selector.select(table, 0, 0, exclude_idxs=frozenset({idx}), now_s=1) is None


@pytest.mark.anyio
async def test_quota_select_any_prefers_real_quota_for_media_paths() -> None:
    table = await bootstrap(
        _Repo(
            [
                _record("default-token", QuotaSource.DEFAULT, 150),
                _record("real-token", QuotaSource.REAL, 1),
            ]
        )
    )

    idx = selector.select_any(table, 0, now_s=1)

    assert idx is not None
    assert table.get_token(idx) == "real-token"

    assert selector.select_any(table, 0, exclude_idxs=frozenset({idx}), now_s=1) is None
