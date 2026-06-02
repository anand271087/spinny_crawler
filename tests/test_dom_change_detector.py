"""Tests for orchestrator/dom_change_detector.py — BRD §6 + §8 contractual check."""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.dom_change_detector import detect_changes


def _sum(brands):
    return {"run_date": "2026-05-18", "brands": brands, "dom_changes": []}


def test_no_baseline_establishes_one(tmp_path):
    state = tmp_path / "last_successful.json"
    current = _sum({"mobil": {"status": "success", "rows": 410, "errors": 0}})
    changes = detect_changes(current, state)
    assert changes == []
    assert state.exists()
    saved = json.loads(state.read_text())
    assert saved["brands"]["mobil"]["rows"] == 410


def test_status_regression_flagged(tmp_path):
    state = tmp_path / "last_successful.json"
    state.write_text(json.dumps(_sum({"mobil": {"status": "success", "rows": 410, "errors": 0}})))
    current = _sum({"mobil": {"status": "failed", "rows": 0, "errors": 1}})
    changes = detect_changes(current, state)
    kinds = {c["kind"] for c in changes}
    assert "status_regression" in kinds
    assert any(c["severity"] == "high" for c in changes)


def test_row_count_drop_flagged_at_25_percent(tmp_path):
    state = tmp_path / "last_successful.json"
    state.write_text(json.dumps(_sum({"hella": {"status": "success", "rows": 1666, "errors": 0}})))

    # 24% drop — under threshold, NOT flagged
    current = _sum({"hella": {"status": "success", "rows": int(1666 * 0.76), "errors": 0}})
    changes = detect_changes(current, state)
    assert not any(c["kind"] == "row_count_drop" for c in changes)

    # Reset baseline (clean run advanced it). Re-seed.
    state.write_text(json.dumps(_sum({"hella": {"status": "success", "rows": 1666, "errors": 0}})))

    # 30% drop — flagged
    current = _sum({"hella": {"status": "success", "rows": int(1666 * 0.70), "errors": 0}})
    changes = detect_changes(current, state)
    assert any(c["kind"] == "row_count_drop" for c in changes)


def test_brand_missing_flagged(tmp_path):
    state = tmp_path / "last_successful.json"
    state.write_text(json.dumps(_sum({
        "mobil": {"status": "success", "rows": 410, "errors": 0},
        "exide": {"status": "partial", "rows": 48, "errors": 0},
    })))
    current = _sum({"mobil": {"status": "success", "rows": 410, "errors": 0}})  # exide missing
    changes = detect_changes(current, state)
    missing = [c for c in changes if c["kind"] == "brand_missing"]
    assert len(missing) == 1
    assert missing[0]["brand"] == "exide"
    assert missing[0]["severity"] == "high"


def test_zero_rows_regression_flagged(tmp_path):
    state = tmp_path / "last_successful.json"
    state.write_text(json.dumps(_sum({"technix": {"status": "success", "rows": 1225, "errors": 0}})))
    current = _sum({"technix": {"status": "failed", "rows": 0, "errors": 1}})
    changes = detect_changes(current, state)
    kinds = {c["kind"] for c in changes}
    assert "zero_rows_regression" in kinds


def test_baseline_NOT_advanced_when_high_severity(tmp_path):
    state = tmp_path / "last_successful.json"
    state.write_text(json.dumps(_sum({"mobil": {"status": "success", "rows": 410, "errors": 0}})))
    current = _sum({"mobil": {"status": "failed", "rows": 0, "errors": 1}})
    detect_changes(current, state)
    # Baseline preserved — would otherwise drift to the broken state
    saved = json.loads(state.read_text())
    assert saved["brands"]["mobil"]["status"] == "success"
    assert saved["brands"]["mobil"]["rows"] == 410


def test_baseline_advanced_on_clean_run(tmp_path):
    state = tmp_path / "last_successful.json"
    state.write_text(json.dumps(_sum({"mobil": {"status": "success", "rows": 410, "errors": 0}})))
    current = _sum({"mobil": {"status": "success", "rows": 415, "errors": 0}})
    detect_changes(current, state)
    saved = json.loads(state.read_text())
    assert saved["brands"]["mobil"]["rows"] == 415  # baseline advanced
