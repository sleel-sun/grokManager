import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.maintainer.runner import (
    _ensure_browser_storage_ready,
    _resolve_browser_tmp_path,
    build_profile,
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


if __name__ == "__main__":
    unittest.main()
