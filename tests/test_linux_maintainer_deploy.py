from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LinuxMaintainerDeployTests(unittest.TestCase):
    def test_package_exposes_grokmanager_maintainer_cli_alias(self) -> None:
        payload = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        scripts = payload["project"]["scripts"]

        self.assertEqual(scripts["grokmanager-maintainer"], "app.maintainer.runner:main")
        self.assertEqual(scripts["grok2api-maintainer"], "app.maintainer.runner:main")

    def test_compose_defines_linux_maintainer_service(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("  maintainer:", compose)
        self.assertIn("/app/scripts/run_maintainer.sh", compose)
        self.assertIn("- MAINTAINER_HEADLESS=${MAINTAINER_HEADLESS:-false}", compose)
        self.assertIn("- MAINTAINER_USE_XVFB=${MAINTAINER_USE_XVFB:-true}", compose)
        self.assertIn("http://grokmanager:8000/admin/api/tokens/add", compose)
        self.assertIn("MAINTAINER_EMAIL_PROVIDER", compose)
        self.assertIn("MAINTAINER_HOTMAIL_CREDENTIALS_FILE", compose)
        self.assertIn("MAINTAINER_GROK_BUILD_AUTO_OAUTH", compose)

    def test_docker_image_installs_browser_and_maintainer_extra(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("uv sync --frozen --no-dev --extra maintainer", dockerfile)
        self.assertIn("COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /uvx /bin/", dockerfile)
        self.assertIn("chromium", dockerfile)
        self.assertIn("xvfb", dockerfile)
        self.assertIn("font-noto-cjk", dockerfile)


if __name__ == "__main__":
    unittest.main()
