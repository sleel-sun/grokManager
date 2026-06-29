import asyncio

from app.control.account.backends.local import LocalAccountRepository
from app.control.account.commands import AccountPatch, AccountUpsert
from app.control.account.enums import AccountStatus
from app.control.account.refresh import AccountRefreshService
from app.control.account.quota_defaults import CONSOLE_LIMIT, CONSOLE_WINDOW_SECONDS
from app.control.model.enums import ModeId
from app.platform.errors import UpstreamError
from app.platform.runtime.clock import now_ms


def test_console_429_uses_independent_sliding_counter(tmp_path):
    async def run():
        repo = LocalAccountRepository(tmp_path / "accounts.db")
        await repo.initialize()
        await repo.upsert_accounts([AccountUpsert(token="tok")])
        svc = AccountRefreshService(repo)
        exc = UpstreamError("rate limited", status=429)

        for _ in range(3):
            await svc.record_failure_async("tok", int(ModeId.CONSOLE), exc)

        record = (await repo.get_accounts(["tok"]))[0]
        assert record.status == AccountStatus.EXPIRED
        assert record.state_reason == "console_429_threshold_exceeded"
        assert record.ext["console_429_count"] == 3
        assert record.ext["expired_reason"] == "console_429_threshold_exceeded"

        await repo.close()

    asyncio.run(run())


def test_clear_failures_removes_console_429_counter(tmp_path):
    async def run():
        repo = LocalAccountRepository(tmp_path / "accounts.db")
        await repo.initialize()
        await repo.upsert_accounts([AccountUpsert(token="tok")])
        await repo.patch_accounts([
            AccountPatch(
                token="tok",
                status=AccountStatus.EXPIRED,
                state_reason="console_429_threshold_exceeded",
                ext_merge={
                    "expired_at": now_ms(),
                    "expired_reason": "console_429_threshold_exceeded",
                    "console_429_count": 3,
                    "console_429_last_at": now_ms(),
                },
            )
        ])

        await repo.patch_accounts([AccountPatch(token="tok", clear_failures=True)])

        record = (await repo.get_accounts(["tok"]))[0]
        assert record.status == AccountStatus.ACTIVE
        assert "console_429_count" not in record.ext
        assert "console_429_last_at" not in record.ext

        await repo.close()

    asyncio.run(run())


def test_clear_last_failure_preserves_account_status(tmp_path):
    async def run():
        repo = LocalAccountRepository(tmp_path / "accounts.db")
        await repo.initialize()
        await repo.upsert_accounts([AccountUpsert(token="tok")])
        await repo.patch_accounts([
            AccountPatch(
                token="tok",
                status=AccountStatus.DISABLED,
                state_reason="gpt_record",
                last_fail_at=now_ms(),
                last_fail_reason="timeout",
            )
        ])

        await repo.patch_accounts([AccountPatch(token="tok", clear_last_failure=True)])

        record = (await repo.get_accounts(["tok"]))[0]
        assert record.status == AccountStatus.DISABLED
        assert record.state_reason == "gpt_record"
        assert record.last_fail_at is None
        assert record.last_fail_reason is None

        await repo.close()

    asyncio.run(run())


def test_recover_console_expired_accounts_restores_healthy_history(tmp_path):
    async def run():
        repo = LocalAccountRepository(tmp_path / "accounts.db")
        await repo.initialize()
        await repo.upsert_accounts([AccountUpsert(token="tok")])
        await repo.patch_accounts([
            AccountPatch(
                token="tok",
                status=AccountStatus.EXPIRED,
                state_reason="console_429_threshold_exceeded",
                usage_use_delta=6,
                ext_merge={
                    "expired_at": now_ms() - 3700 * 1000,
                    "expired_reason": "console_429_threshold_exceeded",
                    "console_429_count": 3,
                    "console_429_last_at": now_ms() - 3700 * 1000,
                },
            )
        ])

        recovered = await repo.recover_console_expired_accounts()

        record = (await repo.get_accounts(["tok"]))[0]
        assert recovered == 1
        assert record.status == AccountStatus.ACTIVE
        assert record.state_reason is None
        assert "expired_at" not in record.ext
        assert "console_429_count" not in record.ext

        await repo.close()

    asyncio.run(run())


def test_reset_expired_console_windows_restores_default_quota(tmp_path):
    async def run():
        repo = LocalAccountRepository(tmp_path / "accounts.db")
        await repo.initialize()
        await repo.upsert_accounts([AccountUpsert(token="tok")])
        await repo.patch_accounts([
            AccountPatch(
                token="tok",
                quota_console={
                    "remaining": 0,
                    "total": CONSOLE_LIMIT,
                    "window_seconds": CONSOLE_WINDOW_SECONDS,
                    "reset_at": now_ms() - 1000,
                    "synced_at": now_ms() - 2000,
                    "source": 0,
                },
            )
        ])

        reset = await repo.reset_expired_console_windows()

        record = (await repo.get_accounts(["tok"]))[0]
        quota = record.quota_set().console
        assert reset == 1
        assert quota is not None
        assert quota.remaining == CONSOLE_LIMIT
        assert quota.reset_at is None

        await repo.close()

    asyncio.run(run())
