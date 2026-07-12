"""Persistent Grok Build quota and usage accounting."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from app.platform.config.snapshot import get_config


def usage_db_path() -> Path:
    configured = get_config().get_str(
        "grok_build.usage_db", "data/grok_build_usage.db"
    )
    path = Path(configured).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def _connect() -> sqlite3.Connection:
    path = usage_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS grok_build_usage (
            source_id TEXT NOT NULL,
            generation TEXT NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            last_status INTEGER,
            last_used_at REAL,
            quota_limit TEXT,
            quota_remaining TEXT,
            quota_reset TEXT,
            quota_updated_at REAL,
            PRIMARY KEY (source_id, generation)
        )
        """
    )
    return connection


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def record_usage(
    source_id: str,
    *,
    generation: str = "legacy",
    status_code: int,
    usage: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
    count_request: bool = True,
) -> bool:
    if not source_id:
        return False
    usage = usage if isinstance(usage, dict) else {}
    normalized_headers = {
        str(key).lower(): str(value)
        for key, value in (headers or {}).items()
        if value is not None
    }
    input_tokens = _as_int(
        usage.get("input_tokens") or usage.get("prompt_tokens")
    )
    output_tokens = _as_int(
        usage.get("output_tokens") or usage.get("completion_tokens")
    )
    total_tokens = _as_int(usage.get("total_tokens"))
    if not total_tokens:
        total_tokens = input_tokens + output_tokens

    def header(*names: str) -> str | None:
        return next(
            (normalized_headers[name] for name in names if name in normalized_headers),
            None,
        )

    quota_limit = header("x-ratelimit-limit-requests", "x-ratelimit-limit")
    quota_remaining = header(
        "x-ratelimit-remaining-requests", "x-ratelimit-remaining"
    )
    quota_reset = header("x-ratelimit-reset-requests", "x-ratelimit-reset")
    quota_updated_at = (
        time.time()
        if any(value is not None for value in (quota_limit, quota_remaining, quota_reset))
        else None
    )
    request_delta = 1 if count_request else 0
    success_delta = 1 if count_request and 200 <= status_code < 400 else 0
    failure_delta = 1 if count_request and not 200 <= status_code < 400 else 0
    now = time.time() if count_request or total_tokens else None

    connection = _connect()
    try:
        with connection:
            connection.execute(
                """
            INSERT INTO grok_build_usage (
                source_id, generation, request_count, success_count, failure_count,
                input_tokens, output_tokens, total_tokens, last_status, last_used_at,
                quota_limit, quota_remaining, quota_reset, quota_updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, generation) DO UPDATE SET
                request_count = request_count + excluded.request_count,
                success_count = success_count + excluded.success_count,
                failure_count = failure_count + excluded.failure_count,
                input_tokens = input_tokens + excluded.input_tokens,
                output_tokens = output_tokens + excluded.output_tokens,
                total_tokens = total_tokens + excluded.total_tokens,
                last_status = COALESCE(excluded.last_status, last_status),
                last_used_at = COALESCE(excluded.last_used_at, last_used_at),
                quota_limit = COALESCE(excluded.quota_limit, quota_limit),
                quota_remaining = COALESCE(excluded.quota_remaining, quota_remaining),
                quota_reset = COALESCE(excluded.quota_reset, quota_reset),
                quota_updated_at = COALESCE(excluded.quota_updated_at, quota_updated_at)
                """,
                (
                    source_id,
                    generation or "legacy",
                    request_delta,
                    success_delta,
                    failure_delta,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    status_code if count_request else None,
                    now,
                    quota_limit,
                    quota_remaining,
                    quota_reset,
                    quota_updated_at,
                ),
            )
    finally:
        connection.close()
    return True


def load_usage() -> dict[tuple[str, str], dict[str, Any]]:
    connection = _connect()
    try:
        rows = connection.execute("SELECT * FROM grok_build_usage").fetchall()
    finally:
        connection.close()
    return {
        (str(row["source_id"]), str(row["generation"])): dict(row)
        for row in rows
    }


def delete_usage(source_ids: list[str]) -> int:
    cleaned = list(dict.fromkeys(str(value or "").strip() for value in source_ids))
    cleaned = [value for value in cleaned if value]
    if not cleaned:
        return 0
    connection = _connect()
    try:
        with connection:
            cursor = connection.executemany(
                "DELETE FROM grok_build_usage WHERE source_id = ?",
                [(source_id,) for source_id in cleaned],
            )
            return max(0, int(cursor.rowcount or 0))
    finally:
        connection.close()


__all__ = ["delete_usage", "load_usage", "record_usage", "usage_db_path"]
