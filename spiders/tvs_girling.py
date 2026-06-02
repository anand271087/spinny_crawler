"""TVS Girling spider — both URLs (URL 1 cracked 2026-05-19 via Playwright).

TWO source URLs (merged into one tvs_girling output file):

  URL 1: https://partscatalogue.brakesindia.com/ (Brakes India parts catalogue, ASP.NET
         WebForms, PV gate via Segment dropdown).
         Fields: item_name, item_code, MRP, vehicle_compatibility.

  URL 2: https://www.tvsgirling.com/passenger-cars-scv/ (marketing site, 3 sub-brands).
         Fields: item_name only.

Output convention: rows from both URLs go into one tvs_girling_<YYYYMMDD>.csv.
`source_website` column distinguishes origin.

URL 1 strategy (verified end-to-end 2026-05-19):

The cascade ddMake→ddSegment→ddModel→ddModelYear uses ASP.NET UpdatePanel partial
postbacks. httpx replays failed (500 from server — likely missing some script-manager
field). So we drive the cascade with Playwright (no captcha, no login, just dropdown
changes), then plain GET the result and detail pages.

Discovered URL patterns:
  List:   /FrmProduct?Model=<mid>&Make=<mkid>&Year=-1&Segment=<sid>
  Detail: /HotspotView?Model=<mid>&Product=<code>&Year=-&Partnumber=<code>

Detail-page table 0 columns:
  BI Part Number | Part Description | Part Specification | MRP as on | Serviceability

Detail-page table N columns (Applicable Models — vehicle_compatibility):
  Make | Model | Assembly Part No.

Per BRD §6 passenger-vehicle gate: keep only Segments matching /CAR|MUV|SUV/i,
filter out 3-WHEELER/LCV/PICKUP/VAN/TRACTOR. Some Makes have HATCHBACK as a
separate segment — we include it (hatchbacks are PV).

Env vars (small defaults; raise to 0 = all for production):
  TVS_MAX_MAKES        default 0 = all 30+ Makes
  TVS_MAX_MODELS       default 0 = all per Make+Segment
"""

from __future__ import annotations

import logging
import os
import re

import httpx
from parsel import Selector
from playwright.sync_api import sync_playwright

from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.tvs_girling")

URL1 = "https://partscatalogue.brakesindia.com"
URL2 = "https://www.tvsgirling.com/passenger-cars-scv/"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TIMEOUT = 30.0

PV_SEGMENT_RX = re.compile(r"\b(car|muv|suv|hatchback)\b", re.IGNORECASE)
BLOCK_SEGMENT_RX = re.compile(r"\b(3\s*wheeler|wheeler|lcv|pickup|van|tractor|truck)\b", re.IGNORECASE)


class Spider(BaseSpider):

    def __init__(self, brand_key: str, brand_cfg: dict) -> None:
        super().__init__(brand_key, brand_cfg)
        self.max_makes = int(os.environ.get("TVS_MAX_MAKES", "0") or "0")
        self.max_models = int(os.environ.get("TVS_MAX_MODELS", "0") or "0")

    def crawl(self) -> list[Row]:
        rows: list[Row] = []
        seen: dict[str, dict] = {}  # part_code → {name, mrp, compat:set[str]}

        # ---- URL 1: drive cascade via Playwright, fetch list+detail via httpx ----
        cookies_for_httpx: dict[str, str] = {}
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = browser.new_context(user_agent=UA, locale="en-US", timezone_id="Asia/Kolkata")
            page = ctx.new_page()
            try:
                page.goto(URL1 + "/", wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(3000)

                makes = self._dd_options(page, "ddMake")
                log.info("URL1: %d Makes available", len(makes))
                if self.max_makes:
                    makes = makes[: self.max_makes]

                # Reuse Playwright cookies for httpx (server may need session cookie)
                for c in ctx.cookies():
                    cookies_for_httpx[c["name"]] = c["value"]

                for make_val, make_name in makes:
                    page.select_option('select[name="ctl00$MainContent$ddMake"]', value=make_val)
                    page.wait_for_timeout(2500)
                    segments = self._dd_options(page, "ddSegment")
                    pv_segments = [(v, t) for v, t in segments
                                   if PV_SEGMENT_RX.search(t or "")
                                   and not BLOCK_SEGMENT_RX.search(t or "")]
                    log.info("Make=%s → %d segments, %d PV",
                             make_name, len(segments), len(pv_segments))

                    for seg_val, seg_name in pv_segments:
                        page.select_option('select[name="ctl00$MainContent$ddSegment"]', value=seg_val)
                        page.wait_for_timeout(2500)
                        models = self._dd_options(page, "ddModel")
                        if self.max_models:
                            models = models[: self.max_models]
                        log.info("  %s/%s → %d models", make_name, seg_name, len(models))

                        with httpx.Client(headers={"User-Agent": UA, "Referer": URL1 + "/"},
                                          cookies=cookies_for_httpx, timeout=TIMEOUT,
                                          follow_redirects=True) as cli:
                            for model_val, model_name in models:
                                list_url = (f"{URL1}/FrmProduct?Model={model_val}&Make={make_val}"
                                            f"&Year=-1&Segment={seg_val}")
                                lr = cli.get(list_url)
                                if lr.status_code != 200:
                                    continue
                                codes = self._parse_product_list(lr.text)
                                if not codes:
                                    continue
                                log.info("    %s %s → %d products", make_name, model_name, len(codes))

                                for code in codes:
                                    detail_url = (f"{URL1}/HotspotView?Model={model_val}"
                                                  f"&Product={code}&Year=-&Partnumber={code}")
                                    dr = cli.get(detail_url)
                                    if dr.status_code != 200:
                                        continue
                                    detail = self._parse_detail(dr.text)
                                    if not detail:
                                        continue
                                    entry = seen.get(detail["code"])
                                    compat_token = f"{make_name} | {model_name}"
                                    if entry:
                                        entry["compat"].add(compat_token)
                                        for am in detail.get("applicable_models") or []:
                                            entry["compat"].add(am)
                                    else:
                                        seen[detail["code"]] = {
                                            "name": detail["name"],
                                            "mrp": detail["mrp"],
                                            "compat": {compat_token, *(detail.get("applicable_models") or [])},
                                        }

                        # Reset Segment to placeholder so next iteration picks fresh
                        page.select_option('select[name="ctl00$MainContent$ddSegment"]', value="0")
                        page.wait_for_timeout(1000)
                    # Reset Make to placeholder
                    page.select_option('select[name="ctl00$MainContent$ddMake"]', value="0")
                    page.wait_for_timeout(1000)
            finally:
                browser.close()

        for code, info in seen.items():
            rows.append(Row(
                item_name=info["name"],
                item_code=code,
                mrp=info["mrp"],
                vehicle_compatibility="; ".join(sorted(info["compat"])),
            ))
        log.info("URL1: %d unique parts", len(rows))

        # ---- URL 2: marketing site (httpx, static) ----
        rows.extend(self._crawl_url2())
        return rows

    # ---------- helpers ----------

    @staticmethod
    def _dd_options(page, dd_name: str) -> list[tuple[str, str]]:
        return page.evaluate(f"""() => {{
            const s = document.querySelector('select[name="ctl00$MainContent${dd_name}"]');
            if (!s) return [];
            return Array.from(s.options).map(o => [o.value, o.text.trim()])
                .filter(([v,t]) => v && v !== '0' && v !== '-1' &&
                    !['select segment','select make','select model','model year/type'].includes(t.toLowerCase()));
        }}""")

    @staticmethod
    def _parse_product_list(html: str) -> list[str]:
        """Extract part codes from /FrmProduct?... result page.

        Each product rendered as: <p><strong>29933041&nbsp;-&nbsp;KIT PAD ASSEMBLY</strong></p>
        """
        codes: list[str] = []
        for m in re.finditer(r'<strong[^>]*>(\d{6,12})\s*(?:&nbsp;|\s|-){1,4}\s*[A-Z][^<]*</strong>', html):
            codes.append(m.group(1))
        seen = set()
        return [c for c in codes if not (c in seen or seen.add(c))]

    @staticmethod
    def _parse_detail(html: str) -> dict | None:
        sel = Selector(text=html)
        result: dict = {}
        for table in sel.css("table"):
            headers = [(h.xpath("normalize-space(.)").get() or "") for h in table.css("th")]
            joined = " | ".join(headers).lower()
            if "bi part number" in joined and "mrp" in joined:
                rows = table.css("tr")
                if len(rows) >= 2:
                    cells = [(c.xpath("normalize-space(.)").get() or "") for c in rows[1].css("td")]
                    if len(cells) >= 5:
                        result["code"] = cells[0].strip()
                        result["name"] = cells[1].strip()
                        rate_raw = cells[3].replace(",", "").strip()
                        try:
                            result["mrp"] = float(rate_raw) if rate_raw else None
                        except ValueError:
                            result["mrp"] = None
                break
        if not result:
            return None
        compat: list[str] = []
        for table in sel.css("table"):
            headers = [(h.xpath("normalize-space(.)").get() or "") for h in table.css("th")]
            joined = " | ".join(headers).lower()
            if "make" in joined and "model" in joined and "assembly" in joined:
                for tr in table.css("tr")[1:]:
                    tds = [(td.xpath("normalize-space(.)").get() or "") for td in tr.css("td")]
                    if len(tds) >= 2 and tds[0] and tds[1]:
                        compat.append(f"{tds[0]} | {tds[1]}")
                break
        result["applicable_models"] = compat
        return result

    # ---------- URL 2 ----------

    @staticmethod
    def _crawl_url2() -> list[Row]:
        rows: list[Row] = []
        try:
            with httpx.Client(headers={"User-Agent": UA}, timeout=TIMEOUT, follow_redirects=True) as cli:
                r = cli.get(URL2)
                if r.status_code != 200:
                    return rows
                sel = Selector(text=r.text)
                for title_node in sel.css("h2, h3, h4, .product-title, .elementor-heading-title"):
                    txt = (title_node.xpath("normalize-space(.)").get() or "").strip()
                    if not txt or len(txt) < 5 or len(txt) > 120:
                        continue
                    if any(s in txt.lower() for s in
                           ("menu", "home", "about", "contact", "career", "follow")):
                        continue
                    rows.append(Row(item_name=txt))
        except Exception as e:
            log.warning("URL2 fetch failed: %s", e)
        log.info("URL2: %d marketing items", len(rows))
        return rows
