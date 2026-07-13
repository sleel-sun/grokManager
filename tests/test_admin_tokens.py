import asyncio

import orjson

from app.control.account.backends.local import LocalAccountRepository
from app.control.account.commands import AccountUpsert, ListAccountsQuery
from app.products.web.admin.tokens import (
    _is_external_gpt_import,
    _replace_grok_pool,
    list_tokens,
)


def test_replace_grok_pool_preserves_gpt_records(tmp_path):
    async def run():
        repo = LocalAccountRepository(tmp_path / "accounts.db")
        await repo.initialize()
        await repo.upsert_accounts(
            [
                AccountUpsert(token="old-grok-token", pool="basic"),
                AccountUpsert(
                    token="gpt_existing_record",
                    pool="basic",
                    tags=["gpt"],
                    ext={"gpt": True, "gpt_access_token": "chatgpt-access-token"},
                ),
            ]
        )

        await _replace_grok_pool(
            repo,
            "basic",
            [AccountUpsert(token="new-grok-token", pool="basic")],
        )

        page = await repo.list_accounts(ListAccountsQuery(pool="basic", page_size=20))
        active_tokens = {record.token for record in page.items}
        deleted_old = (await repo.get_accounts(["old-grok-token"]))[0]
        gpt_record = (await repo.get_accounts(["gpt_existing_record"]))[0]

        assert active_tokens == {"new-grok-token", "gpt_existing_record"}
        assert deleted_old.is_deleted()
        assert not gpt_record.is_deleted()

        await repo.close()

    asyncio.run(run())


def test_list_tokens_excludes_gpt_records(tmp_path):
    async def run():
        repo = LocalAccountRepository(tmp_path / "accounts.db")
        await repo.initialize()
        await repo.upsert_accounts(
            [
                AccountUpsert(token="grok-token", pool="basic"),
                AccountUpsert(
                    token="gpt_existing_record",
                    pool="basic",
                    tags=["gpt"],
                    ext={"gpt": True, "gpt_access_token": "chatgpt-access-token"},
                ),
            ]
        )

        response = await list_tokens(repo=repo)
        body = orjson.loads(response.body)
        tokens = [item["token"] for item in body["tokens"]]

        assert tokens == ["grok-token"]
        assert _is_external_gpt_import("gpt_existing_record")
        assert _is_external_gpt_import("ordinary-token", ["gpt"])

        await repo.close()

    asyncio.run(run())
