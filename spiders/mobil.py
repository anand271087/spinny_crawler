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
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=USER_AGENT)
            page = ctx.new_page()
            page.on("request", on_request)
            page.goto(PAGE_URL, wait_until="networkidle", timeout=60_000)
            page.wait_for_timeout(6_000)

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
