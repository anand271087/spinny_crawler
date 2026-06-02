"""Parse Exide vehicular MRP PDF into {item_code: mrp_inr} dict.

Discovery chain (from exideindustries.com footer "MRP List" link):
    /mrp-list/default.aspx
      → /mrp-list/vehicular-and-two-wheeler-batteries.aspx
        → docs.exideindustries.com/pdf/mrp-list/mrcp-exide-vehicular-and-2wl-batteries.pdf

PDF layout:
- A4 single page (595×842 pt), TWO-column layout left/right of midline.
- Each column has category headers (e.g. "CAR/SUV: EXIDE EPIQ: 77M WARRANTY")
  followed by rows: <Ah>  <Code>  <Warranty>  <MRCP>
- Code may contain `(ISS)`, `(T1)`, `/L`, `/R`, etc. → don't be strict.
- We dedupe by code and keep only categories whose header mentions CAR or SUV
  (passenger-vehicle scope per BRD §3.2). EXIDE DRIVE is multi-segment but
  CAR/SUV is one of its segments, so it qualifies.

pdfplumber raw text comes out character-spaced (e.g. "E P I Q 3 5 L"). The
workaround is `extract_words(x_tolerance=10, y_tolerance=3)` which merges
characters into proper word tokens.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import httpx
import pdfplumber

log = logging.getLogger("lib.exide_pdf")

PDF_URL = "https://docs.exideindustries.com/pdf/mrp-list/mrcp-exide-vehicular-and-2wl-batteries.pdf"
USER_AGENT = "SpinnyOEMCrawler/1.0 (contact@spinny.com)"

# Match category header lines like:
#   "CAR/SUV: EXIDE EPIQ: 77M WARRANTY"
#   "CAR/SUV/3W/TRACTOR/CV: EXIDE DRIVE: 36M WARRANTY"
#   "CV: EXIDE XPRESS: 42M WARRANTY"
CATEGORY_RE = re.compile(
    r"^(?P<segments>[A-Z0-9/\-\s]+):\s*EXIDE\s+(?P<family>[A-Za-z0-9 +]+?)(?::\s*(?P<warranty>[^:]+))?$"
)

# Row pattern: <Ah> <code...> <warranty> <price>
# Warranty: "42F+35P" or "30F + 30P" or "24F" or "24M FOC"
# Code may contain (), /, +, -, letters, digits
ROW_RE = re.compile(
    r"^"
    r"(?P<ah>\d+(?:\.\d+)?)\s+"           # Ah column
    r"(?P<code>\S(?:.*?\S)?)\s+"          # Code (one or more non-space sequences)
    r"(?P<warranty>"                       # Warranty
    r"\d+\s*[FMP]\s*\+\s*\d+\s*[FMP]"     #   42F+35P
    r"|\d+\s*[FMP](?:\s+FOC)?"             #   24F or 24M FOC
    r")\s+"
    r"(?P<price>\d{1,3}(?:,\d{3})+|\d{4,})"  # Price (commas optional)
    r"\s*$"
)


@dataclass
class PdfRow:
    family: str          # e.g. "Exide Epiq"
    segments: str        # raw segments e.g. "CAR/SUV"
    code: str            # nomenclature, e.g. "EPIQ35L"
    warranty: str        # e.g. "42F+35P"
    ah: str              # capacity
    mrp_inr: float       # numeric


def download_pdf(dest: Path) -> Path:
    """Download the MRP PDF to dest. Caches if already present."""
    if dest.exists():
        log.info("PDF cached at %s (%d bytes)", dest, dest.stat().st_size)
        return dest
    with httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=60,
        follow_redirects=True,
    ) as client:
        resp = client.get(PDF_URL)
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        log.info("Downloaded MRP PDF: %d bytes → %s", len(resp.content), dest)
    return dest


def parse_pdf(pdf_path: Path) -> list[PdfRow]:
    """Extract all (family, code, mrp) tuples from the MRP PDF."""
    out: list[PdfRow] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            out.extend(_parse_page(page))
    return out


def _parse_page(page) -> list[PdfRow]:
    width = page.width
    col_split = width / 2  # vertical midline between L and R columns
    words = page.extract_words(x_tolerance=10, y_tolerance=3, keep_blank_chars=False)
    # Group by y-bucket (~4pt)
    lines: dict[int, list[dict]] = defaultdict(list)
    for w in words:
        line_key = round(w["top"] / 4)
        lines[line_key].append(w)
    # Build (left_text, right_text) pairs in y-order
    line_pairs: list[tuple[str, str]] = []
    for k in sorted(lines):
        items = sorted(lines[k], key=lambda x: x["x0"])
        left = [w for w in items if w["x0"] < col_split]
        right = [w for w in items if w["x0"] >= col_split]
        ltxt = " ".join(w["text"] for w in left).strip()
        rtxt = " ".join(w["text"] for w in right).strip()
        line_pairs.append((ltxt, rtxt))

    rows: list[PdfRow] = []
    # Parse each column independently — categories don't span columns
    for col_idx in (0, 1):
        current_family: str | None = None
        current_segments: str | None = None
        for pair in line_pairs:
            text = pair[col_idx]
            if not text:
                continue
            cat = CATEGORY_RE.match(text)
            if cat:
                current_segments = cat.group("segments").strip()
                current_family = cat.group("family").strip()
                continue
            if current_family is None:
                continue
            m = ROW_RE.match(text)
            if not m:
                continue
            code = m.group("code").strip()
            price = float(m.group("price").replace(",", ""))
            rows.append(
                PdfRow(
                    family=_titlecase_family(current_family),
                    segments=current_segments or "",
                    code=code,
                    warranty=m.group("warranty").strip(),
                    ah=m.group("ah"),
                    mrp_inr=price,
                )
            )
    return rows


def passenger_vehicle_rows(pdf_rows: list[PdfRow]) -> list[PdfRow]:
    """Filter to categories whose segment list includes CAR or SUV.

    BRD §3.2: passenger-vehicle scope only. CV/TRACTOR/2-WHEELER/E-RICKSHAW excluded.
    EXIDE DRIVE has segments "CAR/SUV/3W/TRACTOR/CV" → KEPT (CAR present).
    EXIDE EKO has segments "3W/LCV" → DROPPED.
    """
    out = []
    for r in pdf_rows:
        segs = r.segments.upper()
        if "CAR" in segs or "SUV" in segs:
            out.append(r)
    return out


def mrp_index(pdf_rows: list[PdfRow]) -> dict[str, float]:
    """Build {code: mrp} dict. Last-wins on duplicate code (rare; PDF is clean)."""
    return {r.code: r.mrp_inr for r in pdf_rows}


def _titlecase_family(raw: str) -> str:
    """'EPIQ' → 'Epiq', 'EEZY ISS' → 'Eezy ISS', 'AGMi' → 'AGMi'."""
    # Preserve known mixed-case acronyms
    fixed = {"AGMI": "AGMi", "ISS": "ISS", "EEZY": "Eezy", "DRIVE": "Drive",
             "MATRIX": "Matrix", "MILEAGE": "Mileage", "RIDE": "Ride",
             "EPIQ": "Epiq"}
    tokens = raw.split()
    return " ".join(fixed.get(t.upper(), t.capitalize()) for t in tokens)
