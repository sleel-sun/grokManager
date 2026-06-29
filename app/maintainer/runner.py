from __future__ import annotations

import argparse
import datetime
import json
import logging
import multiprocessing as mp
import os
import secrets
import shlex
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

import requests
from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import BrowserConnectError, PageDisconnectedError
try:
    from pyvirtualdisplay import Display
except Exception:
    Display = None

from .mailbox import get_email_and_token, get_oai_code
from .settings import (
    as_bool,
    extension_dir,
    get_config_path,
    load_config,
    maintainer_browser_tmp_dir,
    maintainer_log_dir,
    maintainer_sso_dir,
    project_root,
    set_config_path,
)


SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
SIGNIN_URL = "https://accounts.x.ai/sign-in?redirect=grok-com"
GROK_URL = "https://grok.com/"
DEFAULT_MIN_BROWSER_FREE_BYTES = 256 * 1024 * 1024
DEFAULT_HEADLESS_WINDOW_SIZE = "1440,900"
DEFAULT_WORKER_IDLE_TIMEOUT = 600.0
DEFAULT_TURNSTILE_MANUAL_WAIT_SECONDS = 180.0
DEFAULT_TURNSTILE_SOLVER_TIMEOUT_SECONDS = 150.0
DEFAULT_TURNSTILE_SOLVER_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_REGISTRATION_ENV_RETRY_LIMIT = 2
WORKER_TERMINATE_GRACE_SECONDS = 5.0
WORKER_DEBUG_PORT_MIN = 20_000
WORKER_DEBUG_PORT_SPAN = 40_000
HEADLESS_STABILITY_ARGS = (
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
    "--lang=en-US",
    "--password-store=basic",
    "--use-mock-keychain",
)
PROFILE_GIVEN_NAMES = (
    "Ava",
    "Mia",
    "Luna",
    "Nora",
    "Ivy",
    "Zoe",
    "Leo",
    "Noah",
    "Milo",
    "Evan",
    "Owen",
    "Theo",
)
PROFILE_FAMILY_NAMES = (
    "Chen",
    "Lin",
    "Wang",
    "Li",
    "Zhang",
    "Liu",
    "Yang",
    "Wu",
    "Zhou",
    "Xu",
    "Sun",
    "Guo",
)

AUTH_COOKIE_PRIORITY = (
    "sso",
    "sso-rw",
    "xai-sso",
    "xai_session",
    "session",
    "session_token",
    "access_token",
    "auth_token",
)
AUTH_COOKIE_HINTS = ("sso", "token", "auth", "session", "jwt", "access")
IGNORED_AUTH_COOKIE_NAMES = {
    "__cf_bm",
    "cf_clearance",
    "_cfuvid",
    "cf_chl_rc_i",
    "cf_chl_rc_ni",
    "cf_chl_rc_m",
}

browser = None
page = None
_virtual_display = None
run_logger: logging.Logger | None = None
_turnstile_patch_source_cache: str | None = None
_turnstile_patch_browser_id: int | None = None


def setup_run_logger(label: str | None = None) -> logging.Logger:
    """Create the per-run log file.

    ``label`` is woven into the filename so concurrent workers do not collide
    on the same path. Multi-process orchestration uses ``label="w{worker_id}"``
    for each worker and ``label="parallel"`` for the parent orchestrator log.
    """
    log_dir = maintainer_log_dir()
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{label}" if label else ""
    filename = f"run{suffix}_{ts}_pid{os.getpid()}.log"
    log_path = log_dir / filename

    logger_name = f"grok_maintainer.{label}" if label else "grok_maintainer"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    prefix = f"[w{label.removeprefix('w')}] " if label and label.startswith("w") and label[1:].isdigit() else ""
    fmt = logging.Formatter(f"%(asctime)s | {prefix}%(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fallback_reason = ""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
    except OSError as exc:
        fallback_reason = f"{type(exc).__name__}: {exc}"
        log_dir = Path(tempfile.gettempdir()) / "grokmanager-maintainer-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / filename
        fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.info("日志文件: %s", log_path)
    if fallback_reason:
        logger.warning("原日志目录不可写，已降级到临时目录: %s", fallback_reason)
    return logger


def ensure_stable_python_runtime() -> None:
    if sys.version_info < (3, 14) or os.environ.get("DPE_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        print(
            f"[*] 检测到 Python {sys.version.split()[0]}，自动切换到更稳定的解释器: {candidate}"
        )
        env = os.environ.copy()
        env["DPE_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)


def warn_runtime_compatibility() -> None:
    if sys.version_info >= (3, 14):
        print("[提示] 当前 Python 为 3.14+；若出现 TLS 异常，建议改用 Python 3.12 或 3.13。")


ensure_stable_python_runtime()
warn_runtime_compatibility()


def _running_in_container() -> bool:
    return Path("/.dockerenv").exists()


def _discover_browser_path() -> str | None:
    explicit = (
        os.getenv("CHROME_BIN", "").strip()
        or os.getenv("CHROMIUM_BIN", "").strip()
        or os.getenv("MAINTAINER_BROWSER_PATH", "").strip()
    )
    candidates = [
        explicit,
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _resolve_browser_tmp_path() -> Path:
    explicit = os.getenv("MAINTAINER_TMP_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return maintainer_browser_tmp_dir()


def _compute_worker_chrome_user_data_dir(worker_id: int, pid: int) -> Path:
    """Return a unique absolute Chromium ``--user-data-dir`` path for a worker.

    Each parallel worker MUST get its own user-data-dir so its Chromium
    instance does not contend for the SingletonLock / SingletonCookie files
    that Chromium creates inside the profile directory. Without isolation,
    a second Chromium pointed at the same directory will either fail fast
    with ``ProcessSingletonStartup`` or — worse — silently attach to the
    first instance and serialise registration. Both outcomes look to the
    user like "workers run one at a time" even though spawn IS parallel.

    The path lives under the system tempdir so it's on a fast local FS and
    independent of the project root. ``worker_id`` keeps the dirname stable
    enough for log diagnostics; ``pid`` makes it unique across overlapping
    runs (e.g. orchestrator restarts before cleanup finishes).
    """
    base = Path(tempfile.gettempdir())
    return base / f"grokmgr-chrome-w{int(worker_id)}-{int(pid)}"


def _is_tcp_port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False


def _select_worker_chrome_debug_port(worker_id: int, pid: int) -> int:
    """Pick a per-worker Chromium remote-debugging port.

    DrissionPage disables ``auto_port`` whenever ``set_user_data_path()`` is
    called. Parallel workers need explicit ports or they all keep the default
    ``127.0.0.1:9222`` address and then race/fail during browser connection.
    """
    seed = _worker_chrome_debug_port_candidate(worker_id, pid) - WORKER_DEBUG_PORT_MIN
    for offset in range(WORKER_DEBUG_PORT_SPAN):
        port = WORKER_DEBUG_PORT_MIN + ((seed + offset) % WORKER_DEBUG_PORT_SPAN)
        if _is_tcp_port_available(port):
            return port
    raise RuntimeError("无法为并发注册 worker 分配可用 Chrome 调试端口")


def _worker_chrome_debug_port_candidate(worker_id: int, pid: int) -> int:
    seed = (int(pid) + int(worker_id) * 9973) % WORKER_DEBUG_PORT_SPAN
    return WORKER_DEBUG_PORT_MIN + seed


def _select_browser_debug_port(preferred: int = 42222) -> int:
    if _is_tcp_port_available(preferred):
        return preferred
    pid = os.getpid()
    try:
        return _select_worker_chrome_debug_port(0, pid)
    except RuntimeError:
        return _worker_chrome_debug_port_candidate(0, pid)


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{size}B"


def _min_browser_free_bytes() -> int:
    raw = os.getenv("MAINTAINER_MIN_BROWSER_FREE_BYTES", "").strip()
    if not raw:
        return DEFAULT_MIN_BROWSER_FREE_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MIN_BROWSER_FREE_BYTES


def _worker_idle_timeout_seconds() -> float:
    raw = os.getenv("MAINTAINER_WORKER_IDLE_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_WORKER_IDLE_TIMEOUT
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_WORKER_IDLE_TIMEOUT


def _registration_env_retry_limit() -> int:
    raw = os.getenv("MAINTAINER_REGISTRATION_ENV_RETRY_LIMIT", "").strip()
    if not raw:
        raw = str(_web_config_value("registration_env_retry_limit", "") or "").strip()
    if not raw:
        raw = str(_web_config_value("registration_failure_retry_limit", "") or "").strip()
    if not raw:
        return DEFAULT_REGISTRATION_ENV_RETRY_LIMIT
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_REGISTRATION_ENV_RETRY_LIMIT


def _linux_without_display() -> bool:
    return sys.platform.startswith("linux") and not os.environ.get("DISPLAY")


def _browser_effective_headless() -> bool:
    _no_display = _linux_without_display()
    return as_bool(os.getenv("MAINTAINER_HEADLESS"), default=_no_display)


def _turnstile_auto_manual_wait_seconds() -> float:
    if _browser_effective_headless():
        return 0.0
    if _virtual_display is not None:
        return 0.0
    if as_bool(os.getenv("MAINTAINER_USE_XVFB"), default=False):
        return 0.0
    if _linux_without_display():
        return 0.0
    return DEFAULT_TURNSTILE_MANUAL_WAIT_SECONDS


def _turnstile_manual_wait_seconds() -> float:
    raw = os.getenv("MAINTAINER_TURNSTILE_MANUAL_WAIT_SEC", "").strip()
    if not raw:
        try:
            web_conf = load_config().get("web", {})
            if isinstance(web_conf, dict):
                raw = str(
                    web_conf.get(
                        "turnstile_manual_wait_sec",
                        web_conf.get("turnstile_manual_wait_seconds", ""),
                    )
                    or ""
                ).strip()
        except Exception:
            raw = ""
    if not raw:
        return _turnstile_auto_manual_wait_seconds()
    if raw.lower() in {"off", "false", "disabled", "disable", "none", "no"}:
        return 0.0
    try:
        seconds = max(0.0, float(raw))
    except ValueError:
        return _turnstile_auto_manual_wait_seconds()
    if seconds == 0:
        return _turnstile_auto_manual_wait_seconds()
    return seconds


def _web_config_value(key: str, default: Any = None) -> Any:
    try:
        web_conf = load_config().get("web", {})
    except Exception:
        return default
    if not isinstance(web_conf, dict):
        return default
    return web_conf.get(key, default)


def _turnstile_solver_settings() -> dict[str, Any]:
    env_provider = os.getenv("MAINTAINER_TURNSTILE_SOLVER_PROVIDER", "").strip()
    provider = env_provider or str(_web_config_value("turnstile_solver_provider", "") or "").strip()
    provider = provider.lower().replace("-", "").replace("_", "")

    api_key = (
        os.getenv("MAINTAINER_TURNSTILE_SOLVER_API_KEY", "").strip()
        or str(_web_config_value("turnstile_solver_api_key", "") or "").strip()
    )
    if not api_key and provider == "capsolver":
        api_key = os.getenv("CAPSOLVER_API_KEY", "").strip()
    if not api_key and provider in {"twocaptcha", "2captcha", "two captcha"}:
        api_key = (
            os.getenv("TWOCAPTCHA_API_KEY", "").strip()
            or os.getenv("TWO_CAPTCHA_API_KEY", "").strip()
            or os.getenv("2CAPTCHA_API_KEY", "").strip()
        )
    if not provider and os.getenv("CAPSOLVER_API_KEY", "").strip():
        provider = "capsolver"
        api_key = os.getenv("CAPSOLVER_API_KEY", "").strip()
    if not provider and (
        os.getenv("TWOCAPTCHA_API_KEY", "").strip()
        or os.getenv("TWO_CAPTCHA_API_KEY", "").strip()
        or os.getenv("2CAPTCHA_API_KEY", "").strip()
    ):
        provider = "2captcha"
        api_key = (
            os.getenv("TWOCAPTCHA_API_KEY", "").strip()
            or os.getenv("TWO_CAPTCHA_API_KEY", "").strip()
            or os.getenv("2CAPTCHA_API_KEY", "").strip()
        )

    if provider in {"", "off", "false", "disabled", "disable", "none", "no"}:
        return {"enabled": False, "provider": "", "api_key": ""}
    if provider in {"twocaptcha", "2captcha", "two captcha"}:
        provider = "2captcha"
    if provider not in {"capsolver", "2captcha"}:
        return {
            "enabled": False,
            "provider": provider,
            "api_key": api_key,
            "error": f"unsupported provider {provider}",
        }
    if not api_key:
        return {
            "enabled": False,
            "provider": provider,
            "api_key": "",
            "error": "missing api key",
        }

    def read_float(env_key: str, config_key: str, default: float) -> float:
        raw = os.getenv(env_key, "").strip()
        if not raw:
            raw = str(_web_config_value(config_key, "") or "").strip()
        if not raw:
            return default
        try:
            return max(1.0, float(raw))
        except ValueError:
            return default

    return {
        "enabled": True,
        "provider": provider,
        "api_key": api_key,
        "timeout": read_float(
            "MAINTAINER_TURNSTILE_SOLVER_TIMEOUT_SEC",
            "turnstile_solver_timeout_sec",
            DEFAULT_TURNSTILE_SOLVER_TIMEOUT_SECONDS,
        ),
        "poll_interval": read_float(
            "MAINTAINER_TURNSTILE_SOLVER_POLL_SEC",
            "turnstile_solver_poll_sec",
            DEFAULT_TURNSTILE_SOLVER_POLL_INTERVAL_SECONDS,
        ),
    }


def _turnstile_patch_source() -> str:
    global _turnstile_patch_source_cache
    if _turnstile_patch_source_cache is not None:
        return _turnstile_patch_source_cache

    script_path = extension_dir() / "script.js"
    try:
        source = script_path.read_text(encoding="utf-8")
    except OSError:
        source = ""
    _turnstile_patch_source_cache = source
    return source


def _install_turnstile_patch() -> None:
    """Install the lightweight Turnstile mouse-event patch without loading a Chrome extension."""
    global _turnstile_patch_browser_id
    if page is None or browser is None:
        return
    source = _turnstile_patch_source().strip()
    if not source:
        return

    browser_id = id(browser)
    if _turnstile_patch_browser_id != browser_id:
        try:
            page.run_cdp("Page.addScriptToEvaluateOnNewDocument", source=source)
            _turnstile_patch_browser_id = browser_id
        except Exception as exc:
            print(f"[Debug] Turnstile patch CDP 注入失败: {str(exc)[:200]}")
            if run_logger:
                run_logger.warning("Turnstile patch CDP 注入失败: %s", exc)

    try:
        page.run_js(source)
    except Exception:
        pass


def _ensure_browser_storage_ready(path_like: str | os.PathLike[str]) -> Path:
    path = Path(path_like).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(path)
    except OSError as exc:
        raise RuntimeError(f"浏览器临时目录不可用: {path} ({exc})") from exc

    min_free = _min_browser_free_bytes()
    if usage.free < min_free:
        raise RuntimeError(
            "浏览器临时目录可用空间不足: "
            f"{path} free={_format_bytes(usage.free)} required={_format_bytes(min_free)}。"
            "请清理磁盘空间后重试，或设置 MAINTAINER_TMP_PATH 指向空间充足的目录。"
        )

    return path


def _ensure_virtual_display() -> None:
    global _virtual_display
    if _virtual_display is not None:
        return
    # Linux 无 DISPLAY → 自动视为 headless，无需虚拟显示器
    _no_display = _linux_without_display()
    if os.environ.get("DISPLAY") or as_bool(os.getenv("MAINTAINER_HEADLESS"), default=_no_display):
        return
    if not as_bool(os.getenv("MAINTAINER_USE_XVFB"), default=_running_in_container()):
        return
    if Display is None:
        raise RuntimeError(
            "PyVirtualDisplay 不可用，无法在无 DISPLAY 环境下启动浏览器。"
        )

    width = int(os.getenv("MAINTAINER_DISPLAY_WIDTH", "1440"))
    height = int(os.getenv("MAINTAINER_DISPLAY_HEIGHT", "900"))
    _virtual_display = Display(visible=False, size=(width, height))
    _virtual_display.start()


def _stop_virtual_display() -> None:
    global _virtual_display
    if _virtual_display is None:
        return
    try:
        _virtual_display.stop()
    except Exception:
        pass
    _virtual_display = None


def _extra_chromium_args_from_env() -> list[str]:
    raw = os.getenv("MAINTAINER_CHROME_ARGS", "").strip()
    if not raw:
        return []
    try:
        return shlex.split(raw)
    except ValueError:
        return raw.split()


def _configure_browser_options() -> ChromiumOptions:
    opts = ChromiumOptions()
    # 默认使用固定端口而非 auto_port，解决 Snap Chromium 端口绑定问题
    # 并行 worker 会通过 MAINTAINER_CHROME_DEBUG_PORT 环境变量覆盖此端口
    debug_port = os.getenv("MAINTAINER_CHROME_DEBUG_PORT", "").strip()
    if debug_port:
        try:
            port_int = int(debug_port)
        except ValueError as exc:
            raise RuntimeError(
                f"MAINTAINER_CHROME_DEBUG_PORT 必须是端口号: {debug_port}"
            ) from exc
        if port_int <= 0 or port_int > 65535:
            raise RuntimeError(
                f"MAINTAINER_CHROME_DEBUG_PORT 超出有效端口范围: {debug_port}"
            )
    else:
        port_int = _select_browser_debug_port()
    opts.set_local_port(port_int)
    opts.set_tmp_path(str(_resolve_browser_tmp_path()))
    opts.set_timeouts(base=15)
    # 不通过扩展加载 turnstilePatch（Snap Chromium 兼容性问题）
    # 改为在页面加载时直接注入 script.js 内容
    # opts.add_extension(str(extension_dir()))

    browser_path = _discover_browser_path()
    if browser_path:
        opts.set_browser_path(browser_path)

    user_data_dir = os.getenv("MAINTAINER_CHROME_USER_DATA_DIR", "").strip()
    if user_data_dir:
        opts.set_user_data_path(str(Path(user_data_dir).expanduser().resolve()))

    proxy_url = os.getenv("MAINTAINER_PROXY", "").strip()
    if proxy_url:
        opts.set_argument(f"--proxy-server={proxy_url}")

    opts.set_argument("--no-first-run")
    opts.set_argument("--no-default-browser-check")

    # Linux 无 DISPLAY → 自动启用 headless，避免裸启动报错
    _no_display = _linux_without_display()
    is_headless = as_bool(os.getenv("MAINTAINER_HEADLESS"), default=_no_display)
    if is_headless:
        opts.headless(True)
        for arg in HEADLESS_STABILITY_ARGS:
            opts.set_argument(arg)

    # Linux（非 Windows）默认开启 no-sandbox / disable-dev-shm
    # Chrome 以 root 运行时必须 --no-sandbox，否则直接拒绝启动
    _default_sandbox = sys.platform != "win32"
    if as_bool(os.getenv("MAINTAINER_NO_SANDBOX"), default=_default_sandbox):
        opts.set_argument("--no-sandbox")
    if as_bool(os.getenv("MAINTAINER_DISABLE_DEV_SHM"), default=_default_sandbox):
        opts.set_argument("--disable-dev-shm-usage")

    window_size = os.getenv("MAINTAINER_WINDOW_SIZE", "").strip()
    if is_headless and not window_size:
        window_size = DEFAULT_HEADLESS_WINDOW_SIZE
    if window_size:
        opts.set_argument("--window-size", window_size)

    for arg in _extra_chromium_args_from_env():
        opts.set_argument(arg)

    return opts


co = _configure_browser_options()


def default_sso_file() -> Path:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return maintainer_sso_dir() / f"sso_{ts}.txt"


def resolve_user_path(path_like: str) -> Path:
    path = Path(path_like).expanduser()
    if not path.is_absolute():
        path = project_root() / path
    return path.resolve()


def start_browser():
    global browser, page
    _ensure_virtual_display()
    _ensure_browser_storage_ready(co.tmp_path or _resolve_browser_tmp_path())
    try:
        browser = Chromium(co)
    except BrowserConnectError:
        if not os.getenv("MAINTAINER_CHROME_DEBUG_PORT", "").strip():
            time.sleep(1)
            _reset_isolated_browser_profile()
            retry_port = _select_browser_debug_port(
                _worker_chrome_debug_port_candidate(0, os.getpid())
            )
            co.set_local_port(retry_port)
            try:
                browser = Chromium(co)
            except BrowserConnectError:
                raise RuntimeError(
                    "BrowserConnectError: 浏览器连接失败。请检查：\n"
                    "1. 设置 MAINTAINER_CHROME_USER_DATA_DIR 为一个不与其他 Chrome 冲突的路径\n"
                    "2. 无界面系统请设置 MAINTAINER_HEADLESS=true\n"
                    "3. Linux 系统请设置 MAINTAINER_NO_SANDBOX=true 和 MAINTAINER_DISABLE_DEV_SHM=true\n"
                    "4. 如需固定调试端口，设置 MAINTAINER_CHROME_DEBUG_PORT（如 9222）"
                ) from None
        else:
            raise RuntimeError(
                "BrowserConnectError: 浏览器连接失败。请检查：\n"
                "1. 设置 MAINTAINER_CHROME_USER_DATA_DIR 为一个不与其他 Chrome 冲突的路径\n"
                "2. 无界面系统请设置 MAINTAINER_HEADLESS=true\n"
                "3. Linux 系统请设置 MAINTAINER_NO_SANDBOX=true 和 MAINTAINER_DISABLE_DEV_SHM=true\n"
                "4. 如需固定调试端口，设置 MAINTAINER_CHROME_DEBUG_PORT（如 9222）"
            ) from None
    tabs = browser.get_tabs()
    page = tabs[-1] if tabs else browser.new_tab()
    _install_turnstile_patch()
    return browser, page


def stop_browser() -> None:
    global browser, page, _turnstile_patch_browser_id
    if browser is not None:
        try:
            browser.quit()
        except Exception:
            pass
    browser = None
    page = None
    _turnstile_patch_browser_id = None
    _stop_virtual_display()


def _reset_isolated_browser_profile() -> None:
    user_data_dir = os.getenv("MAINTAINER_CHROME_USER_DATA_DIR", "").strip()
    if not user_data_dir:
        return

    path = Path(user_data_dir).expanduser()
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def reset_browser_for_next_round() -> None:
    stop_browser()
    _reset_isolated_browser_profile()
    start_browser()


def restart_browser() -> None:
    stop_browser()
    start_browser()


def refresh_active_page():
    global browser, page
    if browser is None:
        start_browser()
    try:
        tabs = browser.get_tabs()
        if tabs:
            page = tabs[-1]
        else:
            page = browser.new_tab()
    except Exception:
        restart_browser()
    _install_turnstile_patch()
    return page


def _safe_page_url() -> str:
    try:
        refresh_active_page()
        return str(getattr(page, "url", "") or "")
    except Exception:
        return ""


def _cookie_attr(item: Any, name: str) -> str:
    if isinstance(item, dict):
        return str(item.get(name, "") or "").strip()
    return str(getattr(item, name, "") or "").strip()


def _collect_cookie_items() -> list[Any]:
    """Read browser cookies through every DrissionPage surface we can use."""
    items: list[Any] = []

    try:
        items.extend(page.cookies(all_domains=True, all_info=True) or [])
    except Exception:
        pass

    for owner in (page, browser):
        run_cdp = getattr(owner, "run_cdp", None)
        if not callable(run_cdp):
            continue
        try:
            payload = run_cdp("Network.getAllCookies")
        except Exception:
            continue
        cookies = payload.get("cookies") if isinstance(payload, dict) else None
        if isinstance(cookies, list):
            items.extend(cookies)

    return items


def _extract_auth_token_from_cookie_items(items: list[Any]) -> tuple[str, str] | None:
    candidates: list[tuple[str, str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()

    for item in items:
        name = _cookie_attr(item, "name")
        value = _cookie_attr(item, "value")
        domain = _cookie_attr(item, "domain") or _cookie_attr(item, "host_key")
        if not name or not value:
            continue
        key = (name, value)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        candidates.append((name, value, domain))

    for expected in AUTH_COOKIE_PRIORITY:
        for name, value, domain in candidates:
            if name.lower() == expected and value:
                label = f"{name}@{domain}" if domain else name
                return value, label

    for name, value, domain in candidates:
        lower_name = name.lower()
        if lower_name in IGNORED_AUTH_COOKIE_NAMES:
            continue
        if len(value) <= 20:
            continue
        if any(hint in lower_name for hint in AUTH_COOKIE_HINTS) or value.startswith("eyJ"):
            label = f"{name}@{domain}" if domain else name
            return value, label

    return None


def _extract_auth_token_from_storage_candidates(items: list[dict[str, Any]]) -> tuple[str, str] | None:
    for item in items:
        key = str(item.get("key", "") or "").strip()
        value = str(item.get("value", "") or "").strip()
        source = str(item.get("source", "") or "").strip()
        lower_key = key.lower()
        if len(value) <= 20 or "sso" not in lower_key:
            continue
        return value, f"{source}:{key}" if source else key
    return None


def _collect_web_storage_candidates() -> list[dict[str, Any]]:
    try:
        result = page.run_js(
            r"""
const hints = ['sso', 'token', 'auth', 'session', 'jwt', 'access'];
const out = [];
function scan(storage, source) {
    if (!storage) {
        return;
    }
    for (let i = 0; i < storage.length; i += 1) {
        const key = String(storage.key(i) || '');
        const value = String(storage.getItem(key) || '');
        const haystack = `${key}\n${value}`.toLowerCase();
        if (value.length > 20 && hints.some((hint) => haystack.includes(hint))) {
            out.push({ source, key, value });
        }
    }
}
try { scan(window.localStorage, 'localStorage'); } catch (e) {}
try { scan(window.sessionStorage, 'sessionStorage'); } catch (e) {}
return out.slice(0, 20);
            """
        )
    except Exception:
        return []
    return result if isinstance(result, list) else []


def _maintainer_flaresolverr_url() -> str:
    for key in (
        "MAINTAINER_FLARESOLVERR_URL",
        "GROK_PROXY_CLEARANCE_FLARESOLVERR_URL",
        "FLARESOLVERR_URL",
    ):
        value = os.getenv(key, "").strip()
        if value:
            return value
    return ""


def _maintainer_flaresolverr_timeout() -> int:
    raw = (
        os.getenv("MAINTAINER_FLARESOLVERR_TIMEOUT_SEC", "").strip()
        or os.getenv("GROK_PROXY_CLEARANCE_TIMEOUT_SEC", "").strip()
        or os.getenv("CF_TIMEOUT", "").strip()
        or "60"
    )
    try:
        return max(5, int(raw))
    except ValueError:
        return 60


def _cookie_param_from_flaresolverr(cookie: dict[str, Any]) -> dict[str, Any] | None:
    name = str(cookie.get("name", "") or "").strip()
    value = str(cookie.get("value", "") or "")
    if not name or not value:
        return None

    param: dict[str, Any] = {
        "name": name,
        "value": value,
        "path": str(cookie.get("path", "") or "/"),
    }
    domain = str(cookie.get("domain", "") or "").strip()
    if domain:
        param["domain"] = domain
    if "secure" in cookie:
        param["secure"] = bool(cookie.get("secure"))
    if "httpOnly" in cookie:
        param["httpOnly"] = bool(cookie.get("httpOnly"))
    if cookie.get("sameSite") in {"Strict", "Lax", "None"}:
        param["sameSite"] = cookie["sameSite"]

    expires = cookie.get("expires", cookie.get("expiry"))
    if expires:
        try:
            param["expires"] = float(expires)
        except (TypeError, ValueError):
            pass

    return param


def _inject_flaresolverr_solution(solution: dict[str, Any]) -> int:
    cookies = solution.get("cookies")
    if not isinstance(cookies, list):
        return 0

    cookie_params = [
        param
        for param in (_cookie_param_from_flaresolverr(cookie) for cookie in cookies)
        if param
    ]
    if not cookie_params:
        return 0

    user_agent = str(solution.get("userAgent", "") or "").strip()
    try:
        page.run_cdp("Network.enable")
    except Exception:
        pass
    if user_agent:
        try:
            page.run_cdp("Network.setUserAgentOverride", userAgent=user_agent)
        except Exception:
            pass

    page.run_cdp("Network.setCookies", cookies=cookie_params)
    return len(cookie_params)


def _prewarm_cloudflare_clearance(target_url: str = SIGNUP_URL) -> bool:
    fs_url = _maintainer_flaresolverr_url()
    if not fs_url:
        return False

    timeout_sec = _maintainer_flaresolverr_timeout()
    payload: dict[str, Any] = {
        "cmd": "request.get",
        "url": target_url,
        "maxTimeout": timeout_sec * 1000,
    }
    fs_proxy = os.getenv("MAINTAINER_FLARESOLVERR_PROXY", "").strip()
    if fs_proxy:
        payload["proxy"] = {"url": fs_proxy}

    request = urllib_request.Request(
        f"{fs_url.rstrip('/')}/v1",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib_request.urlopen(request, timeout=timeout_sec + 30) as response:
            result = json.loads(response.read().decode("utf-8", "replace"))
    except HTTPError as exc:
        body_text = exc.read().decode("utf-8", "replace")[:300]
        if run_logger:
            run_logger.warning(
                "FlareSolverr 预热失败: status=%s body=%s",
                exc.code,
                body_text,
            )
        return False
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        if run_logger:
            run_logger.warning("FlareSolverr 预热失败: %s", exc)
        return False

    if result.get("status") != "ok":
        if run_logger:
            run_logger.warning(
                "FlareSolverr 返回非 ok: status=%s message=%s",
                result.get("status"),
                result.get("message", ""),
            )
        return False

    solution = result.get("solution")
    if not isinstance(solution, dict):
        return False

    try:
        injected = _inject_flaresolverr_solution(solution)
    except Exception as exc:
        if run_logger:
            run_logger.warning("FlareSolverr cookies 注入失败: %s", exc)
        return False

    if injected:
        print(f"[*] 已注入 FlareSolverr clearance cookies: {injected}")
        if run_logger:
            run_logger.info("已注入 FlareSolverr clearance cookies: %s", injected)
        return True
    return False


def _click_cloudflare_challenge() -> str:
    """Try a real mouse click on an interactive Cloudflare challenge."""
    refresh_active_page()

    try:
        frames = page.get_frames() or []
    except Exception:
        frames = []

    for frame in frames:
        for locator in (
            'css:input[type="checkbox"]',
            "tag:input",
            "tag:label",
            "tag:button",
        ):
            try:
                target = frame.ele(locator, timeout=0.2)
            except Exception:
                target = None
            if not target:
                continue
            try:
                page.actions.click(target)
                return f"frame-element:{locator}"
            except Exception:
                try:
                    target.click.left(by_js=False)
                    return f"frame-element:{locator}"
                except Exception:
                    continue

    try:
        clicked = page.run_js(
            r"""
function normalize(value) {
    return String(value || '')
        .replace(/[\s\u200b-\u200d\ufeff]+/g, '')
        .toLowerCase();
}

function collectElements(root) {
    const out = [];
    const seenRoots = new Set();

    function walk(currentRoot) {
        if (!currentRoot || seenRoots.has(currentRoot)) {
            return;
        }
        seenRoots.add(currentRoot);

        let nodes = [];
        try {
            nodes = Array.from(currentRoot.querySelectorAll('*'));
        } catch (e) {
            return;
        }

        for (const node of nodes) {
            out.push(node);
            if (node.shadowRoot) {
                walk(node.shadowRoot);
            }
        }
    }

    walk(root);
    return out;
}

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function nodeText(node) {
    const attrs = ['aria-label', 'title', 'data-testid', 'id', 'name'];
    const parts = [node.innerText, node.textContent];
    for (const attr of attrs) {
        parts.push(node.getAttribute?.(attr));
    }
    return normalize(parts.filter(Boolean).join(' '));
}

function hasChallengeIntent(text) {
    const terms = [
        'verifyyouarehuman',
        'verifyhuman',
        'confirmyouarehuman',
        'checkingifyouarehuman',
        'iamhuman',
        'notarobot',
        'turnstile',
        'captcha',
        '验证您是真人',
        '确认您是真人',
        '真人验证',
        '正在检查',
        '人机验证',
    ];
    return terms.some((term) => text.includes(term));
}

const elements = collectElements(document);
const target = elements.find((node) => {
    if (!isVisible(node)) {
        return false;
    }
    const tag = String(node.tagName || '').toLowerCase();
    const role = String(node.getAttribute?.('role') || '').toLowerCase();
    if (!['input', 'button', 'label', 'a', 'div', 'span'].includes(tag) && role !== 'button') {
        return false;
    }
    if (node.disabled || node.getAttribute?.('aria-disabled') === 'true') {
        return false;
    }
    const type = String(node.getAttribute?.('type') || '').toLowerCase();
    return type === 'checkbox' || hasChallengeIntent(nodeText(node));
}) || null;

if (!target) {
    return false;
}

target.scrollIntoView?.({ block: 'center', inline: 'center' });
target.focus?.();
const rect = target.getBoundingClientRect();
const x = Math.max(1, Math.floor(rect.left + Math.min(rect.width / 2, 32)));
const y = Math.max(1, Math.floor(rect.top + rect.height / 2));
target.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y }));
target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y }));
target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y }));
target.click();
return true;
            """
        )
        if clicked:
            return "dom-element"
    except Exception:
        pass

    try:
        iframes = page.eles("tag:iframe", timeout=0.2) or []
    except Exception:
        iframes = []

    for iframe in iframes:
        try:
            src = str(iframe.attr("src") or "")
            title = str(iframe.attr("title") or "")
            name = str(iframe.attr("name") or "")
            marker = f"{src}\n{title}\n{name}".lower()
        except Exception:
            marker = ""

        is_cloudflare = any(
            item in marker
            for item in ("cloudflare", "turnstile", "challenge", "cf-chl", "captcha")
        )
        if not is_cloudflare and marker:
            continue

        try:
            width, height = iframe.rect.size
        except Exception:
            width, height = 300, 80

        click_x = max(8, min(36, int(width) - 4))
        click_y = max(8, min(max(24, int(height) // 2), int(height) - 4))
        try:
            page.actions.move_to(
                iframe,
                offset_x=click_x,
                offset_y=click_y,
                duration=0.35,
            ).click()
            return "iframe-coordinate"
        except Exception:
            try:
                page.actions.click(iframe)
                return "iframe-center"
            except Exception:
                continue

    return "not-found"


def _click_post_signup_continue_button() -> bool:
    try:
        return bool(
            page.run_js(
                r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const target = candidates.find((node) => {
    const text = String(node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return text.includes('continuetogrok')
        || text.includes('gotogrok')
        || text.includes('startusinggrok')
        || text.includes('continue')
        || text.includes('getstarted')
        || text.includes('accept')
        || text.includes('agree')
        || text.includes('继续')
        || text.includes('进入grok')
        || text.includes('开始使用')
        || text.includes('接受')
        || text.includes('同意');
});
if (!target) {
    return false;
}
target.focus();
target.click();
return true;
                """
            )
        )
    except Exception:
        return False


def _fill_visible_input(selectors: str, value: str) -> str:
    return str(
        page.run_js(
            r"""
const selectors = arguments[0];
const value = String(arguments[1] || '');

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setNativeValue(input, nextValue) {
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }
    if (nativeSetter) {
        nativeSetter.call(input, '');
        nativeSetter.call(input, nextValue);
    } else {
        input.value = '';
        input.value = nextValue;
    }
    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: nextValue,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: nextValue,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const input = Array.from(document.querySelectorAll(selectors)).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;
if (!input) {
    return 'not-ready';
}
input.focus();
input.click();
setNativeValue(input, value);
input.blur();
return String(input.value || '') === value ? 'filled' : 'mismatch';
            """,
            selectors,
            value,
        )
    )


def _click_auth_submit_button(kind: str) -> str:
    return str(
        page.run_js(
            r"""
const kind = String(arguments[0] || '').toLowerCase();

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, a, [role="button"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const target = buttons.find((node) => {
    const text = String(node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    if (!text && node.tagName.toLowerCase() !== 'button') {
        return false;
    }
    if (kind === 'email') {
        return text.includes('continue')
            || text.includes('next')
            || text.includes('signin')
            || text.includes('login')
            || text.includes('email')
            || text.includes('继续')
            || text.includes('下一步')
            || text.includes('登录')
            || text.includes('邮箱');
    }
    return text.includes('signin')
        || text.includes('login')
        || text.includes('continue')
        || text.includes('next')
        || text.includes('登录')
        || text.includes('继续')
        || text.includes('下一步');
});
if (!target) {
    return 'no-button';
}
target.focus();
target.click();
return 'clicked';
            """,
            kind,
        )
    )


def sign_in_existing_account(email: str, password: str, timeout: int = 90) -> None:
    print("[*] 未检测到注册后的 sso cookie，尝试使用刚注册的邮箱密码登录兜底。")
    if run_logger:
        run_logger.info("未检测到注册后的 sso cookie，开始登录兜底: email=%s", email)

    refresh_active_page()
    try:
        page.get(SIGNIN_URL)
    except Exception:
        refresh_active_page()
        page.get(SIGNIN_URL)

    deadline = time.time() + timeout
    email_done = False
    password_done = False

    while time.time() < deadline:
        refresh_active_page()
        _click_post_signup_continue_button()

        if not email_done:
            filled = _fill_visible_input(
                'input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]',
                email,
            )
            if filled == "filled":
                clicked = _click_auth_submit_button("email")
                if clicked == "clicked":
                    email_done = True
                    print(f"[*] 登录兜底已提交邮箱: {email}")
                    time.sleep(1.5)
                    continue

        filled_password = _fill_visible_input(
            'input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="current-password"]',
            password,
        )
        if filled_password == "filled":
            clicked = _click_auth_submit_button("password")
            if clicked == "clicked":
                password_done = True
                print("[*] 登录兜底已提交密码。")
                time.sleep(3)
                return

        time.sleep(0.8)

    state = "email_submitted" if email_done else "email_not_submitted"
    if password_done:
        state = "password_submitted"
    raise RuntimeError(f"登录兜底未完成: {state}, url={_safe_page_url()}")


def open_signup_page() -> None:
    global page
    refresh_active_page()
    _prewarm_cloudflare_clearance(SIGNUP_URL)
    try:
        page.get(SIGNUP_URL)
    except Exception:
        refresh_active_page()
        page = browser.new_tab(SIGNUP_URL)
    click_email_signup_button()


def has_profile_form() -> bool:
    refresh_active_page()
    try:
        return bool(
            page.run_js(
                """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
                """
            )
        )
    except Exception:
        return False


def click_email_signup_button(timeout: int = 90) -> bool:
    deadline = time.time() + timeout
    last_result: Any = None
    last_cloudflare_click = 0.0
    clearance_prewarmed = False
    while time.time() < deadline:
        refresh_active_page()
        try:
            result = page.run_js(
                r"""
function normalize(value) {
    return String(value || '')
        .replace(/[\s\u200b-\u200d\ufeff]+/g, '')
        .toLowerCase();
}

function collectElements(root) {
    const out = [];
    const seenRoots = new Set();

    function walk(currentRoot) {
        if (!currentRoot || seenRoots.has(currentRoot)) {
            return;
        }
        seenRoots.add(currentRoot);

        let nodes = [];
        try {
            nodes = Array.from(currentRoot.querySelectorAll('*'));
        } catch (e) {
            return;
        }

        for (const node of nodes) {
            out.push(node);
            if (node.shadowRoot) {
                walk(node.shadowRoot);
            }
        }
    }

    walk(root);
    return out;
}

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function isDisabled(node) {
    return !!(
        node.disabled
        || node.getAttribute('aria-disabled') === 'true'
        || node.getAttribute('disabled') !== null
    );
}

function nodeText(node) {
    const attrs = [
        'aria-label',
        'title',
        'data-testid',
        'data-test',
        'id',
        'name',
        'href',
    ];
    const parts = [
        node.innerText,
        node.textContent,
    ];
    for (const attr of attrs) {
        parts.push(node.getAttribute?.(attr));
    }
    try {
        for (const img of Array.from(node.querySelectorAll('img[alt]'))) {
            parts.push(img.getAttribute('alt'));
        }
    } catch (e) {}
    return normalize(parts.filter(Boolean).join(' '));
}

function rawNodeText(node) {
    const parts = [
        node.innerText,
        node.textContent,
        node.getAttribute?.('aria-label'),
        node.getAttribute?.('title'),
        node.getAttribute?.('data-testid'),
    ];
    return String(parts.filter(Boolean).join(' ')).replace(/\s+/g, ' ').trim();
}

function hasEmailSignupIntent(text) {
    const terms = [
        '使用邮箱注册',
        '使用电子邮件注册',
        '用邮箱注册',
        '通过邮箱注册',
        '邮箱注册',
        '电子邮件注册',
        '使用邮箱继续',
        '使用电子邮件继续',
        '邮箱继续',
        '电子邮件继续',
        'signupwithemail',
        'signupemail',
        'emailsignup',
        'registerwithemail',
        'registeremail',
        'emailregistration',
        'continuewithemail',
        'useemail',
        'email',
    ];
    return terms.some((term) => text.includes(term));
}

function isClickable(node) {
    if (!node || !node.tagName) {
        return false;
    }
    const tag = node.tagName.toLowerCase();
    const role = String(node.getAttribute('role') || '').toLowerCase();
    const tabIndex = node.getAttribute('tabindex');
    const style = window.getComputedStyle(node);
    return tag === 'button'
        || tag === 'a'
        || tag === 'label'
        || role === 'button'
        || role === 'link'
        || tabIndex !== null
        || typeof node.onclick === 'function'
        || style.cursor === 'pointer';
}

function clickableTarget(node) {
    if (!node) {
        return null;
    }
    if (isClickable(node)) {
        return node;
    }
    try {
        return node.closest('button, a, label, [role="button"], [role="link"], [tabindex]');
    } catch (e) {
        return null;
    }
}

const elements = collectElements(document);
const emailInput = elements.find((node) => {
    return node.matches?.('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')
        && isVisible(node)
        && !isDisabled(node)
        && !node.readOnly;
});
if (emailInput) {
    return { status: 'email-form-ready' };
}

const pageTitle = String(document.title || '');
const pageText = String(document.body?.innerText || '');
const frameHints = Array.from(document.querySelectorAll('iframe')).map((node) => {
    return [
        node.getAttribute('src') || '',
        node.getAttribute('title') || '',
        node.getAttribute('name') || '',
    ].join(' ');
});
const hasCloudflareFrame = frameHints.some((value) => {
    return /cloudflare|turnstile|challenge|cf-chl|captcha/i.test(value);
});
const combinedText = `${pageTitle}\n${pageText}`;
const cloudflareHardBlocked = /Attention Required/i.test(pageTitle)
    || /Sorry,\s*you have been blocked/i.test(pageText)
    || /You are unable to access\s+x\.ai/i.test(pageText);
const cloudflareChallenge = hasCloudflareFrame
    || /Just a moment|Checking if|Verify you are human|checking.*secure|cf-challenge|cf-browser|turnstile|captcha|正在检查|验证您是真人|确认您是真人|人机验证/i.test(combinedText);
if (cloudflareHardBlocked || cloudflareChallenge) {
    return {
        status: cloudflareHardBlocked && !cloudflareChallenge ? 'cloudflare-hard-blocked' : 'cloudflare-challenge',
        url: String(window.location.href || ''),
        readyState: String(document.readyState || ''),
        title: pageTitle,
        text: pageText.slice(0, 500),
        frames: frameHints.slice(0, 10),
    };
}

const clickable = elements.filter((node) => isVisible(node) && !isDisabled(node) && isClickable(node));
let target = clickable.find((node) => hasEmailSignupIntent(nodeText(node))) || null;
if (!target) {
    const intentNode = elements.find((node) => isVisible(node) && hasEmailSignupIntent(nodeText(node))) || null;
    target = clickableTarget(intentNode);
}

if (!target || !isVisible(target) || isDisabled(target)) {
    const candidates = clickable
        .map(rawNodeText)
        .filter(Boolean)
        .slice(0, 20);
    return {
        status: 'not-found',
        url: String(window.location.href || ''),
        readyState: String(document.readyState || ''),
        candidates,
    };
}

target.scrollIntoView?.({ block: 'center', inline: 'center' });
target.focus?.();
target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
target.click();
return { status: 'clicked', text: rawNodeText(target).slice(0, 120) };
                """
            )
        except PageDisconnectedError:
            refresh_active_page()
            time.sleep(0.5)
            continue
        except Exception as exc:
            last_result = f"js-error: {exc}"
            time.sleep(0.5)
            continue

        last_result = result
        if result is True:
            return True
        if isinstance(result, dict) and result.get("status") in {
            "clicked",
            "email-form-ready",
        }:
            return True

        if isinstance(result, dict) and result.get("status") in {
            "cloudflare-blocked",
            "cloudflare-hard-blocked",
            "cloudflare-challenge",
        }:
            if not clearance_prewarmed and _prewarm_cloudflare_clearance(SIGNUP_URL):
                clearance_prewarmed = True
                try:
                    page.get(SIGNUP_URL)
                except Exception:
                    pass
                time.sleep(2)
                continue
            clearance_prewarmed = True

            now = time.monotonic()
            if now - last_cloudflare_click >= 2.0:
                last_cloudflare_click = now
                click_result = _click_cloudflare_challenge()
                result["click_result"] = click_result
                last_result = result
                if click_result != "not-found":
                    print(f"[*] 已尝试点击 Cloudflare 检测: {click_result}")
                    time.sleep(3)
                    continue

        time.sleep(0.5)

    detail = ""
    if isinstance(last_result, dict):
        url = str(last_result.get("url", "") or "").strip()
        ready_state = str(last_result.get("readyState", "") or "").strip()
        candidates = last_result.get("candidates")
        parts = []
        if url:
            parts.append(f"url={url}")
        if ready_state:
            parts.append(f"readyState={ready_state}")
        if isinstance(candidates, list) and candidates:
            rendered = " | ".join(str(item)[:80] for item in candidates[:8])
            parts.append(f"候选按钮={rendered}")
        if parts:
            detail = "；" + "；".join(parts)
        if last_result.get("status") == "cloudflare-blocked":
            title = str(last_result.get("title", "") or "").strip()
            if title:
                detail = f"{detail}；title={title}"
            click_result = str(last_result.get("click_result", "") or "").strip()
            if click_result:
                detail = f"{detail}；click={click_result}"
            raise RuntimeError(f"x.ai 注册页被 Cloudflare 硬拦截，无法进入邮箱注册表单{detail}")
        if last_result.get("status") == "cloudflare-hard-blocked":
            title = str(last_result.get("title", "") or "").strip()
            if title:
                detail = f"{detail}；title={title}"
            click_result = str(last_result.get("click_result", "") or "").strip()
            if click_result:
                detail = f"{detail}；click={click_result}"
            raise RuntimeError(f"x.ai 注册页被 Cloudflare 硬拦截，无法进入邮箱注册表单{detail}")
        if last_result.get("status") == "cloudflare-challenge":
            title = str(last_result.get("title", "") or "").strip()
            if title:
                detail = f"{detail}；title={title}"
            click_result = str(last_result.get("click_result", "") or "").strip()
            if click_result:
                detail = f"{detail}；click={click_result}"
            raise RuntimeError(f"Cloudflare 检测未通过，无法进入邮箱注册表单{detail}")
    elif last_result:
        detail = f"；last={last_result}"

    raise RuntimeError(f'未找到“使用邮箱注册”按钮或邮箱输入框{detail}')


def fill_email_and_submit(timeout: int = 15) -> tuple[str, str]:
    email, dev_token = get_email_and_token()
    if not email or not dev_token:
        raise RuntimeError("获取邮箱失败")

    deadline = time.time() + timeout
    while time.time() < deadline:
        filled = page.run_js(
            """
const email = arguments[0];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input) {
    return 'not-ready';
}

input.focus();
input.click();

const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) {
    tracker.setValue('');
}
if (valueSetter) {
    valueSetter.call(input, email);
} else {
    input.value = email;
}

input.dispatchEvent(new InputEvent('beforeinput', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new InputEvent('input', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new Event('change', { bubbles: true }));

if ((input.value || '').trim() !== email || !input.checkValidity()) {
    return false;
}

input.blur();
return 'filled';
            """,
            email,
        )

        if filled == "not-ready":
            time.sleep(0.5)
            continue

        if filled != "filled":
            print(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
            time.sleep(0.5)
            continue

        time.sleep(0.8)
        clicked = page.run_js(
            r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input || !input.checkValidity() || !(input.value || '').trim()) {
    return false;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitButton = buttons.find((node) => {
    const text = String(node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return text === '注册'
        || text.includes('注册')
        || text.includes('signup')
        || text.includes('continue')
        || text.includes('createaccount');
});

if (!submitButton || submitButton.disabled) {
    return false;
}

submitButton.click();
return true;
            """
        )

        if clicked:
            print(f"[*] 已填写邮箱并点击注册: {email}")
            return email, dev_token

        time.sleep(0.5)

    raise RuntimeError("未找到邮箱输入框或注册按钮")


def _recover_verification_page(email: str) -> str:
    try:
        return str(
            page.run_js(
                r"""
const email = String(arguments[0] || '').trim();

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setNativeValue(input, value) {
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }
    if (nativeInputValueSetter) {
        nativeInputValueSetter.call(input, '');
        nativeInputValueSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const buttons = Array.from(document.querySelectorAll('button, [role="button"], a')).filter((node) => {
    return isVisible(node)
        && !node.disabled
        && node.getAttribute('aria-disabled') !== 'true';
});

const retryButton = buttons.find((node) => {
    const text = String(node.innerText || node.textContent || node.getAttribute('aria-label') || '')
        .replace(/\s+/g, '')
        .toLowerCase();
    return text === 'retry'
        || text.includes('retry')
        || text === '重试'
        || text.includes('重试')
        || text.includes('再试');
});
if (retryButton) {
    retryButton.focus?.();
    retryButton.click();
    return 'retry-clicked';
}

const confirmEmailButton = buttons.find((node) => {
    const text = String(node.innerText || node.textContent || node.getAttribute('aria-label') || '')
        .replace(/\s+/g, '')
        .toLowerCase();
    return text === 'confirmemail'
        || text.includes('confirmemail')
        || text === '确认邮箱'
        || text.includes('确认邮箱');
});
if (confirmEmailButton) {
    confirmEmailButton.focus?.();
    for (const eventType of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
        confirmEmailButton.dispatchEvent(new MouseEvent(eventType, {
            bubbles: true,
            cancelable: true,
            view: window,
        }));
    }
    return 'confirm-email-clicked';
}

const emailSignupButton = buttons.find((node) => {
    const text = String(node.innerText || node.textContent || node.getAttribute('aria-label') || '')
        .replace(/\s+/g, '')
        .toLowerCase();
    return text === 'signupwithemail'
        || text.includes('signupwithemail')
        || text.includes('emailsignup')
        || text.includes('registerwithemail')
        || text === '使用邮箱注册'
        || text.includes('使用邮箱注册')
        || text.includes('邮箱注册')
        || text.includes('电子邮件注册');
});
if (emailSignupButton) {
    emailSignupButton.focus?.();
    emailSignupButton.click();
    return 'email-signup-clicked';
}

const emailInput = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;
if (!emailInput) {
    return 'no-recovery-target';
}

emailInput.focus();
emailInput.click();
setNativeValue(emailInput, email);

const submitButton = buttons.find((node) => {
    const text = String(node.innerText || node.textContent || node.getAttribute('aria-label') || '')
        .replace(/\s+/g, '')
        .toLowerCase();
    return text === '注册'
        || text.includes('注册')
        || text.includes('signup')
        || text.includes('continue')
        || text.includes('createaccount');
});
if (!submitButton) {
    return 'email-form-no-submit';
}

submitButton.focus?.();
submitButton.click();
return 'email-resubmitted';
                """,
                email,
            )
            or ""
        )
    except PageDisconnectedError:
        refresh_active_page()
        return "page-disconnected"
    except Exception as exc:
        return f"recovery-error:{str(exc)[:160]}"


def fill_code_and_submit(email: str, dev_token: str, timeout: int = 180) -> str:
    code = get_oai_code(dev_token, email)
    if not code:
        raise RuntimeError("获取验证码失败")

    deadline = time.time() + timeout
    confirm_retry_count = 0
    while time.time() < deadline:
        try:
            filled = page.run_js(
                """
const code = String(arguments[0] || '').trim();

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setNativeValue(input, value) {
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }
    if (nativeInputValueSetter) {
        nativeInputValueSetter.call(input, '');
        nativeInputValueSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }
}

function dispatchInputEvents(input, value) {
    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const input = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || code.length || 6) > 1;
}) || null;

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) {
        return false;
    }
    const maxLength = Number(node.maxLength || 0);
    const autocomplete = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || autocomplete === 'one-time-code';
});

if (!input && otpBoxes.length < code.length) {
    return 'not-ready';
}

if (input) {
    input.focus();
    input.click();
    setNativeValue(input, code);
    dispatchInputEvents(input, code);

    const normalizedValue = String(input.value || '').trim();
    const expectedLength = Number(input.maxLength || code.length || 6);
    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;

    if (normalizedValue !== code) {
        return 'aggregate-mismatch';
    }

    if (expectedLength > 0 && normalizedValue.length !== expectedLength) {
        return 'aggregate-length-mismatch';
    }

    if (slots.length && filledSlots && filledSlots !== normalizedValue.length) {
        return 'aggregate-slot-mismatch';
    }

    input.blur();
    return 'filled';
}

const orderedBoxes = otpBoxes.slice(0, code.length);
for (let i = 0; i < orderedBoxes.length; i += 1) {
    const box = orderedBoxes[i];
    const char = code[i] || '';
    box.focus();
    box.click();
    setNativeValue(box, char);
    dispatchInputEvents(box, char);
    box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: char }));
    box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: char }));
    box.blur();
}

const merged = orderedBoxes.map((node) => String(node.value || '').trim()).join('');
return merged === code ? 'filled' : 'box-mismatch';
                """,
                code,
            )
        except PageDisconnectedError:
            refresh_active_page()
            if has_profile_form():
                print("[*] 验证码提交后已跳转到最终注册页。")
                return code
            time.sleep(1)
            continue

        if filled == "not-ready":
            confirm_retry_count = 0
            if has_profile_form():
                print("[*] 已直接进入最终注册页，跳过验证码按钮确认。")
                return code
            recovery = _recover_verification_page(email)
            if recovery in {
                "retry-clicked",
                "confirm-email-clicked",
                "email-signup-clicked",
                "email-resubmitted",
            }:
                print(f"[*] 验证码页未就绪，已尝试恢复: {recovery}")
                time.sleep(2)
                continue
            time.sleep(0.5)
            continue

        if filled != "filled":
            confirm_retry_count = 0
            print(f"[Debug] 验证码输入框已出现，但写入失败: {filled}")
            time.sleep(0.5)
            continue

        time.sleep(1.2)
        try:
            clicked = page.run_js(
                r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const aggregateInput = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 0) > 1;
}) || null;

let value = '';
if (aggregateInput) {
    value = String(aggregateInput.value || '').trim();
    const expectedLength = Number(aggregateInput.maxLength || value.length || 6);
    if (!value || (expectedLength > 0 && value.length !== expectedLength)) {
        return false;
    }

    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    if (slots.length) {
        const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;
        if (filledSlots && filledSlots !== value.length) {
            return false;
        }
    }
} else {
    const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
        if (!isVisible(node) || node.disabled || node.readOnly) {
            return false;
        }
        const maxLength = Number(node.maxLength || 0);
        const autocomplete = String(node.autocomplete || '').toLowerCase();
        return maxLength === 1 || autocomplete === 'one-time-code';
    });
    value = otpBoxes.map((node) => String(node.value || '').trim()).join('');
    if (!value || value.length < 6) {
        return false;
    }
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const confirmButton = buttons.find((node) => {
    const text = String(node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return text === '确认邮箱'
        || text.includes('确认邮箱')
        || text === '继续'
        || text.includes('继续')
        || text === '下一步'
        || text.includes('下一步')
        || text === 'confirmemail'
        || text.includes('confirmemail');
});

if (!confirmButton) {
    return 'no-button';
}

confirmButton.focus();
for (const eventType of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
    confirmButton.dispatchEvent(new MouseEvent(eventType, {
        bubbles: true,
        cancelable: true,
        view: window,
    }));
}
confirmButton.click?.();

const form = confirmButton.closest('form');
if (form) {
    try {
        if (typeof form.requestSubmit === 'function') {
            form.requestSubmit(confirmButton);
        } else {
            form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
        }
    } catch (e) {
        try {
            form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
        } catch (ignored) {}
    }
}

const activeInput = aggregateInput || document.activeElement;
if (activeInput) {
    for (const eventType of ['keydown', 'keypress', 'keyup']) {
        activeInput.dispatchEvent(new KeyboardEvent(eventType, {
            bubbles: true,
            cancelable: true,
            key: 'Enter',
            code: 'Enter',
        }));
    }
}
return 'clicked';
                """
            )
        except PageDisconnectedError:
            refresh_active_page()
            if has_profile_form():
                print("[*] 确认邮箱后页面跳转成功，已进入最终注册页。")
                return code
            clicked = "disconnected"

        if clicked == "clicked":
            print(f"[*] 已填写验证码并点击确认邮箱: {code}")
            time.sleep(2)
            refresh_active_page()
            if has_profile_form():
                print("[*] 验证码确认完成，最终注册页已就绪。")
                return code
            recovery = _recover_verification_page(email)
            if recovery == "confirm-email-clicked":
                confirm_retry_count += 1
                print(f"[*] 验证码确认后页面未跳转，已重试确认按钮: {recovery}")
                if confirm_retry_count >= 3:
                    fallback = page.run_js(
                        r"""
const code = String(arguments[0] || '').trim();

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const codeInput = Array.from(document.querySelectorAll('input[name="code"], input[autocomplete="one-time-code"], input[data-input-otp="true"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;
if (!codeInput) {
    return 'no-code-input';
}

codeInput.focus();
if (String(codeInput.value || '').trim() !== code) {
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    if (nativeInputValueSetter) {
        nativeInputValueSetter.call(codeInput, code);
    } else {
        codeInput.value = code;
    }
    codeInput.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: code,
        inputType: 'insertText',
    }));
    codeInput.dispatchEvent(new Event('change', { bubbles: true }));
}

for (const eventType of ['keydown', 'keypress', 'keyup']) {
    codeInput.dispatchEvent(new KeyboardEvent(eventType, {
        bubbles: true,
        cancelable: true,
        key: 'Enter',
        code: 'Enter',
    }));
}

const form = codeInput.closest('form') || document.querySelector('form');
if (form) {
    try {
        if (typeof form.requestSubmit === 'function') {
            form.requestSubmit();
        } else {
            form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
        }
    } catch (e) {
        try {
            form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
        } catch (ignored) {}
    }
    return 'form-submitted';
}
return 'enter-dispatched';
                        """,
                        code,
                    )
                    print(f"[*] 验证码确认仍未跳转，已尝试表单提交兜底: {fallback}")
                time.sleep(2)
                continue
            confirm_retry_count = 0
            print("[Debug] 确认邮箱后最终注册页尚未就绪，继续等待验证码页状态。")
            continue

        if clicked == "no-button":
            current_url = page.url
            if "sign-up" in current_url or "signup" in current_url:
                print(f"[*] 已填写验证码，页面已自动跳转到下一步: {current_url}")
                return code

        if clicked == "disconnected":
            time.sleep(1)
            continue

        time.sleep(0.5)

    debug_snapshot = page.run_js(
        r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const inputs = Array.from(document.querySelectorAll('input')).filter(isVisible).map((node) => ({
    type: node.type || '',
    name: node.name || '',
    testid: node.getAttribute('data-testid') || '',
    autocomplete: node.autocomplete || '',
    maxLength: Number(node.maxLength || 0),
    value: String(node.value || ''),
}));

const buttons = Array.from(document.querySelectorAll('button')).filter(isVisible).map((node) => ({
    text: String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim(),
    disabled: !!node.disabled,
    ariaDisabled: node.getAttribute('aria-disabled') || '',
}));

return { url: location.href, inputs, buttons };
        """
    )
    print(f"[Debug] 验证码页 DOM 摘要: {debug_snapshot}")
    try:
        inputs = debug_snapshot.get("inputs", []) if isinstance(debug_snapshot, dict) else []
        buttons = debug_snapshot.get("buttons", []) if isinstance(debug_snapshot, dict) else []
        has_code_input = any(
            str(item.get("name") or "") == "code"
            or str(item.get("autocomplete") or "").lower() == "one-time-code"
            for item in inputs
            if isinstance(item, dict)
        )
        has_confirm_button = any(
            "confirm email" in str(item.get("text") or "").lower()
            or "确认邮箱" in str(item.get("text") or "")
            for item in buttons
            if isinstance(item, dict)
        )
    except Exception:
        has_code_input = False
        has_confirm_button = False
    if has_code_input and has_confirm_button:
        raise RuntimeError("验证码已填写并点击确认邮箱，但页面未跳转到最终注册页")
    raise RuntimeError("未找到验证码输入框或确认邮箱按钮")


def _turnstile_response_value() -> str:
    try:
        value = page.run_js(
            r"""
try {
    const apiResponse = window.turnstile?.getResponse?.();
    if (apiResponse) {
        return String(apiResponse || '').trim();
    }
} catch (e) {}
try {
    const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
    return challengeInput ? String(challengeInput.value || '').trim() : '';
} catch (e) {
    return '';
}
            """
        )
    except Exception:
        return ""
    return str(value or "").strip()


def _turnstile_debug_snapshot() -> dict[str, Any]:
    try:
        snapshot = page.run_js(
            r"""
function collectElements(root) {
    const out = [];
    const seenRoots = new Set();

    function walk(currentRoot) {
        if (!currentRoot || seenRoots.has(currentRoot)) {
            return;
        }
        seenRoots.add(currentRoot);

        let nodes = [];
        try {
            nodes = Array.from(currentRoot.querySelectorAll('*'));
        } catch (e) {
            return;
        }

        for (const node of nodes) {
            out.push(node);
            if (node.shadowRoot) {
                walk(node.shadowRoot);
            }
        }
    }

    walk(root);
    return out;
}

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const elements = collectElements(document);
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
const inputs = elements.filter((node) => String(node.tagName || '').toLowerCase() === 'input').map((node) => ({
    type: node.type || '',
    name: node.name || '',
    testid: node.getAttribute('data-testid') || '',
    valueLength: String(node.value || '').length,
    visible: isVisible(node),
})).slice(0, 20);
const frames = elements.filter((node) => String(node.tagName || '').toLowerCase() === 'iframe').map((node) => ({
    src: String(node.src || '').slice(0, 180),
    title: String(node.title || '').slice(0, 100),
    name: String(node.name || '').slice(0, 100),
    visible: isVisible(node),
})).slice(0, 12);
const buttons = elements.filter((node) => String(node.tagName || '').toLowerCase() === 'button').filter(isVisible).map((node) => ({
    text: String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80),
    disabled: !!node.disabled,
    ariaDisabled: node.getAttribute('aria-disabled') || '',
})).slice(0, 12);

return {
    url: String(location.href || ''),
    title: String(document.title || ''),
    readyState: String(document.readyState || ''),
    turnstileApi: !!window.turnstile,
    challengeInputFound: !!challengeInput,
    challengeInputValueLength: challengeInput ? String(challengeInput.value || '').length : 0,
    inputs,
    frames,
    buttons,
};
            """
        )
    except Exception as exc:
        return {"snapshot_error": str(exc)}
    return snapshot if isinstance(snapshot, dict) else {"snapshot": str(snapshot)}


def _format_turnstile_debug(snapshot: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("url", "readyState", "title", "turnstileApi", "challengeInputFound", "challengeInputValueLength"):
        if key in snapshot:
            parts.append(f"{key}={str(snapshot[key])[:160]}")
    frames = snapshot.get("frames")
    if isinstance(frames, list) and frames:
        rendered = []
        for frame in frames[:4]:
            if not isinstance(frame, dict):
                continue
            marker = " ".join(
                str(frame.get(item, "") or "").strip()
                for item in ("title", "name", "src")
            ).strip()
            if marker:
                rendered.append(marker[:160])
        if rendered:
            parts.append("frames=" + " | ".join(rendered))
    buttons = snapshot.get("buttons")
    if isinstance(buttons, list) and buttons:
        rendered = [
            str(item.get("text", "") or "").strip()
            for item in buttons
            if isinstance(item, dict) and str(item.get("text", "") or "").strip()
        ]
        if rendered:
            parts.append("buttons=" + " | ".join(rendered[:6]))
    if not parts:
        try:
            parts.append(json.dumps(snapshot, ensure_ascii=False)[:500])
        except Exception:
            parts.append(str(snapshot)[:500])
    return "；" + "；".join(parts)


def _turnstile_render_params() -> dict[str, Any]:
    try:
        params = page.run_js(
            r"""
function compact(value) {
    return String(value || '').trim();
}

const state = window.__grokManagerTurnstile || {};
const renders = Array.isArray(state.renders) ? state.renders : [];
const lastRecord = state.last || renders.slice().reverse().find((item) => item && item.sitekey) || {};
let sitekey = compact(lastRecord.sitekey);
if (!sitekey) {
    const keyed = document.querySelector('[data-sitekey], [data-siteKey]');
    sitekey = compact(keyed?.getAttribute?.('data-sitekey') || keyed?.getAttribute?.('data-siteKey'));
}

return {
    action: compact(lastRecord.action),
    cData: compact(lastRecord.cData || lastRecord.cdata || lastRecord.data),
    callbackId: compact(lastRecord.callbackId || state.lastCallbackId),
    chlPageData: compact(lastRecord.chlPageData || lastRecord.pagedata || lastRecord.pageData),
    sitekey,
    url: compact(lastRecord.url || location.href),
    userAgent: compact(navigator.userAgent),
    widgetId: compact(lastRecord.widgetId),
};
            """
        )
    except Exception as exc:
        return {"error": str(exc)}
    return params if isinstance(params, dict) else {"error": str(params)}


def _requests_post_json(url: str, payload: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    try:
        response = requests.post(url, json=payload, timeout=max(1.0, timeout))
    except requests.RequestException as exc:
        raise RuntimeError(f"solver http error: {exc}") from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"solver returned non-json status={response.status_code}: {response.text[:200]}"
        ) from exc
    if response.status_code >= 400:
        raise RuntimeError(f"solver http {response.status_code}: {str(data)[:300]}")
    return data if isinstance(data, dict) else {"response": data}


def _solver_error_message(data: dict[str, Any]) -> str:
    parts = []
    for key in ("errorCode", "errorDescription", "errorId", "status"):
        value = data.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    return " ".join(parts) or str(data)[:300]


def _capsolver_create_turnstile_task(api_key: str, params: dict[str, Any]) -> str:
    task: dict[str, Any] = {
        "type": "AntiTurnstileTaskProxyLess",
        "websiteURL": params["url"],
        "websiteKey": params["sitekey"],
    }
    metadata: dict[str, str] = {}
    if params.get("action"):
        metadata["action"] = str(params["action"])
    if params.get("cData"):
        metadata["cdata"] = str(params["cData"])
    if params.get("chlPageData"):
        metadata["chlPageData"] = str(params["chlPageData"])
    if metadata:
        task["metadata"] = metadata

    data = _requests_post_json(
        "https://api.capsolver.com/createTask",
        {"clientKey": api_key, "task": task},
    )
    if int(data.get("errorId") or 0) != 0:
        raise RuntimeError(f"capsolver createTask failed: {_solver_error_message(data)}")
    task_id = str(data.get("taskId") or "").strip()
    if not task_id:
        raise RuntimeError(f"capsolver createTask missing taskId: {str(data)[:300]}")
    return task_id


def _twocaptcha_create_turnstile_task(api_key: str, params: dict[str, Any]) -> str:
    task: dict[str, Any] = {
        "type": "TurnstileTaskProxyless",
        "websiteURL": params["url"],
        "websiteKey": params["sitekey"],
    }
    if params.get("action"):
        task["action"] = str(params["action"])
    if params.get("cData"):
        task["data"] = str(params["cData"])
    if params.get("chlPageData"):
        task["pagedata"] = str(params["chlPageData"])

    data = _requests_post_json(
        "https://api.2captcha.com/createTask",
        {"clientKey": api_key, "task": task},
    )
    if int(data.get("errorId") or 0) != 0:
        raise RuntimeError(f"2captcha createTask failed: {_solver_error_message(data)}")
    task_id = str(data.get("taskId") or "").strip()
    if not task_id:
        raise RuntimeError(f"2captcha createTask missing taskId: {str(data)[:300]}")
    return task_id


def _poll_turnstile_solver_result(
    *,
    provider: str,
    api_key: str,
    task_id: str,
    timeout: float,
    poll_interval: float,
) -> str:
    endpoint = (
        "https://api.capsolver.com/getTaskResult"
        if provider == "capsolver"
        else "https://api.2captcha.com/getTaskResult"
    )
    deadline = time.monotonic() + max(1.0, float(timeout))
    while time.monotonic() < deadline:
        data = _requests_post_json(
            endpoint,
            {"clientKey": api_key, "taskId": task_id},
            timeout=min(30.0, max(1.0, deadline - time.monotonic())),
        )
        if int(data.get("errorId") or 0) != 0:
            raise RuntimeError(f"{provider} getTaskResult failed: {_solver_error_message(data)}")

        status = str(data.get("status") or "").lower()
        if status == "ready":
            solution = data.get("solution")
            if not isinstance(solution, dict):
                raise RuntimeError(f"{provider} result missing solution: {str(data)[:300]}")
            token = str(
                solution.get("token")
                or solution.get("gRecaptchaResponse")
                or solution.get("code")
                or ""
            ).strip()
            if not token:
                raise RuntimeError(f"{provider} result missing token: {str(data)[:300]}")
            return token

        time.sleep(max(1.0, float(poll_interval)))

    raise RuntimeError(f"{provider} solve timed out after {timeout:.0f}s")


def _solve_turnstile_with_external_solver(max_wait_seconds: float | None = None) -> str:
    settings = _turnstile_solver_settings()
    if not settings.get("enabled"):
        error = str(settings.get("error") or "").strip()
        if error:
            raise RuntimeError(f"external solver disabled: {error}")
        raise RuntimeError("external solver disabled")

    params = _turnstile_render_params()
    if not str(params.get("sitekey") or "").strip():
        raise RuntimeError(f"external solver missing turnstile sitekey: {str(params)[:300]}")
    params["sitekey"] = str(params["sitekey"]).strip()
    params["url"] = str(params.get("url") or _safe_page_url()).strip() or SIGNUP_URL

    provider = str(settings["provider"])
    api_key = str(settings["api_key"])
    timeout = float(settings.get("timeout") or DEFAULT_TURNSTILE_SOLVER_TIMEOUT_SECONDS)
    if max_wait_seconds is not None:
        timeout = min(timeout, max(1.0, float(max_wait_seconds)))
    poll_interval = float(
        settings.get("poll_interval")
        or DEFAULT_TURNSTILE_SOLVER_POLL_INTERVAL_SECONDS
    )

    print(
        f"[*] 使用外部 Turnstile solver: provider={provider}, "
        f"sitekey={params['sitekey'][:16]}..., timeout={timeout:.0f}s"
    )
    if run_logger:
        run_logger.info(
            "使用外部 Turnstile solver: provider=%s sitekey=%s timeout=%.0fs",
            provider,
            params["sitekey"][:16],
            timeout,
        )

    if provider == "capsolver":
        task_id = _capsolver_create_turnstile_task(api_key, params)
    elif provider == "2captcha":
        task_id = _twocaptcha_create_turnstile_task(api_key, params)
    else:
        raise RuntimeError(f"unsupported external solver provider: {provider}")
    return _poll_turnstile_solver_result(
        provider=provider,
        api_key=api_key,
        task_id=task_id,
        timeout=timeout,
        poll_interval=poll_interval,
    )


def _profile_page_snapshot() -> dict[str, Any]:
    try:
        snapshot = page.run_js(
            r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
const buttons = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter(isVisible).map((node) => ({
    text: String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120),
    disabled: !!node.disabled,
    ariaDisabled: node.getAttribute('aria-disabled') || '',
})).slice(0, 16);
const bodyText = String(document.body?.innerText || '').replace(/\s+/g, ' ').trim();
const normalized = bodyText.toLowerCase().replace(/\s+/g, '');
const postSignup = normalized.includes('continuetogrok')
    || normalized.includes('gotogrok')
    || normalized.includes('startusinggrok')
    || normalized.includes('getstarted')
    || normalized.includes('continue')
    || normalized.includes('进入grok')
    || normalized.includes('开始使用')
    || normalized.includes('继续');

return {
    url: String(location.href || ''),
    title: String(document.title || ''),
    readyState: String(document.readyState || ''),
    profilePresent: !!(givenInput && familyInput && passwordInput),
    challengeInputFound: !!challengeInput,
    challengeInputValueLength: challengeInput ? String(challengeInput.value || '').length : 0,
    postSignup,
    buttons,
    text: bodyText.slice(0, 500),
};
            """
        )
    except Exception as exc:
        return {"snapshot_error": str(exc)}
    return snapshot if isinstance(snapshot, dict) else {"snapshot": str(snapshot)}


def _profile_snapshot_indicates_submitted(snapshot: dict[str, Any]) -> bool:
    if bool(snapshot.get("profilePresent")):
        return False

    url = str(snapshot.get("url", "") or "").lower()
    title = str(snapshot.get("title", "") or "").lower()
    text = str(snapshot.get("text", "") or "").lower()
    if "grok.com" in url:
        return True
    if bool(snapshot.get("postSignup")) and (
        "sign-up" not in url or "create your grok account" not in title
    ):
        return True

    success_terms = (
        "continue to grok",
        "go to grok",
        "start using grok",
        "get started",
        "account created",
        "welcome",
        "进入 grok",
        "进入grok",
        "开始使用",
    )
    return any(term in text for term in success_terms)


def _auth_token_candidate_available() -> bool:
    try:
        if _extract_auth_token_from_cookie_items(_collect_cookie_items()):
            return True
    except Exception:
        pass
    try:
        return bool(
            _extract_auth_token_from_storage_candidates(
                _collect_web_storage_candidates()
            )
        )
    except Exception:
        return False


def _format_profile_debug(snapshot: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "url",
        "readyState",
        "title",
        "profilePresent",
        "challengeInputFound",
        "challengeInputValueLength",
        "postSignup",
    ):
        if key in snapshot:
            parts.append(f"{key}={str(snapshot[key])[:160]}")
    buttons = snapshot.get("buttons")
    if isinstance(buttons, list) and buttons:
        rendered = [
            str(item.get("text", "") or "").strip()
            for item in buttons
            if isinstance(item, dict) and str(item.get("text", "") or "").strip()
        ]
        if rendered:
            parts.append("buttons=" + " | ".join(rendered[:8]))
    if not parts:
        try:
            parts.append(json.dumps(snapshot, ensure_ascii=False)[:500])
        except Exception:
            parts.append(str(snapshot)[:500])
    return "；" + "；".join(parts)


def _snapshot_has_pending_turnstile(snapshot: dict[str, Any]) -> bool:
    try:
        value_length = int(snapshot.get("challengeInputValueLength") or 0)
    except (TypeError, ValueError):
        value_length = 0
    return bool(snapshot.get("challengeInputFound")) and value_length <= 0


def _wait_for_manual_turnstile_completion(
    *,
    deadline: float,
    max_wait_seconds: float,
) -> str:
    if max_wait_seconds <= 0:
        return "disabled"

    manual_deadline = min(deadline, time.time() + max_wait_seconds)
    if manual_deadline <= time.time():
        return "expired"

    print(
        "[*] Turnstile 自动求解未返回响应，等待人工完成验证/点击 Complete sign up "
        f"（最长 {max_wait_seconds:.0f}s）。"
    )
    while time.time() < manual_deadline:
        if _turnstile_response_value():
            return "turnstile-ready"
        if _auth_token_candidate_available():
            return "auth-token"
        snapshot = _profile_page_snapshot()
        if _profile_snapshot_indicates_submitted(snapshot):
            return "submitted"
        time.sleep(1.0)
    return "timeout"


def _click_turnstile_iframe_coordinates(challenge_iframe: Any) -> list[str]:
    try:
        width, height = challenge_iframe.rect.size
        width = int(width)
        height = int(height)
    except Exception:
        width, height = 300, 80

    def clamp(value: int, size: int) -> int:
        if size <= 8:
            return max(1, size // 2)
        return max(4, min(value, size - 4))

    jitter_x = secrets.randbelow(9) - 4
    jitter_y = secrets.randbelow(7) - 3
    points = (
        (clamp(30 + jitter_x, width), clamp(max(24, height // 2) + jitter_y, height)),
        (clamp(42 + jitter_x, width), clamp(max(24, height // 2) - jitter_y, height)),
        (clamp(24 - jitter_x, width), clamp(max(28, height // 2 + 8), height)),
    )

    clicked: list[str] = []
    for index, (click_x, click_y) in enumerate(points, start=1):
        try:
            page.actions.move_to(
                challenge_iframe,
                offset_x=click_x,
                offset_y=click_y,
                duration=0.25,
            ).click()
            clicked.append(f"shadow-iframe-coordinate:{index}")
        except Exception:
            continue
    try:
        page.actions.click(challenge_iframe)
        clicked.append("shadow-iframe-center")
    except Exception:
        pass
    return clicked


def _click_turnstile_widget() -> str:
    """Click the Turnstile widget through both the legacy shadow path and fallback heuristics."""
    attempts: list[str] = []
    try:
        challenge_solution = page.ele("@name=cf-turnstile-response", timeout=0.2)
        if challenge_solution:
            challenge_wrapper = challenge_solution.parent()
            challenge_iframe = challenge_wrapper.shadow_root.ele("tag:iframe")
            challenge_iframe.run_js(
                """
window.dtp = 1
function getRandomInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
}

let screenX = getRandomInt(800, 1200);
let screenY = getRandomInt(400, 600);

try {
    Object.defineProperty(MouseEvent.prototype, 'screenX', { value: screenX });
    Object.defineProperty(MouseEvent.prototype, 'screenY', { value: screenY });
} catch (e) {}
                """
            )
            attempts.extend(_click_turnstile_iframe_coordinates(challenge_iframe))
            challenge_iframe_body = challenge_iframe.ele("tag:body").shadow_root
            challenge_button = challenge_iframe_body.ele("tag:input")
            challenge_button.click()
            attempts.append("legacy-shadow-input")
    except Exception:
        pass

    if attempts:
        return "+".join(attempts)

    try:
        return _click_cloudflare_challenge()
    except Exception as exc:
        return f"click-error:{str(exc)[:120]}"


def get_turnstile_token(
    *,
    timeout: float = 45.0,
    poll_interval: float = 1.0,
    reset: bool = True,
) -> str:
    if reset:
        try:
            page.run_js("try { turnstile.reset() } catch(e) { }")
        except Exception:
            pass

    deadline = time.monotonic() + max(1.0, float(timeout))
    last_click = ""
    next_click_at = 0.0

    while time.monotonic() < deadline:
        response = _turnstile_response_value()
        if response:
            return response

        now = time.monotonic()
        if now >= next_click_at:
            click_result = _click_turnstile_widget()
            if click_result != "not-found":
                last_click = click_result
            next_click_at = now + 2.0

        time.sleep(max(0.2, float(poll_interval)))

    snapshot = _turnstile_debug_snapshot()
    detail = _format_turnstile_debug(snapshot)
    if last_click:
        detail = f"{detail}；last_click={last_click}"
    raise RuntimeError(f"failed to solve turnstile{detail}")


def _sync_turnstile_token(token: str) -> bool:
    try:
        return bool(
            page.run_js(
                r"""
const token = arguments[0];
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) {
    return false;
}
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) {
    nativeSetter.call(challengeInput, token);
} else {
    challengeInput.value = token;
}
challengeInput.dispatchEvent(new Event('input', { bubbles: true }));
challengeInput.dispatchEvent(new Event('change', { bubbles: true }));
try {
    const state = window.__grokManagerTurnstile || {};
    const last = state.last || {};
    const callbackId = last.callbackId || state.lastCallbackId || '';
    const callback = state.callbacks && state.callbacks[callbackId];
    if (typeof callback === 'function') {
        callback(token);
    }
} catch (e) {}
return String(challengeInput.value || '').trim() === String(token || '').trim();
                """,
                token,
            )
        )
    except Exception:
        return False


def build_profile() -> tuple[str, str, str]:
    given_name = secrets.choice(PROFILE_GIVEN_NAMES)
    family_name = secrets.choice(PROFILE_FAMILY_NAMES)
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password


def fill_profile_and_submit(timeout: int = 120) -> dict[str, str]:
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    turnstile_token = ""
    turnstile_notice_printed = False
    external_turnstile_notice_printed = False
    last_turnstile_error = ""
    manual_turnstile_wait = _turnstile_manual_wait_seconds()
    profile_filled_once = False

    while time.time() < deadline:
        filled = page.run_js(
            """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) {
        return false;
    }
    input.focus();
    input.click();

    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }

    if (nativeSetter) {
        nativeSetter.call(input, '');
        nativeSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }

    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.dispatchEvent(new Event('blur', { bubbles: true }));

    return String(input.value || '') === String(value || '');
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) {
    return 'not-ready';
}

const givenOk = setInputValue(givenInput, givenName);
const familyOk = setInputValue(familyInput, familyName);
const passwordOk = setInputValue(passwordInput, password);

if (!givenOk || !familyOk || !passwordOk) {
    return 'filled-failed';
}

return [
    String(givenInput.value || '').trim() === String(givenName || '').trim(),
    String(familyInput.value || '').trim() === String(familyName || '').trim(),
    String(passwordInput.value || '') === String(password || ''),
].every(Boolean) ? 'filled' : 'verify-failed';
            """,
            given_name,
            family_name,
            password,
        )

        if filled == "not-ready":
            if profile_filled_once:
                snapshot = _profile_page_snapshot()
                if _profile_snapshot_indicates_submitted(snapshot) or _auth_token_candidate_available():
                    print("[*] 最终注册表单已离开，继续进入 sso cookie 采集阶段。")
                    return {
                        "given_name": given_name,
                        "family_name": family_name,
                        "password": password,
                    }
            time.sleep(0.5)
            continue

        if filled != "filled":
            print(f"[Debug] 最终注册页输入框已出现，但姓名/密码写入失败: {filled}")
            time.sleep(0.5)
            continue

        profile_filled_once = True

        values_ok = page.run_js(
            """
const expectedGiven = arguments[0];
const expectedFamily = arguments[1];
const expectedPassword = arguments[2];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) {
    return false;
}

return String(givenInput.value || '').trim() === String(expectedGiven || '').trim()
    && String(familyInput.value || '').trim() === String(expectedFamily || '').trim()
    && String(passwordInput.value || '') === String(expectedPassword || '');
            """,
            given_name,
            family_name,
            password,
        )
        if not values_ok:
            print("[Debug] 最终注册页字段值校验失败，继续重试填写。")
            time.sleep(0.5)
            continue

        turnstile_state = page.run_js(
            """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) {
    return 'not-found';
}
const value = String(challengeInput.value || '').trim();
return value ? 'ready' : 'pending';
            """
        )

        if turnstile_state == "pending" and not turnstile_token:
            if not turnstile_notice_printed:
                print("[*] 检测到最终注册页存在 Turnstile，开始使用现有真人化点击逻辑。")
                turnstile_notice_printed = True
            try:
                turnstile_token = get_turnstile_token(
                    timeout=15.0,
                    reset=not bool(last_turnstile_error),
                )
            except RuntimeError as exc:
                last_turnstile_error = str(exc)
                print(
                    "[Debug] Turnstile 本次求解失败，继续等待/重试: "
                    f"{last_turnstile_error[:500]}"
                )
                solver_settings = _turnstile_solver_settings()
                if solver_settings.get("enabled"):
                    if not external_turnstile_notice_printed:
                        print("[*] 自动点击未拿到 Turnstile 响应，切换到外部 solver。")
                        external_turnstile_notice_printed = True
                    try:
                        turnstile_token = _solve_turnstile_with_external_solver(
                            max_wait_seconds=max(1.0, deadline - time.time() - 2.0)
                        )
                    except RuntimeError as solver_exc:
                        last_turnstile_error = (
                            f"{last_turnstile_error}; external_solver={str(solver_exc)[:500]}"
                        )
                        print(
                            "[Debug] 外部 Turnstile solver 本次失败: "
                            f"{str(solver_exc)[:500]}"
                        )
                elif solver_settings.get("error") and not external_turnstile_notice_printed:
                    print(f"[Debug] 外部 Turnstile solver 未启用: {solver_settings['error']}")
                    external_turnstile_notice_printed = True

                if not turnstile_token:
                    manual_result = _wait_for_manual_turnstile_completion(
                        deadline=deadline,
                        max_wait_seconds=manual_turnstile_wait,
                    )
                    if manual_result in {"submitted", "auth-token"}:
                        print("[*] 检测到人工完成注册或认证 token 已出现，继续采集 sso cookie。")
                        return {
                            "given_name": given_name,
                            "family_name": family_name,
                            "password": password,
                        }
                    if manual_result == "turnstile-ready":
                        turnstile_token = _turnstile_response_value()
                        if turnstile_token:
                            if _sync_turnstile_token(turnstile_token):
                                print("[*] Turnstile 响应已同步到最终注册表单。")
                            else:
                                turnstile_token = ""
                                last_turnstile_error = "Turnstile token sync failed after manual wait"
                    time.sleep(1.0)
                    continue
            if turnstile_token:
                synced = _sync_turnstile_token(turnstile_token)
                if synced:
                    print("[*] Turnstile 响应已同步到最终注册表单。")
                else:
                    last_turnstile_error = "Turnstile token sync failed"
                    turnstile_token = ""
                    time.sleep(0.5)
                    continue
        elif turnstile_state == "ready":
            last_turnstile_error = ""

        time.sleep(1.2)

        try:
            submit_button = (
                page.ele("tag:button@@text()=完成注册")
                or page.ele("tag:button@@text()=Create account")
                or page.ele("tag:button@@text()=Create Account")
                or page.ele("tag:button@@text()=Sign up")
                or page.ele("tag:button@@text()=Register")
            )
        except Exception:
            submit_button = None

        if not submit_button:
            clicked = page.run_js(
                r"""
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (challengeInput && !String(challengeInput.value || '').trim()) {
    return false;
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button'));
const submitButton = buttons.find((node) => {
    const text = String(node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return text === '完成注册' || text.includes('完成注册')
        || text === 'createaccount' || text.includes('createaccount')
        || text === 'signup' || text.includes('signup')
        || text === 'register' || text.includes('register')
        || text === 'continue' || text.includes('continue')
        || text.includes('注册') || (node.type === 'submit' && buttons.length === 1);
});
if (!submitButton || submitButton.disabled || submitButton.getAttribute('aria-disabled') === 'true') {
    // Debug: log all available button texts
    const debugTexts = buttons.map(b => (b.innerText||b.textContent||'').trim()).filter(t=>t).join(' | ');
    return 'NO_BUTTON: ' + debugTexts.slice(0, 200);
}
submitButton.focus();
submitButton.click();
return true;
                """
            )
        else:
            challenge_value = page.run_js(
                """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
return challengeInput ? String(challengeInput.value || '').trim() : 'not-found';
                """
            )
            if challenge_value != "":
                submit_button.click()
                clicked = True
            else:
                clicked = False

        if clicked is True:
            print(f"[*] 已填写注册资料并点击完成注册: {given_name} {family_name}")
            return {
                "given_name": given_name,
                "family_name": family_name,
                "password": password,
            }
        if _auth_token_candidate_available():
            print("[*] 点击完成注册后检测到认证 token，继续采集 sso cookie。")
            return {
                "given_name": given_name,
                "family_name": family_name,
                "password": password,
            }
        snapshot = _profile_page_snapshot()
        if _profile_snapshot_indicates_submitted(snapshot):
            print("[*] 最终注册页已进入注册后阶段，继续采集 sso cookie。")
            return {
                "given_name": given_name,
                "family_name": family_name,
                "password": password,
            }
        if isinstance(clicked, str) and clicked.startswith("NO_BUTTON:"):
            print(f"[Debug] 最终注册页未找到可点击提交按钮: {clicked}")

        time.sleep(0.5)

    if last_turnstile_error:
        snapshot = _profile_page_snapshot()
        if _snapshot_has_pending_turnstile(snapshot):
            raise RuntimeError(
                "Turnstile 自动验证未通过，cf-turnstile-response 为空；"
                "当前无图形环境无法人工介入时，请更换出口/IP/浏览器指纹，"
                "或改用真实图形桌面/外部验证方案；"
                f"last_turnstile={last_turnstile_error[:800]}"
                f"{_format_profile_debug(snapshot)}"
            )
        raise RuntimeError(
            "未找到最终注册表单或完成注册按钮；"
            f"last_turnstile={last_turnstile_error[:800]}"
            f"{_format_profile_debug(snapshot)}"
        )
    snapshot = _profile_page_snapshot()
    raise RuntimeError(
        "未找到最终注册表单或完成注册按钮"
        f"{_format_profile_debug(snapshot)}"
    )


def extract_visible_numbers(timeout: int = 60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = page.run_js(
            r"""
function isVisible(el) {
    if (!el) {
        return false;
    }
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const selector = [
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'div', 'span', 'p', 'strong', 'b', 'small',
    '[data-testid]', '[class]', '[role="heading"]'
].join(',');

const seen = new Set();
const matches = [];
for (const node of document.querySelectorAll(selector)) {
    if (!isVisible(node)) {
        continue;
    }
    const text = String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
    if (!text) {
        continue;
    }
    const found = text.match(/\d+(?:\.\d+)?/g);
    if (!found) {
        continue;
    }
    for (const value of found) {
        const key = `${value}@@${text}`;
        if (seen.has(key)) {
            continue;
        }
        seen.add(key);
        matches.push({ value, text });
    }
}

return matches.slice(0, 30);
            """
        )

        if result:
            print("[*] 页面可见数字文本提取结果:")
            for item in result:
                try:
                    print(f"    - 数字: {item['value']} | 上下文: {item['text']}")
                except Exception:
                    pass
            return result

        time.sleep(1)

    raise RuntimeError("登录后未提取到可见数字文本")


def wait_for_sso_cookie(timeout: int = 120, *, trigger_grok_redirect: bool = True) -> str:
    deadline = time.time() + timeout
    last_seen_names = set()
    started_at = time.monotonic()
    last_continue_click = 0.0
    grok_redirected = False

    while time.time() < deadline:
        try:
            refresh_active_page()
            if page is None:
                time.sleep(1)
                continue

            cookies = _collect_cookie_items()
            for item in cookies:
                name = _cookie_attr(item, "name")
                if name:
                    last_seen_names.add(name)

            found = _extract_auth_token_from_cookie_items(cookies)
            if found:
                value, label = found
                if label.split("@", 1)[0] == "sso":
                    print("[*] 注册完成后已获取到 sso cookie。")
                else:
                    print(f"[*] 注册完成后获取到潜在认证 cookie: {label}")
                return value

            storage_found = _extract_auth_token_from_storage_candidates(
                _collect_web_storage_candidates()
            )
            if storage_found:
                value, label = storage_found
                print(f"[*] 注册完成后从 Web Storage 获取到潜在 sso token: {label}")
                return value

            now = time.monotonic()
            if now - last_continue_click >= 2.0:
                if _click_post_signup_continue_button():
                    print("[*] 已点击注册后继续/进入按钮，等待 sso cookie。")
                last_continue_click = now

            if trigger_grok_redirect and not grok_redirected and now - started_at >= 25:
                current_url = _safe_page_url()
                if "grok.com" not in current_url:
                    print("[*] 注册后仍未出现 sso cookie，跳转 grok.com 触发登录态写入。")
                    page.get(GROK_URL)
                    grok_redirected = True

        except PageDisconnectedError:
            refresh_active_page()
        except Exception:
            pass

        time.sleep(1)

    raise RuntimeError(f"注册完成后未获取到 sso cookie，当前已见 cookie: {sorted(last_seen_names)}")


def append_sso_to_txt(sso_value: str, output_path: Path) -> None:
    normalized = str(sso_value or "").strip()
    if not normalized:
        raise RuntimeError("待写入的 sso 为空")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(normalized + "\n")

    print(f"[*] 已追加写入 sso 到文件: {output_path}")


def _merge_tokens(existing_tokens: list[str], new_tokens: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for token in [*existing_tokens, *new_tokens]:
        if not token or token in seen:
            continue
        seen.add(token)
        merged.append(token)
    return merged


def _load_existing_legacy_tokens(
    endpoint: str,
    headers: dict[str, str],
    verify_ssl: bool,
) -> list[str]:
    import requests

    resp = requests.get(endpoint, headers=headers, timeout=15, verify=verify_ssl)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    existing = resp.json().get("ssoBasic", [])
    return [
        item["token"] if isinstance(item, dict) else str(item)
        for item in existing
        if item
    ]


def _load_existing_admin_tokens(
    endpoint: str,
    headers: dict[str, str],
    pool: str,
    verify_ssl: bool,
) -> list[str]:
    import requests

    resp = requests.get(endpoint, headers=headers, timeout=15, verify=verify_ssl)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    rows = resp.json().get("tokens", [])
    return [
        str(item.get("token", "")).strip()
        for item in rows
        if isinstance(item, dict) and str(item.get("pool", "basic")).strip().lower() == pool
    ]


def _push_admin_replace(
    endpoint: str,
    headers: dict[str, str],
    tokens_to_push: list[str],
    pool: str,
    verify_ssl: bool,
) -> None:
    import requests

    resp = requests.post(
        endpoint,
        json={pool: tokens_to_push},
        headers=headers,
        timeout=60,
        verify=verify_ssl,
    )
    if resp.status_code == 200:
        print(f"[*] SSO token 已写入 Admin 接口（pool={pool}, 共 {len(tokens_to_push)} 个）: {endpoint}")
        return
    raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:200]}")


def _push_admin_add(
    endpoint: str,
    headers: dict[str, str],
    tokens_to_push: list[str],
    pool: str,
    verify_ssl: bool,
) -> None:
    import requests

    resp = requests.post(
        endpoint,
        json={"pool": pool, "tokens": tokens_to_push},
        headers=headers,
        timeout=60,
        verify=verify_ssl,
    )
    if resp.status_code == 200:
        print(f"[*] SSO token 已追加写入 Admin 接口（pool={pool}, 共 {len(tokens_to_push)} 个）: {endpoint}")
        return
    raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:200]}")


def _push_legacy(
    endpoint: str,
    headers: dict[str, str],
    tokens_to_push: list[str],
    verify_ssl: bool,
) -> None:
    import requests

    resp = requests.post(
        endpoint,
        json={"ssoBasic": tokens_to_push},
        headers=headers,
        timeout=60,
        verify=verify_ssl,
    )
    if resp.status_code == 200:
        print(f"[*] SSO token 已推送到兼容接口（共 {len(tokens_to_push)} 个）: {endpoint}")
        return
    raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:200]}")


def push_sso_to_api(new_tokens: list[str]) -> None:
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    conf = load_config()
    api_conf = conf.get("api")
    if not isinstance(api_conf, dict):
        return

    endpoint = str(api_conf.get("endpoint", "")).strip()
    api_token = str(api_conf.get("token", "")).strip()
    append_mode = as_bool(api_conf.get("append", True), default=True)
    pool = str(api_conf.get("pool", "basic")).strip().lower() or "basic"
    verify_ssl = as_bool(api_conf.get("verify_ssl", True), default=True)

    tokens_to_push = [str(token).strip() for token in new_tokens if str(token).strip()]
    if not endpoint or not api_token or not tokens_to_push:
        return

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    endpoint = endpoint.rstrip("/")
    try:
        if endpoint.endswith("/admin/api/tokens/add"):
            if append_mode:
                _push_admin_add(endpoint, headers, tokens_to_push, pool, verify_ssl)
                return
            endpoint = endpoint[: -len("/add")]

        if endpoint.endswith("/admin/api/tokens"):
            if append_mode:
                try:
                    existing_tokens = _load_existing_admin_tokens(
                        endpoint,
                        headers,
                        pool,
                        verify_ssl,
                    )
                    merged = _merge_tokens(existing_tokens, tokens_to_push)
                    print(
                        f"[*] 查询到 Admin 池 {len(existing_tokens)} 个 token，"
                        f"合并本次 {len(tokens_to_push)} 个，共 {len(merged)} 个"
                    )
                    tokens_to_push = merged
                except Exception as exc:
                    print(f"[Warn] 查询 Admin 池异常: {exc}，仅使用本次 token 覆盖写入")

            _push_admin_replace(endpoint, headers, tokens_to_push, pool, verify_ssl)
            return

        if append_mode:
            try:
                existing_tokens = _load_existing_legacy_tokens(
                    endpoint,
                    headers,
                    verify_ssl,
                )
                merged = _merge_tokens(existing_tokens, tokens_to_push)
                print(
                    f"[*] 查询到兼容接口 {len(existing_tokens)} 个 token，"
                    f"合并本次 {len(tokens_to_push)} 个，共 {len(merged)} 个"
                )
                tokens_to_push = merged
            except Exception as exc:
                print(f"[Warn] 查询兼容接口异常: {exc}，仅推送本次 token")

        _push_legacy(endpoint, headers, tokens_to_push, verify_ssl)
    except Exception as exc:
        print(f"[Warn] 推送 API 失败: {exc}")


def run_single_registration(output_path: Path, extract_numbers: bool = False) -> dict[str, str]:
    open_signup_page()
    email, dev_token = fill_email_and_submit()
    fill_code_and_submit(email, dev_token)
    profile = fill_profile_and_submit()

    try:
        sso_value = wait_for_sso_cookie(timeout=90)
    except RuntimeError as first_error:
        if run_logger:
            run_logger.warning(
                "注册提交后未采集到 sso，尝试登录兜底: email=%s error=%s",
                email,
                first_error,
            )
        try:
            sign_in_existing_account(email, profile["password"])
            sso_value = wait_for_sso_cookie(timeout=90, trigger_grok_redirect=True)
        except Exception as fallback_error:
            raise RuntimeError(
                "注册已提交但未采集到 sso token；"
                f"注册后采集失败: {first_error}; 登录兜底失败: {fallback_error}"
            ) from fallback_error

    append_sso_to_txt(sso_value, output_path)

    if extract_numbers:
        extract_visible_numbers()

    result = {
        "email": email,
        "sso": sso_value,
        **profile,
    }

    if run_logger:
        run_logger.info(
            "注册成功 | email=%s | given=%s | family=%s",
            email,
            profile.get("given_name", ""),
            profile.get("family_name", ""),
        )

    print(f"[*] 本轮注册完成，邮箱: {email}")
    return result


def load_run_count() -> int:
    try:
        conf = load_config()
        value = conf.get("run", {}).get("count")
        if isinstance(value, int) and value >= 0:
            return value
    except Exception:
        pass
    return 10


def _wait_while_paused(
    pause_check: Callable[[], bool] | None,
    stop_check: Callable[[], bool] | None,
    *,
    poll_interval: float = 0.5,
) -> bool:
    """Block while ``pause_check`` reports a paused state.

    Returns ``True`` if the caller should stop entirely (stop signal raised),
    ``False`` otherwise. When ``pause_check`` is ``None`` this is a no-op.
    """
    if pause_check is None:
        return bool(stop_check and stop_check())

    announced = False
    while pause_check():
        if stop_check and stop_check():
            return True
        if not announced:
            announced = True
            print("[Pause] 注册流程已暂停，等待恢复信号……")
            if run_logger:
                run_logger.info("注册流程已暂停，等待恢复信号")
        time.sleep(poll_interval)

    if announced:
        print("[Pause] 收到恢复信号，继续执行。")
        if run_logger:
            run_logger.info("收到恢复信号，继续执行")

    return bool(stop_check and stop_check())


def _is_registration_env_retryable_error(error: BaseException) -> bool:
    message = f"{type(error).__name__}: {error}".lower()
    return (
        "cloudflare" in message
        and (
            "无法进入邮箱注册表单" in message
            or "检测未通过" in message
            or "attention required" in message
        )
    )


def run_batch(
    *,
    config_path: str | os.PathLike[str],
    count: int,
    output: str | os.PathLike[str] | None = None,
    extract_numbers: bool = False,
    pause_check: Callable[[], bool] | None = None,
    stop_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    push_to_api: bool = True,
) -> list[str]:
    """Run a maintainer registration batch and return collected SSO tokens.

    ``pause_check`` and ``stop_check`` are optional callables consulted between
    rounds. When ``pause_check()`` returns truthy, the loop waits (without
    starting a new round) until it returns falsy again. When ``stop_check()``
    returns truthy, the loop exits gracefully after the current round.

    ``progress_callback`` is an optional callable invoked with ``(event_name,
    payload)`` for key transitions in the registration loop so callers (notably
    the parallel orchestrator) can stream interleaved progress lines to the
    UI. Events emitted:

    - ``"started"`` — batch entered, payload contains ``count``.
    - ``"browser_started"`` — Chromium boot succeeded.
    - ``"round_start"`` — round entering, payload contains ``round``.
    - ``"round_done"`` — round succeeded, payload contains ``round``,
      ``sso_tail`` (last 4 chars), ``elapsed_s``.
    - ``"round_failed"`` — round raised, payload contains ``round``,
      ``error``, ``elapsed_s``.
    - ``"finished"`` — batch about to return, payload contains ``token_count``.

    The callback runs in the worker process; exceptions are swallowed so a
    broken hook never aborts a registration loop.

    ``push_to_api`` is disabled by the parallel orchestrator for child
    workers. The parent process then performs one deduplicated import after all
    workers exit, avoiding concurrent writes to the Admin token store.
    """
    global run_logger, co

    def _emit(event: str, payload: dict[str, Any] | None = None) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(event, dict(payload or {}))
        except Exception:
            pass

    set_config_path(config_path)
    co = _configure_browser_options()

    config_path_obj = get_config_path()
    if not config_path_obj.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {config_path_obj}。请先从 maintainer.config.example.json 复制一份。"
        )

    output_path = resolve_user_path(str(output or default_sso_file()))
    run_logger = setup_run_logger(label=os.environ.get("MAINTAINER_RUN_LOG_LABEL") or None)
    run_logger.info("配置文件: %s", config_path_obj)
    run_logger.info("输出文件: %s", output_path)

    collected_sso: list[str] = []
    failed_rounds: list[str] = []
    current_round = 0
    env_retries_used = 0
    env_retry_limit = _registration_env_retry_limit()
    _emit("started", {"count": count})

    try:
        if stop_check and stop_check():
            return collected_sso
        if _wait_while_paused(pause_check, stop_check):
            return collected_sso

        start_browser()
        _emit("browser_started", {})
        while True:
            if count > 0 and current_round >= count:
                break

            if stop_check and stop_check():
                print("\n[Info] 收到停止信号，退出注册循环。")
                if run_logger:
                    run_logger.info("收到停止信号，退出注册循环")
                break

            if _wait_while_paused(pause_check, stop_check):
                print("\n[Info] 暂停期间收到停止信号，退出注册循环。")
                if run_logger:
                    run_logger.info("暂停期间收到停止信号，退出注册循环")
                break

            current_round += 1
            print(f"\n[*] 开始第 {current_round} 轮注册")
            _emit("round_start", {"round": current_round})
            round_started_at = time.monotonic()

            try:
                result = run_single_registration(
                    output_path,
                    extract_numbers=extract_numbers,
                )
                collected_sso.append(result["sso"])
                _emit(
                    "round_done",
                    {
                        "round": current_round,
                        "sso_tail": str(result.get("sso", ""))[-4:],
                        "elapsed_s": round(time.monotonic() - round_started_at, 1),
                    },
                )
            except KeyboardInterrupt:
                print("\n[Info] 收到中断信号，停止后续轮次。")
                _emit(
                    "round_failed",
                    {
                        "round": current_round,
                        "error": "KeyboardInterrupt",
                        "elapsed_s": round(time.monotonic() - round_started_at, 1),
                    },
                )
                break
            except Exception as error:
                print(f"[Error] 第 {current_round} 轮失败: {error}")
                failed_rounds.append(f"round#{current_round}: {type(error).__name__}: {error}")
                retry_env_failure = (
                    count > 0
                    and env_retries_used < env_retry_limit
                    and _is_registration_env_retryable_error(error)
                    and not (stop_check and stop_check())
                )
                if retry_env_failure:
                    env_retries_used += 1
                    count += 1
                    print(
                        "[Warn] 注册入口被环境/Cloudflare 拦截，"
                        f"将重开浏览器重试 ({env_retries_used}/{env_retry_limit})"
                    )
                if run_logger:
                    run_logger.warning(
                        "第 %s 轮失败: %s: %s",
                        current_round,
                        type(error).__name__,
                        error,
                    )
                _emit(
                    "round_failed",
                    {
                        "round": current_round,
                        "error": f"{type(error).__name__}: {error}",
                        "elapsed_s": round(time.monotonic() - round_started_at, 1),
                        "retrying": retry_env_failure,
                        "env_retries_used": env_retries_used,
                        "env_retry_limit": env_retry_limit,
                    },
                )
            finally:
                if (count == 0 or current_round < count) and not (
                    stop_check and stop_check()
                ):
                    reset_browser_for_next_round()

            if count == 0 or current_round < count:
                time.sleep(2)

    finally:
        if collected_sso and push_to_api:
            print(f"\n[*] 注册完成，推送 {len(collected_sso)} 个 token 到 API...")
            push_sso_to_api(collected_sso)

        stop_browser()
        _emit(
            "finished",
            {
                "token_count": len(collected_sso),
                "failed_rounds": len(failed_rounds),
                "last_error": failed_rounds[-1] if failed_rounds else "",
            },
        )

    return collected_sso


def _worker_entry(
    worker_id: int,
    config_path_str: str,
    count: int,
    output_str: str,
    extract_numbers: bool,
    env_overrides: dict[str, str],
    pause_event: Any,
    stop_event: Any,
    result_queue: Any,
    progress_queue: Any = None,
) -> None:
    """Subprocess entry point for one registration worker.

    Imported as a top-level function so it is picklable by ``spawn``.
    Each worker isolates its browser temp directory under
    ``MAINTAINER_TMP_PATH/worker_<id>`` to avoid Chromium user-data-dir
    collisions when multiple browsers run concurrently. The per-worker run
    log is opened with ``label=f"w{worker_id}"`` so concurrent workers do not
    overwrite each other's log files.

    When ``progress_queue`` is provided, the worker forwards every
    :func:`run_batch` progress event as a
    ``(worker_id, event_name, payload)`` tuple plus an initial ``"alive"``
    event the moment the subprocess starts executing. The orchestrator drains
    this queue so the UI can show per-worker progress as it happens (and so
    we can prove on the orchestrator log that all workers are doing real work
    concurrently instead of one-after-another).
    """
    def _emit(event: str, payload: dict[str, Any] | None = None) -> None:
        if progress_queue is None:
            return
        try:
            progress_queue.put((int(worker_id), str(event), dict(payload or {})))
        except Exception:
            pass

    pid = os.getpid()
    user_data_dir = _compute_worker_chrome_user_data_dir(worker_id, pid)

    try:
        for key, val in env_overrides.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(val)

        debug_port = _select_worker_chrome_debug_port(worker_id, pid)
        os.environ["MAINTAINER_CHROME_DEBUG_PORT"] = str(debug_port)
        _emit(
            "alive",
            {
                "pid": pid,
                "user_data_dir": str(user_data_dir),
                "debug_port": debug_port,
            },
        )

        base_tmp = os.environ.get("MAINTAINER_TMP_PATH", "").strip()
        base_path = Path(base_tmp).expanduser() if base_tmp else maintainer_browser_tmp_dir()
        worker_tmp = base_path / f"worker_{worker_id}"
        worker_tmp.mkdir(parents=True, exist_ok=True)
        os.environ["MAINTAINER_TMP_PATH"] = str(worker_tmp)
        os.environ["MAINTAINER_RUN_LOG_LABEL"] = f"w{worker_id}"

        try:
            shutil.rmtree(user_data_dir, ignore_errors=True)
            user_data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _emit(
                "worker_failed",
                {"error": f"无法创建 user-data-dir {user_data_dir}: {exc}"},
            )
            try:
                result_queue.put((worker_id, [], f"user_data_dir error: {exc}"))
            except Exception:
                pass
            return
        os.environ["MAINTAINER_CHROME_USER_DATA_DIR"] = str(user_data_dir)

        def _is_paused() -> bool:
            return pause_event is not None and not pause_event.is_set()

        def _is_stopped() -> bool:
            return stop_event is not None and stop_event.is_set()

        try:
            tokens = run_batch(
                config_path=config_path_str,
                count=count,
                output=output_str or None,
                extract_numbers=extract_numbers,
                pause_check=_is_paused if pause_event is not None else None,
                stop_check=_is_stopped if stop_event is not None else None,
                progress_callback=_emit if progress_queue is not None else None,
                push_to_api=False,
            )
        finally:
            # Best-effort cleanup so a long-running orchestrator doesn't leak
            # one Chromium profile per worker per run.
            shutil.rmtree(user_data_dir, ignore_errors=True)
        result_queue.put((worker_id, list(tokens), None))
    except BaseException as exc:  # noqa: BLE001 - cross-process boundary
        _emit("worker_failed", {"error": f"{type(exc).__name__}: {exc}"})
        try:
            result_queue.put((worker_id, [], f"{type(exc).__name__}: {exc}"))
        except Exception:
            pass
        shutil.rmtree(user_data_dir, ignore_errors=True)


def _split_count(count: int, workers: int) -> list[int]:
    """Return one share entry per worker.

    ``count`` is the **per-worker** registration count (the UI label is
    "每个 worker 的注册轮数"), so the total target across the run is
    ``count * workers`` whenever ``count > 0``. This semantic guarantees that
    picking ``workers=N`` always spawns exactly ``N`` truly concurrent
    processes, which was the behaviour users expected when they selected
    parallel mode.

    ``count == 0`` means "loop forever"; every worker gets the same sentinel
    and only the stop signal terminates the loop.
    """
    if workers <= 0:
        return []
    if count < 0:
        count = 0
    return [count] * workers


def _build_worker_output(base: Path, worker_id: int) -> Path:
    suffix = base.suffix or ".txt"
    return base.with_name(f"{base.stem}.w{worker_id}{suffix}")


def _read_sso_tokens_from_file(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    tokens: list[str] = []
    seen: set[str] = set()
    for line in lines:
        token = line.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def run_batch_parallel(
    *,
    config_path: str | os.PathLike[str],
    count: int,
    workers: int = 1,
    output: str | os.PathLike[str] | None = None,
    extract_numbers: bool = False,
    pause_check: Callable[[], bool] | None = None,
    stop_check: Callable[[], bool] | None = None,
    pause_event: Any = None,
    stop_event: Any = None,
    env_overrides: dict[str, str] | None = None,
    spawned_workers_callback: Callable[[int], None] | None = None,
    progress_callback: Callable[[int, str, dict[str, Any]], None] | None = None,
) -> list[str]:
    """Run registration either sequentially (workers <= 1) or in parallel.

    Parallel mode spawns ``workers`` child processes (start method ``spawn``)
    that each maintain their own browser instance and each run ``count``
    registration rounds (the total across the run is therefore
    ``count * workers``). ``pause_event`` / ``stop_event`` must be
    ``multiprocessing.synchronize.Event`` objects when ``workers > 1`` so they
    can be shared with child processes.

    A dedicated orchestrator run-log (``run_parallel_*.log``) records each
    worker's spawn pid, completion status, and final token count so operators
    can confirm true concurrency from the UI's log tail.
    """
    workers = max(1, int(workers))

    if workers == 1:
        if pause_check is None and pause_event is not None:
            pause_check = lambda: not pause_event.is_set()  # noqa: E731
        if stop_check is None and stop_event is not None:
            stop_check = lambda: stop_event.is_set()  # noqa: E731
        if spawned_workers_callback is not None:
            spawned_workers_callback(1)

        def _forward_progress(event: str, payload: dict[str, Any]) -> None:
            if progress_callback is None:
                return
            progress_callback(0, event, payload)

        return run_batch(
            config_path=config_path,
            count=count,
            output=output,
            extract_numbers=extract_numbers,
            pause_check=pause_check,
            stop_check=stop_check,
            progress_callback=_forward_progress if progress_callback is not None else None,
        )

    if pause_event is None or stop_event is None:
        raise ValueError(
            "run_batch_parallel requires multiprocessing pause_event and stop_event when workers > 1"
        )

    shares = _split_count(count, workers)
    if not shares:
        return []

    base_output = resolve_user_path(str(output or default_sso_file()))
    config_path_str = str(config_path)

    orch_logger = setup_run_logger(label="parallel")
    total_target = sum(shares) if all(s > 0 for s in shares) else 0
    if total_target > 0:
        orch_logger.info(
            "启动 %d 个并发 worker，每 worker count=%d，总目标=%d",
            workers,
            shares[0],
            total_target,
        )
    else:
        orch_logger.info(
            "启动 %d 个并发 worker，count=0 意味着无限循环直到 stop 信号",
            workers,
        )

    ctx = mp.get_context("spawn")
    result_queue: Any = ctx.Queue()
    progress_queue: Any = ctx.Queue()

    processes: list[Any] = []
    worker_outputs: dict[int, Path] = {}
    for i, share in enumerate(shares):
        worker_output = _build_worker_output(base_output, i)
        worker_outputs[i] = worker_output
        p = ctx.Process(
            target=_worker_entry,
            args=(
                i,
                config_path_str,
                share,
                str(worker_output),
                bool(extract_numbers),
                dict(env_overrides or {}),
                pause_event,
                stop_event,
                result_queue,
                progress_queue,
            ),
            daemon=False,
        )
        processes.append(p)

    import threading

    drain_stop = threading.Event()
    last_progress_at: dict[int, float] = {}
    failure_reported: set[int] = set()

    def _emit_worker_failure(worker_id: int, error: str) -> None:
        if worker_id in failure_reported:
            return
        failure_reported.add(worker_id)
        orch_logger.info("Worker #%d: worker_failed error=%s", worker_id, error)
        if progress_callback is not None:
            try:
                progress_callback(worker_id, "worker_failed", {"error": error})
            except Exception:
                pass

    def _drain_progress() -> None:
        """Consume worker progress events and write interleaved log lines.

        Each line includes the wall-clock timestamp the orchestrator received
        the event, so a viewer scrolling the orchestrator log can tell that
        ``Worker #0: round_start round=1`` and ``Worker #2: round_start round=1``
        landed within milliseconds of each other when workers are truly
        concurrent (vs. seconds apart if they were serialised).
        """
        while not drain_stop.is_set():
            try:
                event = progress_queue.get(timeout=0.2)
            except Exception:
                continue
            if event is None:
                break
            try:
                worker_id, name, payload = event
            except Exception:
                continue
            worker_id = int(worker_id)
            name = str(name)
            payload = dict(payload or {})
            last_progress_at[worker_id] = time.monotonic()
            if name == "worker_failed":
                failure_reported.add(worker_id)
            kv = " ".join(f"{k}={v}" for k, v in (payload or {}).items())
            suffix = f" {kv}" if kv else ""
            orch_logger.info("Worker #%d: %s%s", worker_id, name, suffix)
            if progress_callback is not None:
                try:
                    progress_callback(worker_id, name, payload)
                except Exception:
                    pass

    drain_thread = threading.Thread(target=_drain_progress, daemon=True)
    drain_thread.start()

    for idx, p in enumerate(processes):
        p.start()
        last_progress_at[idx] = time.monotonic()
        orch_logger.info("Worker #%d 已启动 pid=%s", idx, p.pid)

    if spawned_workers_callback is not None:
        spawned_workers_callback(len(processes))

    idle_timeout = _worker_idle_timeout_seconds()
    finished_workers: set[int] = set()
    timed_out_workers: set[int] = set()

    def _is_process_alive(process: Any) -> bool:
        try:
            return bool(process.is_alive())
        except Exception:
            return getattr(process, "exitcode", None) is None

    try:
        while len(finished_workers) < len(processes):
            for idx, p in enumerate(processes):
                if idx in finished_workers:
                    continue

                p.join(timeout=0)
                if _is_process_alive(p):
                    idle_for = time.monotonic() - last_progress_at.get(idx, 0.0)
                    if idle_timeout > 0 and idle_for >= idle_timeout:
                        error = f"worker idle timeout after {idle_timeout:.0f}s"
                        timed_out_workers.add(idx)
                        _emit_worker_failure(idx, error)
                        orch_logger.warning(
                            "Worker #%d 空闲 %.1fs 超过阈值 %.1fs，终止 pid=%s",
                            idx,
                            idle_for,
                            idle_timeout,
                            getattr(p, "pid", None),
                        )
                        try:
                            p.terminate()
                        except Exception as exc:
                            orch_logger.warning("Worker #%d terminate 失败: %s", idx, exc)
                        try:
                            p.join(timeout=WORKER_TERMINATE_GRACE_SECONDS)
                        except Exception:
                            pass
                        if _is_process_alive(p):
                            try:
                                p.kill()
                            except Exception:
                                pass
                            try:
                                p.join(timeout=WORKER_TERMINATE_GRACE_SECONDS)
                            except Exception:
                                pass

                    if _is_process_alive(p):
                        continue

                finished_workers.add(idx)
                orch_logger.info(
                    "Worker #%d 已结束 exitcode=%s", idx, p.exitcode
                )
            if len(finished_workers) < len(processes):
                time.sleep(0.2)
    except KeyboardInterrupt:
        stop_event.set()
        if not pause_event.is_set():
            pause_event.set()
        for p in processes:
            p.join(timeout=WORKER_TERMINATE_GRACE_SECONDS)
        drain_stop.set()
        try:
            progress_queue.put(None)
        except Exception:
            pass
        drain_thread.join(timeout=2)
        raise

    drain_stop.set()
    try:
        progress_queue.put(None)
    except Exception:
        pass
    drain_thread.join(timeout=2)

    all_tokens: list[str] = []
    errors: list[str] = []
    result_worker_ids: set[int] = set()
    while True:
        try:
            worker_id, tokens, error = result_queue.get_nowait()
        except Exception:
            break
        worker_id = int(worker_id)
        result_worker_ids.add(worker_id)
        if error:
            errors.append(f"worker#{worker_id}: {error}")
            print(f"[Warn] Worker {worker_id} 失败: {error}")
            _emit_worker_failure(worker_id, str(error))
        all_tokens.extend(tokens)

    for idx, p in enumerate(processes):
        exitcode = getattr(p, "exitcode", None)
        if idx not in result_worker_ids:
            if idx in timed_out_workers:
                error = f"worker idle timeout after {idle_timeout:.0f}s"
            elif exitcode not in (0, None):
                error = f"worker exited without result (exitcode={exitcode})"
            else:
                error = "worker exited without result"
            errors.append(f"worker#{idx}: {error}")
            _emit_worker_failure(idx, error)
        elif exitcode not in (0, None):
            error = f"worker exited with non-zero exitcode={exitcode}"
            errors.append(f"worker#{idx}: {error}")
            _emit_worker_failure(idx, error)

    if errors:
        print(f"[Warn] 并发注册存在 {len(errors)} 个 worker 异常")
        recovered_tokens: list[str] = []
        for idx in range(len(processes)):
            recovered_tokens.extend(_read_sso_tokens_from_file(worker_outputs[idx]))

        existing = {str(token).strip() for token in all_tokens if str(token).strip()}
        missing_tokens = [
            token for token in _merge_tokens([], recovered_tokens) if token not in existing
        ]
        if missing_tokens:
            all_tokens = _merge_tokens(all_tokens, missing_tokens)
            orch_logger.info(
                "从 worker 输出文件恢复 %d 个未上报 token",
                len(missing_tokens),
            )

    all_tokens = _merge_tokens([], all_tokens)
    if all_tokens:
        orch_logger.info("并发注册完成，父进程统一推送 %d 个 token 到 API", len(all_tokens))
        set_config_path(config_path_str)
        push_sso_to_api(all_tokens)

    return all_tokens


def main() -> None:
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config")
    pre_args, _ = pre_parser.parse_known_args()
    if pre_args.config:
        set_config_path(pre_args.config)

    config_count = load_run_count()

    parser = argparse.ArgumentParser(
        description="xAI 自动注册并采集 sso",
        parents=[pre_parser],
    )
    parser.add_argument(
        "--count",
        type=int,
        default=config_count,
        help=f"执行轮数，0 表示无限循环（默认读取配置文件 run.count，当前 {config_count}）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="并发 worker 数量（1 为顺序运行，>1 会起多个子进程同时跑浏览器）",
    )
    parser.add_argument(
        "--output",
        default=str(default_sso_file()),
        help="sso 输出 txt 路径",
    )
    parser.add_argument(
        "--extract-numbers",
        action="store_true",
        help="注册完成后额外提取页面数字文本",
    )
    args = parser.parse_args()

    if args.config:
        set_config_path(args.config)

    if args.workers and args.workers > 1:
        ctx = mp.get_context("spawn")
        pause_event = ctx.Event()
        pause_event.set()
        stop_event = ctx.Event()
        run_batch_parallel(
            config_path=get_config_path(),
            count=args.count,
            workers=args.workers,
            output=args.output,
            extract_numbers=args.extract_numbers,
            pause_event=pause_event,
            stop_event=stop_event,
        )
    else:
        run_batch(
            config_path=get_config_path(),
            count=args.count,
            output=args.output,
            extract_numbers=args.extract_numbers,
        )


if __name__ == "__main__":
    main()
