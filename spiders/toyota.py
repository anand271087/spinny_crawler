"""TOYOTA spider (v2.0) — same SNAP-ON EPC platform as Hyundai.

Re-exports the shared SNAP-ON REST walker via spiders/hyundai.py (which in turn
imports spiders/snapon_rest.py). Credentials and scope read from TOYOTA_* env
vars (TOYOTA_USER, TOYOTA_PASS, TOYOTA_MAX_YEARS, etc.). See .env.example.
"""

from spiders.hyundai import Spider  # re-export — orchestrator instantiates Spider(brand_key, brand_cfg)

__all__ = ["Spider"]
