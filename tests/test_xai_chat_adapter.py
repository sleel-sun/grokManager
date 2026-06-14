import json
import unittest

from app.dataplane.reverse.protocol.xai_chat import StreamAdapter


class XaiChatStreamAdapterImageTests(unittest.TestCase):
    def test_image_chunk_keeps_absolute_asset_url(self) -> None:
        adapter = StreamAdapter()
        payload = {
            "result": {
                "response": {
                    "cardAttachment": {
                        "jsonData": json.dumps(
                            {
                                "id": "card-1",
                                "image_chunk": {
                                    "progress": 100,
                                    "moderated": False,
                                    "imageUuid": "123e4567-e89b-12d3-a456-426614174000",
                                    "imageUrl": "https://assets.grok.com/images/123e4567-e89b-12d3-a456-426614174000.jpg",
                                },
                            }
                        )
                    }
                }
            }
        }

        events = adapter.feed(json.dumps(payload))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "image")
        self.assertEqual(
            events[0].content,
            "https://assets.grok.com/images/123e4567-e89b-12d3-a456-426614174000.jpg",
        )
        self.assertEqual(adapter.image_urls, [(events[0].content, events[0].image_id)])

    def test_render_generated_image_card_falls_back_to_card_url(self) -> None:
        adapter = StreamAdapter()
        card = {
            "id": "card-1",
            "generatedImageUrl": "/images/123e4567-e89b-12d3-a456-426614174000.jpg",
        }
        card_payload = {
            "result": {
                "response": {
                    "cardAttachment": {"jsonData": json.dumps(card)},
                }
            }
        }
        token_payload = {
            "result": {
                "response": {
                    "token": '<grok:render card_id="card-1" card_type="image" type="render_generated_image"></grok:render>',
                    "messageTag": "final",
                }
            }
        }

        adapter.feed(json.dumps(card_payload))
        events = adapter.feed(json.dumps(token_payload))

        self.assertEqual(events, [])
        self.assertEqual(
            adapter.image_urls,
            [
                (
                    "https://assets.grok.com/images/123e4567-e89b-12d3-a456-426614174000.jpg",
                    "123e4567-e89b-12d3-a456-426614174000",
                )
            ],
        )

    def test_generated_image_card_accepts_non_asset_image_url(self) -> None:
        adapter = StreamAdapter()
        card = {
            "id": "card-1",
            "generatedImage": {
                "id": "ig_123",
                "url": "https://imgen.x.ai/generated/image-content?token=abc",
            },
        }
        payload = {
            "result": {
                "response": {
                    "cardAttachment": {"jsonData": json.dumps(card)},
                }
            }
        }

        events = adapter.feed(json.dumps(payload))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "image")
        self.assertEqual(
            events[0].content,
            "https://imgen.x.ai/generated/image-content?token=abc",
        )
        self.assertEqual(adapter.image_urls, [(events[0].content, "ig_123")])

    def test_final_token_markdown_image_is_collected_for_localization(self) -> None:
        adapter = StreamAdapter()
        payload = {
            "result": {
                "response": {
                    "token": "Here is the image: ![image](https://imgen.x.ai/generated/image-content?token=abc)",
                    "messageTag": "final",
                }
            }
        }

        events = adapter.feed(json.dumps(payload))

        self.assertEqual([(ev.kind, ev.content) for ev in events], [
            ("text", "Here is the image: "),
        ])
        self.assertEqual(adapter.text_buf, ["Here is the image: "])
        self.assertEqual(len(adapter.image_urls), 1)
        self.assertEqual(
            adapter.image_urls[0][0],
            "https://imgen.x.ai/generated/image-content?token=abc",
        )

    def test_final_token_bare_grok_image_url_is_collected_for_localization(self) -> None:
        adapter = StreamAdapter()
        url = "https://grok.x.ai/generated-image-city-skyline-dawn-mist-cinematic-wide.jpg"
        payload = {
            "result": {
                "response": {
                    "token": f"Image: {url}",
                    "messageTag": "final",
                }
            }
        }

        events = adapter.feed(json.dumps(payload))

        self.assertEqual([(ev.kind, ev.content) for ev in events], [
            ("text", "Image: "),
        ])
        self.assertEqual(adapter.image_urls, [(url, adapter.image_urls[0][1])])

    def test_split_bare_grok_image_url_is_collected_after_join(self) -> None:
        adapter = StreamAdapter()
        first = {
            "result": {
                "response": {
                    "token": "Image: https://grok.x.ai/generated-image-shanghai-",
                    "messageTag": "final",
                }
            }
        }
        second = {
            "result": {
                "response": {
                    "token": "pudong-golden-hour-realistic-cinematic-wide.jpg",
                    "messageTag": "final",
                }
            }
        }

        adapter.feed(json.dumps(first))
        adapter.feed(json.dumps(second))
        cleaned = adapter.extract_generated_images_from_text("".join(adapter.text_buf))

        self.assertEqual(cleaned, "Image: ")
        self.assertEqual(
            adapter.image_urls[0][0],
            "https://grok.x.ai/generated-image-shanghai-pudong-golden-hour-realistic-cinematic-wide.jpg",
        )

    def test_split_bare_grok_image_url_does_not_emit_partial_url(self) -> None:
        adapter = StreamAdapter()
        first = {
            "result": {
                "response": {
                    "token": "Image: https://grok.x.ai/generated-image-shanghai-",
                    "messageTag": "final",
                }
            }
        }
        second = {
            "result": {
                "response": {
                    "token": "pudong-golden-hour-realistic-cinematic-wide.jpg",
                    "messageTag": "final",
                }
            }
        }

        first_events = adapter.feed(json.dumps(first))
        second_events = adapter.feed(json.dumps(second))

        self.assertEqual([(ev.kind, ev.content) for ev in first_events], [
            ("text", "Image: "),
        ])
        self.assertEqual(second_events, [])
        self.assertEqual(adapter.text_buf, ["Image: "])
        self.assertEqual(
            adapter.image_urls[0][0],
            "https://grok.x.ai/generated-image-shanghai-pudong-golden-hour-realistic-cinematic-wide.jpg",
        )


if __name__ == "__main__":
    unittest.main()
