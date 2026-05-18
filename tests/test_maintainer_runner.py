import os
import re
import tempfile
import unittest
from itertools import count
from pathlib import Path
from unittest.mock import patch

from app.maintainer.runner import (
    _ensure_browser_storage_ready,
    _resolve_browser_tmp_path,
    build_profile,
    run_batch,
)


class MaintainerRunnerTests(unittest.TestCase):
    def test_browser_tmp_path_can_be_overridden_by_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"MAINTAINER_TMP_PATH": tmpdir}):
                self.assertEqual(_resolve_browser_tmp_path(), Path(tmpdir).resolve())

    def test_browser_storage_error_is_actionable_when_space_is_low(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"MAINTAINER_MIN_BROWSER_FREE_BYTES": str(10**18)}):
                with self.assertRaisesRegex(RuntimeError, "浏览器临时目录可用空间不足"):
                    _ensure_browser_storage_ready(tmpdir)

    def test_build_profile_generates_random_ascii_names(self) -> None:
        profiles = [build_profile() for _ in range(20)]
        names = {(given, family) for given, family, _ in profiles}

        self.assertGreater(len(names), 1)
        self.assertNotEqual(names, {("Neo", "Lin")})

        for given, family, password in profiles:
            self.assertRegex(given, r"^[A-Z][a-z]{2,15}$")
            self.assertRegex(family, r"^[A-Z][a-z]{2,15}$")
            self.assertTrue(password)
            self.assertIsNone(re.search(r"\s", given + family))

    def test_run_batch_reports_registration_progress(self) -> None:
        events = []
        seq = count(1)

        def fake_registration(output_path: Path, extract_numbers: bool = False) -> dict[str, str]:
            index = next(seq)
            return {"email": f"user{index}@example.com", "sso": f"sso-{index}"}

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "maintainer.config.json"
            config_path.write_text("{}", encoding="utf-8")
            output_path = Path(tmpdir) / "sso.txt"

            with (
                patch("app.maintainer.runner._configure_browser_options", return_value=object()),
                patch("app.maintainer.runner.start_browser"),
                patch("app.maintainer.runner.run_single_registration", side_effect=fake_registration),
                patch("app.maintainer.runner.restart_browser"),
                patch("app.maintainer.runner.stop_browser"),
                patch("app.maintainer.runner.push_sso_to_api"),
                patch("app.maintainer.runner.time.sleep"),
            ):
                tokens = run_batch(
                    config_path=config_path,
                    count=3,
                    output=output_path,
                    progress_callback=events.append,
                )

        self.assertEqual(tokens, ["sso-1", "sso-2", "sso-3"])
        self.assertEqual(events[0]["event"], "batch_started")
        self.assertEqual(events[0]["total_count"], 3)
        self.assertEqual(
            [event["event"] for event in events if event["event"] == "round_finished"],
            ["round_finished", "round_finished", "round_finished"],
        )
        self.assertEqual(events[-1]["event"], "batch_finished")
        self.assertEqual(events[-1]["completed_count"], 3)
        self.assertEqual(events[-1]["remaining_count"], 0)
        self.assertEqual(events[-1]["token_count"], 3)


if __name__ == "__main__":
    unittest.main()
