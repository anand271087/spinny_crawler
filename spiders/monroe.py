"""MONROE spider — flat_list pattern via BKS Motors reseller (Magento storefront).

Plan ref §12.6. URL: https://www.bksmotors.com/brand/monroe
xlsx steps: open URL → flat product list → extract.
xlsx fields: item_name, item_code, mrp.

Site notes:
- Magento storefront, fully static HTML — no Playwright needed.
- ~326 items, ~41 per page, query-string pagination (?p=N).
- Past last page, server returns generic "related items" — detect via SKU dedupe.
- Strip Google referral `?srsltid=...` param if present in cfg URL (per plan §12.6).
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import httpx
from parsel import Selector

from lib.normalize import clean_mrp
from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.monroe")

USER_AGENT = "SpinnyOEMCrawler/1.0 (contact@spinny.com)"
SKU_PATTERN = re.compile(r"SKU:\s*</strong>\s*([\w\-\.]+)", re.IGNORECASE)
MAX_PAGES = 50  # 326 items / 41 per page = ~8 pages; safety stop


def _strip_referral(url: str) -> str:
    """Drop srsltid (Google search referral) param per plan §12.6."""
    parts = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "srsltid"]
    return urlunparse(parts._replace(query=urlencode(q)))


class Spider(BaseSpider):

    def crawl(self) -> list[Row]:
        clean_url = _strip_referral(self.cfg["url"])
        rows: list[Row] = []
        seen_skus: set[str] = set()
        with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30) as client:
            for page in range(1, MAX_PAGES + 1):
                page_url = f"{clean_url}?p={page}" if page > 1 else clean_url
                resp = client.get(page_url)
                resp.raise_for_status()
                cards = self._parse_cards(resp.text)
                new_count = 0
                for c in cards:
                    if c["item_code"] and c["item_code"] not in seen_skus:
                        seen_skus.add(c["item_code"])
                        rows.append(Row(
                            item_name=c["item_name"],
                            item_code=c["item_code"],
                            mrp=clean_mrp(c["mrp_raw"]),
                        ))
                        new_count += 1
                log.info("monroe page=%d cards=%d new=%d total_unique=%d",
                         page, len(cards), new_count, len(seen_skus))
                # Stop when a page yields zero new SKUs — pagination exhausted
                if new_count == 0:
                    break
        return rows

    @staticmethod
    def _parse_cards(html: str) -> list[dict]:
        sel = Selector(html)
        cards: list[dict] = []
        # Each product card sits in a div / li with class containing "product-item-info"
        for item in sel.css("li.item.product, .product-item-info"):
            # Name: the title link text
            name = (item.css("a.product-item-link::text").get()
                    or item.css(".product-item-name a::text").get()
                    or item.css("strong.product a::text").get()
                    or "").strip()
            if not name:
                continue
            # SKU: extracted from description-table snippet
            html_chunk = item.get()
            m = SKU_PATTERN.search(html_chunk)
            sku = m.group(1).strip() if m else ""
            # Price: prefer special-price if present, else regular
            price_raw = (item.css(".price-wrapper[data-price-amount]::attr(data-price-amount)").get()
                         or item.css(".price::text").get()
                         or "").strip()
            cards.append({
                "item_name": name,
                "item_code": sku,
                "mrp_raw": price_raw,
            })
        return cards
