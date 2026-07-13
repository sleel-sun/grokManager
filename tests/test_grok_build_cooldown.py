import tempfile
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

from app.maintainer.grok_build_cooldown import (
    FREE_USAGE_EXHAUSTED,
    AvailabilityState,
    GrokBuildCooldownStore,
    parse_xai_rate_limit,
)


class GrokBuildRateLimitParserTests(unittest.TestCase):
    def test_free_usage_exhausted_json_blocks_for_24_hours(self) -> None:
        decision = parse_xai_rate_limit(
            429,
            {
                "error": {
                    "code": FREE_USAGE_EXHAUSTED,
                    "message": "free allocation exhausted",
                    "details": {"tokens": {"actual": 1050, "limit": 1000}},
                }
            },
            now=1000.0,
        )

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.state, AvailabilityState.BLOCKED)
        self.assertEqual(decision.reason, FREE_USAGE_EXHAUSTED)
        self.assertEqual(decision.retry_at, 1000.0 + 86400)
        self.assertEqual(decision.actual_tokens, 1050)
        self.assertEqual(decision.limit_tokens, 1000)

    def test_free_usage_exhausted_text_parses_tokens_and_retry_after(self) -> None:
        decision = parse_xai_rate_limit(
            429,
            "subscription:free-usage-exhausted tokens(actual=22, limit=20)",
            {"Retry-After": "120"},
            now=500.0,
        )

        assert decision is not None
        self.assertEqual(decision.state, AvailabilityState.BLOCKED)
        self.assertEqual(decision.retry_at, 620.0)
        self.assertEqual(decision.actual_tokens, 22)
        self.assertEqual(decision.limit_tokens, 20)

    def test_generic_429_uses_http_date_or_configured_backoff(self) -> None:
        retry_date = format_datetime(
            datetime.fromtimestamp(1120.0, tz=timezone.utc), usegmt=True
        )
        dated = parse_xai_rate_limit(
            429,
            {"error": "busy"},
            {"retry-after": retry_date},
            now=1000.0,
        )
        backed_off = parse_xai_rate_limit(
            429,
            "too many requests",
            now=1000.0,
            backoff_seconds=45,
        )

        assert dated is not None and backed_off is not None
        self.assertEqual(dated.state, AvailabilityState.COOLDOWN)
        self.assertEqual(dated.retry_at, 1120.0)
        self.assertEqual(backed_off.retry_at, 1045.0)

    def test_non_429_is_not_a_rate_limit(self) -> None:
        self.assertIsNone(parse_xai_rate_limit(403, {"error": "forbidden"}))


class GrokBuildCooldownStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "grok_build_usage.db"
        self.store = GrokBuildCooldownStore(self.path)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_states_are_isolated_by_source_and_model(self) -> None:
        self.store.mark_result(
            "sso:a",
            "grok-4.5",
            status_code=429,
            body={"error": FREE_USAGE_EXHAUSTED},
            now=1000.0,
        )

        self.assertEqual(
            self.store.filter_candidates(
                ["sso:a", "sso:b"], "grok-4.5", now=1001.0
            ),
            ["sso:b"],
        )
        self.assertEqual(
            self.store.filter_candidates(["sso:a"], "grok-4.3", now=1001.0),
            ["sso:a"],
        )

    def test_expired_block_is_recovered_and_persisted(self) -> None:
        self.store.mark_result(
            "sso:a",
            "grok-4.5",
            status_code=429,
            body={"error": FREE_USAGE_EXHAUSTED},
            now=1000.0,
            free_recovery_seconds=10,
        )

        record = self.store.get("sso:a", "grok-4.5", now=1011.0)
        persisted = GrokBuildCooldownStore(self.path).get(
            "sso:a", "grok-4.5", now=1011.0
        )

        self.assertEqual(record.state, AvailabilityState.READY)
        self.assertIsNone(record.retry_at)
        self.assertEqual(persisted.state, AvailabilityState.READY)

    def test_success_clears_cooldown_and_token_limit_details(self) -> None:
        blocked = self.store.mark_result(
            "sso:a",
            "grok-4.5",
            status_code=429,
            body={
                "error": FREE_USAGE_EXHAUSTED,
                "tokens": {"actual": 12, "limit": 10},
            },
            now=1000.0,
        )
        ready = self.store.mark_result(
            "sso:a", "grok-4.5", status_code=200, now=1001.0
        )

        self.assertEqual(blocked.actual_tokens, 12)
        self.assertEqual(ready.state, AvailabilityState.READY)
        self.assertIsNone(ready.reason)
        self.assertIsNone(ready.actual_tokens)
        self.assertEqual(ready.failure_count, 0)

    def test_generic_429_backoff_grows_for_consecutive_failures(self) -> None:
        first = self.store.mark_result(
            "sso:a",
            "grok-4.5",
            status_code=429,
            body="busy",
            now=1000.0,
            backoff_seconds=30,
        )
        second = self.store.mark_result(
            "sso:a",
            "grok-4.5",
            status_code=429,
            body="still busy",
            now=1001.0,
            backoff_seconds=30,
        )

        self.assertEqual(first.retry_at, 1030.0)
        self.assertEqual(second.retry_at, 1061.0)
        self.assertEqual(second.failure_count, 2)

    def test_disabled_state_requires_explicit_enable(self) -> None:
        disabled = self.store.set_disabled(
            "sso:a", "grok-4.5", reason="operator", now=1000.0
        )
        after_success = self.store.mark_result(
            "sso:a", "grok-4.5", status_code=200, now=1000.5
        )
        still_disabled = self.store.get("sso:a", "grok-4.5", now=999999.0)
        enabled = self.store.set_disabled(
            "sso:a", "grok-4.5", disabled=False, now=1001.0
        )

        self.assertEqual(disabled.state, AvailabilityState.DISABLED)
        self.assertEqual(after_success.state, AvailabilityState.DISABLED)
        self.assertEqual(still_disabled.state, AvailabilityState.DISABLED)
        self.assertEqual(enabled.state, AvailabilityState.READY)


if __name__ == "__main__":
    unittest.main()
