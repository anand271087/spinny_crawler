"""ZIP Filters spider — multi_level_category pattern.

Plan ref §12.5. URL: https://www.zipfilters.com/
xlsx steps: 6 product sub-sections → for each, product list → extract.
xlsx fields: item_name, item_code, mrp.

Site structure (verified 2026-05-18):
- All products served on /products?cat=N (1=Air, 2=Cabin, 3=Fuel, 4=Filter Kits, 5=Oil, 6=Transmission).
- Each category is a SINGLE long HTML page (no pagination) — Air has 707KB, ~1500+ product cards.
- Each card: `.product__item` containing:
  * `.product__item__title a` → "ZIP <Category> <Code>" (full title)
  * `<strong>ZIP Part No. :</strong>` followed by sibling text → item_code
  * `<strong>Suitable For :</strong>` followed by sibling text → vehicle application
  * last `<div>` text → "₹ <N>" → MRP

4W scoping (BRD §3.2):
- ZIP's catalogue mixes 2W and 4W filters. Filter at row level using the "Suitable For"
  text and a 2W vehicle-brand denylist. PV products either name a 4W brand explicitly
  or carry no 2W marker.
"""

from __future__ import annotations

import logging
import re

import httpx
from parsel import Selector

from lib.normalize import clean_mrp
from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.zip")

USER_AGENT = "SpinnyOEMCrawler/1.0 (contact@spinny.com)"
BASE = "https://www.zipfilters.com"
CATEGORIES = {
    1: "Air Filters",
    2: "Cabin Filters",
    3: "Fuel Filters",
    4: "Filter Kits",
    5: "Oil Filters",
    6: "Transmission Filters",
}

# 2W vehicle-brand denylist — drop rows whose Suitable For begins with these.
# Brands that make ONLY 2W (or where ZIP's 4W catalogue clearly omits 4W variants).
# Conservative: only includes pure-2W brands. Honda/Suzuki appear for both 2W and 4W → kept.
NON_4W_PREFIX = re.compile(
    r"^(?:2[-\s]?WHEELERS?|BAJAJ|HERO|TVS\s+(?:APACHE|JUPITER|XL|STAR|SCOOTY|NTORQ)|ROYAL\s+ENFIELD|YAMAHA|KTM)\b",
    re.IGNORECASE,
)


class Spider(BaseSpider):

    def crawl(self) -> list[Row]:
        rows: list[Row] = []
        seen_codes: set[str] = set()
        with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=60) as client:
            for cat_id, cat_name in CATEGORIES.items():
                url = f"{BASE}/products?cat={cat_id}"
                resp = client.get(url)
                resp.raise_for_status()
                cards = self._parse_cards(resp.text, cat_name)
                kept = 0
                dropped_2w = 0
                for c in cards:
                    if c["item_code"] in seen_codes:
                        continue
                    if c["suitable_for"] and NON_4W_PREFIX.match(c["suitable_for"]):
                        dropped_2w += 1
                        continue
                    seen_codes.add(c["item_code"])
                    rows.append(Row(
                        item_name=c["item_name"],
                        item_code=c["item_code"],
                        mrp=clean_mrp(c["mrp_raw"]),
                    ))
                    kept += 1
                log.info("zip cat=%d (%s) cards=%d kept=%d 2W-dropped=%d",
                         cat_id, cat_name, len(cards), kept, dropped_2w)
        return rows

    @staticmethod
    def _parse_cards(html: str, category: str) -> list[dict]:
        sel = Selector(html)
        cards: list[dict] = []
        for item in sel.css(".product__item"):
            title = (item.css(".product__item__title a::text").get() or "").strip()
            if not title:
                continue
            # ZIP Part No. — follows a <strong> label
            part_no = item.xpath(
                ".//strong[contains(normalize-space(.), 'Part No')]/following-sibling::text()[1]"
            ).get() or item.xpath(
                ".//strong[contains(normalize-space(.), 'Part No')]/../text()[normalize-space()][1]"
            ).get() or ""
            part_no = part_no.strip().lstrip(":").strip()
            # Suitable For — same pattern
            suitable_for = item.xpath(
                ".//strong[contains(normalize-space(.), 'Suitable For')]/following-sibling::text()[1]"
            ).get() or ""
            suitable_for = suitable_for.strip().lstrip(":").strip()
            # Price — last <div> in the content block whose text starts with ₹
            price_raw = ""
            for div in item.css(".product__item__content div"):
                t = (div.xpath("string(.)").get() or "").strip()
                if t.startswith("₹"):
                    price_raw = t
                    # Don't break: keep the LAST one — covers cases where label divs appear before price
            if not part_no:
                continue
            cards.append({
                "item_name": title,
                "item_code": part_no,
                "suitable_for": suitable_for,
                "mrp_raw": price_raw,
            })
        return cards
