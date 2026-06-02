"""VALEO India spider — Next.js + TecAssist REST API.

Plan ref §12.14. URL: https://www.valeoservice.in/en-in/passenger-car
xlsx fields: item_name, item_code, compatible_car_model (no MRP per xlsx).

Site reality (verified 2026-05-19, second pass — flipped from SHIPPED-AS-FAILED):
The PUBLIC product catalogue is at /en-in/techassist (Technical Assistance),
NOT under /en-in/passenger-car. This is a Next.js app fronting the public
Valeo TecAssist REST API at api.valeoservice-techassist.com.

Discovery chain (no auth needed):
1. Scrape Next.js BUILD_ID from <script id="__NEXT_DATA__"> on /en-in/techassist.
2. GET /_next/data/<BUILD>/en-in/techassist/products/product-lines.json
   ?selectedProductLineTab=PASSENGER → 24 PASSENGER product lines (P-100001..P-200067).
3. For each line: GET /_next/data/<BUILD>/.../product-line/P-<id>.json
   → 'parts' = sub-categories with `id` (e.g., 402 = "Brake Pad Set, disc brake").
4. For each part id: POST https://api.valeoservice-techassist.com/rest/articles
   ?page=N&country=IN&lang=en with body {"filters":{"partIds":["402"],"brands":[]}}.
   Paginate; default pageSize=10 (server-fixed). Yields `articles` with full part data.

Each article contains:
- reference: Valeo part number (e.g. "207441") → item_code
- description: human-readable name (e.g. "Brake Pad Set, disc brake") → item_name
- oemNumbers: vehicle-OEM cross-reference map (e.g. {"MAHINDRA":[{"articleNumber":"0603BAA-0461N"}]})
  → compatible_car_model (concatenated)
- criteria: technical specs (Front Axle, dimensions, etc.) — not in xlsx output
- eanBarcode, images — not in xlsx output

MRP: NOT in any API response. Same partial-rationale as Schaeffler, Toyota,
Bosch wipers, etc. — Valeo India catalog publishes technical references only,
pricing requires dealer access. Rows finalize as `partial` per BRD §7.

Performance: ~24 line fetches + ~70 sub-category fetches + ~1,400 paginated
articles fetches = ~1,500 GET/POST calls. ~0.2s/call = ~5 min full run.

Dedup: same reference may appear under multiple product lines (Valeo lists each
line + each line-of-business duplicate). Spider dedupes by `reference`.

Status: ⚠️ partial (MRP missing — same as Schaeffler/Toyota/Bosch wipers).
"""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import quote

import httpx

from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.valeo")

USER_AGENT = "SpinnyOEMCrawler/1.0 (contact@spinny.com)"
SITE = "https://www.valeoservice.in"
TECH_API = "https://api.valeoservice-techassist.com/rest/articles"

LANDING_URL = f"{SITE}/en-in/techassist"
COUNTRY = "IN"
LANG = "en"

# Match the Next.js BUILD_ID exposed in window.__NEXT_DATA__ on landing pages
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
    re.DOTALL,
)

# Safety cap. Largest known sub-category has 131 articles → 14 pages.
# We've seen no Valeo sub-cat exceed ~600 articles → 60 pages. 200 is generous.
MAX_PAGES_PER_GROUP = 200


class Spider(BaseSpider):

    def crawl(self) -> list[Row]:
        rows: list[Row] = []
        seen_refs: set[str] = set()
        t_start = time.time()
        with httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Referer": SITE + "/",
                "Origin": SITE,
            },
            timeout=60,
            follow_redirects=True,
            verify=False,  # Valeo cert chain quirks observed on some clients
        ) as client:
            build_id = self._fetch_build_id(client)
            log.info("valeo: Next.js BUILD_ID=%s", build_id)

            lines = self._fetch_product_lines(client, build_id)
            log.info("valeo: %d PASSENGER product lines discovered", len(lines))

            seen_line_ids: set[str] = set()
            seen_part_ids: set[int] = set()
            line_idx = 0
            for line in lines:
                # `id` is a numeric int (e.g., 100006). URL form is "P-<id>".
                raw_id = line.get("id")
                if raw_id is None:
                    continue
                line_id = f"P-{raw_id}"
                if line_id in seen_line_ids:
                    continue
                seen_line_ids.add(line_id)
                line_idx += 1
                line_name = line.get("name") or line.get("description") or line_id
                parts = self._fetch_line_parts(client, build_id, line_id, line_name)
                log.info("[%d/%d] %s (%s) → %d sub-categories | elapsed %.0fs | rows %d",
                         line_idx, len(lines), line_name, line_id, len(parts),
                         time.time() - t_start, len(rows))
                for part in parts:
                    part_id = part.get("id")
                    if part_id is None or part_id in seen_part_ids:
                        continue
                    seen_part_ids.add(part_id)
                    part_desc = part.get("description") or str(part_id)
                    try:
                        articles = self._fetch_articles(client, part_id)
                    except Exception as exc:
                        log.warning("valeo: part_id=%s fetch failed: %s", part_id, exc)
                        continue
                    new = 0
                    for art in articles:
                        ref = (art.get("reference") or "").strip()
                        if not ref or ref in seen_refs:
                            continue
                        seen_refs.add(ref)
                        rows.append(self._make_row(art, line_name, part_desc))
                        new += 1
                    log.debug("  sub-cat id=%s '%s' → %d articles, %d new",
                              part_id, part_desc, len(articles), new)

        elapsed = time.time() - t_start
        log.info("valeo: %d unique articles extracted in %.0fs (%.1f min)",
                 len(rows), elapsed, elapsed / 60)
        return rows

    # ----------------------------------------------------------------- discovery
    def _fetch_build_id(self, client: httpx.Client) -> str:
        resp = client.get(LANDING_URL)
        resp.raise_for_status()
        m = NEXT_DATA_RE.search(resp.text)
        if not m:
            raise RuntimeError("valeo: __NEXT_DATA__ block not found on landing page")
        data = json.loads(m.group(1))
        build_id = data.get("buildId")
        if not build_id:
            raise RuntimeError("valeo: buildId missing from __NEXT_DATA__")
        return build_id

    def _fetch_product_lines(self, client: httpx.Client, build_id: str) -> list[dict]:
        """Return the 24 PASSENGER product lines (with duplicates across line-of-business)."""
        url = (f"{SITE}/_next/data/{build_id}/en-in/techassist/products/product-lines.json"
               f"?selectedProductLineTab=PASSENGER")
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
        return data.get("pageProps", {}).get("productLines") or []

    def _fetch_line_parts(self, client: httpx.Client, build_id: str,
                          line_id: str, line_name: str) -> list[dict]:
        """Return sub-categories ('parts') under one product line."""
        # productLineName slug appears in URLs but the server tolerates any slug
        slug = quote(_slugify(line_name))
        url = (f"{SITE}/_next/data/{build_id}/en-in/techassist/products/product-lines"
               f"/product-line/{line_id}.json?productLineName={slug}&lineId={line_id}")
        resp = client.get(url)
        if resp.status_code != 200:
            log.warning("valeo: line %s returned %s", line_id, resp.status_code)
            return []
        return resp.json().get("pageProps", {}).get("productLine", {}).get("parts") or []

    def _fetch_articles(self, client: httpx.Client, part_id: int) -> list[dict]:
        """POST paginated `/rest/articles` for one sub-category, returning ALL articles."""
        out: list[dict] = []
        page = 1
        while page <= MAX_PAGES_PER_GROUP:
            resp = client.post(
                TECH_API,
                params={"page": page, "country": COUNTRY, "lang": LANG},
                json={"filters": {"partIds": [str(part_id)], "brands": []}},
            )
            if resp.status_code != 200:
                log.warning("valeo: articles page=%d part_id=%s status=%s",
                            page, part_id, resp.status_code)
                break
            payload = resp.json()
            arts = payload.get("articles") or []
            out.extend(arts)
            pag = payload.get("pagination") or {}
            page_count = pag.get("pageCount") or 1
            if page >= page_count:
                break
            page += 1
        return out

    # ----------------------------------------------------------------- transform
    @staticmethod
    def _make_row(article: dict, line_name: str, part_desc: str) -> Row:
        ref = (article.get("reference") or "").strip()
        desc = (article.get("description") or part_desc).strip()
        # OEM cross-reference → compatible_car_model
        oem_strs: list[str] = []
        oems = article.get("oemNumbers") or {}
        for make, nums in oems.items():
            if not isinstance(nums, list):
                continue
            nums_clean = [(n or {}).get("articleNumber", "") for n in nums if n]
            nums_clean = [n.strip() for n in nums_clean if n]
            if nums_clean:
                oem_strs.append(f"{make}: {', '.join(nums_clean[:3])}")
        if oem_strs:
            compat = " | ".join(oem_strs[:8])
        else:
            compat = f"VALEO {line_name} > {part_desc}"
        return Row(
            item_name=f"Valeo {desc}",
            item_code=ref,
            mrp=None,  # NOT in API — same partial-rationale as Schaeffler/Toyota
            compatible_car_model=compat[:400],
        )


def _slugify(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "x"
