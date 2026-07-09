"""Anthropic client compatibility helpers."""

from __future__ import annotations

from app.control.model import registry as model_registry
from app.control.model.spec import ModelSpec


CLAUDE_CODE_ALIAS_TARGET = "grok-4.3"

# Claude Code and Anthropic SDK users often keep the default Claude model IDs
# even when pointing at a compatible gateway. Accept those IDs and route them
# to the strongest broadly available chat model in this deployment.
CLAUDE_MODEL_ALIASES: tuple[tuple[str, str], ...] = (
    ("claude-sonnet-4-5-20250929", "Claude Sonnet 4.5"),
    ("claude-sonnet-4-20250514", "Claude Sonnet 4"),
    ("claude-opus-4-1-20250805", "Claude Opus 4.1"),
    ("claude-opus-4-20250514", "Claude Opus 4"),
    ("claude-3-7-sonnet-20250219", "Claude 3.7 Sonnet"),
    ("claude-3-5-sonnet-20241022", "Claude 3.5 Sonnet"),
    ("claude-3-5-haiku-20241022", "Claude 3.5 Haiku"),
)

_ALIAS_DISPLAY_BY_ID = dict(CLAUDE_MODEL_ALIASES)


def is_claude_model_alias(model_id: str) -> bool:
    """Return whether *model_id* should be treated as a Claude-compatible alias."""
    return str(model_id or "").strip().startswith("claude-")


def claude_alias_display_name(model_id: str) -> str:
    """Return a human-readable display name for a Claude-compatible alias."""
    model_id = str(model_id or "").strip()
    if model_id in _ALIAS_DISPLAY_BY_ID:
        return _ALIAS_DISPLAY_BY_ID[model_id]
    return model_id.replace("-", " ").title()


def resolve_anthropic_model_spec(model_id: str) -> ModelSpec | None:
    """Resolve real Grok model IDs plus Claude-compatible alias IDs."""
    spec = model_registry.get(model_id)
    if spec is not None:
        return spec
    if is_claude_model_alias(model_id):
        return model_registry.get(CLAUDE_CODE_ALIAS_TARGET)
    return None


__all__ = [
    "CLAUDE_CODE_ALIAS_TARGET",
    "CLAUDE_MODEL_ALIASES",
    "claude_alias_display_name",
    "is_claude_model_alias",
    "resolve_anthropic_model_spec",
]
