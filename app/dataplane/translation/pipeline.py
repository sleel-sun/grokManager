"""Middleware-aware orchestration around a protocol translation registry."""

import inspect
from collections.abc import Awaitable
from dataclasses import replace
from typing import TypeVar

from .registry import TranslationRegistry
from .types import (
    FormatLike,
    ProtocolFormat,
    RequestEnvelope,
    RequestHandler,
    RequestMiddleware,
    ResponseEnvelope,
    ResponseHandler,
    ResponseMiddleware,
    TranslationContext,
)

_T = TypeVar("_T")


async def _resolve_awaitable(value: _T | Awaitable[_T]) -> _T:
    if inspect.isawaitable(value):
        return await value
    return value


class TranslationPipeline:
    """Applies request/response middleware around registered transforms."""

    def __init__(self, registry: TranslationRegistry | None = None) -> None:
        self.registry = registry or TranslationRegistry()
        self._request_middleware: list[RequestMiddleware] = []
        self._response_middleware: list[ResponseMiddleware] = []

    def use_request(self, middleware: RequestMiddleware) -> "TranslationPipeline":
        if not callable(middleware):
            raise TypeError("request middleware must be callable")
        self._request_middleware.append(middleware)
        return self

    def use_response(self, middleware: ResponseMiddleware) -> "TranslationPipeline":
        if not callable(middleware):
            raise TypeError("response middleware must be callable")
        self._response_middleware.append(middleware)
        return self

    async def translate_request(
        self,
        source: FormatLike,
        target: FormatLike,
        request: RequestEnvelope,
        *,
        allow_identity: bool | None = None,
        allow_indirect: bool = False,
    ) -> RequestEnvelope:
        source_format = ProtocolFormat(source)
        target_format = ProtocolFormat(target)
        if request.format != source_format:
            raise ValueError(
                f"request envelope format {request.format!r} does not match "
                f"translation source {source_format!r}"
            )

        async def terminal(current: RequestEnvelope) -> RequestEnvelope:
            context = TranslationContext(
                source=source_format,
                target=target_format,
                model=current.model,
                streaming=current.stream,
                original_request=current.body,
                metadata=current.metadata,
            )
            body = await self.registry.translate_request(
                context,
                current.body,
                allow_identity=allow_identity,
                allow_indirect=allow_indirect,
            )
            return replace(current, format=target_format, body=body)

        handler = self._request_handler(terminal)
        result = await handler(request)
        if not isinstance(result, RequestEnvelope):
            raise TypeError("request middleware must return a RequestEnvelope")
        return result

    async def translate_response(
        self,
        source: FormatLike,
        target: FormatLike,
        response: ResponseEnvelope,
        *,
        allow_identity: bool | None = None,
        allow_indirect: bool = False,
    ) -> ResponseEnvelope:
        source_format = ProtocolFormat(source)
        target_format = ProtocolFormat(target)
        if response.format != source_format:
            raise ValueError(
                f"response envelope format {response.format!r} does not match "
                f"translation source {source_format!r}"
            )

        async def terminal(current: ResponseEnvelope) -> ResponseEnvelope:
            context = TranslationContext(
                source=source_format,
                target=target_format,
                model=current.model,
                streaming=current.stream,
                original_request=current.original_request,
                translated_request=current.translated_request,
                metadata=current.metadata,
            )
            if current.stream:
                body = await self.registry.translate_stream(
                    context,
                    current.body,
                    allow_identity=allow_identity,
                    allow_indirect=allow_indirect,
                )
            else:
                body = await self.registry.translate_nonstream(
                    context,
                    current.body,
                    allow_identity=allow_identity,
                    allow_indirect=allow_indirect,
                )
            return replace(current, format=target_format, body=body)

        handler = self._response_handler(terminal)
        result = await handler(response)
        if not isinstance(result, ResponseEnvelope):
            raise TypeError("response middleware must return a ResponseEnvelope")
        return result

    def _request_handler(self, terminal: RequestHandler) -> RequestHandler:
        handler = terminal
        for middleware in reversed(self._request_middleware):
            handler = self._wrap_request(middleware, handler)
        return handler

    def _response_handler(self, terminal: ResponseHandler) -> ResponseHandler:
        handler = terminal
        for middleware in reversed(self._response_middleware):
            handler = self._wrap_response(middleware, handler)
        return handler

    @staticmethod
    def _wrap_request(
        middleware: RequestMiddleware,
        call_next: RequestHandler,
    ) -> RequestHandler:
        async def handler(request: RequestEnvelope) -> RequestEnvelope:
            return await _resolve_awaitable(middleware(request, call_next))

        return handler

    @staticmethod
    def _wrap_response(
        middleware: ResponseMiddleware,
        call_next: ResponseHandler,
    ) -> ResponseHandler:
        async def handler(response: ResponseEnvelope) -> ResponseEnvelope:
            return await _resolve_awaitable(middleware(response, call_next))

        return handler


__all__ = ["TranslationPipeline"]
