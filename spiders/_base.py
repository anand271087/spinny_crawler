"""Base spider contract. Every brand spider implements `run() -> RunResult`.

Auto-fields populated here (BRD §4):
- source_website: brand identifier + originating URL
- crawl_date: ISO-8601 UTC date of run
- crawl_status: success | partial | failed (per row)
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["success", "partial", "failed"]


@dataclass
class Row:
    brand: str = ""
    item_name: str | None = None
    item_code: str | None = None
    mrp: float | None = None
    description: str | None = None
    compatible_car_model: str | None = None
    part_structure: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    tyre_sizes: str | None = None
    vehicle_compatibility: str | None = None
    source_website: str = ""
    crawl_date: str = ""
    crawl_status: Status = "success"

    def finalize(self, brand: str, required_fields: list[str], source_url: str) -> "Row":
        self.brand = brand
        self.source_website = source_url
        self.crawl_date = dt.datetime.now(dt.timezone.utc).date().isoformat()
        missing = [f for f in required_fields if getattr(self, f, None) in (None, "")]
        if missing:
            self.crawl_status = "partial"
        return self


@dataclass
class RunResult:
    rows: list[Row] = field(default_factory=list)
    status: Status = "success"
    errors: int = 0


class BaseSpider:
    """Subclass per brand. Override `crawl()`."""

    def __init__(self, brand_key: str, brand_cfg: dict[str, Any]) -> None:
        self.brand_key = brand_key
        self.cfg = brand_cfg
        self.required_fields = brand_cfg["fields"]

    @property
    def brand_label(self) -> str:
        """Human-readable brand name. Override if config has an explicit `name`."""
        return self.cfg.get("name") or self.brand_key.replace("_", " ").title()

    def crawl(self) -> list[Row]:
        raise NotImplementedError

    def run(self) -> dict[str, Any]:
        rows = self.crawl()
        source_url = self.cfg.get("url") or self.cfg.get("urls", [{}])[0].get("url", "")
        rows = [r.finalize(self.brand_label, self.required_fields, source_url) for r in rows]
        partial = sum(1 for r in rows if r.crawl_status == "partial")
        if not rows:
            status: Status = "failed"
        elif partial:
            status = "partial"
        else:
            status = "success"
        return {"rows": [r.__dict__ for r in rows], "status": status, "errors": 0}
