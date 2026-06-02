"""Parse Autokoi e-catalogue PDF into {code: {mrp, make, section, page}} dict.

Discovery: Autokoi's footer "E-Catalogue" link points to:
    https://www.autokoi.com/wp-content/uploads/2021/12/autokoi-catalouge-with-mrp-2025.pdf
(URL year suffix is hand-updated by Autokoi each cycle; we scrape the link from
the homepage on each run rather than hard-coding.)

PDF structure (96 pages, 15 MB):
- Pages 1-2: cover
- Page 3: ToC
- Pages 4-5: About Us
- Pages 6-96: product data, two-column layout with grids of small product images
  Each product block:
      <product name line> [multiple product names side-by-side, x-aligned]
      <CODE> MRP.`<PRICE> [multiple codes side-by-side, same x-alignment]

The reliable signal per product is `<CODE> MRP.\`<PRICE>` — captured by a simple
text regex. **1,382 unique codes with MRP** confirmed extractable as of 2026-05-22.

Per-code metadata we additionally capture (best-effort):
- page: the PDF page number where the code appears
- make: from "Suitable For <Make>" line on the same page (Maruti Suzuki, Hyundai
  Motors, Mahindra & Mahindra, Honda Cars, TATA Motors, etc.)
- section: page section header ("Steering & Suspension Parts", "RUBBER BUSHING
  KITS", "Strut Kit", etc.) — best-effort; noisy on multi-section pages.

We don't try positional name-mapping because the PDF lays out product names on
wider x-spans than codes, so column-aligned word capture loses most of the name.
Analyst-readable item_name is constructed downstream as `Autokoi {section} -
{make}` or, for codes that overlap with the HTML-walk spider's output, the
spider's existing item_name takes precedence.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
import pdfplumber

log = logging.getLogger("lib.autokoi_pdf")

USER_AGENT = "SpinnyOEMCrawler/1.0 (contact@spinny.com)"
HOMEPAGE = "https://www.autokoi.com/"
# Anchor that links to the e-catalogue PDF. URL has typo: "catalouge" not "catalogue".
# Pattern stays permissive — anything like `autokoi-cat*.pdf` from autokoi.com matches.
ECAT_LINK_RE = re.compile(
    r'href=[\'"]([^\'"]+autokoi-cat[a-z]*[^\'"]*\.pdf)[\'"]',
    re.IGNORECASE,
)

# Code + MRP token. The Indian Rupee shorthand in this PDF is a grave-accent
# (`) before the number, sometimes a smart quote.
CODE_MRP_RE = re.compile(
    r"\b([A-Z]{2,6}\d{3,7}[A-Z]?)\b\s*MRP\.[``‘]?\s*(\d{1,7})"
)
MAKE_RE = re.compile(r"Suitable\s+For\s+([A-Za-z &/\-]+)", re.IGNORECASE)
# Sections we recognize (extend as new ones appear)
SECTION_HINTS = [
    "Steering & Suspension Parts",
    "RUBBER BUSHING KITS",
    "Strut Kit",
    "FRONT STRUT MOUNTS",
    "ENGINE MOUNTING",
    "Balance Rod Bush",
    "Rear Trailing Arm Bush",
    "Rear Traling Arm Bush",  # typo in PDF
    "Other Suspension Parts",
]


@dataclass
class PdfEntry:
    code: str
    mrp_inr: int
    make: str
    section: str
    page: int


def fetch_ecatalogue_url(client: httpx.Client) -> str:
    """Scrape the e-catalogue PDF URL from autokoi.com homepage."""
    resp = client.get(HOMEPAGE)
    resp.raise_for_status()
    m = ECAT_LINK_RE.search(resp.text)
    if not m:
        raise RuntimeError("autokoi: e-catalogue link not found on homepage")
    return m.group(1)


def download_pdf(client: httpx.Client, url: str, dest: Path) -> Path:
    """Download e-catalogue PDF. Caches if dest exists."""
    if dest.exists():
        log.info("autokoi PDF cached: %d bytes", dest.stat().st_size)
        return dest
    log.info("autokoi: downloading e-catalogue PDF from %s", url)
    resp = client.get(url)
    resp.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    log.info("autokoi: downloaded %d bytes -> %s", len(resp.content), dest)
    return dest


def parse_pdf(pdf_path: Path) -> dict[str, PdfEntry]:
    """Walk every page; return {code: PdfEntry} (first-wins on duplicate codes)."""
    out: dict[str, PdfEntry] = {}
    with pdfplumber.open(pdf_path) as pdf:
        page_section = ""
        page_make = ""
        for pi, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            # Update per-page section + make from the page text
            for line in text.splitlines():
                ls = line.strip()
                for hint in SECTION_HINTS:
                    if ls.lower().startswith(hint.lower()):
                        page_section = hint
                        break
                mm = MAKE_RE.search(ls)
                if mm:
                    page_make = _clean_make(mm.group(1))
            # Extract codes from page
            for m in CODE_MRP_RE.finditer(text):
                code = m.group(1)
                if code in out:
                    continue
                out[code] = PdfEntry(
                    code=code,
                    mrp_inr=int(m.group(2)),
                    make=page_make,
                    section=page_section,
                    page=pi,
                )
    return out


def _clean_make(raw: str) -> str:
    """Strip 'Suitable For Maruti Suzuki' echo and trailing whitespace."""
    s = re.sub(r"\s*Suitable\s+For\s+.*$", "", raw, flags=re.IGNORECASE).strip()
    return s


def build_item_name(entry: PdfEntry) -> str:
    """Construct an analyst-readable item_name for a PDF-only code."""
    parts = ["Autokoi"]
    if entry.section:
        parts.append(entry.section.title())
    if entry.make:
        parts.append(f"({entry.make})")
    parts.append(entry.code)
    return " ".join(parts)
