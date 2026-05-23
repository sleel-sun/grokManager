from unittest.mock import patch

import pytest

from app.control.model.registry import resolve
from app.platform.errors import RateLimitError, UpstreamError
from app.products.openai import chat


class _FakeConfig:
    def __init__(self, values: dict[str, int] | None = None) -> None:
        self._values = values or {}

    def get_int(self, key: str, default: int) -> int:
        return int(self._values.get(key, default))


def test_plain_403_retries_with_another_account() -> None:
    exc = UpstreamError("Chat upstream returned 403", status=403, body="")

    assert chat._should_retry_upstream(exc, frozenset())


def test_cloudflare_403_does_not_spin_through_accounts() -> None:
    exc = UpstreamError(
        "Chat upstream returned 403",
        status=403,
        body="<html>cf-mitigated challenge by Cloudflare</html>",
    )

    assert not chat._should_retry_upstream(exc, frozenset())


def test_chat_retry_count_has_account_swap_floor() -> None:
    with patch.object(chat, "selection_max_retries", return_value=1):
        assert chat._chat_max_retries(_FakeConfig()) == 20


def test_chat_retry_count_can_raise_account_swap_floor() -> None:
    cfg = _FakeConfig({"chat.account_retry_min_retries": 8})

    with patch.object(chat, "selection_max_retries", return_value=1):
        assert chat._chat_max_retries(cfg) == 8


def test_chat_account_exhaustion_reports_entitlement_condition() -> None:
    exc = UpstreamError("Chat upstream returned 403", status=403, body="")

    mapped = chat._chat_exhausted_error(
        "grok-4.20-0309",
        attempted_accounts=6,
        last_exc=exc,
    )

    assert isinstance(mapped, RateLimitError)
    assert "grok-4.20-0309" in mapped.message
    assert "6 account attempts" in mapped.message
    assert "quota/entitlement" in mapped.message
    assert "403" in mapped.message


def test_no_account_error_reports_required_pools() -> None:
    spec = resolve("grok-4.3-beta")

    exc = chat._no_available_account_error(spec)

    assert isinstance(exc, RateLimitError)
    assert "grok-4.3-beta" in exc.message
    assert "super" in exc.message
    assert "heavy" in exc.message


def test_console_404_reports_upstream_model_condition() -> None:
    spec = resolve("grok-4.3-high")

    with pytest.raises(UpstreamError) as err:
        chat._raise_chat_status_error(
            spec=spec,
            status_code=404,
            body="",
        )

    assert err.value.status == 404
    assert "Console Responses upstream does not expose model" in err.value.message
    assert "grok-4.3-high" in err.value.message
