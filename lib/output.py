"""CSV+JSON writers per BRD §5.

Naming: <brand>_<YYYYMMDD>.{csv,json}, spinny_oem_master_<YYYYMMDD>.{csv,json}, run_summary_<YYYYMMDD>.json
UTF-8 encoding. MRP unquoted in CSV; typed as number in JSON.

Per-brand CSV: drops product columns that are entirely empty for that brand
(e.g. Mobil has no item_code/mrp/etc — those columns are omitted from mobil_*.csv).
Always-kept columns: brand, source_website, crawl_date, crawl_status.

Master CSV: keeps the full unified schema across all 19 brands so downstream
consumers can join/concat reliably.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd

ALWAYS_KEEP = ["brand", "source_website", "crawl_date", "crawl_status"]
PRODUCT_FIELDS = ["item_name", "item_code", "mrp", "compatible_car_model", "tyre_sizes", "vehicle_compatibility"]


def _to_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _drop_empty_product_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Drop product columns where every value is null/empty. Auto-columns always retained."""
    keep_cols = list(ALWAYS_KEEP)
    for col in PRODUCT_FIELDS:
        if col in df.columns and df[col].notna().any() and not (df[col].astype(str).str.strip() == "").all():
            keep_cols.append(col)
    # Preserve a sensible column order: brand, source_website, product fields..., crawl_date, crawl_status
    ordered = ["brand", "source_website"] + [c for c in PRODUCT_FIELDS if c in keep_cols] + ["crawl_date", "crawl_status"]
    return df[[c for c in ordered if c in df.columns]]


def write_per_brand(brand: str, rows: list[dict], out_dir: Path, run_date: dt.date) -> None:
    stem = f"{brand}_{run_date.strftime('%Y%m%d')}"
    df = _to_df(rows)
    if not df.empty:
        df = _drop_empty_product_columns(df)
    df.to_csv(out_dir / f"{stem}.csv", index=False, encoding="utf-8")
    # JSON: also drop empty columns for parity with CSV
    records = df.to_dict(orient="records")
    (out_dir / f"{stem}.json").write_text(json.dumps(records, ensure_ascii=False, indent=2))


def write_master(all_rows: list[dict], out_dir: Path, run_date: dt.date) -> None:
    """Master file per BRD §5.

    Dedup rule (BRD §5):
      - rows WITH item_code → dedupe on (source_website, item_code)
      - rows WITHOUT item_code → dedupe on (source_website, item_name, compatible_car_model)

    Splitting by item_code presence avoids the pandas-NaN trap where rows missing
    item_code (e.g. Mobil's 406 products) all hash to the same (source_website, NaN)
    key and collapse to a single row.
    """
    stem = f"spinny_oem_master_{run_date.strftime('%Y%m%d')}"
    df = _to_df(all_rows)
    if not df.empty:
        has_code = df["item_code"].notna() & (df["item_code"].astype(str).str.strip() != "")
        with_code = df[has_code].drop_duplicates(subset=["source_website", "item_code"], keep="first")
        without_code = df[~has_code].drop_duplicates(
            subset=["source_website", "item_name", "compatible_car_model"], keep="first"
        )
        df = pd.concat([with_code, without_code], ignore_index=True)

    # Master keeps the full unified schema for cross-brand joins.
    ordered = ["brand", "source_website"] + PRODUCT_FIELDS + ["crawl_date", "crawl_status"]
    df = df[[c for c in ordered if c in df.columns]]
    df.to_csv(out_dir / f"{stem}.csv", index=False, encoding="utf-8")
    (out_dir / f"{stem}.json").write_text(df.to_json(orient="records", force_ascii=False, indent=2))


def write_run_summary(summary: dict, out_dir: Path, run_date: dt.date) -> None:
    path = out_dir / f"run_summary_{run_date.strftime('%Y%m%d')}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
