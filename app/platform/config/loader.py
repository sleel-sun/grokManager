"""TOML configuration loader with environment-variable override support."""

import os
from pathlib import Path
from typing import Any

import tomllib


_ENV_PATH_ALIASES: dict[str, tuple[str, ...]] = {
    "GROK_PROXY_EGRESS_MODE": ("proxy", "egress", "mode"),
    "GROK_PROXY_EGRESS_PROXY_URL": ("proxy", "egress", "proxy_url"),
    "GROK_PROXY_EGRESS_PROXY_POOL": ("proxy", "egress", "proxy_pool"),
    "GROK_PROXY_EGRESS_RESOURCE_PROXY_URL": ("proxy", "egress", "resource_proxy_url"),
    "GROK_PROXY_EGRESS_RESOURCE_PROXY_POOL": ("proxy", "egress", "resource_proxy_pool"),
    "GROK_PROXY_EGRESS_SKIP_SSL_VERIFY": ("proxy", "egress", "skip_ssl_verify"),
    "GROK_PROXY_CLEARANCE_MODE": ("proxy", "clearance", "mode"),
    "GROK_PROXY_CLEARANCE_CF_COOKIES": ("proxy", "clearance", "cf_cookies"),
    "GROK_PROXY_CLEARANCE_USER_AGENT": ("proxy", "clearance", "user_agent"),
    "GROK_PROXY_CLEARANCE_BROWSER": ("proxy", "clearance", "browser"),
    "GROK_PROXY_CLEARANCE_FLARESOLVERR_URL": ("proxy", "clearance", "flaresolverr_url"),
    "GROK_PROXY_CLEARANCE_TIMEOUT_SEC": ("proxy", "clearance", "timeout_sec"),
    "GROK_PROXY_CLEARANCE_REFRESH_INTERVAL": ("proxy", "clearance", "refresh_interval"),
}

_LEGACY_ENV_PATH_ALIASES: dict[str, tuple[tuple[str, ...], ...]] = {
    "FLARESOLVERR_URL": (
        ("proxy", "clearance", "mode"),
        ("proxy", "clearance", "flaresolverr_url"),
    ),
    "CF_REFRESH_INTERVAL": (("proxy", "clearance", "refresh_interval"),),
    "CF_TIMEOUT": (("proxy", "clearance", "timeout_sec"),),
}


def _flatten(mapping: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a nested dict into dotted keys."""
    out: dict[str, Any] = {}
    for k, v in mapping.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, full))
        else:
            out[full] = v
    return out


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into *base* (non-destructive)."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _set_nested(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = data
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[path[-1]] = value


def apply_env_overrides(
    data: dict[str, Any],
    *,
    env: dict[str, str] | None = None,
    env_prefix: str = "GROK_",
) -> dict[str, Any]:
    """Apply environment overrides, including explicit nested proxy aliases."""
    environ = os.environ if env is None else env

    for env_key, paths in _LEGACY_ENV_PATH_ALIASES.items():
        env_val = environ.get(env_key)
        if env_val is None:
            continue
        for path in paths:
            value = "flaresolverr" if env_key == "FLARESOLVERR_URL" and path[-1] == "mode" else env_val
            _set_nested(data, path, value)

    for env_key, path in _ENV_PATH_ALIASES.items():
        env_val = environ.get(env_key)
        if env_val is not None:
            _set_nested(data, path, env_val)

    prefix_len = len(env_prefix)
    for env_key, env_val in environ.items():
        if not env_key.startswith(env_prefix) or env_key in _ENV_PATH_ALIASES:
            continue
        parts = env_key[prefix_len:].lower().split("_", 1)
        if len(parts) == 2:
            section, key = parts
            data.setdefault(section, {})[key] = env_val
    return data


def load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML file and return the raw nested dict."""
    if not path.exists():
        return {}
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def load_config(
    defaults_path: Path,
    user_path: Path | None = None,
    env_prefix: str = "GROK_",
) -> dict[str, Any]:
    """Load configuration: defaults → user file → environment overrides.

    Environment variables use the format ``GROK_SECTION_KEY=value``,
    which maps to the dotted key ``section.key``.
    """
    data = load_toml(defaults_path)
    if user_path and user_path.exists():
        user = load_toml(user_path)
        data = _deep_merge(data, user)

    data = apply_env_overrides(data, env_prefix=env_prefix)

    return data


def get_nested(data: dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    """Retrieve a value from a nested dict using a dotted key path."""
    keys = dotted_key.split(".")
    node: Any = data
    for k in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(k)
        if node is None:
            return default
    return node
