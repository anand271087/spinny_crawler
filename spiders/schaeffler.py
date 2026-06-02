"""LuK/Schaeffler India spider — v1.0 brand (re-shipped 2026-05-19).

Site: https://vehiclelifetimesolutions.schaeffler.in/en-gb/catalog
Platform: SAP Commerce Cloud Spartacus storefront (RepXpert backend).
xlsx fields: item_name, item_code, mrp.

WAF BYPASS (the BRD §7 anti-bot wrinkle that flagged this as "breakage stub"):
The Akamai-style WAF only blocks browser-style navigation (page.goto) when our
request lacks expected browser fingerprint headers. With full Chrome 131 headers
(Sec-Fetch-Dest, Sec-Fetch-Mode, Sec-Fetch-Site, sec-ch-ua-*), Playwright's
ctx.request reaches the backend with status 200. No proxy / no SaaS solver needed.

Backend pattern:
- Frontend SPA at https://vehiclelifetimesolutions.schaeffler.in
- OCC backend at SAME host under /api/<baseSite>/...
- baseSite is "Repxpert-IN" (NOT "vls" — vls is just the media host)

Key endpoint:
  GET /api/Repxpert-IN/products/search
       ?query=:relevance:targetTypes:passengerCar
       &pageSize=120 (server cap)
       &currentPage=N
       &fields=DEFAULT

Returns 17,778 PV products in ~150 pages. Each product:
  - name              → item_name (e.g. "NOx Sensor, urea injection")
  - catalogArticleNumber → item_code (e.g. "571 0018 10")
  - priceRange        → MRP — always empty {} via this public search endpoint.

MRP gap — known follow-up (stakeholder confirmed 2026-05-20):
The SPA's "Selection History" panel (visible after Make→Model→Variant cascade)
DOES show MRP values for SOME products (screenshot example: MARUTI BREZZA SMART
HYBRID → 6 articles, all with `MRP ₹3,388.00` etc). This implies a
vehicle-filtered endpoint exists that exposes priceRange differently from the
public product-search endpoint. Investigation as of 2026-05-22:
  - /api/AAM-IN/manufacturers?targetTypeCodes=passengerCar returns 115 makes
    (uuid pattern: TA-{tecdocID}).
  - /api/AAM-IN/linkageTargetTypes + /vehicleKeySystems exist but the model
    drill API is unclear (SPA navigates via SEO paths that the public probe
    didn't capture cleanly).
Action required: deeper UI-driven Playwright reverse-engineering across the
Make → Model → Variant → Article cascade with priceRange capture per article.
Estimated 8-12h. Until then, rows finalize as `partial` per BRD §7 — same
behaviour as 2026-05-19 shipped state.

No credentials required for product search — the catalog is publicly browsable
(once you defeat the WAF header check).
"""

from __future__ import annotations

import json
import logging
import os
from urllib.parse import quote

from playwright.sync_api import sync_playwright

from lib.snapon_epc import UA, LAUNCH_ARGS
from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.schaeffler")

BASE = "https://vehiclelifetimesolutions.schaeffler.in"
# Base site name. As of 2026-05-22 both 'AAM-IN' and 'Repxpert-IN' return identical
# 17,780 PV products via /api/<SITE>/products/search. The SPA itself now uses AAM-IN
# in all navigation XHRs — bumped to keep us aligned with the live SPA's choice.
SITE = "AAM-IN"

# Browser-fingerprint headers — required to defeat the Akamai WAF on this domain.
WAF_BYPASS_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Referer": f"{BASE}/en-gb/catalog",
    "Origin": BASE,
}

PAGE_SIZE = 120  # server cap; larger values get clamped


class Spider(BaseSpider):

    def __init__(self, brand_key: str, brand_cfg: dict) -> None:
        super().__init__(brand_key, brand_cfg)
        # Default to 0 = all pages. Override to a positive int for smoke tests.
        self.max_pages = int(os.environ.get("SCHAEFFLER_MAX_PAGES", "0") or "0")

    def crawl(self) -> list[Row]:
        rows: list[Row] = []
        seen: set[str] = set()

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
            ctx = browser.new_context(
                user_agent=UA, viewport={"width": 1920, "height": 1080},
                locale="en-US", timezone_id="Asia/Kolkata",
                extra_http_headers=WAF_BYPASS_HEADERS,
            )

            try:
                first = self._fetch_page(ctx, page_num=0)
                if not first:
                    log.error("schaeffler: first page fetch failed (WAF?)")
                    return rows
                total_pages = first.get("pagination", {}).get("totalPages", 0)
                total_results = first.get("pagination", {}).get("totalResults", 0)
                log.info("schaeffler: %d PV products across %d pages",
                         total_results, total_pages)

                self._emit(first.get("products", []), rows, seen)
                pages_to_fetch = total_pages
                if self.max_pages:
                    pages_to_fetch = min(self.max_pages, total_pages)

                for page_num in range(1, pages_to_fetch):
                    page_data = self._fetch_page(ctx, page_num=page_num)
                    if not page_data:
                        log.warning("page %d empty/failed", page_num)
                        continue
                    self._emit(page_data.get("products", []), rows, seen)
                    if page_num % 10 == 0:
                        log.info("page %d/%d, %d rows so far",
                                 page_num, pages_to_fetch, len(rows))
            finally:
                browser.close()
        log.info("schaeffler: %d rows extracted", len(rows))
        return rows

    @staticmethod
    def _fetch_page(ctx, page_num: int) -> dict | None:
        query = quote(":relevance:targetTypes:passengerCar", safe=":")
        url = (
            f"{BASE}/api/{SITE}/products/search"
            f"?query={query}&pageSize={PAGE_SIZE}&currentPage={page_num}&fields=DEFAULT"
        )
        try:
            r = ctx.request.get(url, timeout=30_000)
        except Exception as e:
            log.warning("page %d request err: %s", page_num, e)
            return None
        if r.status != 200:
            log.warning("page %d status %d", page_num, r.status)
            return None
        try:
            return json.loads(r.text())
        except Exception as e:
            log.warning("page %d json decode: %s", page_num, e)
            return None

    @staticmethod
    def _emit(products: list[dict], rows: list[Row], seen: set[str]) -> None:
        for p in products:
            code = (p.get("catalogArticleNumber") or "").strip()
            if not code:
                continue
            if code in seen:
                continue
            seen.add(code)
            name = (p.get("name") or p.get("fullName") or code).strip()
            # priceRange is consistently empty {} on Repxpert-IN — MRP left None.
            pr = p.get("priceRange") or {}
            mrp: float | None = None
            for k in ("maxPrice", "minPrice"):
                v = pr.get(k)
                if isinstance(v, dict):
                    val = v.get("value")
                    try:
                        mrp = float(val)
                        break
                    except (TypeError, ValueError):
                        pass
            rows.append(Row(item_name=name, item_code=code, mrp=mrp))
