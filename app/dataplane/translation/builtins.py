"""Explicit bootstrap for built-in protocol transforms."""

from .formats import (
    ANTHROPIC_MESSAGES,
    CODEX_RESPONSES,
    GROK_BUILD_RESPONSES,
    OPENAI_CHAT_COMPLETIONS,
    OPENAI_RESPONSES,
)
from .registry import TranslationRegistry
from .transforms import (
    translate_anthropic_messages_request,
    translate_grok_build_nonstream_response,
    translate_grok_build_request,
    translate_grok_build_stream_response,
    translate_openai_responses_request,
)


def register_builtin_transforms(registry: TranslationRegistry) -> None:
    """Register product-independent transforms on ``registry`` once."""
    request_routes = (
        (
            OPENAI_RESPONSES,
            OPENAI_CHAT_COMPLETIONS,
            translate_openai_responses_request,
        ),
        (
            ANTHROPIC_MESSAGES,
            OPENAI_CHAT_COMPLETIONS,
            translate_anthropic_messages_request,
        ),
        (OPENAI_RESPONSES, GROK_BUILD_RESPONSES, translate_grok_build_request),
        (CODEX_RESPONSES, GROK_BUILD_RESPONSES, translate_grok_build_request),
    )
    for source, target, transform in request_routes:
        if not registry.has("request", source, target):
            registry.register_request(source, target, transform)

    for target in (OPENAI_RESPONSES, CODEX_RESPONSES):
        if not registry.has("nonstream", GROK_BUILD_RESPONSES, target):
            registry.register_nonstream(
                GROK_BUILD_RESPONSES,
                target,
                translate_grok_build_nonstream_response,
            )
        if not registry.has("stream", GROK_BUILD_RESPONSES, target):
            registry.register_stream(
                GROK_BUILD_RESPONSES,
                target,
                translate_grok_build_stream_response,
            )


__all__ = ["register_builtin_transforms"]
