"""Spark Minda spider — v1.0 brand (cracked 2026-05-19; was form-driven stub).

Site: https://mcl-aftermarket.com/Product/ProductServices
xlsx fields: item_name, item_code, mrp.

Site reality (verified 2026-05-19):

Static HTML site (no JS needed). URL pattern:
   /Product/ProductServices                          — segment landing (lists 14 segments)
   /Product/ProductSegment?id=<sid>                  — segment view, shows vehicle-type cards
   /Product/ProductDetail?typeid=<vt>&producGrouptId=<sid>  — product list for that segment+type

   `typeid` values:
     1 = 2 Wheeler
     2 = 4 Wheeler           ← BRD passenger-vehicle gate
     3 = 3 Wheeler (some segments)
     4 = Tractors
     ... (other commercial/special)

Per BRD passenger-vehicle gate: query with `typeid=2` always.

Per-product HTML structure (each `<div class="product-listing">` block):
   <h3> ITEM_NAME / ITEM_CODE</h3>
   <p><strong>Vehicle Type</strong> 4W</p>
   <p><strong>MRP ₹</strong><span> ₹ 159.57</span></p>

Field mapping per xlsx:
   item_name ← left side of " / " in <h3>
   item_code ← right side of " / " in <h3>
   mrp       ← float from <span> after "MRP ₹"

Segments (from /Product/ProductServices, IDs visible as Download-Brochure links):
   1=AUTO ELECTRICALS, 2=BEARING, 3=BRAKE SHOE & PADS, 4=CONTROL CABLE,
   5=CAPACITOR DISCHARGE IGNITION, 6=CLUTCH PLATES, 8=?, 9=FILTERS,
   10=INSTRUMENT, 11=LOCKS, 31=?, 32=WIRING HARNESS, 41=LUBRICANTS,
   44=ABS PARTS, 46=SPARK MINDA HELMET (+more) — spider auto-discovers all

Volume estimate:
   ~15 segments × ~50-400 products each = ~1K-5K 4W products
   Static HTML, ~0.5s per page → ~10 seconds full crawl.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

import httpx
from parsel import Selector

from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.spark_minda")

BASE = "https://mcl-aftermarket.com"
SERVICES_URL = f"{BASE}/Product/ProductServices"

UA = "SpinnyOEMCrawler/1.0 (contact@spinny.com)"
TIMEOUT = 30.0

# BRD passenger-vehicle gate: typeid=2 = 4 Wheeler
TYPEID_4W = 2


class Spider(BaseSpider):

    def crawl(self) -> list[Row]:
        rows: list[Row] = []
        seen: set[tuple[str, str]] = set()  # (item_code, item_name) dedup

        with httpx.Client(headers={"User-Agent": UA}, timeout=TIMEOUT, follow_redirects=True) as cli:
            # 1. Discover segment IDs from /Product/ProductServices landing page
            r = cli.get(SERVICES_URL)
            if r.status_code != 200:
                log.error("services landing failed: %s", r.status_code)
                return rows
            segments = self._extract_segments(r.text)
            log.info("Spark Minda: %d segments discovered", len(segments))

            # 2. For each segment, fetch 4W product list
            for sid, sname in segments:
                url = f"{BASE}/Product/ProductDetail?typeid={TYPEID_4W}&producGrouptId={sid}"
                try:
                    pr = cli.get(url)
                except Exception as e:
                    log.warning("seg %s fetch err: %s", sid, e)
                    continue
                if pr.status_code != 200:
                    continue
                products = list(self._parse_products(pr.text))
                log.info("  seg=%s (%s) → %d 4W products", sid, sname, len(products))

                for p in products:
                    key = (p["item_code"], p["item_name"])
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(Row(
                        item_name=p["item_name"],
                        item_code=p["item_code"],
                        mrp=p["mrp"],
                    ))

        log.info("Spark Minda: %d unique 4W products", len(rows))
        return rows

    # ---------- helpers ----------

    @staticmethod
    def _extract_segments(html: str) -> list[tuple[int, str]]:
        """From the /Product/ProductServices page, get all (segment_id, segment_name).

        The page renders one card per segment, each with:
          <a href="/Product/ProductSegment?id=N">Download Brochure</a>
        Adjacent text contains the segment name. We pair them up by following each
        segment-id back to its name via the nav menu list which appears verbatim.
        """
        ids = sorted({int(m.group(1)) for m in re.finditer(
            r'href="/Product/ProductSegment\?id=(\d+)"', html
        )})
        # Also keep the names. The nav HTML has e.g. <a href="?id=1">AUTO ELECTRICALS</a>
        names: dict[int, str] = {}
        for m in re.finditer(
            r'href="[^"]*ProductSegment\?id=(\d+)"[^>]*>\s*([^<]+?)\s*<',
            html,
        ):
            sid = int(m.group(1))
            name = m.group(2).strip()
            # Skip generic anchors like "Download Brochure"
            if name.lower() != "download brochure" and sid not in names:
                names[sid] = name
        return [(sid, names.get(sid, f"segment-{sid}")) for sid in ids]

    @staticmethod
    def _parse_products(html: str) -> Iterable[dict]:
        sel = Selector(text=html)
        for block in sel.css("div.product-listing"):
            h3 = (block.css("h3::text").get() or "").strip()
            if not h3:
                continue
            # Expect "<name> / <code>"
            if " / " in h3:
                name, code = h3.rsplit(" / ", 1)
            else:
                # Sometimes the slash isn't surrounded by spaces
                m = re.match(r"^(.*?)\s*/\s*(\S.*)$", h3)
                if m:
                    name, code = m.group(1), m.group(2)
                else:
                    name, code = h3, ""
            name = name.strip()
            code = code.strip()
            # MRP — `<p><strong>MRP ₹</strong><span> ₹ 159.57</span></p>`
            mrp_text = block.xpath('.//p[strong[contains(.,"MRP")]]//span/text()').get() or ""
            mrp_text = mrp_text.replace("₹", "").replace(",", "").strip()
            try:
                mrp = float(mrp_text) if mrp_text else None
            except ValueError:
                mrp = None
            if not code:
                # If parser couldn't split, treat full text as item_name only
                continue
            yield {"item_name": name, "item_code": code, "mrp": mrp}
