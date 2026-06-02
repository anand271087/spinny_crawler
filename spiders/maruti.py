"""MARUTI spider — JSON-API pagination (no auth required).

Plan ref: v2.0 OEM EPC scope. URL: https://www.marutisuzuki.com/genuine-parts
xlsx steps (v2.0 Maruti sheet):
  1. Open the site
  2. Scroll to Genuine Car Parts Categories → View More
  3. Click Clear all filters
  4. Extract all products
xlsx fields: item_name, item_code, mrp, car_model

Site reality (verified 2026-05-18):
- Public, no auth.
- API: POST https://www.marutisuzuki.com/api/sitecore/MSGP/GetFilter
- Body: {"category":[],"model":[],"sortingFilter":"By Relevence","query":"","pageNumber":N}
- Empty arrays = "Clear all filters" — returns 80 product cards per page.
- Total products: ~29,313 (from `length` field in response).
- Pagination: pageNumber 1..N. Past last page server still returns 80 cards (cycles); we
  dedup by item_code and stop after consecutive empty-new pages.
- Response is double-encoded JSON: outer JSON string → inner object with `PView` HTML
  containing `.sliderBox` cards.

Per-card fields:
- `.heart a[data-id]` → item_code (e.g., "35121M66L00")
- `.heart a[data-price]` → MRP (₹)
- `.heart a[data-cat]` + `[data-subcat]` → category info
- `img[title]` inside `.slideImg` → item_name (e.g., "UNIT HEAD LAMP (RIGHT)")

compatible_car_model is NOT available from the unfiltered call — would require
per-model iteration (40+ models × N pages). Deferred. Rows finalise as `partial`.
"""

from __future__ import annotations

import json
import logging
import re

import httpx
from parsel import Selector

from lib.normalize import clean_mrp
from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.maruti")

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
API_URL = "https://www.marutisuzuki.com/api/sitecore/MSGP/GetFilter"
PAGE_SIZE_HINT = 40  # 40 unique products per page (80 cards × 2 anchors each)


class Spider(BaseSpider):

    def crawl(self) -> list[Row]:
        rows: list[Row] = []
        seen: set[str] = set()
        with httpx.Client(
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
            follow_redirects=True, timeout=60,
        ) as client:
            # Probe page 1 to read `length` (total product count)
            page1 = self._fetch_page(client, 1)
            total = page1.get("length", 0)
            log.info("maruti: API reports %d total products", total)

            page_num = 1
            consecutive_empty = 0
            while True:
                data = self._fetch_page(client, page_num) if page_num > 1 else page1
                cards = self._parse_pview(data.get("PView", ""))
                new_count = 0
                for c in cards:
                    if c["item_code"] in seen:
                        continue
                    seen.add(c["item_code"])
                    new_count += 1
                    rows.append(Row(
                        item_name=c["item_name"],
                        item_code=c["item_code"],
                        mrp=clean_mrp(c["mrp_raw"]),
                    ))
                if page_num % 25 == 0 or page_num == 1:
                    log.info("maruti: page %d cards=%d new=%d total_unique=%d",
                             page_num, len(cards), new_count, len(rows))

                if new_count == 0:
                    consecutive_empty += 1
                    if consecutive_empty >= 2:
                        log.info("maruti: pagination exhausted at page %d", page_num)
                        break
                else:
                    consecutive_empty = 0

                if total > 0 and len(rows) >= total:
                    log.info("maruti: reached reported total %d", total)
                    break

                page_num += 1
                if page_num > 1000:
                    log.warning("maruti: safety stop at page 1000")
                    break
        return rows

    @staticmethod
    def _fetch_page(client: httpx.Client, page_num: int) -> dict:
        payload = {"category": [], "model": [], "sortingFilter": "By Relevence",
                   "query": "", "pageNumber": page_num}
        r = client.post(API_URL, json=payload)
        r.raise_for_status()
        # Double-JSON: outer string, inner object
        return json.loads(json.loads(r.text))

    @staticmethod
    def _parse_pview(pview_html: str) -> list[dict]:
        sel = Selector(pview_html)
        out: list[dict] = []
        for box in sel.css(".sliderBox"):
            heart = box.css(".heart a")
            item_code = (heart.attrib.get("data-id") or "").strip() if heart else ""
            price = (heart.attrib.get("data-price") or "").strip() if heart else ""
            # item_name from img title in .slideImg
            name = (box.css(".slideImg img::attr(title)").get()
                    or box.css(".slideContent a::attr(title)").get()
                    or "").strip()
            if not item_code or not name:
                continue
            out.append({"item_code": item_code, "item_name": name, "mrp_raw": price})
        return out
