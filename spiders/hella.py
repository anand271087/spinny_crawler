"""HELLA spider — multi_level_category pattern with detail-page enrichment.

Plan ref §12.1. Source URL: https://shop4hella.com/listing/4-wheeler
xlsx fields: item_name, item_code (SKU), mrp.

⚠️ Site quirk (verified 2026-05-18):
The documented xlsx flow (`/listing/4-wheeler/<pca|pcs>/<category>`) is broken — it
renders the filter chrome but ZERO product cards. The working URL pattern is the
unscoped `/listing/Shop4Hella/Shop4Hella/<category>` which mixes products across
all segments (2W, 3W, 4W, Commercial, Agriculture).

Strategy:
1. Discover categories from the (working-for-discovery) /listing/4-wheeler/<pca|pcs>
   sub-segment pages — they list which categories exist per segment.
2. Crawl the unscoped /listing/Shop4Hella/Shop4Hella/<cat>/<page> for those category
   slugs, walking path-based pagination until a page yields no new products.
3. **Parallel async detail fetch** — for each product card, fetch its detail page
   concurrently (semaphore=8). Parse breadcrumb, MRP, "for <Vehicle>" pattern.
4. Keep only products whose breadcrumb segment is "Passenger Car Accessories (PCA)"
   or "Passenger Car Spare Parts (PCS)" — BRD §3.2 4W passenger-vehicle scope.

Enhanced fields (2026-05-19 deeper pass):
- mrp: pulled from detail page "MRP : Rs ..." regex (authoritative; listing card
  reused for fallback if missing).
- vehicle_compatibility: breadcrumb "Category > Sub-category" hierarchy.
- compatible_car_model: parsed from product name "for <Vehicle>" pattern, when
  present (e.g., "Head Lamp Assy for Piaggio Ape LH" → "Piaggio Ape").

Performance: parallel detail fetches drop runtime from ~14 min sequential to ~2 min.
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urljoin

import httpx
from parsel import Selector

from spiders._base import BaseSpider, Row
from lib.normalize import clean_mrp

log = logging.getLogger("spiders.hella")

USER_AGENT = "SpinnyOEMCrawler/1.0 (contact@spinny.com)"
BASE = "https://shop4hella.com"
SEGMENT_PAGES = [
    "/listing/4-wheeler/passenger-car-accessories-PCA",
    "/listing/4-wheeler/passenger-car-spare-parts-PCS",
]
LISTING_TEMPLATE = "/listing/Shop4Hella/Shop4Hella/{category}"
PV_BREADCRUMB_PREFIXES = {
    "Passenger Car Accessories (PCA)",
    "Passenger Car Spare Parts (PCS)",
}
MAX_PAGES_PER_CATEGORY = 200  # Safety cap; site is 6 products/page

DETAIL_CONCURRENCY = 4  # parallel detail fetches; lowered 2026-07 — the Hella
                        # server drops connections (RemoteProtocolError) under higher
                        # concurrency / rapid pagination.
DETAIL_TIMEOUT = 30.0
REQUEST_DELAY = 0.4     # polite gap between sequential listing-page fetches
GET_RETRIES = 4         # retry transient server disconnects with backoff
# Transient network/server errors the Hella site throws under load. Caught +
# retried by _get(); previously an uncaught RemoteProtocolError on a listing
# fetch crashed the whole spider → 0 rows.
RETRYABLE_ERRORS = (
    httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError,
    httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout, httpx.WriteError,
)

# Detail-page regex extractors
MRP_RE = re.compile(r"MRP\s*:?\s*Rs\.?\s*([\d,]+)", re.IGNORECASE)
# Vehicle compat: product names like "Head Lamp Assy for Piaggio Ape LH" — capture
# the noun phrase after "for", trimmed at common position suffixes (LH/RH/Left/Right).
# Captures the first 1-6 tokens after "for"; we filter by whitelist below.
FOR_VEHICLE_RE = re.compile(
    r"\bfor\s+([A-Za-z&][A-Za-z0-9 /&.\-]+?)"
    r"(?:\s+(?:LH|RH|Left|Right|24V|12V|with\b|w/o\b|with\s)|[.,]|\s*$)",
    re.IGNORECASE,
)
# Whitelist of recognized vehicle makes/manufacturers (lowercase for matching).
# Discovered from `state/probe_hella_categories.py` output + common PV brands.
VEHICLE_MAKES = {
    "maruti", "hyundai", "bmw", "tata", "audi", "toyota", "m&m", "mahindra",
    "honda", "chevrolet", "ford", "renault", "skoda", "vw", "volkswagen",
    "jaguar", "volvo", "mini", "mb", "mercedes", "mercedes-benz", "gm",
    "range", "land", "fiat", "datsun", "mitsubishi", "nissan", "suzuki",
    "kia", "porsche", "lexus", "isuzu", "ssangyong", "tesla", "ashok",
    # Models that imply a unique make (occur as the first word after "for"):
    "touareg", "evoque", "discovery", "s-class", "e-class", "gl-class",
    "c-class", "a-class", "w166", "w205", "w212", "w213",
}


class Spider(BaseSpider):

    def crawl(self) -> list[Row]:
        return asyncio.run(self._crawl_async())

    async def _crawl_async(self) -> list[Row]:
        rows: list[Row] = []
        seen_codes: set[str] = set()
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=DETAIL_TIMEOUT,
        ) as client:
            categories = await self._discover_4w_categories(client)
            log.info(
                "HELLA categories discovered (4W scope): %d → %s",
                len(categories), sorted(categories),
            )
            for cat in sorted(categories):
                cat_rows = await self._crawl_category(client, cat, seen_codes)
                log.info("hella category=%s 4W-items=%d", cat, len(cat_rows))
                rows.extend(cat_rows)
        return rows

    @staticmethod
    async def _get(client: httpx.AsyncClient, url: str):
        """GET with backoff retry on transient server disconnects. Returns the
        response or None after GET_RETRIES failures (caller decides how to handle).
        The Hella server intermittently drops connections under load — one uncaught
        RemoteProtocolError previously crashed the entire spider."""
        delay = 1.0
        for attempt in range(1, GET_RETRIES + 1):
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp
            except RETRYABLE_ERRORS as exc:
                if attempt == GET_RETRIES:
                    log.warning("hella GET failed after %d tries: %s (%s)",
                                GET_RETRIES, url, exc)
                    return None
                await asyncio.sleep(delay)
                delay *= 2
            except httpx.HTTPStatusError as exc:
                log.warning("hella GET %s → %s", url, exc)
                return None
        return None

    async def _discover_4w_categories(self, client: httpx.AsyncClient) -> set[str]:
        """Return the union of category slugs listed on the PCA + PCS sub-segment pages."""
        categories: set[str] = set()
        for seg_path in SEGMENT_PAGES:
            url = urljoin(BASE, seg_path)
            resp = await self._get(client, url)
            if resp is None:
                continue
            sel = Selector(resp.text)
            for href in sel.css("a::attr(href)").getall():
                if seg_path in href:
                    tail = href.split(seg_path, 1)[1].strip("/")
                    if tail and "/" not in tail:
                        categories.add(tail.lower())
        return categories

    async def _crawl_category(
        self,
        client: httpx.AsyncClient,
        category: str,
        seen_codes: set[str],
    ) -> list[Row]:
        """Walk listing pagination for one category, then enrich PV candidates in parallel."""
        rows: list[Row] = []
        page = 1
        candidates: list[dict] = []
        # Phase 1 — sequential listing walk (pagination is path-based, must be ordered)
        while page <= MAX_PAGES_PER_CATEGORY:
            url = urljoin(BASE, LISTING_TEMPLATE.format(category=category)) + (
                f"/{page}" if page > 1 else ""
            )
            resp = await self._get(client, url)
            if resp is None:
                break  # give up this category cleanly instead of crashing the spider
            cards = self._parse_cards(resp.text)
            if not cards:
                break
            await asyncio.sleep(REQUEST_DELAY)  # throttle to avoid server drops
            new_in_page = 0
            for card in cards:
                if card["item_code"] in seen_codes:
                    continue
                seen_codes.add(card["item_code"])
                candidates.append(card)
                new_in_page += 1
            page += 1
            if new_in_page == 0 and len(cards) < 6:
                break
        log.info(
            "hella category=%s listing-candidates=%d (before PV filter)",
            category, len(candidates),
        )

        # Phase 2 — parallel detail fetch + segment filter
        semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def enrich(card):
            async with semaphore:
                return await self._enrich(client, card)

        enriched = await asyncio.gather(*(enrich(c) for c in candidates))
        rows.extend(r for r in enriched if r is not None)
        return rows

    async def _enrich(self, client: httpx.AsyncClient, card: dict) -> Row | None:
        """Fetch detail page, apply PV filter, return enriched Row or None."""
        detail_url = card.get("detail_url") or ""
        if not detail_url:
            return None
        r = await self._get(client, detail_url)
        if r is None:
            return None
        sel = Selector(r.text)

        crumbs = [t.strip() for t in sel.css(".breadcrumb *::text").getall() if t.strip()]
        if len(crumbs) < 2 or crumbs[1] not in PV_BREADCRUMB_PREFIXES:
            return None  # not passenger-vehicle

        # MRP from detail page (authoritative); fall back to listing card if absent
        body_text = " ".join(sel.css("body *::text").getall())
        m = MRP_RE.search(body_text)
        if m:
            mrp = clean_mrp(m.group(1))
        else:
            mrp = clean_mrp(card.get("mrp_raw"))

        # vehicle_compatibility = breadcrumb "Category > Sub-category"
        veh_compat: str | None = None
        if len(crumbs) >= 4:
            veh_compat = f"{crumbs[2]} > {crumbs[3]}"
        elif len(crumbs) >= 3:
            veh_compat = crumbs[2]

        # compatible_car_model from "for <Vehicle>" pattern in item_name (best-effort)
        compat_model: str | None = None
        m2 = FOR_VEHICLE_RE.search(card["item_name"])
        if m2:
            candidate = m2.group(1).strip().rstrip("-").strip()
            first_word = candidate.split()[0].lower() if candidate else ""
            # Only accept if first token matches a known vehicle make/model — avoids
            # false positives like "for High Beam", "for Comet 500" (HELLA product),
            # "for HELLA" etc.
            if first_word in VEHICLE_MAKES:
                compat_model = candidate

        return Row(
            item_name=card["item_name"],
            item_code=card["item_code"],
            mrp=mrp,
            compatible_car_model=compat_model,
            vehicle_compatibility=veh_compat,
        )

    @staticmethod
    def _parse_cards(html: str) -> list[dict]:
        sel = Selector(html)
        cards: list[dict] = []
        for grid in sel.css("div.product-grid"):
            detail = (grid.css("a::attr(href)").get() or "").strip()
            name = (grid.css("a > span::text").get() or grid.css("span::text").get() or "").strip()
            sku_raw = (grid.css("p.sku::text").get() or "").strip()
            sku = re.sub(r"^SKU:\s*", "", sku_raw).strip()
            price_raw = (grid.css("p.price::text").get() or "").strip()
            if not sku and not name:
                continue
            cards.append({
                "item_name": name,
                "item_code": sku,
                "mrp_raw": price_raw,
                "detail_url": detail,
            })
        return cards
