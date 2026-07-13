"""Canonical protocol format names used by grokManager."""

from .types import ProtocolFormat

OPENAI_CHAT_COMPLETIONS = ProtocolFormat("openai.chat.completions")
OPENAI_RESPONSES = ProtocolFormat("openai.responses")
ANTHROPIC_MESSAGES = ProtocolFormat("anthropic.messages")
CODEX_RESPONSES = ProtocolFormat("codex.responses")
XAI_CHAT = ProtocolFormat("xai.chat")
XAI_CONSOLE_RESPONSES = ProtocolFormat("xai.console.responses")
GROK_BUILD_RESPONSES = ProtocolFormat("grok.build.responses")

BUILTIN_FORMATS = (
    OPENAI_CHAT_COMPLETIONS,
    OPENAI_RESPONSES,
    ANTHROPIC_MESSAGES,
    CODEX_RESPONSES,
    XAI_CHAT,
    XAI_CONSOLE_RESPONSES,
    GROK_BUILD_RESPONSES,
)

__all__ = [
    "ANTHROPIC_MESSAGES",
    "BUILTIN_FORMATS",
    "CODEX_RESPONSES",
    "GROK_BUILD_RESPONSES",
    "OPENAI_CHAT_COMPLETIONS",
    "OPENAI_RESPONSES",
    "XAI_CHAT",
    "XAI_CONSOLE_RESPONSES",
]
