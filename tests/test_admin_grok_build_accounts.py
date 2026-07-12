import json
import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import orjson

from app.dataplane.reverse.protocol.grok_build import _save_document
from app.maintainer.grok_build_oauth import (
    delete_pool_entry,
    delete_pool_entries,
    pool_entries,
    save_pool_entry,
    save_pool_credential,
)
from app.products.web.admin import grok_build_accounts as admin


class _Repo:
    def __init__(self, records):
        self.records = records

    async def list_accounts(self, _query):
        return SimpleNamespace(items=self.records, total=len(self.records))


def _record(token: str, *, tags=None, ext=None):
    return SimpleNamespace(
        token=token,
        tags=tags or [],
        ext=ext or {},
    )


class GrokBuildPoolStorageTests(unittest.TestCase):
    def test_saved_credential_includes_searchable_account_metadata(self) -> None:
        claims = base64.urlsafe_b64encode(
            json.dumps({"email": "build@example.com"}).encode("utf-8")
        ).decode("ascii").rstrip("=")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "grok_auth.json"
            with (
                patch("app.maintainer.grok_build_oauth.pool_path", return_value=path),
                patch("app.maintainer.grok_build_oauth._oauth_config", return_value=("client", "url", "scope")),
                patch("app.maintainer.grok_build_oauth.time.time", return_value=1000.0),
            ):
                save_pool_credential(
                    "sso:account",
                    {
                        "access_token": "access-secret",
                        "refresh_token": "refresh-secret",
                        "id_token": f"header.{claims}.signature",
                        "expires_in": 3600,
                    },
                )

                entry = pool_entries()["sso:account"]

        self.assertEqual(entry["email"], "build@example.com")
        self.assertEqual(entry["updated_at"], 1000.0)

    def test_pool_helpers_merge_and_delete_without_losing_other_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "grok_auth.json"
            with patch("app.maintainer.grok_build_oauth.pool_path", return_value=path):
                save_pool_entry(
                    "sso:a", {"access_token": "access-a", "refresh_token": "refresh-a"}
                )
                stale = json.loads(path.read_text(encoding="utf-8"))
                save_pool_entry("sso:b", {"access_token": "access-b"})

                _save_document(
                    path,
                    stale,
                    "sso:a",
                    {"access_token": "access-a-new", "refresh_token": "refresh-a"},
                )

                entries = pool_entries()
                self.assertEqual(set(entries), {"sso:a", "sso:b"})
                self.assertEqual(entries["sso:a"]["access_token"], "access-a-new")
                self.assertTrue(delete_pool_entry("sso:b"))
                self.assertEqual(set(pool_entries()), {"sso:a"})
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_batch_delete_is_atomic_and_preserves_unselected_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "grok_auth.json"
            with patch("app.maintainer.grok_build_oauth.pool_path", return_value=path):
                save_pool_entry("sso:a", {"access_token": "access-a"})
                save_pool_entry("sso:b", {"access_token": "access-b"})
                save_pool_entry("sso:keep", {"access_token": "access-keep"})

                deleted, not_found = delete_pool_entries(
                    ["sso:a", "sso:missing", "sso:b", "sso:a"]
                )

                self.assertEqual(deleted, ["sso:a", "sso:b"])
                self.assertEqual(not_found, ["sso:missing"])
                self.assertEqual(set(pool_entries()), {"sso:keep"})


class GrokBuildAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        async def _inline_to_thread(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        self._to_thread_patcher = patch.object(
            admin.asyncio,
            "to_thread",
            side_effect=_inline_to_thread,
        )
        self._to_thread_patcher.start()

    async def asyncTearDown(self) -> None:
        self._to_thread_patcher.stop()

    async def test_list_response_never_contains_oauth_tokens(self) -> None:
        entry = {
            "access_token": "access-secret",
            "key": "access-secret",
            "refresh_token": "refresh-secret",
            "id_token": "id-secret",
            "expires_at": 4_102_444_800,
            "source": "grok_sso_device_flow",
            "email": "build@example.com",
            "updated_at": 1_700_000_000,
        }
        with patch.object(admin, "_safe_entries", return_value={"sso:abc": entry}):
            response = await admin.list_grok_build_accounts(page=1, page_size=100)

        payload = orjson.loads(response.body)
        encoded = response.body.decode("utf-8")
        self.assertNotIn("access-secret", encoded)
        self.assertNotIn("refresh-secret", encoded)
        self.assertNotIn("id-secret", encoded)
        self.assertEqual(payload["accounts"][0]["source_id"], "sso:abc")
        self.assertEqual(payload["accounts"][0]["email"], "build@example.com")
        self.assertEqual(payload["accounts"][0]["updated_at"], 1_700_000_000)
        self.assertTrue(payload["accounts"][0]["has_refresh_token"])
        self.assertTrue(payload["accounts"][0]["has_id_token"])

    async def test_convert_selects_only_missing_active_grok_accounts(self) -> None:
        existing_id = admin.source_id_for_sso("existing-sso")
        repo = _Repo(
            [
                _record("existing-sso"),
                _record("missing-sso"),
                _record("gpt_credential", tags=["gpt"], ext={"gpt": True}),
            ]
        )
        captured = {}

        def fake_start(candidates, *, scanned, skipped):
            captured.update(candidates=candidates, scanned=scanned, skipped=skipped)
            return {
                "task_id": "task",
                "status": "pending",
                "pending": len(candidates),
                "skipped": skipped,
                "succeeded": 0,
                "failed": 0,
            }

        with (
            patch.object(
                admin, "_safe_entries", return_value={existing_id: {"key": "secret"}}
            ),
            patch.object(admin, "_start_job", side_effect=fake_start),
        ):
            response = await admin.convert_grok_sso_accounts(
                admin.ConvertRequest(limit=10),
                repo,
            )

        payload = orjson.loads(response.body)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["scanned"], 2)
        self.assertEqual(captured["skipped"], 1)
        self.assertEqual(
            captured["candidates"],
            [("missing-sso", admin.source_id_for_sso("missing-sso"))],
        )
        self.assertNotIn("missing-sso", response.body.decode("utf-8"))
        self.assertEqual(payload["pending"], 1)

    async def test_convert_limit_zero_means_all_and_limit_can_exceed_100(self) -> None:
        repo = _Repo([_record(f"sso-{index}") for index in range(130)])
        captured: list[int] = []

        def fake_start(candidates, *, scanned, skipped):
            captured.append(len(candidates))
            return {"task_id": "task", "status": "pending"}

        with (
            patch.object(admin, "_safe_entries", return_value={}),
            patch.object(admin, "_start_job", side_effect=fake_start),
        ):
            await admin.convert_grok_sso_accounts(admin.ConvertRequest(limit=0), repo)
            await admin.convert_grok_sso_accounts(admin.ConvertRequest(limit=121), repo)

        self.assertEqual(captured, [130, 121])

    async def test_batch_refresh_selects_matching_active_sso_accounts(self) -> None:
        matched_id = admin.source_id_for_sso("active-sso")
        missing_id = "sso:missing"
        repo = _Repo(
            [
                _record("active-sso"),
                _record("gpt_credential", tags=["gpt"], ext={"gpt": True}),
            ]
        )
        captured = {}

        def fake_start(
            candidates,
            *,
            scanned,
            skipped,
            job_type="convert",
            errors=None,
        ):
            captured.update(
                candidates=candidates,
                scanned=scanned,
                skipped=skipped,
                job_type=job_type,
                errors=errors,
            )
            return {
                "task_id": "refresh-task",
                "status": "pending",
                "total": len(candidates),
                "progress": 0,
            }

        with patch.object(admin, "_start_job", side_effect=fake_start):
            response = await admin.refresh_grok_build_accounts(
                admin.SourceIdsRequest(source_ids=[matched_id, missing_id, matched_id]),
                repo,
            )

        payload = orjson.loads(response.body)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["candidates"], [("active-sso", matched_id)])
        self.assertEqual(captured["scanned"], 2)
        self.assertEqual(captured["skipped"], 1)
        self.assertEqual(captured["job_type"], "refresh")
        self.assertEqual(
            captured["errors"],
            [{"source_id": missing_id, "error": "active_sso_not_found"}],
        )
        self.assertEqual(payload["task_id"], "refresh-task")

    async def test_job_tracks_progress_and_total(self) -> None:
        task_id = "progress-task"
        admin._JOBS[task_id] = {
            "status": "pending",
            "total": 2,
            "progress": 0,
            "pending": 2,
            "skipped": 0,
            "succeeded": 0,
            "failed": 0,
            "errors": [],
        }
        with patch.object(
            admin,
            "authorize_sso_account",
            side_effect=[{"source_id": "sso:a"}, RuntimeError("secret")],
        ):
            admin._run_authorization_job(
                task_id,
                [("token-a", "sso:a"), ("token-b", "sso:b")],
            )

        job = admin._JOBS.pop(task_id)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["progress"], 2)
        self.assertEqual(job["total"], 2)
        self.assertEqual(job["pending"], 0)
        self.assertEqual(job["succeeded"], 1)
        self.assertEqual(job["failed"], 1)
        self.assertNotIn("secret", str(job))


if __name__ == "__main__":
    unittest.main()
