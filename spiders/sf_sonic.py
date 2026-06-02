"""SF SONIC spider — cascading_dropdown pattern, all httpx (no Playwright needed).

Plan ref §12.8. URL: https://www.sfbatteries.in/battery-finder/4w-battery/
xlsx steps: 4W Battery → cascade Brand × Model × Fuel → extract battery info.
xlsx fields: item_name, item_code, mrp.

Site reality (verified 2026-05-18):
- Drupal-style AJAX form with 3 selects (brand, model, fuel) + Locate submit.
- POST to /battery-finder/4w-battery/ with `ait_action=loadModels|loadModelsFuels|find_battery`.
- AJAX responses are JSON: {"models": "<option>...</option>..."} / {"fuels": "..."}
- `find_battery` POST 302-redirects to /battery-for/car/<brand-slug>/<model-slug>/<fuel-slug>/
- Result page lists batteries; each in `<li class="addAnimate">` with:
  * <aside><h2>F4W5-66S-40B20L</h2> ← item_code
  * <h5>MRP: <strong>Rs 4687</strong></h5> ← MRP
  * <article class="moreProductInfo">
      <h3>66S-40B20L</h3> ← simple battery model
      <p><strong>F4W5-66S-40B20L</strong><span>Enhanced Life ...</span></p> ← variant

Item naming:
- item_code = h2 text (full SF SONIC SKU)
- item_name = "<Variant> <Model>" combining moreProductInfo span text + h3
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import httpx
from parsel import Selector

from lib.normalize import clean_mrp
from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.sf_sonic")

# Per-model fuel concurrency. Set to 1 (serialize) because Drupal's
# find_battery action races on the SAME session — concurrent POSTs for
# different fuels all end up reading the LAST-set fuel's redirect target,
# so we'd dedup down to 1 row per model (verified 2026-05-25: hit 38 rows
# for the whole catalogue vs the old shipped 1,596). Sequential fuel walks
# avoid the race while still using async I/O across BRAND→BRAND parallelism.
FUEL_FETCH_CONCURRENCY = 1

USER_AGENT = "SpinnyOEMCrawler/1.0 (contact@spinny.com)"
BASE = "https://www.sfbatteries.in"
FINDER_URL = f"{BASE}/battery-finder/4w-battery/"

OPTION_RE = re.compile(r'<option[^>]*value=[\'"]([^\'"]*)[\'"][^>]*>([^<]+)</option>')
MRP_RE = re.compile(r"Rs\s*([\d,]+)", re.IGNORECASE)
SLUG_RE = re.compile(r"[^a-z0-9]+")
RESULT_URL_TPL = f"{BASE}/battery-for/car/{{brand}}/{{model}}/{{fuel}}/"


def _slug(s: str) -> str:
    return SLUG_RE.sub("-", s.lower()).strip("-")


class Spider(BaseSpider):

    def crawl(self) -> list[Row]:
        log.info("sf_sonic: spider crawl() entered")
        t_start = time.time()
        rows: list[Row] = []
        # Dedup key is (item_code, compatible_car_model) — see _parse_result_page docstring
        seen: set[tuple[str, str]] = set()
        # Per-model fuel fetches now run in parallel via asyncio (added 2026-05-22
        # after 2026-05-21 run took 45min/24 brands and was killed before TATA/etc).
        # Brand-level walk stays sequential because brand → model → fuel response
        # POSTs use a shared session that's serial inside Drupal's AJAX handler.
        with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30) as client:
            brands = self._bootstrap(client)
            log.info("sf_sonic: %d brands found", len(brands))
            for brand_idx, (brand_value, brand_label) in enumerate(brands, start=1):
                brand_t0 = time.time()
                try:
                    models = self._load_models(client, brand_value)
                except Exception as exc:
                    log.warning("sf_sonic: brand=%s failed: %s", brand_label, exc)
                    continue
                log.info("[%d/%d] sf_sonic: brand=%s models=%d, elapsed=%.0fs, rows=%d",
                         brand_idx, len(brands), brand_label, len(models),
                         time.time() - t_start, len(rows))

                # Walk models — for each model, fetch all fuel result pages in parallel
                brand_rows = asyncio.run(
                    self._crawl_brand_async(brand_value, brand_label, models, seen)
                )
                rows.extend(brand_rows)
                log.info("  brand=%s done in %.0fs, +%d rows",
                         brand_label, time.time() - brand_t0, len(brand_rows))

        elapsed = time.time() - t_start
        log.info("sf_sonic: %d unique batteries extracted in %.0fs (%.1f min)",
                 len(rows), elapsed, elapsed / 60)
        return rows

    async def _crawl_brand_async(
        self, brand_value: str, brand_label: str,
        models: list[tuple[str, str]], seen: set[tuple[str, str]],
    ) -> list[Row]:
        """Walk models sequentially (POST-based model→fuels handshake), but
        fetch each model's fuel result pages in parallel.

        Drupal-session warm-up (2026-05-25): we open a FRESH AsyncClient per
        brand, so the cookies the sync client accumulated during _bootstrap +
        _load_models don't carry over. Drupal's `loadModelsFuels` action
        returns the empty placeholder (`<option value="">Select Fuel Type
        </option>`) unless we've first hit `loadModels` in the SAME session.
        Fix: prime each AsyncClient with a finder-page GET + loadModels POST
        before iterating models.
        """
        out: list[Row] = []
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30,
        ) as ac:
            # Warm up the Drupal session in this AsyncClient: GET finder, then
            # POST loadModels for the brand. Both calls set session cookies
            # the subsequent loadModelsFuels / find_battery actions require.
            try:
                await ac.get(FINDER_URL)
                await ac.post(FINDER_URL, data={
                    "ait_action": "loadModels", "type": "CAR", "brand": brand_value,
                })
            except Exception as exc:
                log.warning("sf_sonic: %s session warm-up failed: %s",
                            brand_label, exc)
                return out

            sem = asyncio.Semaphore(FUEL_FETCH_CONCURRENCY)
            for model_value, model_label in models:
                # Sequential POST for fuels (shared Drupal session expects it)
                try:
                    resp = await ac.post(FINDER_URL, data={
                        "ait_action": "loadModelsFuels", "type": "CAR",
                        "brand": brand_value, "model": model_value,
                    })
                    fuels = self._extract_options(resp.json().get("fuels", ""))
                except Exception as exc:
                    log.warning("sf_sonic: %s/%s fuels failed: %s",
                                brand_label, model_label, exc)
                    continue
                # Parallel fuel-result-page fetches via POST→302→render
                tasks = [
                    self._fetch_battery_page_async(
                        ac, sem,
                        brand_value, brand_label,
                        model_value, model_label,
                        fv, fl, seen,
                    )
                    for fv, fl in fuels
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, list):
                        out.extend(r)
        return out

    async def _fetch_battery_page_async(
        self, ac: httpx.AsyncClient, sem: asyncio.Semaphore,
        brand_value: str, brand_label: str,
        model_value: str, model_label: str,
        fuel_value: str, fuel_label: str,
        seen: set[tuple[str, str]],
    ) -> list[Row]:
        """POST find_battery → 302 → final result page.

        We previously tried to bypass the redirect with a client-side
        `_slug()`, but the server's slug algorithm doesn't match Python's
        kebab-case (it uses `_` for hyphens and `~` for slashes). Most
        model names rendered as 200-OK pages with zero battery cards
        because the slug we built didn't match an existing model.
        Fix 2026-05-25: POST and let the server redirect to its own
        canonical URL.
        """
        async with sem:
            try:
                r = await ac.post(FINDER_URL, data={
                    "ait_action": "find_battery", "type": "CAR",
                    "brand": brand_value, "model": model_value,
                    "fuel": fuel_value,
                })
            except httpx.HTTPError:
                return []
            if r.status_code != 200:
                return []
            return self._parse_result_page(r.text, brand_label, model_label, fuel_label, seen)

    @staticmethod
    def _extract_options(html: str) -> list[tuple[str, str]]:
        out = []
        for v, t in OPTION_RE.findall(html):
            v = v.strip()
            t = t.strip()
            if v and t and "select" not in t.lower():
                out.append((v, t))
        return out

    @staticmethod
    def _parse_result_page(html: str, brand_label: str, model_label: str,
                           fuel_label: str, seen: set) -> list[Row]:
        """Emit one row per (item_code, vehicle) — matches the original shipped
        behaviour where Spinny analysts need a per-vehicle "compatible batteries"
        view, not a unique-SKU listing.

        Dedup key is (item_code, compatible_car_model) so the same battery code
        appears once per vehicle it fits. SF SONIC's catalog has only ~30-50
        unique SKUs that fit many vehicles; deduping by code-only collapses to
        ~30 rows (verified 2026-05-25). Per-vehicle dedup restores ~1,500+ rows.
        """
        sel = Selector(html)
        rows: list[Row] = []
        compat = f"{brand_label} {model_label} ({fuel_label.upper()})"
        for li in sel.css("li.addAnimate"):
            aside = li.css("aside").get() or ""
            if not aside:
                continue
            aside_sel = Selector(aside)
            item_code = (aside_sel.css("h2::text").get() or "").strip()
            if not item_code:
                continue
            key = (item_code, compat)
            if key in seen:
                continue
            mrp_text = aside_sel.css("h5").xpath("string(.)").get() or ""
            mrp_match = MRP_RE.search(mrp_text)
            mrp_val = mrp_match.group(1) if mrp_match else None
            more = li.css("article.moreProductInfo")
            simple_model = (more.css("h3::text").get() or "").strip()
            variant_span = (more.css("p span::text").get() or "").strip()
            variant = re.split(r"warranty", variant_span, maxsplit=1, flags=re.IGNORECASE)[0].strip()
            name_parts = [variant, simple_model] if variant and simple_model else [item_code]
            item_name = " ".join(name_parts).strip() or item_code
            seen.add(key)
            rows.append(Row(
                item_name=item_name,
                item_code=item_code,
                mrp=clean_mrp(mrp_val),
                compatible_car_model=compat,
            ))
        return rows

    @staticmethod
    def _bootstrap(client: httpx.Client) -> list[tuple[str, str]]:
        r = client.get(FINDER_URL)
        r.raise_for_status()
        sel = Selector(r.text)
        brand_html = sel.css('select[name="brand"]').get() or ""
        out = []
        for v, t in OPTION_RE.findall(brand_html):
            v = v.strip()
            t = t.strip()
            if v and t and "select" not in t.lower():
                out.append((v, t))
        return out

    @staticmethod
    def _load_models(client: httpx.Client, brand: str) -> list[tuple[str, str]]:
        r = client.post(FINDER_URL, data={
            "ait_action": "loadModels", "type": "CAR", "brand": brand,
        })
        r.raise_for_status()
        data = r.json()
        html = data.get("models", "")
        out = []
        for v, t in OPTION_RE.findall(html):
            v = v.strip()
            t = t.strip()
            if v and t and "select" not in t.lower():
                out.append((v, t))
        return out

    @staticmethod
    def _load_fuels(client: httpx.Client, brand: str, model: str) -> list[tuple[str, str]]:
        r = client.post(FINDER_URL, data={
            "ait_action": "loadModelsFuels", "type": "CAR",
            "brand": brand, "model": model,
        })
        r.raise_for_status()
        data = r.json()
        html = data.get("fuels", "")
        out = []
        for v, t in OPTION_RE.findall(html):
            v = v.strip()
            t = t.strip()
            if v and t and "select" not in t.lower():
                out.append((v, t))
        return out

