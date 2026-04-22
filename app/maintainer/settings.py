"""Shared config and path helpers for the merged maintainer tool."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.platform.paths import data_path, log_path


_CONFIG_ENV = "GROK_MAINTAINER_CONFIG"
_PACKAGE_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _PACKAGE_DIR.parents[1]


def default_config_path() -> Path:
    return _ROOT_DIR / "maintainer.config.json"


def example_config_path() -> Path:
    return _ROOT_DIR / "maintainer.config.example.json"


def get_config_path() -> Path:
    raw = os.getenv(_CONFIG_ENV, "").strip()
    if not raw:
        return default_config_path()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = _ROOT_DIR / path
    return path.resolve()


def set_config_path(path_like: str | os.PathLike[str] | None) -> Path:
    if not path_like:
        return get_config_path()
    path = Path(path_like).expanduser()
    if not path.is_absolute():
        path = _ROOT_DIR / path
    path = path.resolve()
    os.environ[_CONFIG_ENV] = str(path)
    return path


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise RuntimeError(f"配置文件格式错误，顶层必须是对象: {path}")
    return data


def load_config() -> dict[str, Any]:
    return load_json(get_config_path())


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def pick_conf(
    root: dict[str, Any],
    section: str,
    key: str,
    *legacy_keys: str,
    default: Any = None,
) -> Any:
    sec = root.get(section)
    if not isinstance(sec, dict):
        sec = {}

    value = sec.get(key)
    if value is None:
        for legacy_key in legacy_keys:
            value = sec.get(legacy_key)
            if value is not None:
                break
    if value is not None:
        return value

    value = root.get(key)
    if value is None:
        for legacy_key in legacy_keys:
            value = root.get(legacy_key)
            if value is not None:
                break
    if value is not None:
        return value
    return default


def maintainer_log_dir() -> Path:
    return log_path("maintainer")


def maintainer_sso_dir() -> Path:
    return data_path("maintainer", "sso")


def extension_dir() -> Path:
    return _PACKAGE_DIR / "turnstilePatch"


def project_root() -> Path:
    return _ROOT_DIR


__all__ = [
    "default_config_path",
    "example_config_path",
    "extension_dir",
    "get_config_path",
    "as_bool",
    "load_config",
    "load_json",
    "maintainer_log_dir",
    "maintainer_sso_dir",
    "pick_conf",
    "project_root",
    "set_config_path",
]
