"""Monthly entry point. Loads sites.yaml, fans out spiders, aggregates outputs.

Plan reference: §3 Architecture, §8 Phase Breakdown.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib
import json
import logging
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import yaml

from lib.alerts import send_alert
from lib.output import write_per_brand, write_master, write_run_summary
from orchestrator.dom_change_detector import detect_changes

log = logging.getLogger("orchestrator")

# Default 4 workers — caps memory at ~800MB (4 × Chromium) and stays polite per-site
# since each brand has its own domain. Override via --max-workers or CRAWLER_MAX_WORKERS.
DEFAULT_MAX_WORKERS = 4


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _absorb_result(brand: str, result: dict, summary: dict, all_rows: list,
                   out_dir: Path, run_date) -> None:
    """Write per-brand output + roll into summary/master. Shared by parallel + serial paths."""
    write_per_brand(brand, result["rows"], out_dir, run_date)
    entry = {
        "status": result["status"],
        "rows": len(result["rows"]),
        "errors": result.get("errors", 0),
    }
    if "elapsed_s" in result:
        entry["elapsed_s"] = result["elapsed_s"]
    if "exception" in result:
        entry["exception"] = result["exception"]
    summary["brands"][brand] = entry
    all_rows.extend(result["rows"])


def run_spider(brand: str, brand_cfg: dict) -> dict:
    """Dispatch to spiders.<brand>. Returns {rows: [...], status: ..., errors: int}.

    Called either inline (single-worker) or inside a ProcessPoolExecutor worker.
    Re-initializes its own logging so subprocess output is visible. Catches
    spider crashes and converts them into a `status=failed` result so the
    parent never sees a raw exception from the pool.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,  # subprocess inherits no handlers; force re-init
    )
    t0 = time.time()
    try:
        module = importlib.import_module(f"spiders.{brand}")
        spider = module.Spider(brand, brand_cfg)
        result = spider.run()
        result["elapsed_s"] = round(time.time() - t0, 1)
        return result
    except Exception as exc:
        logging.getLogger(f"spiders.{brand}").exception("brand=%s crashed", brand)
        return {
            "rows": [],
            "status": "failed",
            "errors": 1,
            "exception": str(exc),
            "elapsed_s": round(time.time() - t0, 1),
        }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=Path("config/sites.yaml"))
    ap.add_argument("--brands", help="Comma-sep brand keys; default = all")
    ap.add_argument("--output-dir", type=Path, default=Path("output"))
    ap.add_argument(
        "--max-workers", type=int,
        default=int(os.environ.get("CRAWLER_MAX_WORKERS", DEFAULT_MAX_WORKERS)),
        help=f"Parallel brand workers (default {DEFAULT_MAX_WORKERS}, or env CRAWLER_MAX_WORKERS). "
             f"Set 1 to disable parallelism.",
    )
    args = ap.parse_args()

    cfg = load_config(args.config)
    run_date = dt.datetime.now(dt.timezone.utc).date()
    out_dir = args.output_dir / run_date.strftime("%Y%m%d")
    out_dir.mkdir(parents=True, exist_ok=True)

    brand_keys = args.brands.split(",") if args.brands else list(cfg["brands"].keys())
    max_workers = max(1, min(args.max_workers, len(brand_keys)))
    summary = {"run_date": run_date.isoformat(), "brands": {}, "dom_changes": []}
    all_rows = []
    t_run_start = time.time()

    log.info("starting %d brands with %d worker(s) (parallel=%s)",
             len(brand_keys), max_workers, max_workers > 1)

    if max_workers == 1:
        # Sequential fallback — useful for debugging
        for brand in brand_keys:
            log.info("brand=%s start", brand)
            result = run_spider(brand, cfg["brands"][brand])
            _absorb_result(brand, result, summary, all_rows, out_dir, run_date)
    else:
        with ProcessPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(run_spider, brand, cfg["brands"][brand]): brand
                for brand in brand_keys
            }
            log.info("dispatched %d brand workers", len(futures))
            completed = 0
            for fut in as_completed(futures):
                brand = futures[fut]
                completed += 1
                try:
                    result = fut.result()
                except Exception as exc:
                    log.exception("brand=%s pool-level crash", brand)
                    result = {"rows": [], "status": "failed", "errors": 1,
                              "exception": str(exc), "elapsed_s": 0}
                _absorb_result(brand, result, summary, all_rows, out_dir, run_date)
                log.info("[%d/%d] brand=%s done in %.0fs (status=%s rows=%d)",
                         completed, len(futures), brand,
                         result.get("elapsed_s", 0), result["status"], len(result["rows"]))

    # Preserve original brand-key order in summary even when completed out-of-order
    summary["brands"] = {k: summary["brands"][k] for k in brand_keys if k in summary["brands"]}

    write_master(all_rows, out_dir, run_date)

    elapsed = time.time() - t_run_start
    log.info("all brands done — wall-clock %.0fs (%.1f min, %.2f h)",
             elapsed, elapsed / 60, elapsed / 3600)

    # BRD §6 + §8: detect DOM/site changes by comparing to last successful run.
    state_path = Path("state/last_successful.json")
    changes = detect_changes(summary, state_path)
    summary["dom_changes"] = changes
    if changes:
        log.warning("dom_change_detector: %d change(s) flagged: %s",
                    len(changes),
                    ", ".join(f"{c['brand']}={c['kind']}" for c in changes))

    write_run_summary(summary, out_dir, run_date)

    success_rate = sum(1 for b in summary["brands"].values() if b["status"] == "success") / max(len(summary["brands"]), 1)
    log.info("run complete: success_rate=%.2f", success_rate)

    if success_rate < 0.98 or any(b["status"] == "failed" for b in summary["brands"].values()) or changes:
        send_alert(summary, alerts_config=Path("config/alerts.yaml"))

    return 0 if success_rate >= 0.98 else 1


if __name__ == "__main__":
    sys.exit(main())
