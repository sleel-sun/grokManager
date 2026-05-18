"""macOS bundle launcher for the GrokManager API server."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from multiprocessing import freeze_support
from pathlib import Path

from dotenv import load_dotenv
from granian.constants import Interfaces
from granian.log import LogLevels
from granian.server import Server


APP_NAME = "grokManager"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _bundle_root() -> Path:
    if _is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[1]


def _load_environment() -> None:
    if _is_frozen():
        support_dir = Path.home() / "Library" / "Application Support" / APP_NAME
        env_file = support_dir / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=False)

        os.environ.setdefault("DATA_DIR", str(support_dir / "data"))
        os.environ.setdefault("LOG_DIR", str(Path.home() / "Library" / "Logs" / APP_NAME))
        Path(os.environ["DATA_DIR"]).mkdir(parents=True, exist_ok=True)
        Path(os.environ["LOG_DIR"]).mkdir(parents=True, exist_ok=True)
    else:
        load_dotenv(override=False)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _open_browser_when_ready(url: str, health_url: str) -> None:
    for _ in range(120):
        try:
            with urllib.request.urlopen(health_url, timeout=1) as response:
                if 200 <= response.status < 300:
                    webbrowser.open(url)
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)


def _should_open_browser() -> bool:
    raw = os.getenv("GROKMANAGER_OPEN_BROWSER", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def main(argv: list[str] | None = None) -> int:
    freeze_support()
    _load_environment()

    default_host = os.getenv("SERVER_HOST") or os.getenv("GRANIAN_HOST") or "0.0.0.0"
    default_port = _int_env("SERVER_PORT", _int_env("GRANIAN_PORT", 8000))

    parser = argparse.ArgumentParser(description="Run the GrokManager API server.")
    parser.add_argument("--host", default=default_host)
    parser.add_argument("--port", type=int, default=default_port)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    local_url = f"http://127.0.0.1:{args.port}"
    admin_url = os.getenv("GROKMANAGER_ADMIN_URL", f"{local_url}/admin")

    if _should_open_browser() and not args.no_browser:
        threading.Thread(
            target=_open_browser_when_ready,
            args=(admin_url, f"{local_url}/health"),
            daemon=True,
        ).start()

    print(f"{APP_NAME} starting on http://{args.host}:{args.port}", flush=True)
    print(f"Data directory: {os.getenv('DATA_DIR', 'data')}", flush=True)
    print(f"Log directory: {os.getenv('LOG_DIR', 'logs')}", flush=True)

    server = Server(
        "app.main:app",
        address=args.host,
        port=args.port,
        interface=Interfaces.ASGI,
        workers=1,
        working_dir=_bundle_root(),
        log_enabled=True,
        log_level=LogLevels.info,
    )
    server.serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
