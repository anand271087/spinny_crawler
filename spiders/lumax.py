"""LUMAX spider — pdf_brochure pattern (reclassified from multi_level_category).

Plan ref §12.16. URL: https://www.lumaxworld.in/aftermarket/product-catalogue.html
xlsx fields: item_name, item_code (no MRP listed).

Site reality (verified 2026-05-18):
- The Product Catalogue page has ONLY PDF download links — no on-page product data.
- 4W catalogue is split across several PDFs (Bulb, Electrical, Lubricants, Mirror, etc.)
  plus a consolidated "4W Price List as on Date - NOV 25" that includes Material Code,
  Description, MRP. We use this consolidated price list as the canonical source.

PDF schema per row: `Material Code | Parts Nos | Description | HSN Nos | MRP | STD PKG | MOQ`
Material Code = 8-digit stable SKU.
"""

from __future__ import annotations

import io
import logging
import re

import httpx
import pdfplumber

from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.lumax")

USER_AGENT = "SpinnyOEMCrawler/1.0 (contact@spinny.com)"
LANDING_URL = "https://www.lumaxworld.in/aftermarket/product-catalogue.html"
PDF_ANCHOR_RE = re.compile(
    r'href="([^"]+4W-price-list[^"]+\.pdf)"', re.IGNORECASE,
)
# Row regex: 8-digit code, parts_no token, description (greedy until HSN), HSN 8-digit, MRP int, std_pkg int, moq int
ROW_RE = re.compile(
    r"^(?P<code>\d{8})\s+(?P<parts_no>\S+)\s+(?P<desc>.+?)\s+(?P<hsn>\d{8})\s+(?P<mrp>\d+)\s+(?P<std>\d+)\s+(?P<moq>\d+)\s*$"
)


class Spider(BaseSpider):

    def crawl(self) -> list[Row]:
        with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=120) as client:
            pdf_url = self._discover_price_list_url(client)
            log.info("lumax: price-list PDF = %s", pdf_url)
            r = client.get(pdf_url)
            r.raise_for_status()
            log.info("lumax: downloaded PDF (%d KB)", len(r.content) // 1024)
            rows = self._parse_pdf(r.content)
        return rows

    @staticmethod
    def _discover_price_list_url(client: httpx.Client) -> str:
        r = client.get(LANDING_URL)
        r.raise_for_status()
        m = PDF_ANCHOR_RE.search(r.text)
        if not m:
            raise RuntimeError("lumax: 4W price-list PDF link not found")
        href = m.group(1)
        return href if href.startswith("http") else f"https://www.lumaxworld.in/aftermarket/{href.lstrip('/')}"

    def _parse_pdf(self, pdf_bytes: bytes) -> list[Row]:
        rows: list[Row] = []
        seen: set[str] = set()
        # Vehicle context — track current MAKE/MODEL/SUB-CATEGORY headers as we scan lines
        current_make = ""
        current_model = ""
        current_subsec = ""
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                for line in text.splitlines():
                    s = line.strip()
                    if not s:
                        continue
                    m = ROW_RE.match(s)
                    if m:
                        code = m.group("code")
                        if code in seen:
                            continue
                        seen.add(code)
                        desc = m.group("desc").strip()
                        # Build a contextual name: "<MAKE> <MODEL> <SUBSEC>: <Description>"
                        context = " ".join(x for x in (current_make, current_model, current_subsec) if x).strip()
                        name = f"{context}: {desc}" if context else desc
                        rows.append(Row(
                            item_name=name,
                            item_code=code,
                            mrp=float(m.group("mrp")),
                        ))
                    else:
                        # Header detection — all-caps short lines update context
                        if 2 <= len(s) <= 50 and s == s.upper() and not s.startswith(("PRICE", "MATERIAL", "(", "PARTS", "HSN")):
                            # Heuristic: 1 word = MAKE or SUBSEC, 2+ words = SUBSEC unless after a MAKE
                            tokens = s.split()
                            if len(tokens) == 1:
                                # Could be MAKE (MARUTI, HONDA, TOYOTA) or MODEL (ALTO, SWIFT)
                                # Treat as MAKE if not preceded by MAKE; as MODEL otherwise
                                if not current_make:
                                    current_make = s.title()
                                    current_model = ""
                                else:
                                    current_model = s.title()
                                current_subsec = ""
                            else:
                                # Multi-word: likely a sub-section header (HEAD LAMP, TAIL LAMP)
                                current_subsec = s.title()
        log.info("lumax: extracted %d rows from PDF", len(rows))
        return rows
