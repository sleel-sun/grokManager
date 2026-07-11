"""HTTP sidecar that obtains Cloudflare cookies with Camoufox."""

import asyncio

from aiohttp import web
from camoufox.async_api import AsyncCamoufox


async def solve(request: web.Request) -> web.Response:
    body = await request.json()
    target = str(body.get("url") or "https://grok.com")
    proxy_url = str(body.get("proxy") or "").strip()
    timeout_ms = int(body.get("timeout_ms") or 60_000)
    options = {"headless": True}
    if proxy_url:
        options["proxy"] = {"server": proxy_url}
    async with AsyncCamoufox(**options) as browser:
        page = await browser.new_page()
        await page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        cookies = []
        while asyncio.get_running_loop().time() < deadline:
            cookies = await page.context.cookies()
            if any(cookie.get("name") == "cf_clearance" for cookie in cookies):
                break
            await asyncio.sleep(1)
        user_agent = await page.evaluate("navigator.userAgent")
        return web.json_response({"cookies": cookies, "user_agent": user_agent})


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


app = web.Application()
app.router.add_get("/health", health)
app.router.add_post("/solve", solve)
web.run_app(app, host="0.0.0.0", port=8193)
