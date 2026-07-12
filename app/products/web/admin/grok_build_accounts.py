"""Admin API for the Grok Build OAuth credential pool."""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orjson
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.control.account.commands import ListAccountsQuery
from app.control.account.enums import AccountStatus
from app.maintainer.grok_build_oauth import (
    authorize_sso_account,
    delete_pool_entry,
    delete_pool_entries,
    parse_pool_expiry,
    pool_entries,
    refresh_pool_credential,
    source_id_for_sso,
)
from app.maintainer.grok_build_usage import delete_usage, load_usage
from app.platform.errors import AppError, ErrorKind, ValidationError
from app.platform.config.snapshot import get_config
from app.platform.logging.logger import logger

from . import get_repo

if TYPE_CHECKING:
    from app.control.account.models import AccountRecord
    from app.control.account.repository import AccountRepository


router = APIRouter(prefix="/grok-build", tags=["Admin - Grok Build OAuth"])
_JOB_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_BACKGROUND_TASKS: set[asyncio.Task] = set()
_EXTERNAL_GPT_TAGS = {"gpt", "gpt-image"}
_EXTERNAL_GPT_PREFIXES = ("gpt_", "gptcred_", "gptimg_", "gptimgcred_")


class ConvertRequest(BaseModel):
    limit: int = Field(default=10, ge=0)
    force: bool = False


class SourceIdsRequest(BaseModel):
    source_ids: list[str] = Field(min_length=1)


def _json(data: Any, status_code: int = 200) -> Response:
    return Response(
        content=orjson.dumps(data),
        media_type="application/json",
        status_code=status_code,
    )


def _safe_entries() -> dict[str, dict[str, Any]]:
    try:
        return pool_entries()
    except (OSError, ValueError) as exc:
        raise AppError(
            "Grok Build OAuth pool is unavailable",
            kind=ErrorKind.SERVER,
            code="grok_build_pool_unavailable",
            status=503,
        ) from exc


def _serialize_entry(
    source_id: str,
    entry: dict[str, Any],
    now: float,
    stored_usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expires_at = entry.get("expires_at")
    expiry = parse_pool_expiry(expires_at)
    expired = bool(expiry and expiry <= now)
    legacy_usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
    stored_usage = stored_usage or {}
    legacy_quota = entry.get("quota") if isinstance(entry.get("quota"), dict) else {}

    def safe_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    usage = {
        key: safe_int(legacy_usage.get(key)) + safe_int(stored_usage.get(key))
        for key in (
            "request_count",
            "success_count",
            "failure_count",
            "input_tokens",
            "output_tokens",
            "total_tokens",
        )
    }
    usage["last_status"] = stored_usage.get("last_status")
    if usage["last_status"] is None:
        usage["last_status"] = legacy_usage.get("last_status")
    usage["last_used_at"] = stored_usage.get("last_used_at")
    if usage["last_used_at"] is None:
        usage["last_used_at"] = legacy_usage.get("last_used_at")

    def quota_value(stored_key: str, legacy_key: str) -> Any:
        value = stored_usage.get(stored_key)
        return legacy_quota.get(legacy_key) if value is None else value

    return {
        "source_id": source_id,
        "source": str(entry.get("source") or ""),
        "email": str(entry.get("email") or ""),
        "updated_at": entry.get("updated_at"),
        "expires_at": expires_at,
        "expires_at_epoch": expiry or None,
        "expired": expired,
        "expiring_soon": bool(expiry and not expired and expiry <= now + 900),
        "has_credential": bool(entry.get("key") or entry.get("access_token")),
        "has_access_token": bool(
            str(entry.get("key") or entry.get("access_token") or "").strip()
        ),
        "has_refresh_token": bool(str(entry.get("refresh_token") or "").strip()),
        "has_id_token": bool(str(entry.get("id_token") or "").strip()),
        "oidc_issuer": str(entry.get("oidc_issuer") or ""),
        "oidc_client_id": str(entry.get("oidc_client_id") or ""),
        "usage": {
            "request_count": safe_int(usage.get("request_count")),
            "success_count": safe_int(usage.get("success_count")),
            "failure_count": safe_int(usage.get("failure_count")),
            "input_tokens": safe_int(usage.get("input_tokens")),
            "output_tokens": safe_int(usage.get("output_tokens")),
            "total_tokens": safe_int(usage.get("total_tokens")),
            "last_status": usage.get("last_status"),
            "last_used_at": usage.get("last_used_at"),
        },
        "quota": {
            "limit": quota_value("quota_limit", "limit"),
            "remaining": quota_value("quota_remaining", "remaining"),
            "reset": quota_value("quota_reset", "reset"),
            "updated_at": quota_value("quota_updated_at", "updated_at"),
        },
    }


def _pool_snapshot() -> tuple[list[dict[str, Any]], dict[str, int]]:
    now = time.time()
    usage_by_source = load_usage()
    accounts = [
        _serialize_entry(
            source_id,
            entry,
            now,
            usage_by_source.get(
                (source_id, str(entry.get("generation") or "legacy"))
            ),
        )
        for source_id, entry in _safe_entries().items()
    ]
    accounts.sort(key=lambda item: (item["expired"], item["source_id"]))
    summary = {
        "total": len(accounts),
        "active": sum(1 for item in accounts if not item["expired"]),
        "expired": sum(1 for item in accounts if item["expired"]),
        "expiring_soon": sum(1 for item in accounts if item["expiring_soon"]),
        "with_refresh_token": sum(1 for item in accounts if item["has_refresh_token"]),
        "with_id_token": sum(1 for item in accounts if item["has_id_token"]),
        "sources": len(accounts),
        "request_count": sum(item["usage"]["request_count"] for item in accounts),
        "success_count": sum(item["usage"]["success_count"] for item in accounts),
        "failure_count": sum(item["usage"]["failure_count"] for item in accounts),
        "total_tokens": sum(item["usage"]["total_tokens"] for item in accounts),
        "quota_known": sum(
            1 for item in accounts if item["quota"].get("remaining") is not None
        ),
    }
    return accounts, summary


def _is_grok_sso_record(record: "AccountRecord") -> bool:
    token = str(record.token or "").strip()
    tags = set(record.tags or [])
    ext = record.ext or {}
    return bool(token) and not (
        token.startswith(_EXTERNAL_GPT_PREFIXES)
        or tags.intersection(_EXTERNAL_GPT_TAGS)
        or ext.get("gpt")
        or ext.get("gpt_image")
    )


async def _list_active_sso_records(repo: "AccountRepository") -> list["AccountRecord"]:
    records: list["AccountRecord"] = []
    page_num = 1
    while True:
        page = await repo.list_accounts(
            ListAccountsQuery(
                page=page_num,
                page_size=2000,
                status=AccountStatus.ACTIVE,
                include_deleted=False,
                sort_by="updated_at",
                sort_desc=True,
            )
        )
        records.extend(record for record in page.items if _is_grok_sso_record(record))
        if page_num * 2000 >= page.total:
            break
        page_num += 1
    return records


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in job.items() if key not in {"tokens"}}


def _job_dir() -> Path:
    configured = get_config().get_str(
        "grok_build.job_dir", "data/grok_build_jobs"
    )
    path = Path(configured).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def _persist_job(task_id: str) -> None:
    with _JOB_LOCK:
        job = _JOBS.get(task_id)
        if not job or "created_at" not in job:
            return
        payload = orjson.dumps(_job_view(dict(job)))
    directory = _job_dir()
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{task_id}.", dir=str(directory))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, directory / f"{task_id}.json")
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _load_persisted_job(task_id: str) -> dict[str, Any] | None:
    try:
        payload = orjson.loads((_job_dir() / f"{task_id}.json").read_bytes())
    except (FileNotFoundError, OSError, orjson.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _prune_persisted_jobs(max_jobs: int = 100) -> None:
    try:
        files = sorted(
            _job_dir().glob("*.json"),
            key=lambda path: path.stat().st_mtime,
        )
    except OSError:
        return
    excess = max(0, len(files) - max_jobs)
    for path in files:
        if excess <= 0:
            break
        try:
            payload = orjson.loads(path.read_bytes())
            if payload.get("status") != "completed":
                continue
            path.unlink()
            excess -= 1
        except (OSError, orjson.JSONDecodeError, AttributeError):
            pass


def _update_job(task_id: str, **updates: Any) -> None:
    with _JOB_LOCK:
        job = _JOBS.get(task_id)
        if job is not None:
            job.update(updates)
    _persist_job(task_id)


def _run_authorization_job(task_id: str, candidates: list[tuple[str, str]]) -> None:
    _update_job(task_id, status="running", started_at=int(time.time() * 1000))
    for token, source_id in candidates:
        try:
            authorize_sso_account(token, source_id)
        except Exception as exc:
            with _JOB_LOCK:
                job = _JOBS[task_id]
                job["failed"] += 1
                job["pending"] -= 1
                job["progress"] += 1
                job["errors"].append(
                    {"source_id": source_id, "error": type(exc).__name__}
                )
            logger.warning(
                "admin Grok Build OAuth authorization failed source_id={} error_type={}",
                source_id,
                type(exc).__name__,
            )
            _persist_job(task_id)
        else:
            with _JOB_LOCK:
                job = _JOBS[task_id]
                job["succeeded"] += 1
                job["pending"] -= 1
                job["progress"] += 1
            _persist_job(task_id)
    with _JOB_LOCK:
        job = _JOBS[task_id]
        job.update(
            status="completed",
            completed_at=int(time.time() * 1000),
            result={
                "converted": job["succeeded"],
                "refreshed": job["succeeded"],
                "succeeded": job["succeeded"],
                "failed": job["failed"],
                "skipped": job["skipped"],
            },
        )
    _persist_job(task_id)


def _run_refresh_job(task_id: str, source_ids: list[str]) -> None:
    _update_job(task_id, status="running", started_at=int(time.time() * 1000))
    for source_id in source_ids:
        try:
            result = refresh_pool_credential(source_id)
        except Exception as exc:
            with _JOB_LOCK:
                job = _JOBS[task_id]
                job["failed"] += 1
                job["pending"] -= 1
                job["progress"] += 1
                job["errors"].append(
                    {"source_id": source_id, "error": type(exc).__name__}
                )
            logger.warning(
                "admin Grok Build OAuth refresh failed source_id={} error_type={}",
                source_id,
                type(exc).__name__,
            )
            _persist_job(task_id)
        else:
            with _JOB_LOCK:
                job = _JOBS[task_id]
                if result.get("conflict"):
                    job["skipped"] += 1
                    job["errors"].append(
                        {"source_id": source_id, "error": "refresh_conflict"}
                    )
                else:
                    job["succeeded"] += 1
                job["pending"] -= 1
                job["progress"] += 1
            _persist_job(task_id)
    with _JOB_LOCK:
        job = _JOBS[task_id]
        job.update(
            status="completed",
            completed_at=int(time.time() * 1000),
            result={
                "refreshed": job["succeeded"],
                "succeeded": job["succeeded"],
                "failed": job["failed"],
                "skipped": job["skipped"],
            },
        )
    _persist_job(task_id)


def _start_job(
    candidates: list[tuple[str, str]],
    *,
    scanned: int,
    skipped: int,
    job_type: str = "convert",
    errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    task_id = uuid.uuid4().hex
    job = {
        "task_id": task_id,
        "type": job_type,
        "status": "pending",
        "scanned": scanned,
        "selected": len(candidates),
        "total": len(candidates),
        "progress": 0,
        "pending": len(candidates),
        "skipped": skipped,
        "succeeded": 0,
        "failed": 0,
        "errors": list(errors or []),
        "created_at": int(time.time() * 1000),
        "started_at": None,
        "completed_at": None,
    }
    with _JOB_LOCK:
        if len(_JOBS) >= 100:
            completed = [
                key for key, value in _JOBS.items() if value["status"] == "completed"
            ]
            if completed:
                oldest = min(completed, key=lambda key: _JOBS[key]["created_at"])
                _JOBS.pop(oldest, None)
        _JOBS[task_id] = job
    _persist_job(task_id)
    _prune_persisted_jobs()
    task = asyncio.create_task(
        asyncio.to_thread(_run_authorization_job, task_id, candidates)
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return _job_view(job)


def _start_refresh_job(source_ids: list[str]) -> dict[str, Any]:
    task_id = uuid.uuid4().hex
    job = {
        "task_id": task_id,
        "type": "refresh",
        "status": "pending",
        "scanned": len(source_ids),
        "selected": len(source_ids),
        "total": len(source_ids),
        "progress": 0,
        "pending": len(source_ids),
        "skipped": 0,
        "succeeded": 0,
        "failed": 0,
        "errors": [],
        "created_at": int(time.time() * 1000),
        "started_at": None,
        "completed_at": None,
    }
    with _JOB_LOCK:
        if len(_JOBS) >= 100:
            completed = [
                key for key, value in _JOBS.items() if value["status"] == "completed"
            ]
            if completed:
                oldest = min(completed, key=lambda key: _JOBS[key]["created_at"])
                _JOBS.pop(oldest, None)
        _JOBS[task_id] = job
    _persist_job(task_id)
    _prune_persisted_jobs()
    task = asyncio.create_task(asyncio.to_thread(_run_refresh_job, task_id, source_ids))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return _job_view(job)


@router.get("/accounts")
async def list_grok_build_accounts(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=5000, ge=1, le=5000),
):
    accounts, summary = await asyncio.to_thread(_pool_snapshot)
    start = (page - 1) * page_size
    return _json(
        {
            "summary": summary,
            "page": page,
            "page_size": page_size,
            "accounts": accounts[start : start + page_size],
        }
    )


@router.get("/accounts/summary")
@router.get("/summary")
async def summarize_grok_build_accounts():
    _accounts, summary = await asyncio.to_thread(_pool_snapshot)
    return _json({"summary": summary})


@router.delete("/accounts/{source_id}")
async def delete_grok_build_account(source_id: str):
    deleted = await asyncio.to_thread(delete_pool_entry, source_id)
    if not deleted:
        raise AppError(
            "Grok Build OAuth account not found",
            kind=ErrorKind.VALIDATION,
            code="grok_build_account_not_found",
            status=404,
        )
    await asyncio.to_thread(delete_usage, [source_id])
    return _json({"status": "success", "deleted": 1, "source_id": source_id})


@router.post("/accounts/delete")
async def delete_grok_build_accounts(req: SourceIdsRequest):
    source_ids = list(
        dict.fromkeys(
            source_id for raw in req.source_ids if (source_id := str(raw or "").strip())
        )
    )
    if not source_ids:
        raise ValidationError("No source IDs provided", param="source_ids")
    deleted, not_found = await asyncio.to_thread(delete_pool_entries, source_ids)
    if deleted:
        await asyncio.to_thread(delete_usage, deleted)
    return _json(
        {
            "status": "success",
            "deleted": deleted,
            "deleted_count": len(deleted),
            "not_found": not_found,
        }
    )


@router.post("/convert")
async def convert_grok_sso_accounts(
    req: ConvertRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    records = await _list_active_sso_records(repo)
    existing = set((await asyncio.to_thread(_safe_entries)).keys())
    candidates = [
        (record.token, source_id_for_sso(record.token))
        for record in records
        if req.force or source_id_for_sso(record.token) not in existing
    ]
    selected = candidates if req.limit == 0 else candidates[: req.limit]
    skipped = len(records) - len(selected)
    return _json(_start_job(selected, scanned=len(records), skipped=skipped), 202)


@router.post("/accounts/refresh")
async def refresh_grok_build_accounts(
    req: SourceIdsRequest,
):
    requested = list(
        dict.fromkeys(
            source_id for raw in req.source_ids if (source_id := str(raw or "").strip())
        )
    )
    if not requested:
        raise ValidationError("No source IDs provided", param="source_ids")
    return _json(_start_refresh_job(requested), 202)


@router.get("/convert/{task_id}")
@router.get("/tasks/{task_id}")
async def get_grok_build_conversion(task_id: str):
    with _JOB_LOCK:
        job = _JOBS.get(task_id)
        if job is None:
            payload = None
        else:
            payload = _job_view(job)
    if payload is None:
        payload = await asyncio.to_thread(_load_persisted_job, task_id)
    if payload is None:
        raise AppError(
            "Grok Build OAuth conversion task not found",
            kind=ErrorKind.VALIDATION,
            code="grok_build_conversion_not_found",
            status=404,
        )
    return _json(payload)


@router.post("/accounts/{source_id}/refresh")
async def refresh_grok_build_account(
    source_id: str,
):
    try:
        result = await asyncio.to_thread(refresh_pool_credential, source_id)
    except Exception as exc:
        logger.warning(
            "admin Grok Build OAuth refresh failed source_id={} error_type={}",
            source_id,
            type(exc).__name__,
        )
        raise AppError(
            "Grok Build OAuth refresh failed",
            kind=ErrorKind.UPSTREAM,
            code="grok_build_refresh_failed",
            status=502,
        ) from exc
    if result.get("conflict"):
        raise AppError(
            "Grok Build OAuth credential changed during refresh",
            kind=ErrorKind.VALIDATION,
            code="grok_build_refresh_conflict",
            status=409,
        )
    return _json({"status": "success", **result})


__all__ = ["router"]
