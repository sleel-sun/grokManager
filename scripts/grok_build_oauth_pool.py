#!/usr/bin/env python3
"""Batch-authorize existing Grok SSO accounts for the Grok Build pool."""

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.control.account.commands import ListAccountsQuery
from app.control.account.backends.factory import create_repository
from app.maintainer.grok_build_oauth import authorize_sso_account
from app.platform.config.snapshot import config


def _source_id(token: str) -> str:
    return "sso:" + hashlib.sha256(token.encode()).hexdigest()[:24]


async def _accounts(limit: int, existing: set[str]):
    await config.ensure_loaded()
    repository = create_repository()
    await repository.initialize()
    try:
        page = await repository.list_accounts(ListAccountsQuery(page=1, page_size=2000))
        pending = [
            record
            for record in page.items
            if record.status.value == "active" and _source_id(record.token) not in existing
        ]
        return pending[:limit]
    finally:
        await repository.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--delay", type=float, default=10.0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    import time

    pool_path = Path("data/grok_auth.json")
    try:
        pool = json.loads(pool_path.read_text(encoding="utf-8"))
        existing = set(pool) if isinstance(pool, dict) and not args.force else set()
    except (OSError, json.JSONDecodeError):
        existing = set()
    accounts = asyncio.run(_accounts(max(1, min(args.limit, 2000)), existing))
    if not accounts:
        print("completed: no pending active accounts")
        return 0
    succeeded = 0
    for index, account in enumerate(accounts):
        source_id = _source_id(account.token)
        try:
            result = authorize_sso_account(account.token, source_id)
            print(f"authorized {result['source_id']}")
            succeeded += 1
        except Exception as exc:
            print(f"failed {source_id}: {exc}")
        if index + 1 < len(accounts):
            time.sleep(max(0.0, args.delay))
    print(f"completed: {succeeded}/{len(accounts)}")
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
