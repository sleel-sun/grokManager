"""Admin Account Maintainer endpoints."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import multiprocessing as mp
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from app.platform.config.snapshot import config
from app.platform.errors import AppError, ErrorKind, ValidationError
from app.platform.logging.logger import logger
from app.platform.paths import data_path, log_path


router = APIRouter(prefix="/maintainer", tags=["Admin - Maintainer"])

_DOMAIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::\d{1,5})?$")
_ENV_KEYS = (
    "GROK_MAINTAINER_CONFIG",
    "MAINTAINER_HEADLESS",
    "MAINTAINER_USE_XVFB",
    "MAINTAINER_NO_SANDBOX",
    "MAINTAINER_DISABLE_DEV_SHM",
    "MAINTAINER_WINDOW_SIZE",
)
_SECRET_KEYS = {"email_admin_password", "api_token", "admin_password", "token"}


class MaintainerRunRequest(BaseModel):
    # ``count`` and ``workers`` deliberately have NO upper bound. The historical
    # caps (count<=100, workers<=8) silently clamped values that exceeded them,
    # so a user submitting workers=10 saw only 8 worker windows pop up and read
    # that as "the parallelism setting did not take effect". Operators are
    # responsible for picking values their hardware can sustain; the run-time
    # already reports the actually spawned count via ``spawned_workers`` so
    # silent clamping is no longer needed as a guardrail.
    count: int = Field(default=1, ge=1)
    workers: int = Field(default=1, ge=1)
    email_worker_domain: str = Field(min_length=1, max_length=253)
    email_domains: list[str] = Field(min_length=1, max_length=20)
    email_admin_password: str = Field(default="", max_length=4096)
    pool: Literal["basic", "super", "heavy"] = "basic"
    headless: bool = False
    use_xvfb: bool = False
    no_sandbox: bool = False
    disable_dev_shm: bool = False
    window_size: str = Field(default="1440,900", max_length=32)
    verify_ssl: bool = True
    extract_numbers: bool = False

    @field_validator("email_worker_domain")
    @classmethod
    def _validate_worker_domain(cls, value: str) -> str:
        value = str(value or "").strip()
        if not _DOMAIN_RE.match(value):
            raise ValueError("email_worker_domain must be a hostname, optionally with port")
        return value

    @field_validator("email_domains", mode="before")
    @classmethod
    def _coerce_domains(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",")]
        if not isinstance(value, list):
            raise ValueError("email_domains must be a list or comma-separated string")
        domains = [str(item or "").strip() for item in value if str(item or "").strip()]
        if not domains:
            raise ValueError("email_domains cannot be empty")
        for domain in domains:
            if ":" in domain or not _DOMAIN_RE.match(domain):
                raise ValueError("email_domains must contain hostnames only")
        return domains

    @field_validator("window_size")
    @classmethod
    def _validate_window_size(cls, value: str) -> str:
        value = str(value or "").strip()
        if value and not re.match(r"^\d{3,5},\d{3,5}$", value):
            raise ValueError("window_size must look like 1440,900")
        return value

    @field_validator("email_admin_password", mode="before")
    @classmethod
    def _coerce_admin_password(cls, value: Any) -> str:
        return "" if value is None else str(value)


_state: dict[str, Any] = {
    "running": False,
    "paused": False,
    "status": "idle",
    "message": "",
    "started_at": None,
    "finished_at": None,
    "token_count": 0,
    "config_path": "",
    "output_path": "",
    "workers": 1,
    "spawned_workers": 0,
    "per_worker_progress": {},
}
_task: asyncio.Task | None = None
_lock = asyncio.Lock()


class _MaintainerController:
    """Pause/stop signalling shared between request handlers and the worker(s).

    Uses ``multiprocessing.Event`` so the same controller instance can drive
    both single-thread (``workers=1``) and multi-process (``workers>1``)
    registration runs. ``pause_event`` is *set* when running and *cleared*
    when paused — matching :func:`app.maintainer.runner.run_batch_parallel`.
    """

    def __init__(self) -> None:
        ctx = mp.get_context("spawn")
        self._pause_event = ctx.Event()
        self._pause_event.set()
        self._stop_event = ctx.Event()

    @property
    def pause_event(self) -> Any:
        return self._pause_event

    @property
    def stop_event(self) -> Any:
        return self._stop_event

    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    def is_stopped(self) -> bool:
        return self._stop_event.is_set()

    def pause(self) -> None:
        self._pause_event.clear()

    def resume(self) -> None:
        self._pause_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        # Releasing pause guarantees any worker sleeping in the pause loop
        # wakes up immediately and observes the stop signal.
        self._pause_event.set()

    def reset(self) -> None:
        if not self._pause_event.is_set():
            self._pause_event.set()
        if self._stop_event.is_set():
            self._stop_event.clear()


_controller = _MaintainerController()


def build_runtime_config(
    req: MaintainerRunRequest,
    *,
    base_url: str,
    admin_token: str,
    existing_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the config consumed by app.maintainer.runner."""
    email_conf = (
        existing_config.get("email", {})
        if isinstance(existing_config, dict) and isinstance(existing_config.get("email"), dict)
        else {}
    )
    saved_password = str(email_conf.get("admin_password") or "")
    email_admin_password = req.email_admin_password or saved_password
    if not email_admin_password:
        raise ValidationError(
            "Email Worker admin password is required",
            param="email_admin_password",
        )

    return {
        "email": {
            "worker_domain": req.email_worker_domain,
            "email_domains": list(req.email_domains),
            "admin_password": email_admin_password,
            "verify_ssl": req.verify_ssl,
        },
        "api": {
            "endpoint": f"{base_url.rstrip('/')}/admin/api/tokens/add",
            "token": admin_token,
            "append": True,
            "pool": req.pool,
            "verify_ssl": req.verify_ssl,
        },
        "run": {"count": req.count, "workers": req.workers},
        "web": {
            "headless": req.headless,
            "use_xvfb": req.use_xvfb,
            "no_sandbox": req.no_sandbox,
            "disable_dev_shm": req.disable_dev_shm,
            "window_size": req.window_size,
            "extract_numbers": req.extract_numbers,
        },
    }


def build_saved_config_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Serialize saved maintainer config without exposing secret values."""
    email_conf = payload.get("email") if isinstance(payload.get("email"), dict) else {}
    api_conf = payload.get("api") if isinstance(payload.get("api"), dict) else {}
    run_conf = payload.get("run") if isinstance(payload.get("run"), dict) else {}
    web_conf = payload.get("web") if isinstance(payload.get("web"), dict) else {}

    domains = email_conf.get("email_domains", email_conf.get("domains", []))
    if isinstance(domains, str):
        domains = [part.strip() for part in domains.split(",") if part.strip()]
    if not isinstance(domains, list):
        domains = []
    domains = [str(item).strip() for item in domains if str(item or "").strip()]

    try:
        count = int(run_conf.get("count", 1) or 1)
    except (TypeError, ValueError):
        count = 1
    count = max(count, 1)

    try:
        workers = int(run_conf.get("workers", 1) or 1)
    except (TypeError, ValueError):
        workers = 1
    workers = max(workers, 1)

    pool = str(api_conf.get("pool", "basic") or "basic").strip().lower()
    if pool not in {"basic", "super", "heavy"}:
        pool = "basic"

    return {
        "email_worker_domain": str(email_conf.get("worker_domain") or ""),
        "email_domains": domains,
        "has_email_admin_password": bool(email_conf.get("admin_password")),
        "verify_ssl": bool(email_conf.get("verify_ssl", True)),
        "pool": pool,
        "count": count,
        "workers": workers,
        "headless": bool(web_conf.get("headless", False)),
        "use_xvfb": bool(web_conf.get("use_xvfb", False)),
        "no_sandbox": bool(web_conf.get("no_sandbox", False)),
        "disable_dev_shm": bool(web_conf.get("disable_dev_shm", False)),
        "window_size": str(web_conf.get("window_size") or "1440,900"),
        "extract_numbers": bool(web_conf.get("extract_numbers", False)),
    }


def redact_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of state with secret-looking fields redacted."""
    return {
        key: ("***" if key in _SECRET_KEYS and value else value)
        for key, value in state.items()
    }


def _maintainer_available() -> bool:
    return importlib.util.find_spec("DrissionPage") is not None


def _job_dir() -> Path:
    path = data_path("maintainer", "web")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _runtime_config_path() -> Path:
    return _job_dir() / "maintainer.config.json"


def _read_runtime_config() -> dict[str, Any]:
    path = _runtime_config_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_runtime_config(payload: dict[str, Any]) -> Path:
    path = _runtime_config_path()
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def _output_path() -> Path:
    return _job_dir() / f"sso_{int(time.time())}.txt"


def _latest_log_file() -> Path | None:
    """Pick the most relevant run log for the UI's log tail.

    Prefers the latest parallel-orchestrator log (``run_parallel_*.log``) if
    one was touched within the last 10 minutes — that file records each
    worker's spawn pid and completion so operators can immediately confirm
    true concurrency. Falls back to the most recently modified ``run_*.log``
    otherwise (single-worker runs).
    """
    directory = log_path("maintainer")
    if not directory.exists():
        return None
    files = [path for path in directory.glob("run*.log") if path.is_file()]
    if not files:
        return None
    parallel = [p for p in files if p.name.startswith("run_parallel_")]
    if parallel:
        latest_parallel = max(parallel, key=lambda p: p.stat().st_mtime)
        if time.time() - latest_parallel.stat().st_mtime < 600:
            return latest_parallel
    return max(files, key=lambda path: path.stat().st_mtime, default=None)


def _log_tail(line_count: int = 80) -> list[str]:
    path = _latest_log_file()
    if path is None:
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-line_count:]
    except OSError:
        return []


def _env_for_request(req: MaintainerRunRequest, config_path: Path) -> dict[str, str]:
    env = {
        "GROK_MAINTAINER_CONFIG": str(config_path),
        "MAINTAINER_HEADLESS": "true" if req.headless else "false",
        "MAINTAINER_USE_XVFB": "true" if req.use_xvfb else "false",
        "MAINTAINER_NO_SANDBOX": "true" if req.no_sandbox else "false",
        "MAINTAINER_DISABLE_DEV_SHM": "true" if req.disable_dev_shm else "false",
    }
    if req.window_size:
        env["MAINTAINER_WINDOW_SIZE"] = req.window_size
    return env


def _run_sync(
    config_path: Path,
    output_path: Path,
    count: int,
    workers: int,
    extract_numbers: bool,
    env: dict[str, str],
    pause_event: Any,
    stop_event: Any,
    spawned_workers_callback: Callable[[int], None] | None = None,
    progress_callback: Callable[[int, str, dict[str, Any]], None] | None = None,
) -> list[str]:
    previous = {key: os.environ.get(key) for key in _ENV_KEYS}
    try:
        os.environ.update(env)
        from app.maintainer.runner import run_batch_parallel

        return run_batch_parallel(
            config_path=str(config_path),
            count=count,
            workers=workers,
            output=str(output_path),
            extract_numbers=extract_numbers,
            pause_event=pause_event,
            stop_event=stop_event,
            env_overrides=env if workers > 1 else None,
            spawned_workers_callback=spawned_workers_callback,
            progress_callback=progress_callback,
        )
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def _run_background(
    req: MaintainerRunRequest,
    config_path: Path,
    output_path: Path,
    env: dict[str, str],
    controller: _MaintainerController,
) -> None:
    def _record_spawned(n: int) -> None:
        _state["spawned_workers"] = int(n)

    def _record_progress(worker_id: int, event: str, payload: dict[str, Any]) -> None:
        """Update per-worker status the UI polls via /maintainer/status.

        Keeps a compact snapshot for each worker:

        - ``last_event``: most recent event name (``round_start``, ``round_done`` …)
        - ``last_round``: most recent round number observed for that worker
        - ``rounds_done``: count of successfully completed rounds
        - ``last_sso_tail``: last 4 chars of the most recent successful SSO
        - ``last_elapsed_s``: duration of the most recent round
        - ``last_event_at``: wall-clock ms when the event was observed

        The orchestrator thread invokes this on the worker callback path, so
        keep the work cheap and side-effect-free.
        """
        snap = dict(_state.get("per_worker_progress") or {})
        worker_key = str(int(worker_id))
        entry = dict(snap.get(worker_key) or {})
        entry["last_event"] = event
        entry["last_event_at"] = int(time.time() * 1000)
        if "round" in payload:
            entry["last_round"] = int(payload["round"])
        if event == "round_done":
            entry["rounds_done"] = int(entry.get("rounds_done", 0)) + 1
            if "sso_tail" in payload:
                entry["last_sso_tail"] = str(payload["sso_tail"])
            if "elapsed_s" in payload:
                entry["last_elapsed_s"] = float(payload["elapsed_s"])
        if event == "round_failed" and "error" in payload:
            entry["last_error"] = str(payload["error"])[:200]
        snap[worker_key] = entry
        _state["per_worker_progress"] = snap

    try:
        tokens = await asyncio.to_thread(
            _run_sync,
            config_path,
            output_path,
            req.count,
            req.workers,
            req.extract_numbers,
            env,
            controller.pause_event,
            controller.stop_event,
            _record_spawned,
            _record_progress,
        )
        stopped = controller.is_stopped()
        if stopped:
            status = "stopped"
            message = f"注册任务已停止，已采集 {len(tokens)} 个 token"
        else:
            status = "completed"
            message = f"注册任务完成，采集 {len(tokens)} 个 token"
        _state.update(
            {
                "running": False,
                "paused": False,
                "status": status,
                "message": message,
                "finished_at": int(time.time()),
                "token_count": len(tokens),
            }
        )
    except Exception as exc:
        logger.exception("maintainer web run failed")
        _state.update(
            {
                "running": False,
                "paused": False,
                "status": "failed",
                "message": f"{type(exc).__name__}: {exc}",
                "finished_at": int(time.time()),
            }
        )
    finally:
        controller.reset()


@router.get("/status")
async def maintainer_status():
    state = redact_state(dict(_state))
    state["available"] = _maintainer_available()
    state["log_tail"] = _log_tail()
    state["paused"] = bool(state.get("paused")) or _controller.is_paused()
    state["stop_requested"] = _controller.is_stopped()
    return state


@router.get("/config")
async def maintainer_config_get():
    return build_saved_config_response(_read_runtime_config())


@router.post("/config")
async def maintainer_config_save(req: MaintainerRunRequest, request: Request):
    admin_token = config.get_str("app.app_key", "")
    if not admin_token:
        raise ValidationError("Admin app key is empty", param="app.app_key")

    runtime_config = build_runtime_config(
        req,
        base_url=str(request.base_url),
        admin_token=admin_token,
        existing_config=_read_runtime_config(),
    )
    path = _write_runtime_config(runtime_config)
    response = build_saved_config_response(runtime_config)
    response["config_path"] = str(path)
    return response


@router.post("/run")
async def maintainer_run(req: MaintainerRunRequest, request: Request):
    global _task

    if not _maintainer_available():
        raise AppError(
            "Maintainer dependencies are not installed. Run `uv sync --extra maintainer`.",
            kind=ErrorKind.SERVER,
            code="maintainer_unavailable",
            status=503,
        )

    async with _lock:
        if _task is not None and not _task.done():
            raise AppError(
                "Maintainer task is already running",
                kind=ErrorKind.VALIDATION,
                code="maintainer_running",
                status=409,
            )

        admin_token = config.get_str("app.app_key", "")
        if not admin_token:
            raise ValidationError("Admin app key is empty", param="app.app_key")

        runtime_config = build_runtime_config(
            req,
            base_url=str(request.base_url),
            admin_token=admin_token,
            existing_config=_read_runtime_config(),
        )
        config_path = _write_runtime_config(runtime_config)
        output_path = _output_path()
        env = _env_for_request(req, config_path)

        _controller.reset()

        _state.update(
            {
                "running": True,
                "paused": False,
                "status": "running",
                "message": "注册任务已启动",
                "started_at": int(time.time()),
                "finished_at": None,
                "token_count": 0,
                "config_path": str(config_path),
                "output_path": str(output_path),
                "workers": req.workers,
                "spawned_workers": 0,
                "per_worker_progress": {},
            }
        )
        _task = asyncio.create_task(
            _run_background(req, config_path, output_path, env, _controller)
        )

    return redact_state(dict(_state))


def _require_running() -> None:
    if _task is None or _task.done():
        raise AppError(
            "Maintainer task is not running",
            kind=ErrorKind.VALIDATION,
            code="maintainer_not_running",
            status=409,
        )


@router.post("/pause")
async def maintainer_pause():
    async with _lock:
        _require_running()
        _controller.pause()
        _state["paused"] = True
        _state["status"] = "paused"
        _state["message"] = "注册任务已暂停，当前轮结束后不会启动新轮"
    return redact_state(dict(_state))


@router.post("/resume")
async def maintainer_resume():
    async with _lock:
        _require_running()
        _controller.resume()
        _state["paused"] = False
        _state["status"] = "running"
        _state["message"] = "注册任务已恢复"
    return redact_state(dict(_state))


@router.post("/stop")
async def maintainer_stop():
    async with _lock:
        _require_running()
        _controller.stop()
        _state["paused"] = False
        _state["status"] = "stopping"
        _state["message"] = "已发出停止信号，等待当前轮结束后退出"
    return redact_state(dict(_state))


__all__ = [
    "MaintainerRunRequest",
    "build_saved_config_response",
    "build_runtime_config",
    "redact_state",
    "router",
    "_MaintainerController",
]
