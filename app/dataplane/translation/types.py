"""Shared types for protocol translation registries and pipelines."""

from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeAlias


class ProtocolFormat(str):
    """Stable, extensible name for a request or response wire format."""

    def __new__(cls, name: str) -> "ProtocolFormat":
        if not isinstance(name, str):
            raise TypeError("protocol format name must be a string")
        name = name.strip()
        if not name:
            raise ValueError("protocol format name must not be empty")
        return super().__new__(cls, name)

    @property
    def name(self) -> str:
        return str(self)


FormatLike: TypeAlias = ProtocolFormat | str
Payload: TypeAlias = Any
StreamPayload: TypeAlias = AsyncIterable[Payload]


@dataclass(frozen=True, slots=True)
class TranslationContext:
    """Metadata shared by one translation operation."""

    source: ProtocolFormat
    target: ProtocolFormat
    model: str = ""
    streaming: bool = False
    original_request: Payload = None
    translated_request: Payload = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RequestEnvelope:
    """Request body and routing metadata passed through a pipeline."""

    format: ProtocolFormat
    body: Payload
    model: str = ""
    stream: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.format = ProtocolFormat(self.format)


@dataclass(slots=True)
class ResponseEnvelope:
    """Response body and routing metadata passed through a pipeline."""

    format: ProtocolFormat
    body: Payload
    model: str = ""
    stream: bool = False
    original_request: Payload = None
    translated_request: Payload = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.format = ProtocolFormat(self.format)


class RequestTransform(Protocol):
    def __call__(
        self,
        body: Payload,
        context: TranslationContext,
    ) -> Payload | Awaitable[Payload]: ...


class NonStreamTransform(Protocol):
    def __call__(
        self,
        body: Payload,
        context: TranslationContext,
    ) -> Payload | Awaitable[Payload]: ...


class StreamTransform(Protocol):
    def __call__(
        self,
        body: StreamPayload,
        context: TranslationContext,
    ) -> StreamPayload | Awaitable[StreamPayload]: ...


RequestHandler: TypeAlias = Callable[[RequestEnvelope], Awaitable[RequestEnvelope]]
ResponseHandler: TypeAlias = Callable[[ResponseEnvelope], Awaitable[ResponseEnvelope]]


class RequestMiddleware(Protocol):
    def __call__(
        self,
        request: RequestEnvelope,
        call_next: RequestHandler,
    ) -> RequestEnvelope | Awaitable[RequestEnvelope]: ...


class ResponseMiddleware(Protocol):
    def __call__(
        self,
        response: ResponseEnvelope,
        call_next: ResponseHandler,
    ) -> ResponseEnvelope | Awaitable[ResponseEnvelope]: ...


__all__ = [
    "FormatLike",
    "NonStreamTransform",
    "Payload",
    "ProtocolFormat",
    "RequestEnvelope",
    "RequestHandler",
    "RequestMiddleware",
    "RequestTransform",
    "ResponseEnvelope",
    "ResponseHandler",
    "ResponseMiddleware",
    "StreamPayload",
    "StreamTransform",
    "TranslationContext",
]
