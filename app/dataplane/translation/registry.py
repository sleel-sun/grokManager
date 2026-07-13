"""Strict registry for protocol request and response transformations."""

import inspect
from collections import deque
from collections.abc import AsyncIterable, Awaitable
from dataclasses import replace
from typing import Literal, TypeAlias, TypeVar, cast

from .types import (
    FormatLike,
    NonStreamTransform,
    Payload,
    ProtocolFormat,
    RequestTransform,
    StreamPayload,
    StreamTransform,
    TranslationContext,
)

TransformKind: TypeAlias = Literal["request", "nonstream", "stream"]
Transform = RequestTransform | NonStreamTransform | StreamTransform
_T = TypeVar("_T")


class MissingTransformError(LookupError):
    """Raised when no transform exists and identity fallback is disabled."""

    def __init__(
        self,
        kind: TransformKind,
        source: ProtocolFormat,
        target: ProtocolFormat,
    ) -> None:
        self.kind = kind
        self.source = source
        self.target = target
        super().__init__(
            f"no {kind} protocol transform registered for {source!r} -> {target!r}"
        )


class DuplicateTransformError(ValueError):
    """Raised when registration would silently replace a transform."""


class AmbiguousTransformPathError(LookupError):
    """Raised when more than one shortest indirect transform path exists."""

    def __init__(
        self,
        kind: TransformKind,
        source: ProtocolFormat,
        target: ProtocolFormat,
        paths: tuple[tuple[ProtocolFormat, ...], ...],
    ) -> None:
        self.kind = kind
        self.source = source
        self.target = target
        self.paths = paths
        rendered_paths = ", ".join(" -> ".join(path) for path in paths)
        super().__init__(
            f"ambiguous {kind} protocol transform path for {source!r} -> "
            f"{target!r}: {rendered_paths}"
        )


async def _resolve_awaitable(value: _T | Awaitable[_T]) -> _T:
    if inspect.isawaitable(value):
        return await value
    return value


class TranslationRegistry:
    """Stores transforms keyed by source format, target format, and kind."""

    def __init__(self, *, allow_identity: bool = False) -> None:
        self.allow_identity = allow_identity
        self._transforms: dict[
            TransformKind,
            dict[tuple[ProtocolFormat, ProtocolFormat], Transform],
        ] = {
            "request": {},
            "nonstream": {},
            "stream": {},
        }

    def register_request(
        self,
        source: FormatLike,
        target: FormatLike,
        transform: RequestTransform,
        *,
        replace: bool = False,
    ) -> None:
        self._register("request", source, target, transform, replace=replace)

    def register_nonstream(
        self,
        source: FormatLike,
        target: FormatLike,
        transform: NonStreamTransform,
        *,
        replace: bool = False,
    ) -> None:
        self._register("nonstream", source, target, transform, replace=replace)

    def register_stream(
        self,
        source: FormatLike,
        target: FormatLike,
        transform: StreamTransform,
        *,
        replace: bool = False,
    ) -> None:
        self._register("stream", source, target, transform, replace=replace)

    def register(
        self,
        source: FormatLike,
        target: FormatLike,
        *,
        request: RequestTransform | None = None,
        nonstream: NonStreamTransform | None = None,
        stream: StreamTransform | None = None,
        replace: bool = False,
    ) -> None:
        """Register any combination of transforms for one format pair."""
        if request is None and nonstream is None and stream is None:
            raise ValueError("at least one protocol transform is required")
        key = self._key(source, target)
        pending = {
            kind: transform
            for kind, transform in (
                ("request", request),
                ("nonstream", nonstream),
                ("stream", stream),
            )
            if transform is not None
        }
        for kind, transform in pending.items():
            if not callable(transform):
                raise TypeError("protocol transform must be callable")
            if key in self._transforms[kind] and not replace:
                raise DuplicateTransformError(
                    f"{kind} protocol transform already registered for "
                    f"{key[0]!r} -> {key[1]!r}"
                )
        for kind, transform in pending.items():
            self._transforms[kind][key] = transform

    def has(
        self,
        kind: TransformKind,
        source: FormatLike,
        target: FormatLike,
    ) -> bool:
        return self._key(source, target) in self._transforms[kind]

    def routes(
        self, kind: TransformKind
    ) -> tuple[tuple[ProtocolFormat, ProtocolFormat], ...]:
        """Return registered routes in insertion order."""
        return tuple(self._transforms[kind])

    def resolve_path(
        self,
        kind: TransformKind,
        source: FormatLike,
        target: FormatLike,
        *,
        allow_identity: bool | None = None,
        allow_indirect: bool = False,
    ) -> tuple[ProtocolFormat, ...]:
        """Resolve a direct route or the unique shortest indirect route."""
        source_format, target_format = self._key(source, target)
        transforms = self._transforms[kind]
        if (source_format, target_format) in transforms:
            return source_format, target_format

        identity_enabled = (
            self.allow_identity if allow_identity is None else allow_identity
        )
        if identity_enabled and source_format == target_format:
            return (source_format,)
        if not allow_indirect:
            raise MissingTransformError(kind, source_format, target_format)

        adjacency: dict[ProtocolFormat, list[ProtocolFormat]] = {}
        for route_source, route_target in transforms:
            adjacency.setdefault(route_source, []).append(route_target)

        queue = deque([(source_format,)])
        best_depth: dict[ProtocolFormat, int] = {source_format: 0}
        shortest_paths: list[tuple[ProtocolFormat, ...]] = []
        shortest_depth: int | None = None
        while queue:
            path = queue.popleft()
            depth = len(path) - 1
            if shortest_depth is not None and depth >= shortest_depth:
                continue
            for next_format in adjacency.get(path[-1], ()):
                if next_format in path:
                    continue
                next_path = (*path, next_format)
                next_depth = depth + 1
                if next_format == target_format:
                    shortest_depth = next_depth
                    shortest_paths.append(next_path)
                    continue
                known_depth = best_depth.get(next_format)
                if known_depth is None or next_depth <= known_depth:
                    best_depth[next_format] = next_depth
                    queue.append(next_path)

        if not shortest_paths:
            raise MissingTransformError(kind, source_format, target_format)
        if len(shortest_paths) > 1:
            raise AmbiguousTransformPathError(
                kind,
                source_format,
                target_format,
                tuple(shortest_paths),
            )
        return shortest_paths[0]

    async def translate_request(
        self,
        context: TranslationContext,
        body: Payload,
        *,
        allow_identity: bool | None = None,
        allow_indirect: bool = False,
    ) -> Payload:
        path = self.resolve_path(
            "request",
            context.source,
            context.target,
            allow_identity=allow_identity,
            allow_indirect=allow_indirect,
        )
        result = body
        for source, target in zip(path, path[1:]):
            transform = cast(
                RequestTransform,
                self._transforms["request"][(source, target)],
            )
            hop_context = replace(context, source=source, target=target)
            result = await _resolve_awaitable(transform(result, hop_context))
        return result

    async def translate_nonstream(
        self,
        context: TranslationContext,
        body: Payload,
        *,
        allow_identity: bool | None = None,
        allow_indirect: bool = False,
    ) -> Payload:
        path = self.resolve_path(
            "nonstream",
            context.source,
            context.target,
            allow_identity=allow_identity,
            allow_indirect=allow_indirect,
        )
        result = body
        for source, target in zip(path, path[1:]):
            transform = cast(
                NonStreamTransform,
                self._transforms["nonstream"][(source, target)],
            )
            hop_context = replace(context, source=source, target=target)
            result = await _resolve_awaitable(transform(result, hop_context))
        return result

    async def translate_stream(
        self,
        context: TranslationContext,
        body: StreamPayload,
        *,
        allow_identity: bool | None = None,
        allow_indirect: bool = False,
    ) -> StreamPayload:
        if not isinstance(body, AsyncIterable):
            raise TypeError("stream response body must be an AsyncIterable")
        path = self.resolve_path(
            "stream",
            context.source,
            context.target,
            allow_identity=allow_identity,
            allow_indirect=allow_indirect,
        )
        result = body
        for source, target in zip(path, path[1:]):
            transform = cast(
                StreamTransform,
                self._transforms["stream"][(source, target)],
            )
            hop_context = replace(context, source=source, target=target)
            result = await _resolve_awaitable(transform(result, hop_context))
            if not isinstance(result, AsyncIterable):
                raise TypeError(
                    "stream protocol transform must return an AsyncIterable"
                )
        return result

    def _register(
        self,
        kind: TransformKind,
        source: FormatLike,
        target: FormatLike,
        transform: Transform,
        *,
        replace: bool,
    ) -> None:
        if not callable(transform):
            raise TypeError("protocol transform must be callable")
        key = self._key(source, target)
        transforms = self._transforms[kind]
        if key in transforms and not replace:
            raise DuplicateTransformError(
                f"{kind} protocol transform already registered for "
                f"{key[0]!r} -> {key[1]!r}"
            )
        transforms[key] = transform

    @staticmethod
    def _key(
        source: FormatLike,
        target: FormatLike,
    ) -> tuple[ProtocolFormat, ProtocolFormat]:
        return ProtocolFormat(source), ProtocolFormat(target)


__all__ = [
    "AmbiguousTransformPathError",
    "DuplicateTransformError",
    "MissingTransformError",
    "TransformKind",
    "TranslationRegistry",
]
