"""JK Tyre spider — v1.0 brand (cracked 2026-05-19; was Next.js RSC stub).

Site: https://www.jktyre.com/ (Next.js 13+ App Router + RSC).
xlsx fields: item_name, compatible_car_model, tyre_sizes.

Site reality (verified 2026-05-19):

The "tyre finder" widget on the homepage is a heavy react-select cascade
(Make × Model × Variant). Initially declared as a Next.js RSC reverse-engineering
challenge — but the site ALSO exposes a clean static URL tree under /pcr/:

   /pcr                       — landing; lists 3 segments + Make list per segment
   /pcr/<segment>             — segment landing
   /pcr/<segment>/<make>      — make page; lists all compatible tyres with
                                  "VIEW TYRE DETAILS" links
   /pcr/<segment>/<make>/tyre-details/<tyre-slug>  — tyre detail page

The detail page contains an AVAILABLE SIZE(S) section with all sizes for that
tyre. No login, no cookies, no JS needed — plain httpx + parsel works.

Segments and Makes (per BRD passenger-vehicle gate — implicit in /pcr tree):
  SUV/MUV    → BMW, BYD, CITROEN, FORD, HONDA, HYUNDAI, ISUZU, JEEP, KIA,
                LAND ROVER, MAHINDRA, MARUTI SUZUKI, MORRIS GARAGES, NISSAN,
                RENAULT, SKODA, TATA, TOYOTA, VOLKSWAGEN
  SEDAN      → AUDI, BMW, GM, HONDA, HYUNDAI, JAGUAR, MAHINDRA, MARUTI SUZUKI,
                MERCEDES, RENAULT, SKODA, TATA, TOYOTA, VOLKSWAGEN, VOLVO
  HATCHBACK  → CITROEN, DATSUN, FIAT, FORD, GM, HONDA, HYUNDAI, MARUTI SUZUKI,
                MINI COOPER, MORRIS GARAGES, NISSAN, RENAULT, TATA, TOYOTA,
                VOLKSWAGEN

Field mapping:
  item_name           ← detail page H1, e.g. "UX ROYALE SMART - EMBEDDED SMART TYRE"
  compatible_car_model ← "<segment> | <make>" derived from URL
  tyre_sizes          ← "AVAILABLE SIZE(S)" section, comma-joined
                          (e.g. "165/80R14, 185/65R15, 215/60R16, 215/60R17")

Per-tyre dedup: same tyre name (e.g. UX ROYALE) appears under many (segment, make)
combos. Dedup on tyre-slug; merge compatible_car_model into a "; "-joined list.

Volume estimate:
  ~50 (segment × make) combos × ~6 tyres each ≈ 300 detail pages
  ~0.5 s per page → ~3 minutes full crawl.
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import urljoin, urlparse

from parsel import Selector
from playwright.sync_api import sync_playwright

from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.jk_tyre")

BASE = "https://www.jktyre.com"
PCR_URL = f"{BASE}/pcr"

UA = "SpinnyOEMCrawler/1.0 (contact@spinny.com)"
TIMEOUT = 30.0

TYRE_SIZE_RX = re.compile(
    r"\b(?:\d{3}/\d{2}\s*R\s*\d{1,2}|\d{2,3}x\d{1,2}(?:\.\d)?R\s*\d{1,2})\b",
    re.IGNORECASE,
)


class Spider(BaseSpider):

    def __init__(self, brand_key: str, brand_cfg: dict) -> None:
        super().__init__(brand_key, brand_cfg)
        self.max_combos = int(os.environ.get("JKTYRE_MAX_COMBOS", "0") or "0")

    def crawl(self) -> list[Row]:
        rows: list[Row] = []
        seen: dict[str, dict] = {}  # tyre_slug → {name, compat, sizes}

        # JK Tyre's TLS chain is incomplete server-side — strict CA verification
        # fails on stock Python. Playwright's bundled Chromium handles it fine.
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            # jktyre.com's TLS chain is incomplete server-side. Skip strict cert
            # verification for THIS site only (other spiders are unaffected).
            ctx = browser.new_context(
                user_agent=UA, viewport={"width": 1920, "height": 1080},
                locale="en-US", timezone_id="Asia/Kolkata",
                ignore_https_errors=True,
            )
            try:
                r = ctx.request.get(PCR_URL, timeout=TIMEOUT * 1000)
                if r.status != 200:
                    log.error("/pcr fetch failed: %s", r.status)
                    return rows
                sel = Selector(text=r.text())
                combos = self._enumerate_make_pages(sel)
                log.info("JK Tyre: %d (segment, make) combos", len(combos))
                if self.max_combos:
                    combos = combos[: self.max_combos]

                for seg, make, page_url in combos:
                    try:
                        pr = ctx.request.get(page_url, timeout=TIMEOUT * 1000)
                    except Exception as e:
                        log.warning("Make page %s failed: %s", page_url, e)
                        continue
                    if pr.status != 200:
                        continue
                    psel = Selector(text=pr.text())
                    detail_urls = self._extract_detail_urls(psel)
                    log.info("  %s/%s -> %d tyres", seg, make, len(detail_urls))

                    for d_url in detail_urls:
                        slug = urlparse(d_url).path.rsplit("/", 1)[-1]
                        compat_token = f"{seg} | {make}"
                        if slug in seen:
                            seen[slug]["compat"].add(compat_token)
                            continue
                        try:
                            dr = ctx.request.get(d_url, timeout=TIMEOUT * 1000)
                        except Exception:
                            continue
                        if dr.status != 200:
                            continue
                        detail = self._parse_detail(dr.text())
                        if not detail:
                            continue
                        seen[slug] = {
                            "name": detail["name"],
                            "compat": {compat_token},
                            "sizes": set(detail["sizes"]),
                        }
            finally:
                browser.close()

        for slug, info in seen.items():
            rows.append(Row(
                item_name=info["name"],
                compatible_car_model="; ".join(sorted(info["compat"])),
                tyre_sizes=", ".join(sorted(info["sizes"])) if info["sizes"] else None,
            ))
        log.info("JK Tyre: %d unique tyres", len(rows))
        return rows

    # ---------- helpers ----------

    @staticmethod
    def _enumerate_make_pages(sel: Selector) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        for a in sel.css('a[href*="/pcr/"]'):
            href = a.css("::attr(href)").get() or ""
            text = (a.xpath("normalize-space(.)").get() or "").strip()
            if not href or not text:
                continue
            full = href if href.startswith("http") else urljoin(BASE, href)
            path = urlparse(full).path.strip("/")
            parts = path.split("/")
            if len(parts) != 3 or parts[0] != "pcr":
                continue
            seg = parts[1]
            make_slug = parts[2]
            if make_slug in ("view-all", ""):
                continue
            seg_display = {"suv": "SUV/MUV", "sedan": "SEDAN", "hatchback": "HATCHBACK"}.get(seg, seg.upper())
            make_display = text.upper().replace(" AND ", " & ")
            out.append((seg_display, make_display, full))
        seen_urls = set()
        return [(s, m, u) for s, m, u in out if not (u in seen_urls or seen_urls.add(u))]

    @staticmethod
    def _extract_detail_urls(sel: Selector) -> list[str]:
        urls: list[str] = []
        for a in sel.css('a[href*="/tyre-details/"]'):
            href = a.css("::attr(href)").get() or ""
            if href:
                full = href if href.startswith("http") else urljoin(BASE, href)
                urls.append(full)
        seen = set()
        return [u for u in urls if not (u in seen or seen.add(u))]

    @staticmethod
    def _parse_detail(html: str) -> dict | None:
        sel = Selector(text=html)
        name = sel.css("h1::text").get() or sel.css("h2::text").get()
        if not name:
            return None
        name = re.sub(r"\s+", " ", name).strip()
        body_text = sel.xpath("string(//body)").get() or ""
        sizes_raw = TYRE_SIZE_RX.findall(body_text)
        sizes_norm = list(dict.fromkeys(re.sub(r"\s+", "", s).upper() for s in sizes_raw))
        return {"name": name, "sizes": sizes_norm}
