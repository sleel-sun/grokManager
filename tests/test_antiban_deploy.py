import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class AntiBanComposeTests(unittest.TestCase):
    def test_antiban_compose_wires_warp_privoxy_flaresolverr(self) -> None:
        root = Path(__file__).resolve().parents[1]
        compose = (root / "docker-compose.antiban.yml").read_text()
        privoxy = (root / "deploy/anti-ban/privoxy/config").read_text()

        self.assertIn("warp:", compose)
        self.assertIn("privoxy:", compose)
        self.assertIn("flaresolverr:", compose)
        self.assertIn("GROK_PROXY_EGRESS_MODE: ${GROK_PROXY_EGRESS_MODE:-single_proxy}", compose)
        self.assertIn(
            "GROK_PROXY_EGRESS_PROXY_URL: ${GROK_PROXY_EGRESS_PROXY_URL:-http://privoxy:8118}",
            compose,
        )
        self.assertIn(
            "GROK_PROXY_CLEARANCE_MODE: ${GROK_PROXY_CLEARANCE_MODE:-flaresolverr}",
            compose,
        )
        self.assertIn(
            "GROK_PROXY_CLEARANCE_FLARESOLVERR_URL: ${GROK_PROXY_CLEARANCE_FLARESOLVERR_URL:-http://flaresolverr:8191}",
            compose,
        )
        self.assertIn(
            "MAINTAINER_FLARESOLVERR_URL: ${MAINTAINER_FLARESOLVERR_URL:-http://flaresolverr:8191}",
            compose,
        )
        self.assertIn("forward-socks5t / warp:1080 .", privoxy)

    def test_macos_package_includes_antiban_deployment_assets(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts/package-macos.sh").read_text()

        self.assertIn("docker-compose.yml:.", script)
        self.assertIn("docker-compose.antiban.yml:.", script)
        self.assertIn("deploy/anti-ban:deploy/anti-ban", script)
        self.assertIn("scripts/deploy-antiban-local.sh:scripts", script)
        self.assertIn("scripts/grokmanager-antiban.command:scripts", script)
        self.assertIn("Start Anti-Ban.command", script)

    def test_local_antiban_deploy_configures_env_without_docker(self) -> None:
        root = Path(__file__).resolve().parents[1]
        deploy_script = root / "scripts/deploy-antiban-local.sh"
        env = {
            **os.environ,
            "ANTI_BAN_SKIP_PORT_CHECK": "1",
            "ANTI_BAN_SKIP_WARP_CONFIG": "1",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                [
                    str(deploy_script),
                    "--prefix",
                    tmpdir,
                    "--configure-only",
                    "--proxy-url",
                    "http://127.0.0.1:40000",
                    "--flaresolverr-url",
                    "http://127.0.0.1:8191",
                ],
                cwd=root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertNotIn("docker compose", result.stdout.lower())

            env_file = Path(tmpdir) / ".env"
            runner = Path(tmpdir) / "run-grokmanager-antiban.sh"
            self.assertTrue(env_file.exists())
            self.assertTrue(runner.exists())
            self.assertTrue(os.access(runner, os.X_OK))

            content = env_file.read_text()
            self.assertIn("GROK_PROXY_EGRESS_MODE=single_proxy", content)
            self.assertIn("GROK_PROXY_EGRESS_PROXY_URL=http://127.0.0.1:40000", content)
            self.assertIn("GROK_PROXY_EGRESS_RESOURCE_PROXY_URL=http://127.0.0.1:40000", content)
            self.assertIn("GROK_PROXY_CLEARANCE_MODE=flaresolverr", content)
            self.assertIn(
                "GROK_PROXY_CLEARANCE_FLARESOLVERR_URL=http://127.0.0.1:8191",
                content,
            )
            self.assertIn("GROK_PROXY_CLEARANCE_REFRESH_INTERVAL=600", content)
            self.assertNotIn("docker", runner.read_text().lower())


class AntiBanEnvOverrideTests(unittest.TestCase):
    def test_proxy_nested_env_aliases_override_config(self) -> None:
        from app.platform.config.snapshot import _apply_env

        data = {
            "proxy": {
                "egress": {
                    "mode": "direct",
                    "proxy_url": "",
                    "resource_proxy_url": "",
                },
                "clearance": {
                    "mode": "none",
                    "flaresolverr_url": "",
                    "refresh_interval": 3600,
                    "timeout_sec": 60,
                },
            }
        }
        env = {
            "GROK_PROXY_EGRESS_MODE": "single_proxy",
            "GROK_PROXY_EGRESS_PROXY_URL": "http://privoxy:8118",
            "GROK_PROXY_EGRESS_RESOURCE_PROXY_URL": "http://privoxy:8118",
            "GROK_PROXY_CLEARANCE_MODE": "flaresolverr",
            "GROK_PROXY_CLEARANCE_FLARESOLVERR_URL": "http://flaresolverr:8191",
            "GROK_PROXY_CLEARANCE_REFRESH_INTERVAL": "600",
            "GROK_PROXY_CLEARANCE_TIMEOUT_SEC": "90",
        }

        with patch.dict(os.environ, env, clear=True):
            result = _apply_env(data)

        self.assertEqual(result["proxy"]["egress"]["mode"], "single_proxy")
        self.assertEqual(result["proxy"]["egress"]["proxy_url"], "http://privoxy:8118")
        self.assertEqual(result["proxy"]["egress"]["resource_proxy_url"], "http://privoxy:8118")
        self.assertEqual(result["proxy"]["clearance"]["mode"], "flaresolverr")
        self.assertEqual(result["proxy"]["clearance"]["flaresolverr_url"], "http://flaresolverr:8191")
        self.assertEqual(result["proxy"]["clearance"]["refresh_interval"], "600")
        self.assertEqual(result["proxy"]["clearance"]["timeout_sec"], "90")

    def test_legacy_cf_env_aliases_override_clearance_config(self) -> None:
        from app.platform.config.snapshot import _apply_env

        data = {"proxy": {"clearance": {"mode": "none", "flaresolverr_url": ""}}}
        env = {
            "FLARESOLVERR_URL": "http://flaresolverr:8191",
            "CF_REFRESH_INTERVAL": "600",
            "CF_TIMEOUT": "75",
        }

        with patch.dict(os.environ, env, clear=True):
            result = _apply_env(data)

        self.assertEqual(result["proxy"]["clearance"]["mode"], "flaresolverr")
        self.assertEqual(result["proxy"]["clearance"]["flaresolverr_url"], "http://flaresolverr:8191")
        self.assertEqual(result["proxy"]["clearance"]["refresh_interval"], "600")
        self.assertEqual(result["proxy"]["clearance"]["timeout_sec"], "75")


if __name__ == "__main__":
    unittest.main()
