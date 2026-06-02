"""MRP and field normalization per BRD §5.

Numeric MRP unquoted in CSV, typed number in JSON. Strip currency symbols, commas, whitespace.
"""

from __future__ import annotations

import re

_MRP_STRIP = re.compile(r"[₹$€£,\s]")


def clean_mrp(raw: str | None) -> float | None:
    if raw is None:
        return None
    s = _MRP_STRIP.sub("", str(raw))
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None
