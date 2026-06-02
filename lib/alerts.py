"""Email alerts via SMTP per plan §4.

Triggers (plan §4):
1. Per-spider failure (called from orchestrator on exception)
2. End-of-run aggregate (<98% success)
3. DOM-change detection

Non-fatal: if SMTP fails (placeholder host during dev, network down, etc.),
log a warning and return — the run itself is what matters.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

import yaml

log = logging.getLogger("lib.alerts")


def send_alert(summary: dict, alerts_config: Path) -> None:
    cfg = yaml.safe_load(alerts_config.read_text())
    failed = [b for b, s in summary["brands"].items() if s["status"] == "failed"]
    partial = [b for b, s in summary["brands"].items() if s["status"] == "partial"]
    dom_changes = summary.get("dom_changes", [])
    if not (failed or partial or dom_changes):
        return

    msg = EmailMessage()
    msg["Subject"] = f"[Spinny Crawler] {len(failed)} failed, {len(partial)} partial, {len(dom_changes)} DOM changes"
    msg["From"] = cfg["from_address"]
    msg["To"] = ", ".join(cfg["recipients"])
    msg.set_content(json.dumps(summary, indent=2, ensure_ascii=False))

    smtp_cfg = cfg["smtp"]
    try:
        with smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"], timeout=10) as s:
            s.starttls()
            s.login(smtp_cfg["username"], os.environ[smtp_cfg["password_env"]])
            s.send_message(msg)
        log.info("alert sent to %d recipients", len(cfg["recipients"]))
    except Exception as exc:
        log.warning("alert send failed (continuing): %s", exc)
