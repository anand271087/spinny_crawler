"""GABRIEL spider — actually a pdf_brochure pattern (not multi_level_category as xlsx implied).

Plan ref §12.4. Site URL: https://www.anandgroupindia.com/gabrielindia/aftermarkets/?pcatid=all&vcatid=passenger-vehicle
xlsx flow: open URL → "go to Aftermarket tab and below is the catalogue" → extract 4W products.

Site reality (verified 2026-05-18):
- The aftermarkets landing page is a category-card showcase with marketing copy in 18 modal divs.
- ZERO SKUs / MRPs in the HTML.
- The "catalogue" referenced by the xlsx is the downloadable PDF linked from the page:
  https://www.anandgroupindia.com/wp-content/uploads/2025/09/GIL-All-Products-Catalogue-Jun25_compressed.pdf
- 111-page PDF, ANAND/Gabriel "GIL All Products Catalogue Jun25".
- Each product section is a table with: ITEM CODE | SUITABLE FOR/APPLICATION | STD PACK | MRP.

Scope decision (per BRD §3.2 — passenger vehicles only):
- Pages have segment-specific headers ("2W SHOCK ABSORBERS", "3W SHOCK ABSORBERS",
  "4W SHOCK ABSORBERS", "CV SHOCK ABSORBERS"). We accept pages whose header is
  4W-explicit OR non-segment-tagged (Struts, Bush Kit, Brake Pads, etc. — these are
  4W-relevant product groups per the PDF index). We reject 2W/3W/CV-explicit pages
  and known 2W-only product groups (Front Fork, Spokes, Wheel Rims, Cone Sets, Scooter CVT).
"""

from __future__ import annotations

import io
import logging
import re

import httpx
import pdfplumber

from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.gabriel")

USER_AGENT = "SpinnyOEMCrawler/1.0 (contact@spinny.com)"
LANDING_URL = "https://www.anandgroupindia.com/gabrielindia/aftermarkets/?pcatid=all&vcatid=passenger-vehicle"
# Resolved per the live page download link. If Gabriel publishes a new edition,
# the landing page's <a href="...wp-content/uploads/.../catalogue....pdf"> changes;
# discovery below handles that.
PDF_ANCHOR_RE = re.compile(r"https?://[^\"\s>]+/wp-content/uploads/[^\"\s>]+?[Cc]atalogue[^\"\s>]+?\.pdf", re.IGNORECASE)

# Section page-ranges from the PDF Index (page 2). 1-indexed, inclusive.
# Verified 2026-05-18 against GIL-All-Products-Catalogue-Jun25.
SECTION_PAGE_RANGES = [
    ("EV Products",          ( 4,  4)),  # Reviewed: 4W EV-shock units (battery-pack support). Include.
    ("2W Shock Absorbers",   ( 5,  9)),  # OUT — 2W
    ("3W Shock Absorbers",   (10, 11)),  # OUT — 3W
    ("4W Shock Absorbers",   (12, 18)),
    ("CV Shock Absorbers",   (19, 24)),  # OUT — CV
    ("Struts",               (25, 40)),
    ("Bush Kit",             (41, 46)),
    ("OC Spring",            (47, 47)),
    ("Front Fork Component", (48, 54)),  # OUT — 2W
    ("Front Fork Oil & Oil Seal", (55, 55)),  # OUT — 2W
    ("Gas Spring",           (56, 60)),
    ("Brake Fluid",          (61, 61)),
    ("Coolant",              (62, 63)),
    ("Suspension Bush Kits", (64, 68)),
    ("Suspension Parts",     (69, 77)),
    ("Brake Pads",           (78, 86)),
    ("Drive Shaft",          (87, 91)),
    ("Synchronizer Rings",   (92, 92)),
    ("Spokes",               (93, 93)),  # OUT — 2W
    ("Wheel Rims",           (94, 94)),  # OUT — 2W
    ("Alloy Wheels",         (95, 95)),
    ("Cone Sets",            (96, 97)),  # OUT — 2W
    ("Scooter CVT Products", (98, 99)),  # OUT — 2W
    ("Tyres & Tubes",        (100, 106)),  # Row-level filter — many are 3W/2W
]
OUT_OF_SCOPE_SECTIONS = {
    "2W Shock Absorbers", "3W Shock Absorbers", "CV Shock Absorbers",
    "Front Fork Component", "Front Fork Oil & Oil Seal",
    "Spokes", "Wheel Rims", "Cone Sets", "Scooter CVT Products",
}
# Row-level filter — drop rows whose SUITABLE FOR contains explicit 2W/3W/CV markers.
NON_4W_SUITABLE_RE = re.compile(r"\b(2W|3W|CV)\b")

ITEM_CODE_RE = re.compile(r"^AM-[A-Z0-9\-]+$")
PRICE_RE = re.compile(r"^\d{1,7}(?:\.\d{1,2})?$")


class Spider(BaseSpider):

    def crawl(self) -> list[Row]:
        with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=120) as client:
            pdf_url = self._discover_pdf_url(client)
            log.info("gabriel: catalogue PDF = %s", pdf_url)
            resp = client.get(pdf_url)
            resp.raise_for_status()
            log.info("gabriel: downloaded PDF (%d KB)", len(resp.content) // 1024)
            rows = self._parse_pdf(resp.content)
        return rows

    def _discover_pdf_url(self, client: httpx.Client) -> str:
        """Find the latest catalogue PDF link from the live landing page."""
        r = client.get(LANDING_URL)
        r.raise_for_status()
        m = PDF_ANCHOR_RE.search(r.text)
        if not m:
            raise RuntimeError("Gabriel catalogue PDF link not found on landing page")
        return m.group(0)

    def _parse_pdf(self, pdf_bytes: bytes) -> list[Row]:
        rows: list[Row] = []
        seen: set[str] = set()
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            page_to_section = self._build_page_section_map()
            for page in pdf.pages:
                section = page_to_section.get(page.page_number)
                if not section or section in OUT_OF_SCOPE_SECTIONS:
                    continue
                text = page.extract_text() or ""
                for code, suitable_for, mrp in self._extract_table_rows(text):
                    if code in seen:
                        continue
                    # Row-level 4W filter — catches mixed "Tyres & Tubes" 3W/2W entries
                    if NON_4W_SUITABLE_RE.search(suitable_for):
                        continue
                    seen.add(code)
                    rows.append(Row(
                        item_name=f"{section}: {suitable_for}".strip(),
                        item_code=code,
                        mrp=mrp,
                    ))
        log.info("gabriel: extracted %d in-scope rows from PDF", len(rows))
        return rows

    @staticmethod
    def _build_page_section_map() -> dict[int, str]:
        """Map page number → section name from SECTION_PAGE_RANGES (1-indexed)."""
        m: dict[int, str] = {}
        for name, (start, end) in SECTION_PAGE_RANGES:
            for p in range(start, end + 1):
                m[p] = name
        return m

    @staticmethod
    def _extract_table_rows(page_text: str) -> list[tuple[str, str, float | None]]:
        """Parse lines like 'AM-SG05091 AL BEAVER SA FR 4 2239.00' → (code, suitable_for, mrp).

        Splits on whitespace; first token must match ITEM_CODE_RE, last must be a price.
        The second-to-last is STD PACK (integer); everything between code and STD PACK
        is the SUITABLE FOR/APPLICATION description.
        """
        out: list[tuple[str, str, float | None]] = []
        for line in page_text.splitlines():
            s = line.strip()
            if not s:
                continue
            tokens = s.split()
            if len(tokens) < 4:
                continue
            code = tokens[0]
            if not ITEM_CODE_RE.match(code):
                continue
            # Find price (last token matching PRICE_RE)
            if not PRICE_RE.match(tokens[-1]):
                continue
            try:
                mrp = float(tokens[-1])
            except ValueError:
                continue
            # second-to-last should be STD PACK (small integer)
            std_pack = tokens[-2]
            try:
                int(std_pack)
                suitable_tokens = tokens[1:-2]
            except ValueError:
                # No STD PACK present — treat all middle tokens as suitable_for
                suitable_tokens = tokens[1:-1]
            suitable_for = " ".join(suitable_tokens).strip()
            if not suitable_for:
                continue
            out.append((code, suitable_for, mrp))
        return out
