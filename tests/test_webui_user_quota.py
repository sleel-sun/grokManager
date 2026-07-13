from __future__ import annotations

import pytest

from app.platform.auth.middleware import WebUIUser
from app.platform.errors import RateLimitError
from app.products.web.webui import quota as quota_module


def test_webui_user_quota_consumes_daily_limit(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(quota_module, "_USAGE_PATH", tmp_path / "usage.json")
    monkeypatch.setattr(quota_module, "_LOCK_PATH", tmp_path / "usage.lock")
    monkeypatch.setattr(quota_module, "_today_key", lambda: "2026-07-04")

    user = WebUIUser(id="alice-id", username="alice", grok_daily_quota=3, gpt_daily_quota=2)

    assert quota_module.quota_status_for_user(user)["grok"]["used"] == 0
    quota_module.consume_user_quota(user, "grok", amount=2)
    assert quota_module.quota_status_for_user(user)["grok"] == {
        "limit": 3,
        "used": 2,
        "remaining": 1,
        "unlimited": False,
    }

    with pytest.raises(RateLimitError):
        quota_module.consume_user_quota(user, "grok", amount=2)


def test_webui_user_quota_zero_limit_is_unlimited(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(quota_module, "_USAGE_PATH", tmp_path / "usage.json")
    monkeypatch.setattr(quota_module, "_LOCK_PATH", tmp_path / "usage.lock")
    monkeypatch.setattr(quota_module, "_today_key", lambda: "2026-07-04")

    user = WebUIUser(id="alice-id", username="alice", grok_daily_quota=0)

    quota_module.consume_user_quota(user, "grok", amount=1000)
    assert quota_module.quota_status_for_user(user)["grok"] == {
        "limit": 0,
        "used": 0,
        "remaining": None,
        "unlimited": True,
    }
