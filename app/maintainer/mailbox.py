from __future__ import annotations

from email import policy
from email.parser import Parser
import html
import logging
import random
import re
import string
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .settings import as_bool, load_config, pick_conf


_temp_email_cache: dict[str, str] = {}
_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_RECIPIENT_FIELD_NAMES = {
    "address",
    "delivered_to",
    "email",
    "envelope_to",
    "mail_to",
    "original_recipient",
    "rcpt_to",
    "recipient",
    "recipients",
    "to",
}
_RAW_RECIPIENT_HEADERS = {
    "delivered-to",
    "envelope-to",
    "original-recipient",
    "to",
    "x-forwarded-to",
}
_CODE_LABEL_RE = re.compile(
    r"(?:"
    r"(?:chatgpt|openai)\s+(?:login\s+|sign[-\s]?in\s+)?code|"
    r"login\s+code|"
    r"sign[-\s]?in\s+code|"
    r"authentication\s+code|"
    r"auth\s+code|"
    r"email\s+code|"
    r"code\s+is|"
    r"verification\s+code|"
    r"security\s+code|"
    r"one[-\s]?time(?:\s+security)?\s+code|"
    r"confirmation\s+code|"
    r"登录验证码|"
    r"登入验证码|"
    r"登录代码|"
    r"登入代码|"
    r"邮箱验证码|"
    r"代码为|"
    r"验证码|"
    r"安全代码|"
    r"一次性(?:安全)?代码"
    r")",
    re.IGNORECASE,
)
_CODE_CONTEXT_RE = re.compile(
    r"(?:chatgpt|openai|verification|verify|security|one[-\s]?time|otp|login|sign[-\s]?in|验证码|代码|登录|登入|安全)",
    re.IGNORECASE,
)
_CODE_CANDIDATE_RE = re.compile(
    r"(?<![A-Z0-9])([A-Z0-9](?:[\s\-‐‑‒–—―ー]*[A-Z0-9]){5})(?![A-Z0-9])",
    re.IGNORECASE,
)
_NUMERIC_CODE_RE = re.compile(r"(?<![#&A-Z0-9])(\d{6})(?![A-Z0-9])", re.IGNORECASE)
_HIGHLIGHTED_NUMERIC_CODE_RE = re.compile(
    r"background-color:\s*#F3F3F3[^>]*>[\s\S]*?(\d{6})[\s\S]*?</p>",
    re.IGNORECASE,
)
_CODE_SEPARATOR_RE = re.compile(r"[\s\-‐‑‒–—―ー]+")


def get_email_and_token() -> tuple[str | None, str | None]:
    conf = load_config()

    worker_domain = str(pick_conf(conf, "email", "worker_domain", default="") or "")
    admin_password = str(pick_conf(conf, "email", "admin_password", default="") or "")
    verify_ssl = as_bool(
        pick_conf(conf, "email", "verify_ssl", default=True),
        default=True,
    )
    email_domains = pick_conf(conf, "email", "email_domains", default=None)
    if not isinstance(email_domains, list):
        old_domain = str(
            pick_conf(conf, "email", "email_domain", default="tuxixilax.cfd")
            or "tuxixilax.cfd"
        )
        email_domains = [old_domain]
    else:
        email_domains = [str(item).strip() for item in email_domains if str(item).strip()]

    if not worker_domain or not admin_password:
        print("[Error] 配置缺少 email.worker_domain 或 email.admin_password")
        return None, None

    session = create_session(verify_ssl=verify_ssl)
    email, token = create_temp_email(
        session=session,
        worker_domain=worker_domain,
        email_domains=email_domains,
        admin_password=admin_password,
        logger=logging.getLogger("grok_maintainer"),
    )

    if email and token:
        _temp_email_cache[email] = token
        return email, token

    return None, None


def get_oai_code(dev_token: str, email: str, timeout: int = 120) -> str | None:
    conf = load_config()
    worker_domain = str(pick_conf(conf, "email", "worker_domain", default="") or "")
    admin_password = str(pick_conf(conf, "email", "admin_password", default="") or "")
    verify_ssl = as_bool(
        pick_conf(conf, "email", "verify_ssl", default=True),
        default=True,
    )

    if not worker_domain:
        print("[Error] 配置缺少 email.worker_domain")
        return None

    session = create_session(verify_ssl=verify_ssl)
    code = wait_for_verification_code(
        session=session,
        worker_domain=worker_domain,
        cf_token=dev_token,
        target_email=email,
        admin_password=admin_password,
        timeout=timeout,
    )

    if code:
        code = code.replace("-", "")

    return code


def wait_for_verification_code(
    session: requests.Session,
    worker_domain: str,
    cf_token: str,
    target_email: str = "",
    admin_password: str = "",
    timeout: int = 120,
    ignore_codes: set[str] | None = None,
    ignore_ids: set[Any] | None = None,
) -> str | None:
    ignored = {code.replace("-", "").upper() for code in (ignore_codes or set()) if code}
    ignored_ids = set(ignore_ids or set())

    def should_ignore_code(item: dict[str, Any], code: str) -> bool:
        normalised = code.replace("-", "").upper()
        if normalised not in ignored:
            return False
        mail_id = item.get("id")
        # If the worker provides a new message id, treat it as a fresh OTP even
        # when OpenAI reused the same six-digit value from an earlier email.
        return mail_id is None or mail_id in ignored_ids

    old_ids = set()
    old = fetch_emails(session, worker_domain, cf_token, target_email, admin_password)
    if old:
        old_ids = {
            item.get("id")
            for item in old
            if isinstance(item, dict)
            and item.get("id") is not None
            and mail_matches_target_email(item, target_email)
        }
        old_ids.update(ignored_ids)
        for item in old:
            if not isinstance(item, dict):
                continue
            if item.get("id") in ignored_ids:
                continue
            code = extract_verification_code_from_mail(item, target_email)
            if code and not should_ignore_code(item, code):
                return code

    start = time.time()
    while time.time() - start < timeout:
        emails = fetch_emails(session, worker_domain, cf_token, target_email, admin_password)
        if emails:
            for item in emails:
                if not isinstance(item, dict):
                    continue
                if item.get("id") in old_ids:
                    continue
                code = extract_verification_code_from_mail(item, target_email)
                if code and not should_ignore_code(item, code):
                    return code
        time.sleep(3)
    return None


def create_session(proxy: str = "", verify_ssl: bool = True) -> requests.Session:
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    session.verify = verify_ssl
    return session


def create_temp_email(
    session: requests.Session,
    worker_domain: str,
    email_domains: list[str],
    admin_password: str,
    logger: logging.Logger,
) -> tuple[str | None, str | None]:
    name_len = random.randint(10, 14)
    name_chars = list(random.choices(string.ascii_lowercase, k=name_len))
    for _ in range(random.choice([1, 2])):
        pos = random.randint(2, len(name_chars) - 1)
        name_chars.insert(pos, random.choice(string.digits))
    name = "".join(name_chars)

    chosen_domain = random.choice(email_domains) if email_domains else "tuxixilax.cfd"

    try:
        res = session.post(
            f"https://{worker_domain}/admin/new_address",
            json={"enablePrefix": True, "name": name, "domain": chosen_domain},
            headers={"x-admin-auth": admin_password, "Content-Type": "application/json"},
            timeout=10,
        )
        if res.status_code == 200:
            data = res.json()
            email = data.get("address")
            token = data.get("jwt")
            if email:
                logger.info("创建临时邮箱成功: %s (domain=%s)", email, chosen_domain)
                return str(email), str(token or "")
        logger.warning("创建临时邮箱失败: HTTP %s", res.status_code)
    except Exception as exc:
        logger.warning("创建临时邮箱异常: %s", exc)
    return None, None


def fetch_emails(
    session: requests.Session,
    worker_domain: str,
    cf_token: str,
    target_email: str = "",
    admin_password: str = "",
) -> list[dict[str, Any]]:
    if admin_password and target_email:
        ok, rows = fetch_emails_request(
            session=session,
            url=f"https://{worker_domain}/admin/mails",
            params={
                "limit": 20,
                "offset": 0,
                "address": normalise_email(target_email),
            },
            headers={"x-admin-auth": admin_password},
        )
        if ok and rows:
            return rows

    ok, rows = fetch_emails_request(
        session=session,
        url=f"https://{worker_domain}/api/mails",
        params={"limit": 10, "offset": 0},
        headers={"Authorization": f"Bearer {cf_token}"},
    )
    return rows if ok else []


def fetch_emails_request(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
    headers: dict[str, str],
) -> tuple[bool, list[dict[str, Any]]]:
    try:
        res = session.get(
            url,
            params=params,
            headers={**headers, "Content-Type": "application/json"},
            timeout=30,
        )
        if res.status_code == 200:
            return True, normalise_mail_rows(res.json())
    except Exception:
        pass
    return False, []


def normalise_mail_rows(payload: Any) -> list[dict[str, Any]]:
    rows: Any = payload
    if isinstance(payload, dict):
        for key in ("results", "data", "mails", "items", "messages", "rows", "list", "emails"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break
            if isinstance(candidate, dict):
                nested = normalise_mail_rows(candidate)
                if nested:
                    return nested
        else:
            mail_keys = {
                "body",
                "headers",
                "html",
                "id",
                "raw",
                "source",
                "subject",
                "text",
                "to",
            }
            rows = [payload] if any(key in payload for key in mail_keys) else []

    if not isinstance(rows, list):
        return []

    return [item for item in rows if isinstance(item, dict)]


def normalise_email(email: str) -> str:
    return str(email or "").strip().lower()


def emails_from_value(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {match.group(0).lower() for match in _EMAIL_RE.finditer(value)}
    if isinstance(value, dict):
        emails: set[str] = set()
        for nested in value.values():
            emails.update(emails_from_value(nested))
        return emails
    if isinstance(value, (list, tuple, set)):
        emails = set()
        for nested in value:
            emails.update(emails_from_value(nested))
        return emails
    return set()


def recipient_emails_from_mail(mail: dict[str, Any]) -> set[str]:
    recipients: set[str] = set()
    for key, value in mail.items():
        field_name = str(key).lower().replace("-", "_")
        if field_name in _RECIPIENT_FIELD_NAMES:
            recipients.update(emails_from_value(value))

    headers = mail.get("headers")
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).strip().lower() in _RAW_RECIPIENT_HEADERS:
                recipients.update(emails_from_value(value))

    raw = raw_mail_source(mail)
    for line in raw.splitlines():
        header, sep, value = line.partition(":")
        if sep and header.strip().lower() in _RAW_RECIPIENT_HEADERS:
            recipients.update(emails_from_value(value))

    if recipients:
        return recipients

    return emails_from_value(raw)


def mail_matches_target_email(mail: dict[str, Any], target_email: str) -> bool:
    target = normalise_email(target_email)
    if not target:
        return True

    recipients = recipient_emails_from_mail(mail)
    if not recipients:
        return True

    return target in recipients


def extract_verification_code_from_mail(
    mail: dict[str, Any],
    target_email: str = "",
) -> str | None:
    if not mail_matches_target_email(mail, target_email):
        return None

    content_parts = []
    for key in (
        "text",
        "text_content",
        "content",
        "html",
        "html_content",
        "html_body",
        "body",
        "body_html",
        "subject",
    ):
        value = mail.get(key)
        if isinstance(value, str) and value.strip():
            content_parts.append(value)
    content_parts.extend(parsed_rfc822_text_parts(raw_mail_source(mail)))

    return extract_verification_code("\n".join(content_parts))


def raw_mail_source(mail: dict[str, Any]) -> str:
    for key in ("raw", "source"):
        value = mail.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def parsed_rfc822_text_parts(raw: str) -> list[str]:
    if not raw.strip():
        return []

    try:
        message = Parser(policy=policy.default).parsestr(raw)
    except Exception:
        return [raw]

    parts: list[str] = []
    subject = message.get("subject")
    if subject:
        parts.append(str(subject))

    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            else:
                content = str(payload or "")
        if content:
            parts.append(str(content))

    if not parts:
        parts.append(raw)

    return parts


def extract_verification_code(content: str) -> str | None:
    code = highlighted_numeric_verification_candidate(str(content or ""))
    if code:
        return code

    searchable = normalise_verification_content(content)

    for label in _CODE_LABEL_RE.finditer(searchable):
        after = searchable[label.end() : label.end() + 400]
        code = first_verification_candidate(after)
        if code:
            return code

        before = searchable[max(0, label.start() - 120) : label.start()]
        code = first_verification_candidate(before, reverse=True)
        if code:
            return code

    if _CODE_CONTEXT_RE.search(searchable):
        return first_numeric_verification_candidate(searchable)

    return None


def normalise_verification_content(content: str) -> str:
    text = html.unescape(str(content or ""))
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)

    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if stripped.startswith("--"):
            continue
        lines.append(stripped)

    return "\n".join(lines)


def first_verification_candidate(text: str, reverse: bool = False) -> str | None:
    candidates = list(_CODE_CANDIDATE_RE.finditer(text))
    if reverse:
        candidates.reverse()

    for match in candidates:
        code = normalise_verification_candidate(match.group(1))
        if code:
            return code

    return None


def highlighted_numeric_verification_candidate(text: str) -> str | None:
    for match in _HIGHLIGHTED_NUMERIC_CODE_RE.finditer(text):
        code = normalise_verification_candidate(match.group(1))
        if code:
            return code
    return None


def first_numeric_verification_candidate(text: str) -> str | None:
    for match in _NUMERIC_CODE_RE.finditer(text):
        code = normalise_verification_candidate(match.group(1))
        if code:
            return code
    return None


def normalise_verification_candidate(candidate: str) -> str | None:
    raw = str(candidate or "").strip()
    value = _CODE_SEPARATOR_RE.sub("", raw).upper()
    if len(value) != 6 or not value.isalnum():
        return None

    has_digit = any(ch.isdigit() for ch in value)
    alpha_chars = [ch for ch in raw if ch.isalpha()]
    alpha_is_upper = bool(alpha_chars) and all(ch.upper() == ch for ch in alpha_chars)
    has_visible_separator = bool(_CODE_SEPARATOR_RE.search(raw))
    if not (has_digit or alpha_is_upper or (has_visible_separator and alpha_is_upper)):
        return None

    if has_visible_separator:
        return f"{value[:3]}-{value[3:]}"
    return value
