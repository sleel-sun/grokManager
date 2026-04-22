"""Legacy token compatibility endpoints used by the merged maintainer tool."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.control.account.commands import AccountUpsert, BulkReplacePoolCommand, ListAccountsQuery
from app.platform.auth.middleware import verify_admin_key
from app.platform.logging.logger import logger
from .admin import get_refresh_svc, get_repo
from .admin.tokens import _json, _sanitize


router = APIRouter(tags=["Admin - Tokens"], dependencies=[Depends(verify_admin_key)])


class LegacyTokenItem(BaseModel):
    token: str
    tags: list[str] = Field(default_factory=list)


class LegacyTokensRequest(BaseModel):
    ssoBasic: list[str | LegacyTokenItem] = Field(default_factory=list)


async def _refresh_imported(refresh_svc, tokens: list[str]) -> None:
    try:
        await refresh_svc.refresh_on_import(tokens)
        logger.info("legacy admin import quota sync completed: token_count={}", len(tokens))
    except Exception as exc:
        logger.warning(
            "legacy admin import quota sync failed: token_count={} error={}",
            len(tokens),
            exc,
        )


@router.get("/v1/admin/tokens")
async def list_legacy_tokens(repo=Depends(get_repo)):
    items: list = []
    page_num = 1
    while True:
        page = await repo.list_accounts(
            ListAccountsQuery(page=page_num, page_size=2000, pool="basic")
        )
        items.extend(page.items)
        if page_num * 2000 >= page.total:
            break
        page_num += 1

    payload = [
        {"token": record.token, "tags": record.tags or []}
        for record in items
        if not record.is_deleted()
    ]
    return _json({"ssoBasic": payload})


@router.post("/v1/admin/tokens")
async def replace_legacy_tokens(
    req: LegacyTokensRequest,
    repo=Depends(get_repo),
    refresh_svc=Depends(get_refresh_svc),
):
    upserts: list[AccountUpsert] = []
    seen: set[str] = set()
    for item in req.ssoBasic:
        data = {"token": item} if isinstance(item, str) else item.model_dump()
        token = _sanitize(data.get("token", ""))
        if not token or token in seen:
            continue
        seen.add(token)
        upserts.append(
            AccountUpsert(
                token=token,
                pool="basic",
                tags=data.get("tags") or [],
            )
        )

    await repo.replace_pool(BulkReplacePoolCommand(pool="basic", upserts=upserts))
    logger.info("legacy admin pool replaced: pool=basic token_count={}", len(upserts))
    if upserts:
        asyncio.create_task(_refresh_imported(refresh_svc, [item.token for item in upserts]))
    return _json({"status": "success", "count": len(upserts)})
