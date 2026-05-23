from unittest.mock import patch

from app.platform.errors import UpstreamError
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
        assert chat._chat_max_retries(_FakeConfig()) == 5


def test_chat_retry_count_can_raise_account_swap_floor() -> None:
    cfg = _FakeConfig({"chat.account_retry_min_retries": 8})

    with patch.object(chat, "selection_max_retries", return_value=1):
        assert chat._chat_max_retries(cfg) == 8
