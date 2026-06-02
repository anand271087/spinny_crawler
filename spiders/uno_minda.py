"""UNO MINDA spider — search-results pattern.

Plan ref §12.2. URL: https://unomindakart.com/  (xlsx flow)
Effective entry: https://unomindakart.com/search?segment=car&page=N

Site reality (verified 2026-05-18):
- /search?segment=car returns 717 products across 60 pages of ~12 products each.
- Each card has h6.card-title with the product name (often includes the SKU embedded).
- Card text shows selling price + "MRP :₹<NUMBER>" + discount %.
- The xlsx-mentioned "products by segment → car → tabs" flow lands on this same search.

Strategy:
1. Hit /search?segment=car&page=N via Playwright (page is React-rendered).
2. Parse all h6.card-title elements + their parent card-body containing ₹ prices.
3. Extract SKU from item_name (UNO MINDA embeds the SKU like "Uno Minda H27-7009 ...").
4. Paginate from 1 to last page (extract last from .pagination links on page 1).
"""

from __future__ import annotations

import logging
import re

from playwright.sync_api import sync_playwright

from lib.normalize import clean_mrp
from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.uno_minda")

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
BASE = "https://unomindakart.com"
SEARCH_TPL = f"{BASE}/search?segment=car&page={{page}}"

# SKU pattern in UNO MINDA product titles (after "Uno Minda " prefix)
SKU_RE = re.compile(r"\b(?:Uno\s*Minda\s+)?([A-Z][A-Z0-9]+(?:-[A-Z0-9]+)+)\b")
MRP_RE = re.compile(r"MRP\s*:?\s*₹\s*([\d,]+(?:\.\d{1,2})?)")
SELLING_RE = re.compile(r"₹\s*([\d,]+(?:\.\d{1,2})?)")


class Spider(BaseSpider):

    def crawl(self) -> list[Row]:
        rows: list[Row] = []
        seen: set[str] = set()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=USER_AGENT)
            page = ctx.new_page()
            # Page 1 — also find max page from pagination
            page.goto(SEARCH_TPL.format(page=1), wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3500)
            page_urls = page.evaluate(
                "Array.from(document.querySelectorAll('.pagination a')).map(a => a.href)"
            )
            page_numbers = sorted({
                int(m.group(1)) for u in page_urls
                if (m := re.search(r"page=(\d+)", u))
            })
            max_page = max(page_numbers) if page_numbers else 1
            log.info("uno_minda: max_page=%d", max_page)

            # Walk pages
            for pn in range(1, max_page + 1):
                if pn > 1:
                    page.goto(SEARCH_TPL.format(page=pn), wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2500)
                added = self._extract_page(page, rows, seen)
                log.info("uno_minda: page %d/%d  added=%d total=%d", pn, max_page, added, len(rows))
                if added == 0 and pn > 1:
                    break  # safety stop
            browser.close()
        log.info("uno_minda: %d unique products extracted", len(rows))
        return rows

    @staticmethod
    def _extract_page(page, rows: list[Row], seen: set[str]) -> int:
        # Each product card has h6.card-title and a sibling <p class="card-text"> with prices.
        cards = page.locator(".card-body").all()
        added = 0
        for card in cards:
            try:
                title = (card.locator("h6.card-title").first.inner_text(timeout=500)).strip()
            except Exception:
                continue
            if not title or "uno minda" not in title.lower():
                continue
            # Card text holds prices
            try:
                text = card.inner_text()
            except Exception:
                text = ""
            mrp_match = MRP_RE.search(text)
            mrp_val = mrp_match.group(1) if mrp_match else None
            # Item code from title
            sku_match = SKU_RE.search(title)
            item_code = sku_match.group(1) if sku_match else None
            if not item_code:
                # Fallback: skip if no SKU detectable (avoid dedup collision)
                continue
            if item_code in seen:
                continue
            seen.add(item_code)
            rows.append(Row(
                item_name=title,
                item_code=item_code,
                mrp=clean_mrp(mrp_val),
            ))
            added += 1
        return added
