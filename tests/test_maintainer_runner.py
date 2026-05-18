import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.maintainer.runner import _ensure_browser_storage_ready, _resolve_browser_tmp_path


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


if __name__ == "__main__":
    unittest.main()
