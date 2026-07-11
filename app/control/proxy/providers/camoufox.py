"""Camoufox-sidecar managed clearance provider."""

import asyncio
import json
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

from app.platform.config.snapshot import get_config
from app.platform.logging.logger import logger
from ..models import ClearanceBundle, ClearanceMode


class CamoufoxClearanceProvider:
    async def refresh_bundle(self, *, affinity_key: str, proxy_url: str, target_url: str = "https://grok.com") -> ClearanceBundle | None:
        cfg = get_config()
        if ClearanceMode.parse(cfg.get_str("proxy.clearance.mode", "none")) != ClearanceMode.CAMOUFOX:
            return None
        base = cfg.get_str("proxy.clearance.camoufox_url", "http://camoufox:8193").rstrip("/")
        timeout = cfg.get_int("proxy.clearance.timeout_sec", 60)
        req = urllib_request.Request(
            f"{base}/solve",
            data=json.dumps({"url": target_url, "proxy": proxy_url, "timeout_ms": timeout * 1000}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            def _post() -> dict:
                with urllib_request.urlopen(req, timeout=timeout + 30) as response:
                    return json.loads(response.read().decode())
            result = await asyncio.to_thread(_post)
        except (HTTPError, URLError, OSError, ValueError) as exc:
            logger.warning("camoufox clearance refresh failed: error={}", exc)
            return None
        cookies = result.get("cookies") or []
        cookie_text = "; ".join(
            f"{item.get('name')}={item.get('value')}"
            for item in cookies
            if item.get("name") and item.get("value")
        )
        if not cookie_text:
            logger.warning("camoufox returned no cookies: target={}", target_url)
            return None
        host = (urlparse(target_url).hostname or "grok.com").lower()
        return ClearanceBundle(
            bundle_id=f"camoufox:{affinity_key}@{host}",
            cf_cookies=cookie_text,
            user_agent=str(result.get("user_agent") or ""),
            affinity_key=affinity_key,
            clearance_host=host,
        )


__all__ = ["CamoufoxClearanceProvider"]
