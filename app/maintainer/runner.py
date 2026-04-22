from __future__ import annotations

import argparse
import datetime
import logging
import os
import secrets
import sys
import time
from pathlib import Path

from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import PageDisconnectedError

from .mailbox import get_email_and_token, get_oai_code
from .settings import (
    as_bool,
    get_config_path,
    load_config,
    maintainer_log_dir,
    maintainer_sso_dir,
    project_root,
    set_config_path,
    extension_dir,
)


SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

browser = None
page = None
run_logger: logging.Logger | None = None


def setup_run_logger() -> logging.Logger:
    log_dir = maintainer_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"run_{ts}.log"

    logger = logging.getLogger("grok_maintainer")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
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

co = ChromiumOptions()
co.auto_port()
co.set_timeouts(base=1)
co.add_extension(str(extension_dir()))


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
            print(f"[*] 已填写注册资料并点击完成注册: {given_name} {family_name} / {password}")
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


def main() -> None:
    global run_logger

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

    config_path = get_config_path()
    if not config_path.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {config_path}。请先从 maintainer.config.example.json 复制一份。"
        )

    output_path = resolve_user_path(args.output)
    run_logger = setup_run_logger()
    run_logger.info("配置文件: %s", config_path)
    run_logger.info("输出文件: %s", output_path)

    collected_sso: list[str] = []
    current_round = 0

    try:
        start_browser()
        while True:
            if args.count > 0 and current_round >= args.count:
                break

            current_round += 1
            print(f"\n[*] 开始第 {current_round} 轮注册")

            try:
                result = run_single_registration(
                    output_path,
                    extract_numbers=args.extract_numbers,
                )
                collected_sso.append(result["sso"])
            except KeyboardInterrupt:
                print("\n[Info] 收到中断信号，停止后续轮次。")
                break
            except Exception as error:
                print(f"[Error] 第 {current_round} 轮失败: {error}")
            finally:
                restart_browser()

            if args.count == 0 or current_round < args.count:
                time.sleep(2)

    finally:
        if collected_sso:
            print(f"\n[*] 注册完成，推送 {len(collected_sso)} 个 token 到 API...")
            push_sso_to_api(collected_sso)

        stop_browser()


if __name__ == "__main__":
    main()
