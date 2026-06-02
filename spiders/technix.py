"""Technix spider — flat_list pattern, WP-REST product enumeration + detail-page extraction.

Plan ref §12.3. URL: https://technixauto.com/
xlsx steps: click search w/o filters → flat list of all products across tabs → extract.
xlsx fields: item_name, item_code, mrp.

Strategy (verified 2026-05-18):
1. Enumerate ALL 1596 products via WP REST API:  /wp-json/wp/v2/singlecar?per_page=100&page=N
   - title.rendered = item_code (e.g., "YVU-A5600")
   - link = product detail URL
2. For each detail page, parse:
   - item_name = jet-listing-dynamic-field__content following "Product Name" label
   - mrp = first jet-listing-dynamic-field__content text starting with "MRP ₹"

Tradeoff: ~1596 detail fetches/run. Sequential ~13min. Acceptable for monthly.
"""

from __future__ import annotations

import logging
import re

import httpx
from parsel import Selector

from lib.normalize import clean_mrp
from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.technix")

USER_AGENT = "SpinnyOEMCrawler/1.0 (contact@spinny.com)"
BASE = "https://technixauto.com"
API_LIST = f"{BASE}/wp-json/wp/v2/singlecar"
PER_PAGE = 100
MRP_PREFIX_RE = re.compile(r"^\s*MRP\s*₹", re.IGNORECASE)


class Spider(BaseSpider):

    def crawl(self) -> list[Row]:
        with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=60) as client:
            posts = self._enumerate_posts(client)
            log.info("technix: enumerated %d products via WP REST API", len(posts))
            rows: list[Row] = []
            for i, post in enumerate(posts, 1):
                row = self._fetch_detail(client, post["title"], post["link"])
                if row:
                    rows.append(row)
                if i % 100 == 0:
                    log.info("technix: %d/%d processed (%d rows so far)", i, len(posts), len(rows))
            return rows

    def _enumerate_posts(self, client: httpx.Client) -> list[dict]:
        """Return [{'title': 'YVU-A5600', 'link': '...'}, ...] across all REST pages.

        Dedups on `title` (= item_code). Technix's WP DB has ~170 SKUs published
        twice (slug-2 suffix; same name/MRP); first occurrence wins.
        """
        first = client.get(API_LIST, params={"per_page": PER_PAGE, "page": 1, "_fields": "title,link"})
        first.raise_for_status()
        total = int(first.headers.get("X-WP-Total", "0") or "0")
        total_pages = int(first.headers.get("X-WP-TotalPages", "1") or "1")
        log.info("technix: WP-API total=%d, total_pages=%d", total, total_pages)
        seen_titles: set[str] = set()
        posts: list[dict] = []
        for record_page in [first.json()] + [
            client.get(API_LIST, params={"per_page": PER_PAGE, "page": p, "_fields": "title,link"}).json()
            for p in range(2, total_pages + 1)
        ]:
            for rec in self._extract_titles(record_page):
                if rec["title"] in seen_titles:
                    continue
                seen_titles.add(rec["title"])
                posts.append(rec)
        log.info("technix: %d unique SKUs after dedup (from %d raw posts)", len(posts), total)
        return posts

    @staticmethod
    def _extract_titles(records: list[dict]) -> list[dict]:
        out = []
        for rec in records:
            t = (rec.get("title") or {}).get("rendered", "").strip()
            link = rec.get("link", "").strip()
            if t and link:
                out.append({"title": t, "link": link})
        return out

    @staticmethod
    def _fetch_detail(client: httpx.Client, item_code: str, link: str) -> Row | None:
        try:
            r = client.get(link)
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        sel = Selector(r.text)
        # item_name = jet-listing-dynamic-field__content immediately after 'Product Name' label
        name = sel.xpath(
            "//*[normalize-space(text())='Product Name']/following::*"
            "[contains(@class,'jet-listing-dynamic-field__content')][1]/text()"
        ).get()
        # mrp = first jet-listing-dynamic-field text starting with 'MRP'
        mrp_text = None
        for v in sel.css(".jet-listing-dynamic-field__content::text").getall():
            if MRP_PREFIX_RE.search(v):
                mrp_text = v.strip()
                break
        if mrp_text:
            mrp_text = re.sub(r"^MRP\s*₹\s*", "", mrp_text, flags=re.IGNORECASE)
        return Row(
            item_name=(name or "").strip() or None,
            item_code=item_code,
            mrp=clean_mrp(mrp_text),
        )
