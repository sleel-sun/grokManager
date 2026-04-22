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
    del email

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
        timeout=timeout,
    )

    if code:
        code = code.replace("-", "")

    return code


def wait_for_verification_code(
    session: requests.Session,
    worker_domain: str,
    cf_token: str,
    timeout: int = 120,
) -> str | None:
    old_ids = set()
    old = fetch_emails(session, worker_domain, cf_token)
    if old:
        old_ids = {item.get("id") for item in old if isinstance(item, dict) and "id" in item}
        for item in old:
            if not isinstance(item, dict):
                continue
            raw = str(item.get("raw") or "")
            code = extract_verification_code(raw)
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
                raw = str(item.get("raw") or "")
                code = extract_verification_code(raw)
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
