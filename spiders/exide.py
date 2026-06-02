"""EXIDE spider — extracts the passenger-vehicle catalogue from the MRP PDF.

Plan ref §12.9. URL: https://www.exideindustries.com/products/automotive-batteries/four-wheeler-batteries.aspx

xlsx steps: scroll → select battery type → for each type, extract products.
xlsx fields: item_name, item_code, mrp.

Source-of-truth decision (2026-05-19):
- The four-wheeler-batteries landing page shows nomenclature codes per family but
  NO MRP. MRP only appears on the official "MRP List" PDF linked from the footer.
- We use the PDF as the single source: it has the complete catalogue, MRPs, and
  authoritative category headers (CAR/SUV/CV/TRACTOR/etc.) so passenger-vehicle
  filtering per BRD §3.2 is unambiguous.
- PDF URL (cached month-to-month — Exide reissues it with each price revision):
    https://docs.exideindustries.com/pdf/mrp-list/mrcp-exide-vehicular-and-2wl-batteries.pdf

PV families covered: Epiq, Matrix, Mileage, Mileage ISS, AGMi, Eezy, Eezy ISS,
Ride, Drive. Sub-brand SF Batteries and Dynex have separate PDFs — out of scope.

Item identity:
- item_code = the Battery Nomenclature value (e.g., EPIQ35L)
- item_name = "Exide <Family> <Nomenclature>" (e.g., "Exide Epiq EPIQ35L")
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from lib.exide_pdf import (
    download_pdf,
    parse_pdf,
    passenger_vehicle_rows,
)
from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.exide")


class Spider(BaseSpider):

    def crawl(self) -> list[Row]:
        # Cache PDF inside output/ so re-runs don't re-download
        pdf_dir = Path(tempfile.gettempdir()) / "spinny_crawler_exide"
        pdf_path = pdf_dir / "mrcp-exide-vehicular-and-2wl-batteries.pdf"
        download_pdf(pdf_path)

        all_rows = parse_pdf(pdf_path)
        pv_rows = passenger_vehicle_rows(all_rows)
        log.info(
            "Exide PDF parsed: %d total rows, %d passenger-vehicle (CAR/SUV)",
            len(all_rows),
            len(pv_rows),
        )

        rows: list[Row] = []
        for pr in pv_rows:
            rows.append(Row(
                item_name=f"Exide {pr.family} {pr.code}",
                item_code=pr.code,
                mrp=pr.mrp_inr,
            ))
        return rows
