# Spinny OEM Spare-Parts Crawler

Monthly automated extraction of passenger-vehicle spare-parts catalogue data from **26 OEM and aftermarket-brand websites** for Spinny.

Output: a single deduped master CSV (~120,000 rows) delivered to Spinny's shared cloud folder on the first Monday of each month.

**Repository**: https://github.com/anand271087/spinny_crawler

---

## What this crawler does

| | |
|---|---|
| **Sources** | 19 v1.0 aftermarket-brand websites (Amaron, Autokoi, Bosch, Exide, Gabriel, HELLA, JK Tyre, Lumax, Mobil, Monroe, Schaeffler, SF Sonic, Spark Minda, Technix, TVS Girling, Uno Minda, Valeo, ZIP, ZF) + 7 v2.0 OEM EPCs (Ford, Hyundai, Mahindra, Maruti, MG, Tata, Toyota) |
| **Cadence** | Monthly — first Monday at 22:00 IST, finishes by 06:00 IST Tuesday |
| **Output** | Per-brand CSV + JSON + a deduped master CSV (~120K rows) + run_summary.json |
| **Schema** | Per-brand fields per the BRD's companion xlsx (item_name, item_code, mrp, compatible_car_model, etc.); deduped per BRD §5 |
| **Cost** | $0 infra (self-hosted on Spinny VM via cron); optional $16/mo Firecrawl as escape hatch |

---

## Quick start (laptop)

```bash
git clone https://github.com/anand271087/spinny_crawler.git
cd spinny_crawler

python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .[dev]
playwright install chromium       # ~150 MB browser binary

# Credentials are NOT in this repo. Ask the project owner for the values.
cp .env.example .env
# Edit .env with the real values, then:
set -a; source .env; set +a

# Run a fast smoke (one public brand, no creds needed):
python -m orchestrator.run_monthly --brands=zip

# Run all 26 brands (default 4 parallel workers, ~3.5h wall-clock):
python -m orchestrator.run_monthly
```

**Output lands in `output/<YYYYMMDD>/`**.

For a full step-by-step including server install + cron, see [docs/deployment.md](docs/deployment.md).

---

## Documentation map

| Doc | Audience | What's in it |
|---|---|---|
| [CLAUDE.md](CLAUDE.md) | Engineers + AI assistants | Conventions, locked decisions, sources of truth, don'ts |
| [docs/deployment.md](docs/deployment.md) | DevOps / new operator | Full step-by-step install (laptop + server), cron, alerts, troubleshooting |
| [docs/per_site_notes.md](docs/per_site_notes.md) | Engineers maintaining spiders | Per-brand extraction logic, site quirks, locked techniques — **READ BEFORE TOUCHING ANY SPIDER** |
| [docs/runbook.md](docs/runbook.md) | On-call / ops | How to respond to alerts, fix broken spiders, escalate |
| [docs/kickoff_checklist.md](docs/kickoff_checklist.md) | Project owner | Open commercial/decision items still pending |
| [BRD/](BRD/) | Reference | Source-of-truth requirement docs (PDF + xlsx) |

---

## Architecture at a glance

```
┌─────────────────────────────────────────────────────────────┐
│  Linux VM (Spinny-provided) — cron 0 22 1-7 * 1            │
└─────────────────────────────────────────────────────────────┘
   │
   ├─► flat list / pagination  (3 brands)        → Scrapy + httpx
   ├─► multi-level category    (8 brands)        → Scrapy / Playwright tree walk
   ├─► cascading dropdowns     (4 brands)        → Playwright + XHR intercept
   ├─► SNAP-ON EPC REST        (2 brands)        → Playwright login → httpx data
   ├─► Intelli Catalogue       (2 brands)        → Playwright + offline OCR captcha
   ├─► PDF brochures           (3 brands)        → pdfplumber
   ├─► Vue/SPA REST APIs       (5 brands)        → bundle mining + httpx
   └─► hidden / off-canvas nav (1 brand)         → Playwright

                              │
                              ▼
         Per-brand CSV + JSON  ─►  Deduped master CSV (BRD §5)  ─►  Cloud upload
                              │
                              ▼
            Custom Python alert: email all stakeholders
            on FAILED / PARTIAL / DOM-change
```

---

## Tech stack

- **Python 3.11+**
- [Playwright](https://playwright.dev/python/) for JS-rendered sites
- [httpx](https://www.python-httpx.org/) for plain REST APIs
- [pdfplumber](https://github.com/jsvine/pdfplumber) for Bosch + Lumax PDF brochures
- [ddddocr](https://github.com/sml2h3/ddddocr) for the Mahindra + MG captcha (offline, no SaaS)
- [pandas](https://pandas.pydata.org/) for dedup + master assembly
- Built-in `cron` + `smtplib` (zero infra cost beyond the VM)

---

## Status

| Phase | Status |
|---|---|
| 0. Kickoff | ✅ Closed (see [docs/kickoff_checklist.md](docs/kickoff_checklist.md) for residual decisions) |
| 1. Framework + pilots | ✅ Done |
| 2. All 26 spiders | ✅ Done |
| 3. Master assembly + alerts + DOM detector | ✅ Done |
| 4. UAT | ✅ Done (latest delivery: ~120,000 rows / 26 brands) |
| 5. Handover | 🔁 In progress (this repo + docs) |

---

## License

Proprietary — internal use by Spinny and the project owner only. Not for redistribution.

---

## Contact

Project owner: [@anand271087](https://github.com/anand271087). For credentials, dealer-portal access, or commercial questions, contact Spinny directly.
