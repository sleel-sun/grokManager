"""HTTP-driven automatic registration for ordinary ChatGPT/GPT accounts."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
import json
import random
import secrets
import string
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import requests

from .mailbox import (
    create_session,
    create_temp_email,
    extract_verification_code_from_mail,
    fetch_emails,
    mail_matches_target_email,
    wait_for_verification_code,
)
from .settings import as_bool, load_json, set_config_path


CHATGPT_BASE = "https://chatgpt.com"
AUTH_BASE = "https://auth.openai.com"

_FIRST_NAMES = (
    "James",
    "Emma",
    "Liam",
    "Olivia",
    "Noah",
    "Ava",
    "Ethan",
    "Sophia",
    "Lucas",
    "Mia",
    "Mason",
    "Isabella",
    "Logan",
    "Charlotte",
    "Alexander",
    "Amelia",
)
_LAST_NAMES = (
    "Smith",
    "Johnson",
    "Brown",
    "Davis",
    "Wilson",
    "Moore",
    "Taylor",
    "Clark",
    "Hall",
    "Young",
    "Anderson",
    "Thomas",
)
_CHROME_PROFILES = (
    (131, 6778, (69, 205), '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'),
    (133, 6943, (33, 153), '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"'),
    (136, 7103, (48, 175), '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"'),
    (142, 7540, (30, 150), '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"'),
)
_ACCOUNT_NOT_CREATED_MARKERS = (
    "registration_disallowed",
    "cannot create your account",
    "cannot create account",
    "创建账号资料失败",
)
_WRONG_EMAIL_OTP_MARKERS = (
    "wrong_email_otp_code",
    "wrong code",
)
_DEFAULT_REGISTRATION_ATTEMPTS = 2
_DEFAULT_OTP_TIMEOUT_S = 90
_DEFAULT_LOGIN_OTP_TIMEOUT_S = 90


@dataclass(slots=True)
class GPTRegistrationResult:
    email: str
    password: str
    mail_token: str
    status: str
    access_token: str = ""
    error: str = ""
    phone_verification_required: bool = False

    def saved_identifier(self) -> str:
        return self.access_token or self.email


class GPTRegistrationError(RuntimeError):
    """Raised when one registration round cannot continue."""


class GPTAccountNotCreatedError(GPTRegistrationError):
    """Raised when OpenAI rejected the registration before an account existed."""


def _looks_like_account_not_created_error(error: BaseException | str) -> bool:
    if isinstance(error, GPTAccountNotCreatedError):
        return True
    text = str(error).lower()
    return any(marker in text for marker in _ACCOUNT_NOT_CREATED_MARKERS)


def _looks_like_wrong_email_otp_error(error: BaseException | str) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _WRONG_EMAIL_OTP_MARKERS)


def _int_config(conf: dict[str, Any], key: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(conf.get(key) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


class ChatGPTRegistrationClient:
    """Small requests-based port of CodexManager's ChatGPT registration flow."""

    def __init__(self, *, session: requests.Session | None = None) -> None:
        profile = random.choice(_CHROME_PROFILES)
        major, build, patch_range, sec_ch_ua = profile
        patch = random.randint(*patch_range)
        self.chrome_full_version = f"{major}.0.{build}.{patch}"
        self.user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{self.chrome_full_version} Safari/537.36"
        )
        self.sec_ch_ua = sec_ch_ua
        self.device_id = str(uuid.uuid4()).lower()
        self.auth_session_logging_id = str(uuid.uuid4()).lower()
        self.callback_url = ""
        self.session = session or create_session()
        for domain in (".auth.openai.com", "auth.openai.com", ".chatgpt.com", "chatgpt.com"):
            self.session.cookies.set("oai-did", self.device_id, domain=domain, path="/")

    def _browser_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-arch": '"x86"',
            "sec-ch-ua-bitness": '"64"',
            "sec-ch-ua-full-version": f'"{self.chrome_full_version}"',
            "sec-ch-ua-platform-version": f'"{random.randint(10, 15)}.0.0"',
            "Accept-Language": random.choice(
                ("en-US,en;q=0.9", "en-US,en;q=0.9,zh-CN;q=0.8", "en,en-US;q=0.9")
            ),
        }

    @staticmethod
    def _trace_headers() -> dict[str, str]:
        parent_id = random.randint(100_000_000_000_000_000, 999_999_999_999_999_999)
        trace_id = random.randint(100_000_000_000_000_000, 999_999_999_999_999_999)
        return {
            "traceparent": f"00-{uuid.uuid4().hex}-{parent_id:016x}-01",
            "tracestate": "dd=s:1;o:rum",
            "x-datadog-origin": "rum",
            "x-datadog-sampling-priority": "1",
            "x-datadog-trace-id": str(trace_id),
            "x-datadog-parent-id": str(parent_id),
        }

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        headers = dict(self._browser_headers())
        headers.update(kwargs.pop("headers", {}) or {})
        response = self.session.request(method, url, headers=headers, timeout=30, **kwargs)
        return response

    def visit_homepage(self) -> None:
        response = self._request(
            "GET",
            f"{CHATGPT_BASE}/",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        if response.status_code >= 400:
            raise GPTRegistrationError(f"访问 ChatGPT 首页失败: HTTP {response.status_code}")

    def get_csrf(self) -> str:
        response = self._request(
            "GET",
            f"{CHATGPT_BASE}/api/auth/csrf",
            headers={"Accept": "application/json", "Referer": f"{CHATGPT_BASE}/"},
        )
        token = (response.json() if response.ok else {}).get("csrfToken")
        if not token:
            raise GPTRegistrationError(f"获取 CSRF 失败: HTTP {response.status_code}")
        return str(token)

    def signin(self, email: str, csrf: str) -> str:
        params = {
            "prompt": "login",
            "ext-oai-did": self.device_id,
            "auth_session_logging_id": self.auth_session_logging_id,
            "screen_hint": "login_or_signup",
            "login_hint": email,
        }
        form = {
            "callbackUrl": f"{CHATGPT_BASE}/",
            "csrfToken": csrf,
            "json": "true",
        }
        for attempt in range(3):
            response = self._request(
                "POST",
                f"{CHATGPT_BASE}/api/auth/signin/openai?{urlencode(params)}",
                data=form,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "Referer": f"{CHATGPT_BASE}/",
                    "Origin": CHATGPT_BASE,
                },
            )
            body = response.text[:240]
            if response.status_code == 403 and body.lstrip().startswith("<") and attempt < 2:
                time.sleep(3 + attempt)
                self.visit_homepage()
                csrf = self.get_csrf()
                form["csrfToken"] = csrf
                continue
            try:
                url = response.json().get("url")
            except ValueError:
                url = None
            if url:
                return str(url)
            raise GPTRegistrationError(f"获取 OpenAI authorize URL 失败: HTTP {response.status_code} {body}")
        raise GPTRegistrationError("获取 OpenAI authorize URL 重试耗尽")

    def authorize(self, url: str) -> str:
        response = self._request(
            "GET",
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": f"{CHATGPT_BASE}/",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        if response.status_code >= 400:
            raise GPTRegistrationError(f"OpenAI authorize 失败: HTTP {response.status_code}")
        return response.url

    def register_password(self, email: str, password: str) -> None:
        headers = {
            **self._trace_headers(),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": f"{AUTH_BASE}/create-account/password",
            "Origin": AUTH_BASE,
        }
        response = self._request(
            "POST",
            f"{AUTH_BASE}/api/accounts/user/register",
            json={"username": email, "password": password},
            headers=headers,
        )
        if response.status_code != 200:
            raise GPTRegistrationError(f"注册密码失败: HTTP {response.status_code} {response.text[:240]}")

    def submit_login_password(self, email: str, password: str) -> None:
        response = self._request(
            "POST",
            f"{AUTH_BASE}/api/accounts/user/login",
            json={"username": email, "password": password},
            headers={
                **self._trace_headers(),
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Referer": f"{AUTH_BASE}/log-in/password",
                "Origin": AUTH_BASE,
            },
        )
        if response.status_code >= 500:
            raise GPTRegistrationError(f"提交 ChatGPT 登录密码失败: HTTP {response.status_code}")

    def send_otp(self, *, referer: str | None = None) -> None:
        response = self._request(
            "GET",
            f"{AUTH_BASE}/api/accounts/email-otp/send",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": referer or f"{AUTH_BASE}/create-account/password",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        if response.status_code >= 400:
            raise GPTRegistrationError(f"发送邮箱 OTP 失败: HTTP {response.status_code}")

    def validate_otp(self, code: str) -> str:
        response = self._request(
            "POST",
            f"{AUTH_BASE}/api/accounts/email-otp/validate",
            json={"code": code},
            headers={
                **self._trace_headers(),
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Referer": f"{AUTH_BASE}/email-verification",
                "Origin": AUTH_BASE,
            },
        )
        if response.status_code != 200:
            raise GPTRegistrationError(f"验证邮箱 OTP 失败: HTTP {response.status_code} {response.text[:240]}")
        try:
            data = response.json()
        except ValueError:
            data = {}
        return str(data.get("continue_url") or data.get("url") or data.get("redirect_url") or "")

    def create_account(self, name: str, birthdate: str) -> bool:
        response = self._request(
            "POST",
            f"{AUTH_BASE}/api/accounts/create_account",
            json={"name": name, "birthdate": birthdate},
            headers={
                **self._trace_headers(),
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Referer": f"{AUTH_BASE}/about-you",
                "Origin": AUTH_BASE,
            },
        )
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code != 200:
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            redirect_uri = data.get("redirect_uri") or error.get("redirect_uri")
            if error.get("code") == "unsupported_email" and redirect_uri:
                self.callback_url = str(redirect_uri)
                return False
            if error.get("code") == "registration_disallowed":
                raise GPTAccountNotCreatedError(
                    f"创建账号资料失败: HTTP {response.status_code} {response.text[:240]}"
                )
            raise GPTAccountNotCreatedError(
                f"创建账号资料失败: HTTP {response.status_code} {response.text[:240]}"
            )

        continue_url = data.get("continue_url") or data.get("url") or data.get("redirect_url") or ""
        self.callback_url = str(continue_url)
        probe = f"{data.get('page_type', '')} {continue_url}".lower()
        return any(
            marker in probe
            for marker in (
                "phone-verification",
                "phone_verification",
                "verify-phone",
                "verify_phone",
                "add-phone",
                "add_phone",
                "sms-verification",
                "mobile-verification",
            )
        )

    def perform_callback(self, url: str = "") -> None:
        callback = url or self.callback_url
        if not callback:
            return
        full_url = callback if callback.startswith("http") else f"{AUTH_BASE}{callback}"
        self._request(
            "GET",
            full_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1",
            },
        )

    def session_access_token(self) -> str:
        response = self._request(
            "GET",
            f"{CHATGPT_BASE}/api/auth/session",
            headers={"Accept": "application/json", "Referer": f"{CHATGPT_BASE}/"},
        )
        if response.status_code != 200:
            return ""
        try:
            token = response.json().get("accessToken")
        except ValueError:
            token = None
        return str(token or "").strip()

    def login_with_otp(
        self,
        email: str,
        password: str,
        mail_token: str,
        *,
        ignore_otp: str | None = None,
        timeout: int = _DEFAULT_LOGIN_OTP_TIMEOUT_S,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> str:
        self.visit_homepage()
        csrf = self.get_csrf()
        auth_url = self.signin(email, csrf)
        final_url = self.authorize(auth_url)
        if not self.session_access_token():
            self.submit_login_password(email, password)
        token = self.session_access_token()
        if token:
            return token

        attempted_codes: set[str] = set()
        for attempt in range(3):
            old_ids, old_codes = _mail_snapshot(mail_token, email)
            ignore_codes = set(old_codes)
            if ignore_otp:
                ignore_codes.add(ignore_otp)
            ignore_codes.update(attempted_codes)
            if progress_callback:
                progress_callback(
                    "login_otp_wait_start",
                    {"email": email, "attempt": attempt + 1, "attempts": 3, "timeout_s": timeout},
                )
            self.send_otp(referer=f"{AUTH_BASE}/log-in/password")
            code = _wait_for_code(
                mail_token,
                email,
                timeout=timeout,
                ignore_codes=ignore_codes,
                ignore_ids=old_ids,
            )
            if not code:
                if progress_callback:
                    progress_callback(
                        "login_otp_wait_timeout",
                        {"email": email, "attempt": attempt + 1, "attempts": 3, "timeout_s": timeout},
                    )
                raise GPTRegistrationError("等待 ChatGPT 登录邮箱验证码超时")
            attempted_codes.add(code)
            try:
                continue_url = self.validate_otp(code)
            except GPTRegistrationError as exc:
                if attempt < 2 and _looks_like_wrong_email_otp_error(exc):
                    if progress_callback:
                        progress_callback(
                            "login_otp_wrong",
                            {"email": email, "attempt": attempt + 1, "attempts": 3},
                        )
                    time.sleep(2)
                    continue
                raise
            if continue_url:
                self.perform_callback(continue_url)
            if "phone" in final_url.lower():
                raise GPTRegistrationError("ChatGPT 登录需要手机验证")
            token = self.session_access_token()
            if not token:
                raise GPTRegistrationError("ChatGPT 登录成功后未返回 session access token")
            return token
        raise GPTRegistrationError("ChatGPT 登录邮箱验证码连续错误")

    def run_register(
        self,
        email: str,
        password: str,
        mail_token: str,
        *,
        timeout: int = _DEFAULT_OTP_TIMEOUT_S,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> tuple[bool, str | None]:
        try:
            self.visit_homepage()
            csrf = self.get_csrf()
            auth_url = self.signin(email, csrf)
            final_url = self.authorize(auth_url)
            need_otp = False

            if "create-account/password" in final_url:
                self.register_password(email, password)
                self.send_otp()
                need_otp = True
            elif "email-verification" in final_url or "email-otp" in final_url:
                need_otp = True
            elif "about-you" in final_url:
                return self.create_account(_random_name(), _random_birthdate()), None
            elif "callback" in final_url or "chatgpt.com" in final_url:
                return False, None
            else:
                self.register_password(email, password)
                self.send_otp()
                need_otp = True

            used_otp: str | None = None
            continue_url = ""
            if need_otp:
                if progress_callback:
                    progress_callback("otp_wait_start", {"email": email, "timeout_s": timeout})
                code = _wait_for_code(mail_token, email, timeout=timeout)
                if not code:
                    if progress_callback:
                        progress_callback("otp_wait_timeout", {"email": email, "timeout_s": timeout})
                    raise GPTAccountNotCreatedError("等待 OpenAI 邮箱验证码超时")
                used_otp = code
                continue_url = self.validate_otp(code)

            if "create-account/password" in continue_url:
                self.register_password(email, password)

            phone_required = self.create_account(_random_name(), _random_birthdate())
            if not phone_required:
                self.perform_callback()
            return phone_required, used_otp
        except GPTAccountNotCreatedError:
            raise
        except GPTRegistrationError as exc:
            raise GPTAccountNotCreatedError(str(exc)) from exc


def _random_name() -> str:
    return f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"


def _random_birthdate() -> str:
    return f"{random.randint(1985, 2002)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


def _random_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return f"{''.join(secrets.choice(alphabet) for _ in range(14))}!Aa1"


def _wait_for_code(
    mail_token: str,
    email: str,
    timeout: int = 120,
    ignore_codes: set[str] | None = None,
    ignore_ids: set[Any] | None = None,
) -> str | None:
    conf = _load_current_config()
    email_conf = conf.get("email") if isinstance(conf.get("email"), dict) else {}
    worker_domain = str(email_conf.get("worker_domain") or "").strip()
    admin_password = str(email_conf.get("admin_password") or "")
    verify_ssl = as_bool(email_conf.get("verify_ssl", True), default=True)
    if not worker_domain:
        return None
    session = create_session(verify_ssl=verify_ssl)
    code = wait_for_verification_code(
        session=session,
        worker_domain=worker_domain,
        cf_token=mail_token,
        target_email=email,
        admin_password=admin_password,
        timeout=timeout,
        ignore_codes=ignore_codes,
        ignore_ids=ignore_ids,
    )
    return code.replace("-", "") if code else None


def _mail_snapshot(mail_token: str, email: str) -> tuple[set[Any], set[str]]:
    conf = _load_current_config()
    email_conf = conf.get("email") if isinstance(conf.get("email"), dict) else {}
    worker_domain = str(email_conf.get("worker_domain") or "").strip()
    admin_password = str(email_conf.get("admin_password") or "")
    verify_ssl = as_bool(email_conf.get("verify_ssl", True), default=True)
    if not worker_domain:
        return set(), set()
    session = create_session(verify_ssl=verify_ssl)
    mails = fetch_emails(
        session=session,
        worker_domain=worker_domain,
        cf_token=mail_token,
        target_email=email,
        admin_password=admin_password,
    )
    ids: set[Any] = set()
    codes: set[str] = set()
    for item in mails or []:
        if not isinstance(item, dict) or not mail_matches_target_email(item, email):
            continue
        if item.get("id") is not None:
            ids.add(item.get("id"))
        code = extract_verification_code_from_mail(item, email)
        if code:
            codes.add(code.replace("-", "").upper())
    return ids, codes


def _mail_ids(mail_token: str, email: str) -> set[Any]:
    ids, _codes = _mail_snapshot(mail_token, email)
    return ids


def _load_current_config() -> dict[str, Any]:
    from .settings import load_config

    return load_config()


def _create_temp_email_from_config(conf: dict[str, Any]) -> tuple[str, str]:
    email_conf = conf.get("email") if isinstance(conf.get("email"), dict) else {}
    worker_domain = str(email_conf.get("worker_domain") or "").strip()
    admin_password = str(email_conf.get("admin_password") or "")
    verify_ssl = as_bool(email_conf.get("verify_ssl", True), default=True)
    domains_raw = email_conf.get("email_domains")
    domains = [str(item).strip() for item in domains_raw if str(item).strip()] if isinstance(domains_raw, list) else []
    if not worker_domain or not admin_password or not domains:
        raise GPTRegistrationError("缺少临时邮箱配置: worker_domain/email_domains/admin_password")
    email, token = create_temp_email(
        session=create_session(verify_ssl=verify_ssl),
        worker_domain=worker_domain,
        email_domains=domains,
        admin_password=admin_password,
        logger=__import__("logging").getLogger("grok_maintainer.gpt"),
    )
    if not email or not token:
        raise GPTRegistrationError("创建临时邮箱失败")
    return email, token


def _gpt_account_payload(result: GPTRegistrationResult, *, email_provider: str = "CF Temp Mail") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "email": result.email,
        "password": result.password,
        "mail_token": result.mail_token,
        "email_provider": email_provider,
        "alias": result.email.split("@", 1)[0] if "@" in result.email else result.email,
        "plan_type": "free",
    }
    if result.access_token:
        payload["access_token"] = result.access_token
    if result.error:
        payload["registration_error"] = result.error
    payload["registration_status"] = result.status
    return payload


def push_gpt_account_to_api(conf: dict[str, Any], result: GPTRegistrationResult) -> None:
    api_conf = conf.get("api") if isinstance(conf.get("api"), dict) else {}
    endpoint = str(api_conf.get("endpoint") or "").strip()
    token = str(api_conf.get("token") or "").strip()
    verify_ssl = as_bool(api_conf.get("verify_ssl", True), default=True)
    if not endpoint or not token:
        return
    response = requests.post(
        endpoint.rstrip("/"),
        json={"accounts": [_gpt_account_payload(result)]},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=20,
        verify=verify_ssl,
    )
    response.raise_for_status()


def run_single_gpt_registration(
    conf: dict[str, Any],
    *,
    client_factory: Callable[[], ChatGPTRegistrationClient] = ChatGPTRegistrationClient,
    push_account: Callable[[dict[str, Any], GPTRegistrationResult], None] = push_gpt_account_to_api,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> GPTRegistrationResult:
    gpt_conf = conf.get("gpt") if isinstance(conf.get("gpt"), dict) else {}
    attempts = _int_config(gpt_conf, "registration_attempts_per_account", _DEFAULT_REGISTRATION_ATTEMPTS)
    otp_timeout = _int_config(gpt_conf, "otp_timeout_s", _DEFAULT_OTP_TIMEOUT_S)
    login_otp_timeout = _int_config(gpt_conf, "login_otp_timeout_s", _DEFAULT_LOGIN_OTP_TIMEOUT_S)
    last_error = ""
    for attempt_index in range(attempts):
        if progress_callback:
            progress_callback(
                "registration_attempt_start",
                {"attempt": attempt_index + 1, "attempts": attempts},
            )
        email, mail_token = _create_temp_email_from_config(conf)
        if progress_callback:
            progress_callback(
                "email_created",
                {"attempt": attempt_index + 1, "attempts": attempts, "email": email},
            )
        password = _random_password()
        client = client_factory()
        try:
            phone_required, used_otp = client.run_register(
                email,
                password,
                mail_token,
                timeout=otp_timeout,
                progress_callback=progress_callback,
            )
            access_token = "" if phone_required else client.session_access_token()
            login_error = ""
            if not phone_required and not access_token:
                try:
                    access_token = login_gpt_credentials(
                        email=email,
                        password=password,
                        mail_token=mail_token,
                        ignore_otp=used_otp,
                        timeout=login_otp_timeout,
                        client_factory=client_factory,
                        progress_callback=progress_callback,
                    )
                except Exception as exc:
                    login_error = f"{type(exc).__name__}: {exc}"
                    if _looks_like_account_not_created_error(exc):
                        last_error = login_error
                        continue
            status = "available" if access_token else "login_required"
            if access_token:
                error = ""
            elif phone_required:
                error = "需要手机验证或未能获取 ChatGPT session access token"
            else:
                error = login_error or "未能获取 ChatGPT session access token"
            result = GPTRegistrationResult(
                email=email,
                password=password,
                mail_token=mail_token,
                status=status,
                access_token=access_token,
                error=error,
                phone_verification_required=phone_required,
            )
            push_account(conf, result)
            if progress_callback:
                progress_callback(
                    "account_saved",
                    {"attempt": attempt_index + 1, "email": email, "status": status},
                )
            return result
        except GPTAccountNotCreatedError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if progress_callback:
                progress_callback(
                    "registration_attempt_failed",
                    {"attempt": attempt_index + 1, "attempts": attempts, "error": last_error},
                )
            continue
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if progress_callback:
                progress_callback(
                    "registration_attempt_failed",
                    {"attempt": attempt_index + 1, "attempts": attempts, "error": last_error},
                )
            continue
    raise GPTAccountNotCreatedError(
        f"GPT 账号未创建，已重试 {attempts} 次，最后错误: {last_error}"
    )


def login_gpt_credentials(
    *,
    email: str,
    password: str,
    mail_token: str,
    ignore_otp: str | None = None,
    timeout: int = _DEFAULT_LOGIN_OTP_TIMEOUT_S,
    config_path: str | Path | None = None,
    client_factory: Callable[[], ChatGPTRegistrationClient] = ChatGPTRegistrationClient,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> str:
    if config_path:
        set_config_path(config_path)
    client = client_factory()
    return client.login_with_otp(
        email,
        password,
        mail_token,
        ignore_otp=ignore_otp,
        timeout=timeout,
        progress_callback=progress_callback,
    )


def _wait_while_paused(pause_check: Callable[[], bool] | None, stop_check: Callable[[], bool] | None) -> bool:
    while pause_check and pause_check():
        if stop_check and stop_check():
            return True
        time.sleep(0.5)
    return bool(stop_check and stop_check())


def run_gpt_batch(
    *,
    config_path: str | Path,
    count: int,
    pause_check: Callable[[], bool] | None = None,
    stop_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    registration_func: Callable[[dict[str, Any]], GPTRegistrationResult] | None = None,
) -> list[GPTRegistrationResult]:
    set_config_path(config_path)
    conf = load_json(Path(config_path))
    results: list[GPTRegistrationResult] = []
    if progress_callback:
        progress_callback("started", {"count": count})
    for index in range(count):
        if _wait_while_paused(pause_check, stop_check):
            break
        if progress_callback:
            progress_callback("round_start", {"round": index + 1})
        started = time.monotonic()
        try:
            if registration_func:
                result = registration_func(conf)
            else:
                def _progress(event: str, payload: dict[str, Any]) -> None:
                    if progress_callback:
                        progress_callback(event, {"round": index + 1, **payload})

                result = run_single_gpt_registration(conf, progress_callback=_progress)
            results.append(result)
            if progress_callback:
                progress_callback(
                    "round_done",
                    {
                        "round": index + 1,
                        "email": result.email,
                        "status": result.status,
                        "elapsed_s": round(time.monotonic() - started, 3),
                    },
                )
        except Exception as exc:
            if progress_callback:
                progress_callback(
                    "round_failed",
                    {
                        "round": index + 1,
                        "error": f"{type(exc).__name__}: {exc}",
                        "elapsed_s": round(time.monotonic() - started, 3),
                    },
                )
    if progress_callback:
        progress_callback("finished", {"token_count": len(results)})
    return results


def run_gpt_batch_parallel(
    *,
    config_path: str | Path,
    count: int,
    workers: int,
    pause_event: Any = None,
    stop_event: Any = None,
    progress_callback: Callable[[int, str, dict[str, Any]], None] | None = None,
) -> list[GPTRegistrationResult]:
    if workers <= 1:
        return run_gpt_batch(
            config_path=config_path,
            count=count,
            pause_check=(lambda: pause_event is not None and not pause_event.is_set()),
            stop_check=(lambda: stop_event is not None and stop_event.is_set()),
            progress_callback=(
                (lambda event, payload: progress_callback and progress_callback(0, event, payload))
                if progress_callback
                else None
            ),
        )

    set_config_path(config_path)
    conf = load_json(Path(config_path))
    results: list[GPTRegistrationResult] = []
    submitted = 0
    finished = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures: dict[Any, int] = {}
        while finished < count:
            while submitted < count and len(futures) < workers:
                if stop_event is not None and stop_event.is_set():
                    break
                while pause_event is not None and not pause_event.is_set():
                    if stop_event is not None and stop_event.is_set():
                        break
                    time.sleep(0.5)
                if stop_event is not None and stop_event.is_set():
                    break
                worker_id = submitted % workers
                round_no = submitted + 1
                if progress_callback:
                    progress_callback(worker_id, "round_start", {"round": round_no})
                def _make_progress(wid: int, round_value: int):
                    def _progress(event: str, payload: dict[str, Any]) -> None:
                        if progress_callback:
                            progress_callback(wid, event, {"round": round_value, **payload})

                    return _progress

                futures[
                    executor.submit(
                        run_single_gpt_registration,
                        dict(conf),
                        progress_callback=_make_progress(worker_id, round_no),
                    )
                ] = worker_id
                submitted += 1
            if not futures:
                break
            done, _pending = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                worker_id = futures.pop(future)
                finished += 1
                try:
                    result = future.result()
                    results.append(result)
                    if progress_callback:
                        progress_callback(
                            worker_id,
                            "round_done",
                            {"round": finished, "email": result.email, "status": result.status},
                        )
                except Exception as exc:
                    if progress_callback:
                        progress_callback(worker_id, "round_failed", {"round": finished, "error": str(exc)})
        if progress_callback:
            for worker_id in range(workers):
                progress_callback(worker_id, "finished", {"token_count": len(results)})
    return results


__all__ = [
    "ChatGPTRegistrationClient",
    "GPTAccountNotCreatedError",
    "GPTRegistrationResult",
    "run_gpt_batch",
    "run_gpt_batch_parallel",
    "login_gpt_credentials",
    "run_single_gpt_registration",
]
