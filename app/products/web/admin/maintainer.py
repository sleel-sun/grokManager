"""Admin Account Maintainer endpoints."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Literal

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
    count: int = Field(default=1, ge=0)
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
    "status": "idle",
    "message": "",
    "started_at": None,
    "finished_at": None,
    "total_count": 0,
    "completed_count": 0,
    "remaining_count": 0,
    "current_round": 0,
    "progress_percent": 0,
    "token_count": 0,
    "config_path": "",
    "output_path": "",
}
_task: asyncio.Task | None = None
_lock = asyncio.Lock()


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
        "run": {"count": req.count},
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
        raw_count = run_conf.get("count", 1)
        count = int(1 if raw_count in (None, "") else raw_count)
    except (TypeError, ValueError):
        count = 1
    count = max(count, 0)

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


def build_progress_fields(
    *,
    total_count: int,
    completed_count: int = 0,
    token_count: int = 0,
    current_round: int = 0,
) -> dict[str, int]:
    """Normalize batch progress fields for status responses."""
    total = max(0, int(total_count or 0))
    completed = max(0, int(completed_count or 0))
    if total:
        completed = min(completed, total)
    remaining = max(total - completed, 0) if total else 0
    current = max(0, int(current_round or 0))
    if total:
        current = min(current, total)
    percent = int((completed / total) * 100) if total else 0
    return {
        "total_count": total,
        "completed_count": completed,
        "remaining_count": remaining,
        "current_round": current,
        "progress_percent": percent,
        "token_count": max(0, int(token_count or 0)),
    }


def _progress_message(event: dict[str, Any], progress: dict[str, int]) -> str:
    total = progress["total_count"]
    completed = progress["completed_count"]
    remaining = progress["remaining_count"]
    current = progress["current_round"]
    token_count = progress["token_count"]
    name = str(event.get("event") or "")
    total_label = str(total) if total else "?"
    if name == "round_started":
        return f"正在注册第 {current}/{total_label} 个，已完成 {completed} 个，剩余 {remaining} 个"
    if name == "round_finished":
        if event.get("success"):
            return f"第 {current}/{total_label} 个注册完成，已完成 {completed} 个，剩余 {remaining} 个，已采集 {token_count} 个 token"
        return f"第 {current}/{total_label} 个注册失败，已处理 {completed} 个，剩余 {remaining} 个，已采集 {token_count} 个 token"
    if name == "batch_finished":
        return f"注册任务收尾中，已处理 {completed} 个，剩余 {remaining} 个，已采集 {token_count} 个 token"
    return f"注册任务已启动，共 {total_label} 个"


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
    directory = log_path("maintainer")
    if not directory.exists():
        return None
    files = [path for path in directory.glob("run_*.log") if path.is_file()]
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
    extract_numbers: bool,
    env: dict[str, str],
    progress_callback: Any = None,
) -> list[str]:
    previous = {key: os.environ.get(key) for key in _ENV_KEYS}
    try:
        os.environ.update(env)
        from app.maintainer.runner import run_batch

        return run_batch(
            config_path=str(config_path),
            count=count,
            output=str(output_path),
            extract_numbers=extract_numbers,
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
) -> None:
    def _on_progress(event: dict[str, Any]) -> None:
        progress = build_progress_fields(
            total_count=int(event.get("total_count") or req.count),
            completed_count=int(event.get("completed_count") or 0),
            token_count=int(event.get("token_count") or 0),
            current_round=int(event.get("current_round") or 0),
        )
        _state.update(
            {
                **progress,
                "message": _progress_message(event, progress),
            }
        )

    try:
        tokens = await asyncio.to_thread(
            _run_sync,
            config_path,
            output_path,
            req.count,
            req.extract_numbers,
            env,
            _on_progress,
        )
        progress = build_progress_fields(
            total_count=req.count,
            completed_count=req.count,
            token_count=len(tokens),
            current_round=req.count,
        )
        _state.update(
            {
                "running": False,
                "status": "completed",
                "message": f"注册任务完成，采集 {len(tokens)} 个 token",
                "finished_at": int(time.time()),
                **progress,
            }
        )
    except Exception as exc:
        logger.exception("maintainer web run failed")
        _state.update(
            {
                "running": False,
                "status": "failed",
                "message": f"{type(exc).__name__}: {exc}",
                "finished_at": int(time.time()),
            }
        )


@router.get("/status")
async def maintainer_status():
    state = redact_state(dict(_state))
    state["available"] = _maintainer_available()
    state["log_tail"] = _log_tail()
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

        _state.update(
            {
                "running": True,
                "status": "running",
                "message": "注册任务已启动",
                "started_at": int(time.time()),
                "finished_at": None,
                **build_progress_fields(
                    total_count=req.count,
                    completed_count=0,
                    token_count=0,
                    current_round=0,
                ),
                "token_count": 0,
                "config_path": str(config_path),
                "output_path": str(output_path),
            }
        )
        _task = asyncio.create_task(_run_background(req, config_path, output_path, env))

    return redact_state(dict(_state))


__all__ = [
    "MaintainerRunRequest",
    "build_progress_fields",
    "build_saved_config_response",
    "build_runtime_config",
    "redact_state",
    "router",
]
