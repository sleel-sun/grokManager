import asyncio
import unittest
from dataclasses import replace

from app.dataplane.translation import (
    AmbiguousTransformPathError,
    BUILTIN_FORMATS,
    GROK_BUILD_RESPONSES,
    OPENAI_RESPONSES,
    DuplicateTransformError,
    MissingTransformError,
    ProtocolFormat,
    RequestEnvelope,
    ResponseEnvelope,
    TranslationPipeline,
    TranslationRegistry,
    get_translation_pipeline,
    get_translation_registry,
    register_builtin_transforms,
)


async def _collect(stream) -> list:
    return [chunk async for chunk in stream]


async def _chunks(*values):
    for value in values:
        yield value


class ProtocolFormatTests(unittest.TestCase):
    def test_format_is_a_named_string_value(self) -> None:
        format_name = ProtocolFormat(" openai.responses ")

        self.assertEqual(format_name, "openai.responses")
        self.assertEqual(format_name.name, "openai.responses")
        with self.assertRaises(ValueError):
            ProtocolFormat("  ")

    def test_builtin_formats_are_named_and_unique(self) -> None:
        self.assertEqual(OPENAI_RESPONSES.name, "openai.responses")
        self.assertEqual(GROK_BUILD_RESPONSES.name, "grok.build.responses")
        self.assertEqual(len(BUILTIN_FORMATS), len(set(BUILTIN_FORMATS)))


class TranslationRegistryTests(unittest.TestCase):
    def test_runtime_pipeline_and_registry_share_state(self) -> None:
        self.assertIs(get_translation_pipeline().registry, get_translation_registry())

    def test_registry_keys_each_transform_kind_by_direction(self) -> None:
        registry = TranslationRegistry()
        registry.register(
            "openai.responses",
            "grok.build",
            request=lambda body, _context: {**body, "request": True},
            nonstream=lambda body, _context: {**body, "response": True},
        )

        request_context = self._context("openai.responses", "grok.build")
        response_context = self._context("openai.responses", "grok.build")

        request = asyncio.run(registry.translate_request(request_context, {}))
        response = asyncio.run(registry.translate_nonstream(response_context, {}))

        self.assertEqual(request, {"request": True})
        self.assertEqual(response, {"response": True})
        self.assertTrue(registry.has("request", "openai.responses", "grok.build"))
        self.assertFalse(registry.has("request", "grok.build", "openai.responses"))

    def test_identity_only_allows_same_format_passthrough(self) -> None:
        registry = TranslationRegistry()
        context = self._context("openai.chat", "anthropic.messages")

        with self.assertRaises(MissingTransformError) as raised:
            asyncio.run(registry.translate_request(context, {"input": "hello"}))

        self.assertEqual(raised.exception.kind, "request")
        self.assertEqual(raised.exception.source, "openai.chat")
        with self.assertRaises(MissingTransformError):
            asyncio.run(
                registry.translate_request(
                    context,
                    {"input": "hello"},
                    allow_identity=True,
                )
            )

        self.assertEqual(
            asyncio.run(
                registry.translate_request(
                    self._context("openai.chat", "openai.chat"),
                    {"input": "hello"},
                    allow_identity=True,
                )
            ),
            {"input": "hello"},
        )

    def test_duplicate_registration_requires_replace(self) -> None:
        registry = TranslationRegistry()
        registry.register_request("a", "b", lambda body, _context: body)

        with self.assertRaises(DuplicateTransformError):
            registry.register_request("a", "b", lambda body, _context: body)

        registry.register_request(
            "a",
            "b",
            lambda _body, _context: "replacement",
            replace=True,
        )
        result = asyncio.run(
            registry.translate_request(self._context("a", "b"), "original")
        )
        self.assertEqual(result, "replacement")

    def test_multi_kind_registration_is_atomic(self) -> None:
        registry = TranslationRegistry()
        registry.register_nonstream("a", "b", lambda body, _context: body)

        with self.assertRaises(DuplicateTransformError):
            registry.register(
                "a",
                "b",
                request=lambda body, _context: body,
                nonstream=lambda body, _context: body,
            )

        self.assertFalse(registry.has("request", "a", "b"))

    def test_indirect_translation_is_explicitly_enabled(self) -> None:
        registry = TranslationRegistry()
        contexts = []

        def first(body, context):
            contexts.append(context)
            return body + ["canonical"]

        async def second(body, context):
            contexts.append(context)
            await asyncio.sleep(0)
            return body + ["target"]

        registry.register_request("source", "canonical", first)
        registry.register_request("canonical", "target", second)
        context = self._context("source", "target")

        with self.assertRaises(MissingTransformError):
            asyncio.run(registry.translate_request(context, []))

        result = asyncio.run(
            registry.translate_request(context, [], allow_indirect=True)
        )

        self.assertEqual(result, ["canonical", "target"])
        self.assertEqual(
            [(item.source, item.target) for item in contexts],
            [("source", "canonical"), ("canonical", "target")],
        )

    def test_resolve_path_prefers_direct_then_unique_shortest_route(self) -> None:
        registry = TranslationRegistry()

        def transform(body, _context):
            return body

        registry.register_request("source", "long-a", transform)
        registry.register_request("long-a", "long-b", transform)
        registry.register_request("long-b", "target", transform)
        registry.register_request("source", "short", transform)
        registry.register_request("short", "target", transform)

        self.assertEqual(
            registry.resolve_path(
                "request", "source", "target", allow_indirect=True
            ),
            ("source", "short", "target"),
        )

        registry.register_request("source", "target", transform)
        registry.register_request("source", "other", transform)
        registry.register_request("other", "target", transform)
        self.assertEqual(
            registry.resolve_path(
                "request", "source", "target", allow_indirect=True
            ),
            ("source", "target"),
        )

    def test_ambiguous_shortest_indirect_path_is_rejected(self) -> None:
        registry = TranslationRegistry()

        def transform(body, _context):
            return body

        registry.register_request("source", "left", transform)
        registry.register_request("left", "target", transform)
        registry.register_request("source", "right", transform)
        registry.register_request("right", "target", transform)

        with self.assertRaises(AmbiguousTransformPathError) as raised:
            registry.resolve_path(
                "request", "source", "target", allow_indirect=True
            )

        self.assertEqual(raised.exception.kind, "request")
        self.assertEqual(raised.exception.source, "source")
        self.assertEqual(raised.exception.target, "target")
        self.assertEqual(
            raised.exception.paths,
            (
                ("source", "left", "target"),
                ("source", "right", "target"),
            ),
        )

    def test_indirect_paths_are_resolved_per_transform_kind(self) -> None:
        registry = TranslationRegistry()

        def transform(body, _context):
            return body

        registry.register_request("source", "middle", transform)
        registry.register_request("middle", "target", transform)
        registry.register_nonstream("source", "other", transform)
        registry.register_nonstream("other", "target", transform)

        self.assertEqual(
            registry.resolve_path(
                "request", "source", "target", allow_indirect=True
            ),
            ("source", "middle", "target"),
        )
        self.assertEqual(
            registry.resolve_path(
                "nonstream", "source", "target", allow_indirect=True
            ),
            ("source", "other", "target"),
        )
        with self.assertRaises(MissingTransformError):
            registry.resolve_path(
                "stream", "source", "target", allow_indirect=True
            )

    def test_stream_translation_validates_every_hop(self) -> None:
        registry = TranslationRegistry()
        second_called = False

        def invalid_first(_body, _context):
            return ["not", "async"]

        def second(body, _context):
            nonlocal second_called
            second_called = True
            return body

        registry.register_stream("source", "middle", invalid_first)
        registry.register_stream("middle", "target", second)

        with self.assertRaisesRegex(TypeError, "must return an AsyncIterable"):
            asyncio.run(
                registry.translate_stream(
                    self._context("source", "target"),
                    _chunks("chunk"),
                    allow_indirect=True,
                )
            )
        self.assertFalse(second_called)

    def test_stream_translation_applies_each_indirect_hop(self) -> None:
        registry = TranslationRegistry()

        async def add_prefix(chunks, context):
            async def translated():
                async for chunk in chunks:
                    yield f"{context.source}:{chunk}"

            return translated()

        def add_suffix(chunks, context):
            async def translated():
                async for chunk in chunks:
                    yield f"{chunk}:{context.target}"

            return translated()

        registry.register_stream("source", "canonical", add_prefix)
        registry.register_stream("canonical", "target", add_suffix)

        translated = asyncio.run(
            registry.translate_stream(
                self._context("source", "target"),
                _chunks("chunk"),
                allow_indirect=True,
            )
        )

        self.assertEqual(
            asyncio.run(_collect(translated)),
            ["source:chunk:target"],
        )

    @staticmethod
    def _context(source, target):
        from app.dataplane.translation import TranslationContext

        return TranslationContext(
            source=ProtocolFormat(source),
            target=ProtocolFormat(target),
        )


class TranslationPipelineTests(unittest.TestCase):
    def test_envelope_format_must_match_translation_source(self) -> None:
        pipeline = TranslationPipeline()

        with self.assertRaises(ValueError):
            asyncio.run(
                pipeline.translate_request(
                    "client",
                    "upstream",
                    RequestEnvelope("different", {}),
                )
            )

    def test_request_and_response_middleware_wrap_transforms_in_order(self) -> None:
        events = []
        registry = TranslationRegistry()

        async def request_transform(body, context):
            events.append(("request-transform", context.model))
            return body + ["transform"]

        def response_transform(body, context):
            events.append(("response-transform", context.original_request))
            return body + ["transform"]

        registry.register(
            "client",
            "upstream",
            request=request_transform,
            nonstream=response_transform,
        )
        pipeline = TranslationPipeline(registry)

        async def request_outer(request, call_next):
            events.append("request-outer-before")
            result = await call_next(replace(request, body=request.body + ["outer"]))
            events.append("request-outer-after")
            return replace(result, body=result.body + ["outer-after"])

        async def request_inner(request, call_next):
            events.append("request-inner-before")
            result = await call_next(replace(request, body=request.body + ["inner"]))
            events.append("request-inner-after")
            return result

        async def response_middleware(response, call_next):
            events.append("response-before")
            result = await call_next(replace(response, body=response.body + ["mw"]))
            events.append("response-after")
            return result

        pipeline.use_request(request_outer).use_request(request_inner)
        pipeline.use_response(response_middleware)

        request = asyncio.run(
            pipeline.translate_request(
                "client",
                "upstream",
                RequestEnvelope("client", [], model="grok-test"),
            )
        )
        response = asyncio.run(
            pipeline.translate_response(
                "client",
                "upstream",
                ResponseEnvelope(
                    "client",
                    [],
                    original_request={"prompt": "hello"},
                ),
            )
        )

        self.assertEqual(request.format, "upstream")
        self.assertEqual(request.body, ["outer", "inner", "transform", "outer-after"])
        self.assertEqual(response.format, "upstream")
        self.assertEqual(response.body, ["mw", "transform"])
        self.assertEqual(
            events,
            [
                "request-outer-before",
                "request-inner-before",
                ("request-transform", "grok-test"),
                "request-inner-after",
                "request-outer-after",
                "response-before",
                ("response-transform", {"prompt": "hello"}),
                "response-after",
            ],
        )

    def test_async_stream_transform_can_keep_chunk_state(self) -> None:
        registry = TranslationRegistry()

        async def stream_transform(chunks, context):
            prefix = context.metadata["prefix"]

            async def translated():
                index = 0
                async for chunk in chunks:
                    index += 1
                    await asyncio.sleep(0)
                    yield f"{prefix}:{index}:{chunk}"

            await asyncio.sleep(0)
            return translated()

        registry.register_stream("grok.build", "openai.responses", stream_transform)
        pipeline = TranslationPipeline(registry)
        response = asyncio.run(
            pipeline.translate_response(
                "grok.build",
                "openai.responses",
                ResponseEnvelope(
                    "grok.build",
                    _chunks("a", "b"),
                    stream=True,
                    metadata={"prefix": "event"},
                ),
            )
        )

        self.assertEqual(response.format, "openai.responses")
        self.assertEqual(
            asyncio.run(_collect(response.body)),
            ["event:1:a", "event:2:b"],
        )

    def test_stream_and_nonstream_registrations_are_independent(self) -> None:
        registry = TranslationRegistry()
        registry.register_nonstream("a", "b", lambda body, _context: body)
        pipeline = TranslationPipeline(registry)

        with self.assertRaises(MissingTransformError) as raised:
            asyncio.run(
                pipeline.translate_response(
                    "a",
                    "b",
                    ResponseEnvelope("a", _chunks("chunk"), stream=True),
                )
            )

        self.assertEqual(raised.exception.kind, "stream")

    def test_pipeline_forwards_indirect_opt_in_for_requests(self) -> None:
        registry = TranslationRegistry()
        registry.register_request(
            "client", "canonical", lambda body, _context: body + ["canonical"]
        )
        registry.register_request(
            "canonical", "upstream", lambda body, _context: body + ["upstream"]
        )
        pipeline = TranslationPipeline(registry)

        translated = asyncio.run(
            pipeline.translate_request(
                "client",
                "upstream",
                RequestEnvelope("client", []),
                allow_indirect=True,
            )
        )

        self.assertEqual(translated.format, "upstream")
        self.assertEqual(translated.body, ["canonical", "upstream"])

    def test_pipeline_forwards_indirect_opt_in_for_responses(self) -> None:
        registry = TranslationRegistry()
        registry.register_nonstream(
            "upstream", "canonical", lambda body, _context: body + ["canonical"]
        )
        registry.register_nonstream(
            "canonical", "client", lambda body, _context: body + ["client"]
        )
        pipeline = TranslationPipeline(registry)

        translated = asyncio.run(
            pipeline.translate_response(
                "upstream",
                "client",
                ResponseEnvelope("upstream", []),
                allow_indirect=True,
            )
        )

        self.assertEqual(translated.format, "client")
        self.assertEqual(translated.body, ["canonical", "client"])

    def test_pipeline_forwards_indirect_opt_in_for_streams(self) -> None:
        registry = TranslationRegistry()

        def append(label):
            def transform(chunks, _context):
                async def translated():
                    async for chunk in chunks:
                        yield f"{chunk}:{label}"

                return translated()

            return transform

        registry.register_stream("upstream", "canonical", append("canonical"))
        registry.register_stream("canonical", "client", append("client"))
        pipeline = TranslationPipeline(registry)

        translated = asyncio.run(
            pipeline.translate_response(
                "upstream",
                "client",
                ResponseEnvelope("upstream", _chunks("chunk"), stream=True),
                allow_indirect=True,
            )
        )

        self.assertEqual(translated.format, "client")
        self.assertEqual(
            asyncio.run(_collect(translated.body)),
            ["chunk:canonical:client"],
        )


class BuiltinTranslationTests(unittest.TestCase):
    def test_application_exposes_shared_translation_runtime(self) -> None:
        from app.main import create_app

        app = create_app()

        self.assertIs(app.state.translation_pipeline, get_translation_pipeline())
        self.assertIs(app.state.translation_registry, get_translation_registry())

    def test_builtin_bootstrap_registers_product_independent_routes(self) -> None:
        registry = TranslationRegistry()

        register_builtin_transforms(registry)
        register_builtin_transforms(registry)

        self.assertEqual(
            registry.routes("request"),
            (
                (
                    ProtocolFormat("openai.responses"),
                    ProtocolFormat("openai.chat.completions"),
                ),
                (
                    ProtocolFormat("anthropic.messages"),
                    ProtocolFormat("openai.chat.completions"),
                ),
                (
                    ProtocolFormat("openai.responses"),
                    ProtocolFormat("grok.build.responses"),
                ),
                (
                    ProtocolFormat("codex.responses"),
                    ProtocolFormat("grok.build.responses"),
                ),
            ),
        )
        expected_response_routes = (
            (
                ProtocolFormat("grok.build.responses"),
                ProtocolFormat("openai.responses"),
            ),
            (
                ProtocolFormat("grok.build.responses"),
                ProtocolFormat("codex.responses"),
            ),
        )
        self.assertEqual(registry.routes("nonstream"), expected_response_routes)
        self.assertEqual(registry.routes("stream"), expected_response_routes)

    def test_openai_responses_request_translation(self) -> None:
        pipeline = get_translation_pipeline()
        translated = asyncio.run(
            pipeline.translate_request(
                "openai.responses",
                "openai.chat.completions",
                RequestEnvelope(
                    "openai.responses",
                    {
                        "instructions": "Follow instructions",
                        "input": [
                            {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {"type": "input_text", "text": "hello"}
                                ],
                            },
                            {
                                "type": "function_call_output",
                                "call_id": "call-1",
                                "output": "done",
                            },
                        ],
                        "tools": [
                            {
                                "type": "function",
                                "name": "lookup",
                                "parameters": {"type": "object"},
                            }
                        ],
                    },
                ),
            )
        )

        self.assertEqual(translated.body["messages"][0]["role"], "system")
        self.assertEqual(translated.body["messages"][1]["role"], "user")
        self.assertEqual(translated.body["messages"][2]["role"], "tool")
        self.assertEqual(
            translated.body["tools"][0]["function"]["name"], "lookup"
        )

    def test_anthropic_request_translation(self) -> None:
        translated = asyncio.run(
            get_translation_pipeline().translate_request(
                "anthropic.messages",
                "openai.chat.completions",
                RequestEnvelope(
                    "anthropic.messages",
                    {
                        "system": "Be concise",
                        "messages": [{"role": "user", "content": "hello"}],
                        "tools": [{
                            "name": "lookup",
                            "description": "Lookup a value",
                            "input_schema": {"type": "object"},
                        }],
                        "tool_choice": {"type": "tool", "name": "lookup"},
                    },
                ),
            )
        )

        self.assertEqual(translated.body["messages"][0]["role"], "system")
        self.assertEqual(translated.body["messages"][1]["content"], "hello")
        self.assertEqual(translated.body["tools"][0]["function"]["name"], "lookup")
        self.assertEqual(
            translated.body["tool_choice"],
            {"type": "function", "function": {"name": "lookup"}},
        )


if __name__ == "__main__":
    unittest.main()
