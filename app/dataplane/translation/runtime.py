"""Process-wide protocol translation registry and pipeline."""

from .pipeline import TranslationPipeline
from .registry import TranslationRegistry
from .builtins import register_builtin_transforms

_registry = TranslationRegistry()
register_builtin_transforms(_registry)
_pipeline = TranslationPipeline(_registry)


def get_translation_registry() -> TranslationRegistry:
    """Return the shared registry used by product and upstream boundaries."""
    return _registry


def get_translation_pipeline() -> TranslationPipeline:
    """Return the shared middleware-aware translation pipeline."""
    return _pipeline


__all__ = ["get_translation_pipeline", "get_translation_registry"]
