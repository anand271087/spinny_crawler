"""AUTOKOI spider — HYBRID: HTML walk for item_names + PDF for MRP coverage.

Plan ref §12.12. URL: https://www.autokoi.com/products/
xlsx fields: item_name, item_code, mrp.

2026-05-22 update — pivoted from partial-status (no MRP) to FULL MRP coverage:
- Stakeholder confirmed: autokoi.com/ has an "E-Catalogue" footer link to a
  monthly-updated PDF (currently `autokoi-catalouge-with-mrp-2025.pdf`, ~15 MB)
  that lists EVERY part number with MRP.
- PDF has 1,382 unique codes with MRP; the HTML /products/ walk only surfaces 98.
- We now use BOTH sources: HTML walk for clean item_names (where available),
  PDF for full code+MRP catalogue.

Source-of-truth dispatch per code:
- In BOTH HTML + PDF (~93 codes): item_name from HTML (more descriptive),
  MRP from PDF → status=success.
- HTML-only (~5 codes): item_name from HTML, MRP=None → status=partial.
- PDF-only (~1,289 codes): item_name derived from PDF section + make headers
  (e.g., "Autokoi Steering & Suspension Parts (Maruti Suzuki) KMSF1081"),
  MRP from PDF → status=success.

Total expected: ~1,382 rows, of which ~1,377 ship as success and ~5 as partial.

See [docs/per_site_notes.md §17](../docs/per_site_notes.md) for breakage history.
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path

import httpx
from parsel import Selector

from lib.autokoi_pdf import (
    build_item_name,
    download_pdf,
    fetch_ecatalogue_url,
    parse_pdf,
)
from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.autokoi")

USER_AGENT = "SpinnyOEMCrawler/1.0 (contact@spinny.com)"
PRODUCTS_URL = "https://www.autokoi.com/products/"
SKU_RE = re.compile(r"\b[A-Z]{2,5}\d{4,8}\b")


class Spider(BaseSpider):

    def crawl(self) -> list[Row]:
        # --- Phase 1: HTML walk for item_names per /product/<slug>/ page
        html_items: dict[str, str] = {}  # {code: item_name}
        with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30) as c:
            r = c.get(PRODUCTS_URL)
            r.raise_for_status()
            sel = Selector(r.text)
            product_urls = sorted(set(
                u for u in sel.css("a::attr(href)").getall()
                if u and "/product/" in u and u.endswith("/")
            ))
            log.info("autokoi: %d product categories from HTML walk", len(product_urls))
            for url in product_urls:
                try:
                    rd = c.get(url)
                except httpx.HTTPError:
                    continue
                if rd.status_code != 200:
                    continue
                sel_d = Selector(rd.text)
                title = (sel_d.css("title::text").get() or "").strip()
                product_name = (title.split("-")[0].strip() if title
                                else url.rstrip("/").split("/")[-1].upper())
                main_html = (sel_d.xpath("//main").get()
                             or sel_d.xpath("//article").get()
                             or sel_d.xpath("//div[@class='entry-content']").get()
                             or rd.text)
                skus = sorted(set(SKU_RE.findall(
                    Selector(main_html).xpath("string(.)").get() or "")))
                for sku in skus:
                    if sku not in html_items:
                        html_items[sku] = f"{product_name} {sku}"
            log.info("autokoi: HTML walk → %d codes with item_names", len(html_items))

            # --- Phase 2: PDF for MRPs (and PDF-only codes)
            try:
                pdf_url = fetch_ecatalogue_url(c)
                log.info("autokoi: e-catalogue URL = %s", pdf_url)
                cache = Path(tempfile.gettempdir()) / "spinny_autokoi_ecat.pdf"
                pdf_path = download_pdf(c, pdf_url, cache)
                pdf_index = parse_pdf(pdf_path)
                log.info("autokoi: PDF walk → %d codes with MRP", len(pdf_index))
            except Exception as exc:
                log.error("autokoi: PDF parse failed: %s — falling back to HTML-only (MRP missing)", exc)
                pdf_index = {}

        # --- Phase 3: merge per code
        rows: list[Row] = []
        all_codes = set(html_items) | set(pdf_index)
        success = partial = 0
        for code in sorted(all_codes):
            entry = pdf_index.get(code)
            html_name = html_items.get(code)
            if entry and html_name:
                # Best: spider name + PDF MRP
                rows.append(Row(item_name=html_name, item_code=code, mrp=float(entry.mrp_inr)))
                success += 1
            elif entry:
                # PDF-only: derive name from section + make headers
                rows.append(Row(
                    item_name=build_item_name(entry),
                    item_code=code,
                    mrp=float(entry.mrp_inr),
                ))
                success += 1
            else:
                # HTML-only: no MRP available (rare; ~5 codes)
                rows.append(Row(item_name=html_name, item_code=code, mrp=None))
                partial += 1

        log.info("autokoi: %d total rows (%d success with MRP, %d partial)",
                 len(rows), success, partial)
        return rows
