from __future__ import annotations

from app.products.web.admin.users import normalize_webui_users


def test_admin_webui_users_normalize_legacy_shapes() -> None:
    users = normalize_webui_users(
        {
            "users": [
                {
                    "username": "alice",
                    "password": "alice-secret",
                    "apiKey": "alice-api-secret",
                    "displayName": "Alice",
                    "allowNsfw": False,
                    "gptEnabled": True,
                    "gptImageQuality": "4k",
                    "grokDailyQuota": 100,
                    "gptDailyQuota": 20,
                },
                {
                    "name": "bob",
                    "key": "bob-secret",
                    "enabled": "off",
                    "gptModels": ["codex-gpt-image-2"],
                    "gpt_quality": "2",
                },
                {"username": "alice", "key": "duplicate-is-skipped"},
            ]
        }
    )

    assert users == [
        {
            "username": "alice",
            "key": "alice-secret",
            "api_key": "alice-api-secret",
            "display_name": "Alice",
            "enabled": True,
            "allow_nsfw": False,
            "gpt_enabled": True,
            "gpt_models": ["gpt-image-1", "gpt-image-2", "codex-gpt-image-2"],
            "gpt_image_quality": "4k",
            "grok_daily_quota": 100,
            "gpt_daily_quota": 20,
        },
        {
            "username": "bob",
            "key": "bob-secret",
            "api_key": "",
            "display_name": "bob",
            "enabled": False,
            "allow_nsfw": True,
            "gpt_enabled": True,
            "gpt_models": ["codex-gpt-image-2"],
            "gpt_image_quality": "2k",
            "grok_daily_quota": 0,
            "gpt_daily_quota": 0,
        },
    ]


def test_admin_webui_users_normalize_line_config() -> None:
    assert normalize_webui_users("carol=carol-secret\n# ignored\ndave:dave-secret") == [
        {
            "username": "carol",
            "key": "carol-secret",
            "api_key": "",
            "display_name": "carol",
            "enabled": True,
            "allow_nsfw": True,
            "gpt_enabled": False,
            "gpt_models": [],
            "gpt_image_quality": "1k",
            "grok_daily_quota": 0,
            "gpt_daily_quota": 0,
        },
        {
            "username": "dave",
            "key": "dave-secret",
            "api_key": "",
            "display_name": "dave",
            "enabled": True,
            "allow_nsfw": True,
            "gpt_enabled": False,
            "gpt_models": [],
            "gpt_image_quality": "1k",
            "grok_daily_quota": 0,
            "gpt_daily_quota": 0,
        },
    ]
