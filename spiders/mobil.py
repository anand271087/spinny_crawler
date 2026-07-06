"""Mobil spider — flat_list pattern, implemented via Coveo Search API capture-and-replay.

Plan reference: §12.19.  Discovery (2026-05-18):
- Page is a Coveo Search widget.
- POST https://www.mobil.co.in/coveo/rest/search/v2?sitecoreItemUri=...&siteName=Mobil_IN_PROD
- Body carries a heavy `aq` filter scoping to India + Mobil IN. Without it, Coveo returns
  the global Esso/Mobil index (~10K results across all regions).
- Page default `numberOfResults: 10`. We replay with 1000 and paginate.

Strategy:
1. Playwright once to capture the live request URL/headers/body (resilient to Coveo tweaks).
2. httpx replay with numberOfResults=1000, firstResult iterating until totalCount reached.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qsl, urlencode

from playwright.sync_api import sync_playwright

from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.mobil")

USER_AGENT = "SpinnyOEMCrawler/1.0 (contact@spinny.com)"
# Mobil sits behind Akamai (2026-07): it returns "Access Denied" to non-browser
# UAs and headless fingerprints. A real Chrome-131 UA + full fingerprint header
# set + anti-detection launch args gets the page to render so the Coveo search
# XHR fires. Same class of WAF bypass as Schaeffler. (Verified 2026-07-07.)
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
WAF_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
PAGE_URL = "https://www.mobil.co.in/en-in/our-products"
COVEO_PATH = "/coveo/rest/search/v2"
PAGE_SIZE = 100  # Coveo caps numberOfResults at 100; paginate via firstResult.


def _rewrite_pagination(body: str, first_result: int, page_size: int) -> str:
    pairs = parse_qsl(body, keep_blank_values=True)
    out = []
    for k, v in pairs:
        if k == "numberOfResults":
            out.append((k, str(page_size)))
        elif k == "firstResult":
            out.append((k, str(first_result)))
        else:
            out.append((k, v))
    return urlencode(out)


class Spider(BaseSpider):
    """Capture the Mobil page's own Coveo request, then replay inside the same browser
    context to preserve Bearer token + cookies."""

    def crawl(self) -> list[Row]:
        seen: set[str] = set()
        captured: dict[str, Any] = {}

        def on_request(req):
            if COVEO_PATH in req.url and req.method == "POST" and not captured:
                captured["url"] = req.url
                captured["body"] = req.post_data or ""
                captured["headers"] = {k: v for k, v in req.headers.items() if k.lower() in {
                    "authorization", "content-type", "accept", "accept-language", "origin", "referer",
                }}

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            ctx = browser.new_context(
                user_agent=BROWSER_UA, locale="en-US",
                viewport={"width": 1920, "height": 1080},
                extra_http_headers=WAF_HEADERS,
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = ctx.new_page()
            page.on("request", on_request)
            # NOTE: do NOT use wait_until="networkidle" — Mobil keeps background
            # connections open (analytics/long-poll) so networkidle never fires and
            # goto times out at 60s before the Coveo XHR is even read. Load on
            # domcontentloaded, then poll for the captured Coveo POST (fires shortly
            # after load as the search widget initializes).
            page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60_000)
            for _ in range(25):
                if captured:
                    break
                page.wait_for_timeout(1_000)

            if not captured:
                browser.close()
                raise RuntimeError("did not capture Coveo POST — page structure may have changed")

            log.info("captured coveo request: %s (%d-byte body)", captured["url"], len(captured["body"]))

            first_result = 0
            total: int | None = None
            while True:
                body = _rewrite_pagination(captured["body"], first_result, PAGE_SIZE)
                # Replay inside the same browser context — auth/cookies intact.
                resp = ctx.request.post(
                    captured["url"],
                    data=body,
                    headers=captured["headers"],
                )
                if resp.status != 200:
                    raise RuntimeError(f"coveo replay HTTP {resp.status}: {resp.text()[:300]}")
                data = resp.json()
                if total is None:
                    total = data.get("totalCount", 0)
                    log.info("coveo totalCount=%d (India + Mobil IN scope)", total)
                results = data.get("results", [])
                if not results:
                    break
                for r in results:
                    title = (r.get("title") or "").strip()
                    if title and title not in seen:
                        seen.add(title)
                first_result += PAGE_SIZE
                if first_result >= total:
                    break

            browser.close()

        log.info("mobil: %d unique products extracted", len(seen))
        return [Row(item_name=t) for t in sorted(seen)]
