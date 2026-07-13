"""Persistent per-account availability state for Grok Build requests."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping


FREE_USAGE_EXHAUSTED = "subscription:free-usage-exhausted"
DEFAULT_FREE_RECOVERY_SECONDS = 24 * 60 * 60
DEFAULT_BACKOFF_SECONDS = 60
MAX_BACKOFF_SECONDS = 60 * 60

_TOKEN_PAIR_RE = re.compile(
    r"tokens\s*\(\s*actual\s*[:=]\s*(\d+)\s*[,;]\s*"
    r"limit\s*[:=]\s*(\d+)\s*\)",
    re.IGNORECASE,
)


class AvailabilityState(str, Enum):
    READY = "ready"
    COOLDOWN = "cooldown"
    BLOCKED = "blocked"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    state: AvailabilityState
    reason: str
    retry_at: float
    retry_after_seconds: float
    actual_tokens: int | None = None
    limit_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class AvailabilityRecord:
    source_id: str
    model: str
    state: AvailabilityState
    reason: str | None = None
    retry_at: float | None = None
    actual_tokens: int | None = None
    limit_tokens: int | None = None
    failure_count: int = 0
    last_status: int | None = None
    updated_at: float | None = None

    def is_candidate(self, *, now: float | None = None) -> bool:
        if self.state == AvailabilityState.DISABLED:
            return False
        if self.state == AvailabilityState.READY:
            return True
        return self.retry_at is not None and self.retry_at <= (
            time.time() if now is None else now
        )


def _normalized_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    return {
        str(key).strip().lower(): str(value).strip()
        for key, value in (headers or {}).items()
        if value is not None
    }


def _decoded_body(body: Any) -> Any:
    if isinstance(body, (bytes, bytearray, memoryview)):
        body = bytes(body).decode("utf-8", "replace")
    if not isinstance(body, str):
        return body
    text = body.strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _iter_values(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, Mapping):
        for nested in value.values():
            yield from _iter_values(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _iter_values(nested)


def _body_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _non_negative_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, number)


def _token_counts(value: Any) -> tuple[int | None, int | None]:
    for nested in _iter_values(value):
        if not isinstance(nested, Mapping):
            continue
        tokens = nested.get("tokens")
        if isinstance(tokens, Mapping):
            actual = _non_negative_int(tokens.get("actual"))
            limit = _non_negative_int(tokens.get("limit"))
            if actual is not None or limit is not None:
                return actual, limit

    match = _TOKEN_PAIR_RE.search(_body_text(value))
    if match:
        return int(match.group(1)), int(match.group(2))
    return None, None


def _numeric_seconds(value: Any, *, milliseconds: bool = False) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if milliseconds:
        seconds /= 1000
    return max(0.0, seconds)


def _retry_after_seconds(
    headers: Mapping[str, Any] | None,
    body: Any,
    *,
    now: float,
) -> float | None:
    value = _normalized_headers(headers).get("retry-after")
    seconds = _numeric_seconds(value)
    if seconds is not None:
        return seconds
    if value:
        try:
            retry_at = parsedate_to_datetime(value).timestamp()
        except (TypeError, ValueError, OverflowError):
            pass
        else:
            return max(0.0, retry_at - now)

    for nested in _iter_values(body):
        if not isinstance(nested, Mapping):
            continue
        for key in ("retry_after", "retryAfter"):
            seconds = _numeric_seconds(nested.get(key))
            if seconds is not None:
                return seconds
        seconds = _numeric_seconds(nested.get("retry_after_ms"), milliseconds=True)
        if seconds is not None:
            return seconds
    return None


def _error_reason(body: Any) -> tuple[str, bool]:
    text_values = [
        value.strip()
        for value in _iter_values(body)
        if isinstance(value, str) and value.strip()
    ]
    for value in text_values:
        if FREE_USAGE_EXHAUSTED in value.lower():
            return FREE_USAGE_EXHAUSTED, True
    return (text_values[0][:240] if text_values else "rate_limited"), False


def parse_xai_rate_limit(
    status_code: int,
    body: Any = None,
    headers: Mapping[str, Any] | None = None,
    *,
    now: float | None = None,
    free_recovery_seconds: float = DEFAULT_FREE_RECOVERY_SECONDS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
) -> RateLimitDecision | None:
    """Parse an xAI 429 response without depending on one JSON error shape."""
    if status_code != 429:
        return None

    current = time.time() if now is None else float(now)
    decoded = _decoded_body(body)
    reason, free_exhausted = _error_reason(decoded)
    actual_tokens, limit_tokens = _token_counts(decoded)
    explicit_retry = _retry_after_seconds(headers, decoded, now=current)
    fallback = free_recovery_seconds if free_exhausted else backoff_seconds
    retry_after = max(0.0, explicit_retry if explicit_retry is not None else fallback)
    return RateLimitDecision(
        state=(
            AvailabilityState.BLOCKED
            if free_exhausted
            else AvailabilityState.COOLDOWN
        ),
        reason=reason,
        retry_at=current + retry_after,
        retry_after_seconds=retry_after,
        actual_tokens=actual_tokens,
        limit_tokens=limit_tokens,
    )


def cooldown_db_path() -> Path:
    """Share the usage database by default while keeping the store injectable."""
    from app.maintainer.grok_build_usage import usage_db_path

    return usage_db_path()


class GrokBuildCooldownStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else cooldown_db_path()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS grok_build_availability (
                source_id TEXT NOT NULL,
                model TEXT NOT NULL,
                state TEXT NOT NULL,
                reason TEXT,
                retry_at REAL,
                actual_tokens INTEGER,
                limit_tokens INTEGER,
                failure_count INTEGER NOT NULL DEFAULT 0,
                last_status INTEGER,
                updated_at REAL NOT NULL,
                PRIMARY KEY (source_id, model)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_grok_build_availability_model_state
            ON grok_build_availability (model, state, retry_at)
            """
        )
        return connection

    @staticmethod
    def _validate_key(source_id: str, model: str) -> tuple[str, str]:
        source_id = str(source_id or "").strip()
        model = str(model or "").strip()
        if not source_id:
            raise ValueError("source_id is required")
        if not model:
            raise ValueError("model is required")
        return source_id, model

    @staticmethod
    def _row(row: sqlite3.Row) -> AvailabilityRecord:
        return AvailabilityRecord(
            source_id=str(row["source_id"]),
            model=str(row["model"]),
            state=AvailabilityState(str(row["state"])),
            reason=row["reason"],
            retry_at=row["retry_at"],
            actual_tokens=row["actual_tokens"],
            limit_tokens=row["limit_tokens"],
            failure_count=int(row["failure_count"] or 0),
            last_status=row["last_status"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _expire_due(connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            """
            UPDATE grok_build_availability
            SET state = ?, reason = NULL, retry_at = NULL,
                actual_tokens = NULL, limit_tokens = NULL,
                failure_count = 0, updated_at = ?
            WHERE state IN (?, ?) AND retry_at IS NOT NULL AND retry_at <= ?
            """,
            (
                AvailabilityState.READY.value,
                now,
                AvailabilityState.COOLDOWN.value,
                AvailabilityState.BLOCKED.value,
                now,
            ),
        )

    def get(
        self, source_id: str, model: str, *, now: float | None = None
    ) -> AvailabilityRecord:
        source_id, model = self._validate_key(source_id, model)
        current = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            with connection:
                self._expire_due(connection, current)
                row = connection.execute(
                    """
                    SELECT * FROM grok_build_availability
                    WHERE source_id = ? AND model = ?
                    """,
                    (source_id, model),
                ).fetchone()
        finally:
            connection.close()
        return (
            self._row(row)
            if row is not None
            else AvailabilityRecord(
                source_id=source_id,
                model=model,
                state=AvailabilityState.READY,
            )
        )

    def filter_candidates(
        self,
        source_ids: Iterable[str],
        model: str,
        *,
        now: float | None = None,
    ) -> list[str]:
        model = str(model or "").strip()
        if not model:
            raise ValueError("model is required")
        candidates = [str(source_id or "").strip() for source_id in source_ids]
        candidates = [source_id for source_id in candidates if source_id]
        if not candidates:
            return []

        current = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            with connection:
                self._expire_due(connection, current)
                rows = connection.execute(
                    """
                    SELECT * FROM grok_build_availability WHERE model = ?
                    """,
                    (model,),
                ).fetchall()
        finally:
            connection.close()
        states = {str(row["source_id"]): self._row(row) for row in rows}
        return [
            source_id
            for source_id in candidates
            if source_id not in states or states[source_id].is_candidate(now=current)
        ]

    def set_disabled(
        self,
        source_id: str,
        model: str,
        *,
        disabled: bool = True,
        reason: str | None = None,
        now: float | None = None,
    ) -> AvailabilityRecord:
        source_id, model = self._validate_key(source_id, model)
        current = time.time() if now is None else float(now)
        state = (
            AvailabilityState.DISABLED if disabled else AvailabilityState.READY
        )
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO grok_build_availability (
                        source_id, model, state, reason, retry_at,
                        actual_tokens, limit_tokens, failure_count,
                        last_status, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, 0, NULL, ?)
                    ON CONFLICT(source_id, model) DO UPDATE SET
                        state = excluded.state,
                        reason = excluded.reason,
                        retry_at = NULL,
                        actual_tokens = NULL,
                        limit_tokens = NULL,
                        failure_count = 0,
                        updated_at = excluded.updated_at
                    """,
                    (source_id, model, state.value, reason, current),
                )
        finally:
            connection.close()
        return self.get(source_id, model, now=current)

    def mark_result(
        self,
        source_id: str,
        model: str,
        *,
        status_code: int,
        body: Any = None,
        headers: Mapping[str, Any] | None = None,
        now: float | None = None,
        free_recovery_seconds: float = DEFAULT_FREE_RECOVERY_SECONDS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        max_backoff_seconds: float = MAX_BACKOFF_SECONDS,
    ) -> AvailabilityRecord:
        source_id, model = self._validate_key(source_id, model)
        current = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                self._expire_due(connection, current)
                existing = connection.execute(
                    """
                    SELECT * FROM grok_build_availability
                    WHERE source_id = ? AND model = ?
                    """,
                    (source_id, model),
                ).fetchone()
                previous = self._row(existing) if existing is not None else None

                if previous and previous.state == AvailabilityState.DISABLED:
                    state = previous.state
                    reason = previous.reason
                    retry_at = None
                    actual_tokens = previous.actual_tokens
                    limit_tokens = previous.limit_tokens
                    failure_count = previous.failure_count
                elif 200 <= status_code < 400:
                    state = AvailabilityState.READY
                    reason = None
                    retry_at = None
                    actual_tokens = None
                    limit_tokens = None
                    failure_count = 0
                elif status_code == 429:
                    previous_failures = previous.failure_count if previous else 0
                    calculated_backoff = min(
                        max(0.0, float(max_backoff_seconds)),
                        max(0.0, float(backoff_seconds))
                        * (2 ** min(previous_failures, 10)),
                    )
                    decision = parse_xai_rate_limit(
                        status_code,
                        body,
                        headers,
                        now=current,
                        free_recovery_seconds=free_recovery_seconds,
                        backoff_seconds=calculated_backoff,
                    )
                    assert decision is not None
                    state = decision.state
                    reason = decision.reason
                    retry_at = decision.retry_at
                    actual_tokens = decision.actual_tokens
                    limit_tokens = decision.limit_tokens
                    failure_count = previous_failures + 1
                else:
                    state = previous.state if previous else AvailabilityState.READY
                    reason = previous.reason if previous else None
                    retry_at = previous.retry_at if previous else None
                    actual_tokens = previous.actual_tokens if previous else None
                    limit_tokens = previous.limit_tokens if previous else None
                    failure_count = previous.failure_count if previous else 0

                connection.execute(
                    """
                    INSERT INTO grok_build_availability (
                        source_id, model, state, reason, retry_at,
                        actual_tokens, limit_tokens, failure_count,
                        last_status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id, model) DO UPDATE SET
                        state = excluded.state,
                        reason = excluded.reason,
                        retry_at = excluded.retry_at,
                        actual_tokens = excluded.actual_tokens,
                        limit_tokens = excluded.limit_tokens,
                        failure_count = excluded.failure_count,
                        last_status = excluded.last_status,
                        updated_at = excluded.updated_at
                    """,
                    (
                        source_id,
                        model,
                        state.value,
                        reason,
                        retry_at,
                        actual_tokens,
                        limit_tokens,
                        failure_count,
                        status_code,
                        current,
                    ),
                )
        finally:
            connection.close()
        return self.get(source_id, model, now=current)


__all__ = [
    "AvailabilityRecord",
    "AvailabilityState",
    "DEFAULT_BACKOFF_SECONDS",
    "DEFAULT_FREE_RECOVERY_SECONDS",
    "FREE_USAGE_EXHAUSTED",
    "GrokBuildCooldownStore",
    "MAX_BACKOFF_SECONDS",
    "RateLimitDecision",
    "cooldown_db_path",
    "parse_xai_rate_limit",
]
