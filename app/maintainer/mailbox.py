from __future__ import annotations

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
    timeout: int = 120,
) -> str | None:
    old_ids = set()
    old = fetch_emails(session, worker_domain, cf_token)
    if old:
        old_ids = {
            item.get("id")
            for item in old
            if isinstance(item, dict)
            and item.get("id") is not None
            and mail_matches_target_email(item, target_email)
        }
        for item in old:
            if not isinstance(item, dict):
                continue
            code = extract_verification_code_from_mail(item, target_email)
            if code:
                return code

    start = time.time()
    while time.time() - start < timeout:
        emails = fetch_emails(session, worker_domain, cf_token)
        if emails:
            for item in emails:
                if not isinstance(item, dict):
                    continue
                if item.get("id") in old_ids:
                    continue
                code = extract_verification_code_from_mail(item, target_email)
                if code:
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
) -> list[dict[str, Any]]:
    try:
        res = session.get(
            f"https://{worker_domain}/api/mails",
            params={"limit": 10, "offset": 0},
            headers={"Authorization": f"Bearer {cf_token}"},
            timeout=30,
        )
        if res.status_code == 200:
            rows = res.json().get("results", [])
            return rows if isinstance(rows, list) else []
    except Exception:
        pass
    return []


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

    raw = str(mail.get("raw") or "")
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
    for key in ("raw", "text", "html", "body", "subject"):
        value = mail.get(key)
        if isinstance(value, str) and value.strip():
            content_parts.append(value)

    return extract_verification_code("\n".join(content_parts))


def extract_verification_code(content: str) -> str | None:
    patterns = [
        r"([A-Z0-9]{3}-[A-Z0-9]{3})",
        r"验证码[:：\s]*([A-Z0-9]{6,8})",
        r"verification code[:：\s]*([A-Z0-9]{6,8})",
        r"\b([A-Z0-9]{6,8})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return None
