from __future__ import annotations

import argparse
import datetime
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

from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import PageDisconnectedError
try:
    from pyvirtualdisplay import Display
except Exception:
    Display = None

from .mailbox import get_email_and_token, get_oai_code
from .settings import (
    as_bool,
    get_config_path,
    load_config,
    maintainer_browser_tmp_dir,
    maintainer_log_dir,
    maintainer_sso_dir,
    project_root,
    set_config_path,
    extension_dir,
)


SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
DEFAULT_MIN_BROWSER_FREE_BYTES = 256 * 1024 * 1024
DEFAULT_HEADLESS_WINDOW_SIZE = "1440,900"
WORKER_DEBUG_PORT_MIN = 20_000
WORKER_DEBUG_PORT_SPAN = 40_000
HEADLESS_STABILITY_ARGS = (
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
    "--lang=en-US",
    "--password-store=basic",
    "--use-mock-keychain",
)

browser = None
page = None
_virtual_display = None
run_logger: logging.Logger | None = None


def setup_run_logger(label: str | None = None) -> logging.Logger:
    """Create the per-run log file.

    ``label`` is woven into the filename so concurrent workers do not collide
    on the same path. Multi-process orchestration uses ``label="w{worker_id}"``
    for each worker and ``label="parallel"`` for the parent orchestrator log.
    """
    log_dir = maintainer_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{label}" if label else ""
    log_path = log_dir / f"run{suffix}_{ts}_pid{os.getpid()}.log"

    logger_name = f"grok_maintainer.{label}" if label else "grok_maintainer"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    prefix = f"[w{label.removeprefix('w')}] " if label and label.startswith("w") and label[1:].isdigit() else ""
    fmt = logging.Formatter(f"%(asctime)s | {prefix}%(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.info("日志文件: %s", log_path)
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
    seed = (int(pid) + int(worker_id) * 9973) % WORKER_DEBUG_PORT_SPAN
    for offset in range(WORKER_DEBUG_PORT_SPAN):
        port = WORKER_DEBUG_PORT_MIN + ((seed + offset) % WORKER_DEBUG_PORT_SPAN)
        if _is_tcp_port_available(port):
            return port
    raise RuntimeError("无法为并发注册 worker 分配可用 Chrome 调试端口")


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
    if os.getenv("DISPLAY") or as_bool(os.getenv("MAINTAINER_HEADLESS"), default=False):
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
    opts.auto_port()
    opts.set_tmp_path(str(_resolve_browser_tmp_path()))
    opts.set_timeouts(base=1)
    opts.add_extension(str(extension_dir()))

    browser_path = _discover_browser_path()
    if browser_path:
        opts.set_browser_path(browser_path)

    user_data_dir = os.getenv("MAINTAINER_CHROME_USER_DATA_DIR", "").strip()
    if user_data_dir:
        opts.set_user_data_path(str(Path(user_data_dir).expanduser().resolve()))

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
        opts.set_local_port(port_int)

    opts.set_argument("--no-first-run")
    opts.set_argument("--no-default-browser-check")

    is_headless = as_bool(os.getenv("MAINTAINER_HEADLESS"), default=False)
    if is_headless:
        opts.headless(True)
        for arg in HEADLESS_STABILITY_ARGS:
            opts.set_argument(arg)

    if as_bool(os.getenv("MAINTAINER_NO_SANDBOX"), default=_running_in_container()):
        opts.set_argument("--no-sandbox")
    if as_bool(
        os.getenv("MAINTAINER_DISABLE_DEV_SHM"),
        default=_running_in_container(),
    ):
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
    browser = Chromium(co)
    tabs = browser.get_tabs()
    page = tabs[-1] if tabs else browser.new_tab()
    return browser, page


def stop_browser() -> None:
    global browser, page
    if browser is not None:
        try:
            browser.quit()
        except Exception:
            pass
    browser = None
    page = None
    _stop_virtual_display()


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
    return page


def open_signup_page() -> None:
    global page
    refresh_active_page()
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


def click_email_signup_button(timeout: int = 10) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        clicked = page.run_js(
            r"""
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = candidates.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    return text.includes('使用邮箱注册');
});

if (!target) {
    return false;
}

target.click();
return true;
            """
        )

        if clicked:
            return True

        time.sleep(0.5)

    raise RuntimeError('未找到“使用邮箱注册”按钮')


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
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    return text === '注册' || text.includes('注册');
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


def fill_code_and_submit(email: str, dev_token: str, timeout: int = 180) -> str:
    code = get_oai_code(dev_token, email)
    if not code:
        raise RuntimeError("获取验证码失败")

    deadline = time.time() + timeout
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
            if has_profile_form():
                print("[*] 已直接进入最终注册页，跳过验证码按钮确认。")
                return code
            time.sleep(0.5)
            continue

        if filled != "filled":
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
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    return text === '确认邮箱' || text.includes('确认邮箱') || text === '继续' || text.includes('继续') || text === '下一步' || text.includes('下一步');
});

if (!confirmButton) {
    return 'no-button';
}

confirmButton.focus();
confirmButton.click();
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
    raise RuntimeError("未找到验证码输入框或确认邮箱按钮")


def get_turnstile_token() -> str:
    page.run_js("try { turnstile.reset() } catch(e) { }")

    for _ in range(15):
        try:
            response = page.run_js(
                "try { return turnstile.getResponse() } catch(e) { return null }"
            )
            if response:
                return response

            challenge_solution = page.ele("@name=cf-turnstile-response")
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

Object.defineProperty(MouseEvent.prototype, 'screenX', { value: screenX });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: screenY });
                """
            )

            challenge_iframe_body = challenge_iframe.ele("tag:body").shadow_root
            challenge_button = challenge_iframe_body.ele("tag:input")
            challenge_button.click()
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("failed to solve turnstile")


def build_profile() -> tuple[str, str, str]:
    given_name = "Neo"
    family_name = "Lin"
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password


def fill_profile_and_submit(timeout: int = 120) -> dict[str, str]:
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    turnstile_token = ""

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
            time.sleep(0.5)
            continue

        if filled != "filled":
            print(f"[Debug] 最终注册页输入框已出现，但姓名/密码写入失败: {filled}")
            time.sleep(0.5)
            continue

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
            print("[*] 检测到最终注册页存在 Turnstile，开始使用现有真人化点击逻辑。")
            turnstile_token = get_turnstile_token()
            if turnstile_token:
                synced = page.run_js(
                    """
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
return String(challengeInput.value || '').trim() === String(token || '').trim();
                    """,
                    turnstile_token,
                )
                if synced:
                    print("[*] Turnstile 响应已同步到最终注册表单。")

        time.sleep(1.2)

        try:
            submit_button = page.ele("tag:button@@text()=完成注册")
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
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    return text === '完成注册' || text.includes('完成注册');
});
if (!submitButton || submitButton.disabled || submitButton.getAttribute('aria-disabled') === 'true') {
    return false;
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
            if challenge_value not in ("not-found", ""):
                submit_button.click()
                clicked = True
            else:
                clicked = False

        if clicked:
            print(f"[*] 已填写注册资料并点击完成注册: {given_name} {family_name}")
            return {
                "given_name": given_name,
                "family_name": family_name,
                "password": password,
            }

        time.sleep(0.5)

    raise RuntimeError("未找到最终注册表单或完成注册按钮")


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


def wait_for_sso_cookie(timeout: int = 120) -> str:
    deadline = time.time() + timeout
    last_seen_names = set()

    while time.time() < deadline:
        try:
            refresh_active_page()
            if page is None:
                time.sleep(1)
                continue

            cookies = page.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()

                if name:
                    last_seen_names.add(name)

                if name == "sso" and value:
                    print("[*] 注册完成后已获取到 sso cookie。")
                    return value

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
    sso_value = wait_for_sso_cookie()
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


def run_batch(
    *,
    config_path: str | os.PathLike[str],
    count: int,
    output: str | os.PathLike[str] | None = None,
    extract_numbers: bool = False,
    pause_check: Callable[[], bool] | None = None,
    stop_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
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
    current_round = 0
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
                _emit(
                    "round_failed",
                    {
                        "round": current_round,
                        "error": f"{type(error).__name__}: {error}",
                        "elapsed_s": round(time.monotonic() - round_started_at, 1),
                    },
                )
            finally:
                restart_browser()

            if count == 0 or current_round < count:
                time.sleep(2)

    finally:
        if collected_sso:
            print(f"\n[*] 注册完成，推送 {len(collected_sso)} 个 token 到 API...")
            push_sso_to_api(collected_sso)

        stop_browser()
        _emit("finished", {"token_count": len(collected_sso)})

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
        return run_batch(
            config_path=config_path,
            count=count,
            output=output,
            extract_numbers=extract_numbers,
            pause_check=pause_check,
            stop_check=stop_check,
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
    for i, share in enumerate(shares):
        worker_output = _build_worker_output(base_output, i)
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
            kv = " ".join(f"{k}={v}" for k, v in (payload or {}).items())
            suffix = f" {kv}" if kv else ""
            orch_logger.info("Worker #%d: %s%s", int(worker_id), str(name), suffix)
            if progress_callback is not None:
                try:
                    progress_callback(int(worker_id), str(name), dict(payload or {}))
                except Exception:
                    pass

    drain_thread = threading.Thread(target=_drain_progress, daemon=True)
    drain_thread.start()

    for idx, p in enumerate(processes):
        p.start()
        orch_logger.info("Worker #%d 已启动 pid=%s", idx, p.pid)

    if spawned_workers_callback is not None:
        spawned_workers_callback(len(processes))

    try:
        for idx, p in enumerate(processes):
            p.join()
            orch_logger.info(
                "Worker #%d 已结束 exitcode=%s", idx, p.exitcode
            )
    except KeyboardInterrupt:
        stop_event.set()
        if not pause_event.is_set():
            pause_event.set()
        for p in processes:
            p.join()
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
    while True:
        try:
            worker_id, tokens, error = result_queue.get_nowait()
        except Exception:
            break
        if error:
            errors.append(f"worker#{worker_id}: {error}")
            print(f"[Warn] Worker {worker_id} 失败: {error}")
        all_tokens.extend(tokens)

    if errors:
        print(f"[Warn] 并发注册存在 {len(errors)} 个 worker 异常")

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
