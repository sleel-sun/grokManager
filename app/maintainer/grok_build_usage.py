"""Persistent Grok Build quota and usage accounting."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
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


@dataclass(slots=True)
class _UsageEvent:
    source_id: str
    generation: str
    request_count: int
    success_count: int
    failure_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    last_status: int | None
    last_used_at: float | None
    quota_limit: str | None
    quota_remaining: str | None
    quota_reset: str | None
    quota_updated_at: float | None


def _usage_event(
    source_id: str,
    *,
    generation: str,
    status_code: int,
    usage: dict[str, Any] | None,
    headers: dict[str, Any] | None,
    count_request: bool,
) -> _UsageEvent | None:
    if not source_id:
        return None
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
    return _UsageEvent(
        source_id=source_id,
        generation=generation or "legacy",
        request_count=request_delta,
        success_count=1 if count_request and 200 <= status_code < 400 else 0,
        failure_count=1 if count_request and not 200 <= status_code < 400 else 0,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        last_status=status_code if count_request else None,
        last_used_at=time.time() if count_request or total_tokens else None,
        quota_limit=quota_limit,
        quota_remaining=quota_remaining,
        quota_reset=quota_reset,
        quota_updated_at=quota_updated_at,
    )


def _merge_events(events: list[_UsageEvent]) -> list[_UsageEvent]:
    merged: dict[tuple[str, str], _UsageEvent] = {}
    for event in events:
        key = (event.source_id, event.generation)
        current = merged.get(key)
        if current is None:
            merged[key] = event
            continue
        current.request_count += event.request_count
        current.success_count += event.success_count
        current.failure_count += event.failure_count
        current.input_tokens += event.input_tokens
        current.output_tokens += event.output_tokens
        current.total_tokens += event.total_tokens
        for field in (
            "last_status",
            "last_used_at",
            "quota_limit",
            "quota_remaining",
            "quota_reset",
            "quota_updated_at",
        ):
            value = getattr(event, field)
            if value is not None:
                setattr(current, field, value)
    return list(merged.values())


def _write_events(events: list[_UsageEvent]) -> None:
    if not events:
        return
    connection = _connect()
    try:
        with connection:
            connection.executemany(
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
                [
                    (
                        event.source_id,
                        event.generation,
                        event.request_count,
                        event.success_count,
                        event.failure_count,
                        event.input_tokens,
                        event.output_tokens,
                        event.total_tokens,
                        event.last_status,
                        event.last_used_at,
                        event.quota_limit,
                        event.quota_remaining,
                        event.quota_reset,
                        event.quota_updated_at,
                    )
                    for event in _merge_events(events)
                ],
            )
    finally:
        connection.close()


def record_usage(
    source_id: str,
    *,
    generation: str = "legacy",
    status_code: int,
    usage: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
    count_request: bool = True,
) -> bool:
    event = _usage_event(
        source_id,
        generation=generation,
        status_code=status_code,
        usage=usage,
        headers=headers,
        count_request=count_request,
    )
    if event is None:
        return False
    _write_events([event])
    return True


class _UsageWriter:
    def __init__(self, *, capacity: int, batch_size: int, flush_interval: float):
        self.queue: asyncio.Queue[_UsageEvent] = asyncio.Queue(maxsize=max(1, capacity))
        self.batch_size = max(1, batch_size)
        self.flush_interval = max(0.01, flush_interval)
        self.stopping = False
        self.wake = asyncio.Event()
        self.idle = asyncio.Event()
        self.idle.set()
        self.task = asyncio.create_task(self._run(), name="grok-build-usage-writer")

    def publish(self, event: _UsageEvent) -> bool:
        if self.stopping or self.task.done():
            return False
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            return False
        self.idle.clear()
        self.wake.set()
        return True

    async def flush(self) -> None:
        self.wake.set()
        await self.idle.wait()

    async def stop(self, *, flush: bool) -> None:
        self.stopping = True
        if flush:
            self.wake.set()
            await self.idle.wait()
            await self.task
            return
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.queue.task_done()
        self.wake.set()
        await self.task

    async def _run(self) -> None:
        while not self.stopping or not self.queue.empty():
            if self.queue.empty():
                self.wake.clear()
                try:
                    await asyncio.wait_for(
                        self.wake.wait(), timeout=self.flush_interval
                    )
                except TimeoutError:
                    pass
            batch: list[_UsageEvent] = []
            while len(batch) < self.batch_size:
                try:
                    batch.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if not batch:
                continue
            try:
                await asyncio.to_thread(_write_events, batch)
            finally:
                for _ in batch:
                    self.queue.task_done()
                if self.queue.empty():
                    self.idle.set()


_writer: _UsageWriter | None = None


async def start_usage_writer(
    *, capacity: int = 2048, batch_size: int = 100, flush_interval: float = 0.25
) -> None:
    """Start the process-local bounded usage writer."""
    global _writer
    if _writer is not None and not _writer.task.done():
        return
    _writer = _UsageWriter(
        capacity=capacity,
        batch_size=batch_size,
        flush_interval=flush_interval,
    )


def publish_usage(
    source_id: str,
    *,
    generation: str = "legacy",
    status_code: int,
    usage: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
    count_request: bool = True,
) -> bool:
    """Publish without blocking; return False when stopped or at capacity."""
    writer = _writer
    if writer is None:
        return False
    event = _usage_event(
        source_id,
        generation=generation,
        status_code=status_code,
        usage=usage,
        headers=headers,
        count_request=count_request,
    )
    return event is not None and writer.publish(event)


async def flush_usage() -> None:
    """Wait until every accepted event has reached SQLite."""
    if _writer is not None:
        await _writer.flush()


async def stop_usage_writer(*, flush: bool = True) -> None:
    """Stop the writer, flushing accepted events by default."""
    global _writer
    writer, _writer = _writer, None
    if writer is not None:
        await writer.stop(flush=flush)


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


__all__ = [
    "delete_usage",
    "flush_usage",
    "load_usage",
    "publish_usage",
    "record_usage",
    "start_usage_writer",
    "stop_usage_writer",
    "usage_db_path",
]
