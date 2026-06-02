"""Bosch spider — pdf_brochure pattern (Passenger Car rows from catalogue PDFs).

Plan ref §12.13. URL: https://ap.boschaftermarket.com/in/en/parts/
xlsx fields: item_name, item_code, mrp.

Site reality (verified 2026-05-18, deeper pass 2026-05-19):
- 17 product category landing pages at /in/en/parts/<category>/.
- 7 categories link to a catalogue PDF (spark plugs, brakes, gasoline, sensors, starters,
  wipers, diesel). `sensors` and `gasoline-parts` resolve to the SAME pdf — deduped.
- Bosch part-number formats vary by catalogue:
    spark plugs / brakes / diesel / gasoline / wipers → `N NNN NNN NNN` (10 digits with
    3 spaces). First digit varies: 0=spark/brake/diesel/gasoline, 3=wipers.
    starters / alternators → completely different (e.g. `1986A00576`, `F002G70212`).
- MRP column exists in the line-format catalogues; absent in the table-format ones.

Strategy:
1. GET /in/en/parts/, list all category page URLs.
2. For each category, find any *.pdf anchor containing "catalogue" or "catalog".
3. Dedupe PDF URLs (sensors == gasoline-parts).
4. Dispatch per-PDF parser:
     - LINE_FORMAT_CATEGORIES (spark-plugs, brakes, diesel-parts-and-components,
       gasoline-parts): line-based regex on `extract_text()`.
     - TABLE_FORMAT_WIPER ('wiper-blades'): structured 7-column table; brand-row +
       model-row pattern, filtered to passenger-vehicle makes.
     - TABLE_FORMAT_STARTER ('starters-and-alternators'): 17-column table; pull Bosch
       Part No. + Vehicle Manufacturer columns; PC pages only (per PDF ToC).
5. Rows from table-format catalogues have no MRP in PDF → finalize as `partial` per BRD §7.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Iterable

import httpx
import pdfplumber

from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.bosch")

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
BASE = "https://ap.boschaftermarket.com"
PARTS_INDEX = f"{BASE}/in/en/parts/"

PDF_HREF_RE = re.compile(r'href="([^"]+\.pdf)"', re.IGNORECASE)
CATALOGUE_HINT_RE = re.compile(r"catalog", re.IGNORECASE)

# Bosch part number in space-separated catalogues. First digit any 0-9.
# Matched: "0 242 NNN NNN" (spark plugs), "3 397 NNN NNN" (wipers).
BOSCH_PN_SPACED_RE = re.compile(r"\b\d\s\d{3}\s\d{3}\s\d{3}\b")
# Row regex for line-format catalogues
ROW_RE = re.compile(
    r"^(?P<model>.*?)\s+(?P<pn>\b\d\s\d{3}\s\d{3}\s\d{3}\b)\s+(?P<rest>.+?)$"
)
# MRP at end of line: a number (e.g., 650.00) or "On Request"
MRP_END_RE = re.compile(r"(?P<mrp>\d{1,5}(?:\.\d{1,2})?|On\s*Request)\s*$", re.IGNORECASE)
# Lines that look like vehicle-brand headers (1-2 all-caps words, no part number)
BRAND_HEADER_RE = re.compile(r"^[A-Z][A-Z\s&/\-]{1,30}$")
SKIP_BRANDS = {"PASSENGER CARS", "CONTENTS", "INDEX", "MRP", "STANDARD", "PREMIUM"}

# Passenger-vehicle make whitelist for table-format catalogues (BRD §3.2 PV scope).
# All matched case-insensitively. Excludes CV-only OEMs (ASHOK LEYLAND, EICHER,
# BHARAT BENZ) — they appear in the wiper PDF for LCV products like Dost.
PV_MAKES = {
    "audi", "bmw", "chevrolet", "citroen", "datsun", "fiat", "ford", "force motors",
    "honda", "hyundai", "jaguar", "jeep", "kia", "lamborghini", "land rover", "lexus",
    "mahindra", "maruti", "maruti suzuki", "mclaren", "mercedes benz", "mercedes-benz",
    "mg", "mini", "mitsubishi", "nissan", "opel", "porsche", "range rover", "renault",
    "rolls royce", "skoda", "ssangyong", "subaru", "suzuki", "tata", "toyota",
    "volkswagen", "volvo", "tesla", "isuzu", "hindustan motors", "hindustan motors limited",
    "hm", "fiat india", "general motors", "gm",
}

LINE_FORMAT_CATEGORIES = {
    "spark-plugs",
    "brakes",
    "diesel-parts-and-components",
    "gasoline-parts",
    "sensors",  # alias for gasoline-parts (same PDF URL — deduped by URL anyway)
}


class Spider(BaseSpider):

    def crawl(self) -> list[Row]:
        rows: list[Row] = []
        seen_codes: set[str] = set()
        seen_pdf_urls: set[str] = set()
        with httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=120,
            verify=False,  # Bosch India cert chain issues on some clients
        ) as client:
            catalogues = self._discover_catalogues(client)
            log.info("bosch: discovered %d catalogue PDFs across categories", len(catalogues))
            for category, pdf_url in catalogues.items():
                if pdf_url in seen_pdf_urls:
                    log.info("bosch: %s → duplicate URL of earlier catalogue (skip)", category)
                    continue
                seen_pdf_urls.add(pdf_url)
                try:
                    cat_rows = self._parse_catalogue(client, category, pdf_url, seen_codes)
                    log.info("bosch: %s → %d rows", category, len(cat_rows))
                    rows.extend(cat_rows)
                except Exception as exc:
                    log.warning("bosch: %s failed: %s", category, exc)
        return rows

    def _discover_catalogues(self, client: httpx.Client) -> dict[str, str]:
        """Walk /in/en/parts/<category>/ pages; collect any catalogue.pdf link."""
        idx = client.get(PARTS_INDEX)
        idx.raise_for_status()
        cat_urls = re.findall(r'href="(/in/en/parts/[a-z0-9\-]+/)"', idx.text)
        cat_urls = sorted(set(cat_urls))
        out: dict[str, str] = {}
        for path in cat_urls:
            category = path.strip("/").split("/")[-1]
            try:
                r = client.get(f"{BASE}{path}")
            except httpx.HTTPError:
                continue
            if r.status_code != 200:
                continue
            for pdf in PDF_HREF_RE.findall(r.text):
                if CATALOGUE_HINT_RE.search(pdf):
                    out[category] = pdf if pdf.startswith("http") else f"{BASE}{pdf}"
                    break
        return out

    def _parse_catalogue(self, client: httpx.Client, category: str, pdf_url: str,
                         seen_codes: set[str]) -> list[Row]:
        r = client.get(pdf_url)
        if r.status_code != 200:
            return []
        pdf_bytes = r.content
        if category in LINE_FORMAT_CATEGORIES:
            return self._parse_line_format(category, pdf_bytes, seen_codes)
        if category == "wiper-blades":
            return self._parse_wiper_table(pdf_bytes, seen_codes)
        if category == "starters-and-alternators":
            return self._parse_starter_table(pdf_bytes, seen_codes)
        log.warning("bosch: %s has no registered parser (unknown layout)", category)
        return []

    # ------------------------------------------------------------------ line-format
    def _parse_line_format(self, category: str, pdf_bytes: bytes,
                           seen_codes: set[str]) -> list[Row]:
        rows: list[Row] = []
        in_passenger_section = False
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if "passenger car" in text.lower():
                    in_passenger_section = True
                if re.search(r"\b(two[\s-]?wheel|commercial vehicle|cross reference chart)\b",
                             text, re.IGNORECASE):
                    if in_passenger_section and "passenger car" not in text.lower():
                        in_passenger_section = False
                if not in_passenger_section:
                    continue
                for parsed in self._parse_lines(text):
                    code = parsed["item_code"]
                    if code in seen_codes:
                        continue
                    seen_codes.add(code)
                    rows.append(Row(
                        item_name=parsed["item_name"],
                        item_code=code,
                        mrp=parsed["mrp"],
                        compatible_car_model=parsed["model"],
                    ))
        return rows

    @staticmethod
    def _parse_lines(text: str) -> Iterable[dict]:
        for line in text.splitlines():
            s = line.strip()
            if not s or not BOSCH_PN_SPACED_RE.search(s):
                continue
            m = ROW_RE.match(s)
            if not m:
                continue
            model = m.group("model").strip()
            pn = m.group("pn").strip()
            rest = m.group("rest").strip()

            mm = MRP_END_RE.search(rest)
            mrp_val: float | None = None
            if mm:
                token = mm.group("mrp")
                if token.lower().replace(" ", "") != "onrequest":
                    try:
                        mrp_val = float(token)
                    except ValueError:
                        mrp_val = None
                rest = rest[:mm.start()].strip()

            yield {
                "item_code": pn,
                "item_name": rest if rest else f"Bosch Part {pn}",
                "model": model,
                "mrp": mrp_val,
            }

    # ----------------------------------------------------------------- wiper-table
    def _parse_wiper_table(self, pdf_bytes: bytes, seen_codes: set[str]) -> list[Row]:
        """Wiper catalogue table format (7 cols):
            [Brand-description, Size, Classic Driver, Classic Passenger,
             Set, ClearAdvantage Driver, ClearAdvantage Passenger]
        Brand-header rows (first cell = make in caps, others blank) set current make.
        Data rows have a model description in col 0; columns 2..6 contain Bosch part
        numbers — one row per non-blank (model, part_number) pair.
        """
        rows: list[Row] = []
        WIPER_VARIANTS = ("Classic Driver", "Classic Passenger", "Set",
                          "ClearAdvantage Driver", "ClearAdvantage Passenger")
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            current_make: str | None = None
            for page in pdf.pages:
                for table in page.extract_tables() or []:
                    if not table or len(table) < 3:
                        continue
                    # Header row check
                    header_text = " ".join(str(c or "") for c in table[0]).lower()
                    if "brand" not in header_text and "wiper" not in header_text:
                        continue
                    # Walk data rows
                    for raw in table[2:]:  # skip header + sub-header
                        cells = [(c or "").strip() for c in raw]
                        if not any(cells):
                            continue
                        # Brand-header row: first cell non-empty, others empty
                        non_empty = [c for c in cells if c]
                        if len(non_empty) == 1 and self._is_make_header(non_empty[0]):
                            current_make = self._normalize_make(non_empty[0])
                            continue
                        # Sometimes a brand header is split across cells (e.g.
                        # "HINDUSTAN MOTORS LIMITED (H" + "ML)" in adjacent cells).
                        # Merge col0..col1 if col1 is short and all-uppercase-ish.
                        merged_first = self._maybe_merge_make_header(cells)
                        if merged_first and self._is_make_header(merged_first):
                            current_make = self._normalize_make(merged_first)
                            continue
                        if current_make is None or current_make.lower() not in PV_MAKES:
                            continue  # filter to PV scope
                        # Data row: col0 = model description (multi-line ok), 2..6 = part #s
                        model_desc = cells[0]
                        if not model_desc:
                            continue
                        for col_idx, variant in zip((2, 3, 4, 5, 6), WIPER_VARIANTS):
                            if col_idx >= len(cells):
                                continue
                            pn = cells[col_idx].strip()
                            # Strip newlines/extra whitespace inside part numbers
                            pn = " ".join(pn.split())
                            if not BOSCH_PN_SPACED_RE.fullmatch(pn):
                                continue
                            if pn in seen_codes:
                                continue
                            seen_codes.add(pn)
                            # Clean up model description: collapse newlines
                            model_clean = " ".join(model_desc.split())
                            rows.append(Row(
                                item_name=f"Bosch {variant} Wiper Blade — {model_clean[:80]}",
                                item_code=pn,
                                mrp=None,  # MRP not in wiper PDF → partial
                                compatible_car_model=f"{current_make} {model_clean}".strip()[:200],
                            ))
        return rows

    # ---------------------------------------------------------------- starter-table
    def _parse_starter_table(self, pdf_bytes: bytes, seen_codes: set[str]) -> list[Row]:
        """Starter/alternator catalogue (17-column table). Find columns by header text.
        PC section per ToC = pages 24-33 (1-indexed). We process ALL pages but skip
        any whose first table column 0 ("Brand Name") starts with non-PV makes; that
        captures the PC subset cleanly.
        """
        rows: list[Row] = []
        # Bosch starter/alternator codes seen on these tables:
        starter_pn_re = re.compile(
            r"\b("
            r"1986[A-Z]{1,2}\d{3,5}"          # 1986A00576, 1986AE0703
            r"|F00[0-9A-Z]{2,3}\d{3,6}"        # F002G70212, F00M131657
            r"|0\s?\d{3}\s?\d{3}\s?\d{3}"      # 0 124 555 056 (with/without spaces)
            r"|\d{10,13}"                       # 1986A00610 or 9000033015
            r")\b"
        )
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            current_make: str | None = None
            in_pc = False  # passenger-cars section flag (per ToC layout)
            for pi, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                if re.search(r"\bpassenger car", text, re.IGNORECASE):
                    in_pc = True
                if re.search(r"\b(light commercial vehicle|heavy commercial vehicle|3[\-\s]?wheeler|tractors?|gensets?|successor list|obsolete part|imported parts)\b",
                             text, re.IGNORECASE) and in_pc:
                    # PC section ends when these headers reappear
                    if not re.search(r"\bpassenger car", text, re.IGNORECASE):
                        in_pc = False
                if not in_pc:
                    continue

                for table in page.extract_tables() or []:
                    if not table or len(table) < 2:
                        continue
                    # Identify header row; locate the Bosch Part No. column index
                    header_row = next((r for r in table if r and any(
                        c and "bosch" in (c or "").lower() and "part" in (c or "").lower()
                        for c in r
                    )), None)
                    if not header_row:
                        continue
                    pn_col = None
                    brand_col = None
                    app_col = None
                    vmr_col = None  # Vehicle Manufacturer's Reference No.
                    for ci, cell in enumerate(header_row):
                        cell_l = (cell or "").lower()
                        if "bosch" in cell_l and "part" in cell_l and pn_col is None:
                            pn_col = ci
                        elif "brand" in cell_l and brand_col is None:
                            brand_col = ci
                        elif "application" in cell_l and app_col is None:
                            app_col = ci
                        elif "manufacture" in cell_l and "reference" in cell_l and vmr_col is None:
                            vmr_col = ci
                    if pn_col is None:
                        continue
                    # Now iterate data rows past the header
                    started = False
                    for raw in table:
                        if not started:
                            if raw is header_row:
                                started = True
                            continue
                        cells = [(c or "").strip() for c in raw]
                        if not any(cells):
                            continue
                        # Track brand if present
                        if brand_col is not None and brand_col < len(cells) and cells[brand_col]:
                            cand = self._normalize_make(cells[brand_col])
                            if self._is_make_header(cells[brand_col]):
                                current_make = cand
                        if pn_col >= len(cells):
                            continue
                        pn_raw = cells[pn_col]
                        # The Bosch Part No. cell might contain multiple part numbers
                        # separated by newline (variants). Take the first non-empty token.
                        for token in pn_raw.split("\n"):
                            tok = token.strip()
                            if not tok:
                                continue
                            m = starter_pn_re.search(tok)
                            if not m:
                                continue
                            pn = m.group(1)
                            if pn in seen_codes:
                                continue
                            seen_codes.add(pn)
                            app = cells[app_col] if app_col is not None and app_col < len(cells) else ""
                            vmr = cells[vmr_col] if vmr_col is not None and vmr_col < len(cells) else ""
                            model_text = " ".join((app or vmr).split())[:200]
                            make_label = (current_make or "").strip()
                            if make_label and make_label.lower() not in PV_MAKES:
                                continue  # PV filter
                            rows.append(Row(
                                item_name=(f"Bosch Starter/Alternator — {model_text}"
                                           if model_text else f"Bosch Part {pn}"),
                                item_code=pn,
                                mrp=None,  # MRP not in starter PDF → partial
                                compatible_car_model=(f"{make_label} {model_text}".strip()[:200]
                                                      if model_text or make_label else None),
                            ))
                            break  # one row per cell (first PN)
        return rows

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _is_make_header(text: str) -> bool:
        if not text:
            return False
        s = text.strip()
        if len(s) < 2 or len(s) > 60:
            return False
        # Must be mostly upper-case letters; allow &, -, parens, digits, spaces
        upper = sum(1 for c in s if c.isalpha() and c.isupper())
        letters = sum(1 for c in s if c.isalpha())
        if letters == 0:
            return False
        return (upper / letters) >= 0.7

    @staticmethod
    def _normalize_make(text: str) -> str:
        if not text:
            return ""
        s = " ".join(text.split())
        # Strip trailing notes in parens like "(HML)"
        s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
        # Drop trailing "LIMITED" / "LTD" / "INDIA" suffixes for cleaner make
        s = re.sub(r"\s+(LIMITED|LTD\.?|INDIA|MOTORS\s+LIMITED)\s*$", "", s, flags=re.IGNORECASE).strip()
        return s

    @staticmethod
    def _maybe_merge_make_header(cells: list[str]) -> str | None:
        """Merge cells[0] + cells[1] if cells[0] ends with an opening paren and
        cells[1] closes it (e.g. 'HINDUSTAN MOTORS LIMITED (H' + 'ML)')."""
        if len(cells) < 2:
            return None
        a, b = cells[0], cells[1]
        if not a or not b:
            return None
        if "(" in a and ")" in b and not any(cells[2:]):
            return f"{a}{b}".strip()
        return None
