"""Hyundai spider — REST-based (2026-05-30).

Replaced the legacy Playwright AG-Grid clicker (preserved at
spiders/hyundai_legacy.py) after discovery that SNAP-ON EPC exposes a clean
REST API at /epc-services/.

The new implementation lives in spiders.snapon_rest and is shared with Toyota
(same SNAP-ON platform, different credentials + dataset). This module just
re-exports the Spider so the orchestrator can find it under brand_key="hyundai".

Defaults (smoke-friendly; production typically wants HYUNDAI_MAX_YEARS=1 only):
  HYUNDAI_MAX_YEARS=0   (0 = all years; year=2026 is the latest catalog)
  HYUNDAI_MAX_MODELS=0  (0 = all models)
  HYUNDAI_MAX_LEAVES=0  (0 = all leaf sections per branch)
  HYUNDAI_USER / HYUNDAI_PASS  (credentials supplied via env vars; see .env.example)

Performance vs legacy:
  - Legacy: ~10-20 min for ~168 rows (lots crashed/0 due to AG-Grid race)
  - REST: ~12 min for 22,000+ rows from 1 year x 1 model (smoke 2026-05-30)
  - 100x more data, no Playwright fragility after the 15s login.

Coverage: item_name, item_code, compatible_car_model all 100%. MRP is NOT in
the /pages/parts/ payload — that requires picklist API; queued as next-phase
work. Current rows ship with empty MRP and crawl_status='partial' per BRD section 7.
"""
from spiders.snapon_rest import Spider  # re-export for orchestrator

__all__ = ["Spider"]
