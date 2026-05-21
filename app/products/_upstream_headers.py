"""Helpers to surface upstream routing details as HTTP response headers.

Lets clients see which upstream profile, model name, and endpoint URL the
relay actually used — useful for debugging cases where the model's self-
reported identity (`I am grok-1.5`) does not match the requested model
(`grok-4.3`). LLMs are unreliable at self-identification; this header
provides the deterministic ground truth.
"""

from __future__ import annotations

from app.control.model.spec import ModelSpec
from app.dataplane.reverse.runtime.endpoint_table import (
    CHAT,
    CONSOLE_RESPONSES,
    WS_IMAGINE,
)


def _endpoint_for(spec: ModelSpec) -> str:
    if spec.uses_console_responses():
        return CONSOLE_RESPONSES
    if spec.is_image():
        return WS_IMAGINE
    return CHAT


def build_upstream_response_headers(spec: ModelSpec) -> dict[str, str]:
    """Return ``X-Upstream-*`` headers describing how the relay routed *spec*."""
    return {
        "X-Upstream-Profile": spec.upstream_profile,
        "X-Upstream-Model": spec.upstream_model_name(),
        "X-Upstream-Endpoint": _endpoint_for(spec),
    }


__all__ = ["build_upstream_response_headers"]
