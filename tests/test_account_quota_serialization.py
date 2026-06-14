import json

from app.control.account.backends.redis import RedisAccountRepository
from app.control.account.enums import AccountStatus, QuotaSource
from app.control.account.models import AccountQuotaSet, AccountRecord, QuotaWindow
from app.products.web.admin.tokens import _quota_brief


def _window(remaining: int) -> QuotaWindow:
    return QuotaWindow(
        remaining=remaining,
        total=150,
        window_seconds=7200,
        reset_at=1_700_000_007_200,
        synced_at=1_700_000_000_000,
        source=QuotaSource.REAL,
    )


def test_admin_quota_brief_includes_grok_4_3_and_source_metadata() -> None:
    brief = _quota_brief({"grok_4_3": _window(42).to_dict()})

    assert brief["grok_4_3"] == {
        "remaining": 42,
        "total": 150,
        "window_seconds": 7200,
        "reset_at": 1_700_000_007_200,
        "synced_at": 1_700_000_000_000,
        "source": int(QuotaSource.REAL),
    }


def test_admin_quota_brief_includes_console_quota() -> None:
    brief = _quota_brief({"console": _window(17).to_dict()})

    assert brief["console"] == {
        "remaining": 17,
        "total": 150,
        "window_seconds": 7200,
        "reset_at": 1_700_000_007_200,
        "synced_at": 1_700_000_000_000,
        "source": int(QuotaSource.REAL),
    }


def test_redis_account_hash_round_trips_grok_4_3_and_console_quota() -> None:
    qs = AccountQuotaSet(
        auto=_window(1),
        fast=_window(2),
        expert=_window(3),
        heavy=_window(4),
        grok_4_3=_window(5),
        console=_window(6),
    )
    record = AccountRecord(
        token="sso-test-token",
        pool="heavy",
        status=AccountStatus.ACTIVE,
        quota=qs.to_dict(),
    )

    hashed = RedisAccountRepository._to_hash(record, revision=11)
    assert json.loads(hashed["quota_grok_4_3"])["remaining"] == 5
    assert json.loads(hashed["quota_console"])["remaining"] == 6

    restored = RedisAccountRepository._from_hash(record.token, hashed)
    assert restored.quota_set().grok_4_3 is not None
    assert restored.quota_set().grok_4_3.remaining == 5
    assert restored.quota_set().console is not None
    assert restored.quota_set().console.remaining == 6
