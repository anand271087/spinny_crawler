"""DOM-change / site-breakage detector. BRD §6 + §8 contractual requirement.

Strategy: compare the current run's per-brand counts/status against the last
SUCCESSFUL run (stored in state/last_successful.json). Surface significant
regressions in `run_summary.dom_changes[]` so the existing email alert path picks
them up. No per-selector fingerprinting — row-count + status delta catches the
same breakage classes with far less code.

Conditions that count as a "change":
- Brand status changed from success → partial OR success → failed
- Brand status changed from partial → failed
- Brand row count dropped by more than ROW_DROP_THRESHOLD (default 25%)
- Brand was present in last run but missing in current (spider didn't run)
- Brand emitted 0 rows after previously emitting >0 (catches silent breakage)

Each flagged change is a dict with brand, kind, current, baseline, severity.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("orchestrator.dom_change_detector")

ROW_DROP_THRESHOLD = 0.25  # 25% drop vs baseline → flag
SEVERITY_BY_KIND = {
    "status_regression":   "high",
    "row_count_drop":      "medium",
    "brand_missing":       "high",
    "zero_rows_regression":"high",
}


def detect_changes(current: dict, state_path: Path) -> list[dict]:
    """Compare `current` run_summary against last successful run.

    Returns a list of change records (possibly empty).
    Writes baseline forward if current run is itself a success.
    """
    baseline = _load_baseline(state_path)
    changes: list[dict] = []

    if not baseline:
        log.info("dom_change_detector: no baseline yet — establishing this run as baseline")
        _save_baseline(state_path, current)
        return changes

    for brand, cur in current.get("brands", {}).items():
        base = baseline.get("brands", {}).get(brand)
        if not base:
            # New brand → not a regression, just informational
            continue

        # 1. Status regression
        regression = _status_regression(base["status"], cur["status"])
        if regression:
            changes.append(_change(brand, "status_regression",
                                   f"{base['status']} → {cur['status']}",
                                   base["status"]))

        # 2. Row count drop
        if base.get("rows", 0) > 0 and cur.get("rows", 0) > 0:
            drop_pct = 1 - (cur["rows"] / base["rows"])
            if drop_pct >= ROW_DROP_THRESHOLD:
                changes.append(_change(brand, "row_count_drop",
                                       f"{cur['rows']} (was {base['rows']}, -{drop_pct:.0%})",
                                       base["rows"]))

        # 3. Zero rows when baseline had rows
        if base.get("rows", 0) > 0 and cur.get("rows", 0) == 0:
            changes.append(_change(brand, "zero_rows_regression",
                                   f"0 (baseline {base['rows']})",
                                   base["rows"]))

    # 4. Brand missing entirely
    cur_brands = set(current.get("brands", {}).keys())
    for brand in baseline.get("brands", {}):
        if brand not in cur_brands:
            changes.append(_change(brand, "brand_missing",
                                   "absent",
                                   f"baseline had {baseline['brands'][brand].get('rows', 0)} rows"))

    # Roll baseline forward only on clean (no high-severity changes) runs
    if not any(c["severity"] == "high" for c in changes):
        _save_baseline(state_path, current)
        log.info("dom_change_detector: baseline updated (clean run)")
    else:
        log.warning("dom_change_detector: baseline NOT updated (high-severity changes pending review)")

    return changes


def _status_regression(base: str, cur: str) -> bool:
    rank = {"success": 0, "partial": 1, "failed": 2}
    return rank.get(cur, 0) > rank.get(base, 0)


def _change(brand: str, kind: str, current_val, baseline_val) -> dict:
    return {
        "brand": brand,
        "kind": kind,
        "current": current_val,
        "baseline": baseline_val,
        "severity": SEVERITY_BY_KIND.get(kind, "low"),
    }


def _load_baseline(state_path: Path) -> dict | None:
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("dom_change_detector: failed to load baseline (%s) — treating as absent", exc)
        return None


def _save_baseline(state_path: Path, summary: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
