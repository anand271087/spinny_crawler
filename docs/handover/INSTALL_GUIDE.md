# Spinny OEM Spare-Parts Crawler — Installation & Run Guide

**Audience:** Spinny in-house engineer installing and running the crawler on their own machine (laptop or server).

**What you received:** a ZIP folder exported from the project's Git repository. Unzip it anywhere; the unzipped folder (referred to below as the **project folder**) contains `orchestrator/`, `spiders/`, `lib/`, `config/`, `docs/`, `BRD/`, `pyproject.toml`, and `CLAUDE.md`.

This guide gives **two installation paths**:

- **Way 1 — Manual setup** (you run each command yourself). Use this if you are comfortable with a terminal.
- **Way 2 — Claude Code assisted setup** (Claude Code reads the project and installs it for you). Use this if you prefer a guided, conversational setup.

Both paths end at the same place: a working crawler you can run with one command.

---

## 0. Before you start — what the crawler is

A monthly batch crawler that extracts passenger-vehicle spare-parts catalogue data from **26 OEM / aftermarket brand websites** and writes per-brand CSV/JSON files plus one consolidated "master" file. It runs on your machine — there is **no cloud service and no recurring cost**.

- **20 brands** have public catalogs and need **no login**.
- **6 brands** (Hyundai, Toyota, Mahindra, MG, Tata, Ford) need **dealer credentials** that Spinny supplies separately (never stored in the code).

A full run of all 26 brands takes roughly **3.5–7 hours** depending on options (one site, ZF, is the slow one).

---

## 1. System requirements

| Component | Minimum | Recommended |
|---|---|---|
| OS | macOS 11+ **or** Ubuntu 22.04+ / Debian 12+ **or** Windows 10/11 | macOS / Linux (or Windows via WSL2) |
| Python | **3.11 or 3.12** (NOT 3.10 or earlier) | 3.11 |
| RAM | 2 GB | 4 GB |
| Disk | 10 GB free | 20 GB free |
| Network | unrestricted HTTPS (talks to ~30 .in/.com domains) | same |

> **Why Python 3.11+ is mandatory:** the code uses 3.11-only syntax. Python 3.10 or earlier will fail to start.

**Check what you have:**
```bash
python3.11 --version      # must print Python 3.11.x (or use python3.12)
git --version             # any recent version
```

**If Python 3.11 is missing:**
- **macOS:** `brew install python@3.11` (install Homebrew first from https://brew.sh if needed)
- **Ubuntu/Debian:** `sudo apt update && sudo apt install -y python3.11 python3.11-venv python3.11-dev`
- **Windows:** install from https://www.python.org/downloads/ (tick **"Add python.exe to PATH"** during setup), or `winget install Python.Python.3.11`

> **Windows users:** the crawler runs on Windows — see **the "Windows users" box inside each step** below for the Windows command equivalents, and **Section 1.10** for the two recommended Windows approaches. The Python code is identical across all OSes; only the shell commands (venv activation, loading credentials, scheduling) differ.

---

## WAY 1 — Manual setup

> **Windows readers:** Steps 1.1–1.9 below use macOS/Linux syntax. Each step's Windows (PowerShell) equivalent is given in Section **1.10**. If you'd rather not translate commands at all, the cleanest Windows route is **WSL2** (also in 1.10) — it lets you run the macOS/Linux commands verbatim.

### Step 1.1 — Open a terminal in the project folder
```bash
cd /path/to/spinny_crawler          # the unzipped folder
ls                                  # should list: orchestrator  spiders  lib  config  docs  pyproject.toml  CLAUDE.md ...
```
If those folders are missing, the ZIP is incomplete — ask for a fresh export.

### Step 1.2 — Create and activate a virtual environment
A "venv" isolates this project's Python packages from the rest of your system.
```bash
python3.11 -m venv .venv
source .venv/bin/activate           # macOS/Linux. Prompt changes to (.venv)
```
> You must run `source .venv/bin/activate` in **every new terminal** before using the crawler.

### Step 1.3 — Install the Python dependencies
```bash
pip install --upgrade pip
pip install -e .[dev]
```
This reads `pyproject.toml` and installs everything (Playwright, pandas, httpx, pdfplumber, scrapy, ddddocr, …). Takes ~3–5 minutes.

### Step 1.4 — Install the headless browser
Several brands need a real browser (Chromium), driven invisibly.
```bash
playwright install chromium
# Ubuntu/Debian ONLY — also install system libraries Chromium needs:
playwright install-deps chromium
```
(macOS does not need the `install-deps` step.)

### Step 1.5 — Verify the install
```bash
python -c "
from playwright.sync_api import sync_playwright
import pdfplumber, pandas, scrapy, httpx, yaml, parsel, ddddocr
print('python deps: OK')
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=['--no-sandbox']); b.close()
print('playwright chromium: OK')
"
```
Expected:
```
python deps: OK
playwright chromium: OK
```

### Step 1.6 — Add credentials for the 6 login brands
The code ships with **no credentials**. Copy the template and fill in the values Spinny gives you:
```bash
cp .env.example .env
# Open .env in any text editor and fill in the 6 brands' USER/PASS values
```
The `.env` file looks like this (fill the blanks):
```
HYUNDAI_USER=...     HYUNDAI_PASS=...
TOYOTA_USER=...      TOYOTA_PASS=...
TATA_USER=...        TATA_PASS=...
MAHINDRA_USER=...    MAHINDRA_PASS=...
MG_USER=...          MG_PASS=...
FORD_USER=...        FORD_PASS=...
```
Then load them into your terminal session:
```bash
set -a; source .env; set +a
```
> `.env` is git-ignored — it never gets committed. The other 20 brands need nothing here.

### Step 1.7 — First test run (fast)
```bash
python -m orchestrator.run_monthly --brands=zip
```
This runs the smallest spider (~2 seconds) and writes files under `output/<today's date>/`. If you see a `zip_<date>.csv`, the install works.

### Step 1.8 — A credentialed test (optional, ~2 min)
```bash
HYUNDAI_MAX_YEARS=1 HYUNDAI_MAX_MODELS=1 HYUNDAI_MAX_LEAVES=5 python -m orchestrator.run_monthly --brands=hyundai
```
If it produces rows, your credentials are working.

### Step 1.9 — Full run (all 26 brands)
```bash
python -m orchestrator.run_monthly
```
Runs everything with 4 parallel workers (~3.5–7 h). Output lands in `output/<date>/` (see Section 3 below).

---

## 1.10 — Windows installation

The crawler runs on Windows. Choose **one** of two approaches.

### Approach A (recommended) — WSL2 (Windows Subsystem for Linux)

WSL2 gives you a real Ubuntu environment inside Windows, so **all the macOS/Linux commands in Steps 1.1–1.9 work exactly as written**, including the optional cron scheduling.

1. Open **PowerShell as Administrator** and run:
   ```powershell
   wsl --install -d Ubuntu
   ```
   Reboot if prompted, then launch **Ubuntu** from the Start menu and set a username/password.
2. Inside the Ubuntu terminal, install prerequisites:
   ```bash
   sudo apt update && sudo apt install -y python3.11 python3.11-venv python3.11-dev git
   ```
3. Copy the unzipped project into WSL (e.g. `cp -r /mnt/c/Users/<you>/Downloads/spinny_crawler ~/` ), `cd` into it, and **follow Steps 1.1–1.9 verbatim** (remember `playwright install-deps chromium` is needed here).

### Approach B — Native Windows (PowerShell)

Use this if you don't want WSL. Same steps as 1.1–1.9, with these Windows equivalents:

| Step | macOS / Linux | Windows (PowerShell) |
|---|---|---|
| Create venv | `python3.11 -m venv .venv` | `py -3.11 -m venv .venv` |
| Activate venv | `source .venv/bin/activate` | `.venv\Scripts\Activate.ps1` |
| Install deps | `pip install -e .[dev]` | `pip install -e ".[dev]"` |
| Install browser | `playwright install chromium` | `playwright install chromium` (no `install-deps` needed) |
| Run a brand | `python -m orchestrator.run_monthly --brands=zip` | `python -m orchestrator.run_monthly --brands=zip` |

**If activation is blocked** ("running scripts is disabled"), allow it once for your user:
```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

**Loading credentials on Windows.** There is no `source .env`. Either set each variable for the session:
```powershell
$env:HYUNDAI_USER="..."; $env:HYUNDAI_PASS="..."
```
…or load the whole `.env` file at once with this one-liner (run it after activating the venv, before crawling):
```powershell
Get-Content .env | Where-Object { $_ -match '^\s*[^#].+=' } | ForEach-Object { $k,$v = $_ -split '=',2; [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim()) }
```

**Scheduling on Windows (optional).** The `cron/` setup is Linux-only. On native Windows, use **Task Scheduler** to run, on your schedule, a small `.bat`/`.ps1` that does: `cd` to the project → activate the venv → load `.env` → `python -m orchestrator.run_monthly`. (Under WSL2, the normal cron instructions in `docs/deployment.md` work.)

> **Note on speed:** native Windows and WSL2 perform comparably for this workload. Pick WSL2 if you also want the monthly cron; pick native if you just run it on demand.

---

## WAY 2 — Claude Code assisted setup

> **Works on Windows too** — Claude Code runs on Windows (PowerShell or WSL2). It will detect your OS and give you the correct commands automatically. WSL2 is still the smoothest underlying environment.

If you'd rather have an AI assistant do the setup and answer questions as you go, use **Claude Code** (Anthropic's terminal coding agent). The project ships with a `CLAUDE.md` file that already documents every rule and command, so Claude Code can install and verify the project for you.

### Step 2.1 — Install Claude Code
You need Node.js 18+ installed first (https://nodejs.org). Then:
```bash
npm install -g @anthropic-ai/claude-code
```
(You will need an Anthropic account / API access — see https://claude.com/claude-code.)

### Step 2.2 — Open the project folder in Claude Code
```bash
cd /path/to/spinny_crawler          # the unzipped folder
claude
```
Claude Code starts in the project folder and automatically reads `CLAUDE.md`.

### Step 2.3 — Ask it to install the project
Paste this prompt:

> Read `CLAUDE.md` and `docs/deployment.md`, then set this project up on my machine: create a Python 3.11 virtual environment, install dependencies with `pip install -e .[dev]`, install the Playwright Chromium browser, and run the verification self-checks. Tell me each command before you run it, and stop if any check fails.

Claude Code will run the same steps as Way 1, explain what each does, and fix common issues (missing Python, missing Chromium libraries) as they arise.

### Step 2.4 — Add credentials
Tell Claude Code:

> Copy `.env.example` to `.env`. I'll paste the 6 dealer credentials Spinny gave me; put them in `.env` and confirm it's git-ignored.

> ⚠️ **Security:** only paste credentials into your own local Claude Code session. Never commit `.env`.

### Step 2.5 — Ask it to run a test
> Run a fast smoke test with `--brands=zip`, then a small credentialed test for Hyundai, and show me where the output files are.

### Step 2.6 — Full run
> Now run the full monthly crawl for all 26 brands and report the per-brand status when it finishes.

> **Tip:** Because `CLAUDE.md` documents all the per-brand quirks and tuning knobs, you can also ask Claude Code things like *"why is ZF slow and how do I speed it up?"* or *"re-run just the brands that failed."*

---

## 2. Tuning knobs (optional)

Set these as environment variables before running (or add to `.env`). Defaults are sensible; you usually don't need to change them.

| Variable | Default | Effect |
|---|---|---|
| `CRAWLER_MAX_WORKERS` | 4 | How many brands run in parallel |
| `HYUNDAI_MAX_YEARS` / `TOYOTA_MAX_YEARS` | 1 | 1 = current model year only (recommended) |
| `HYUNDAI_FETCH_MRP` | 0 | 1 = also fetch Hyundai MRP/price (slower; ~45 min, ~76% coverage) |
| `ZF_USE_CACHE` | 0 | 1 = fast ZF run (~15 min) using last run's cache; 0 = full discovery (~3.5 h) |
| `MAHINDRA_PART_LEVEL` / `MG_PART_LEVEL` | 1 | 1 = detailed per-part data; 0 = faster summary level |

> **First run:** leave `ZF_USE_CACHE=0` (no cache exists yet). After the first successful run, set `ZF_USE_CACHE=1` for monthly runs to cut ZF from ~3.5 h to ~15 min. Refresh with a full run every 3 months.

---

## 3. Understanding the output

A run creates `output/<YYYYMMDD>/` containing:

| File | What it is |
|---|---|
| `<brand>_<date>.csv` / `.json` | One file per brand (e.g. `maruti_20260613.csv`) |
| `spinny_oem_master_<date>.csv` / `.json` | All brands combined into one de-duplicated file (the main deliverable) |
| `run_summary_<date>.json` | Per-brand status (`success` / `partial` / `failed`) and row counts |

**Status meanings** (per row and per brand):
- **success** — all required fields present
- **partial** — a required field isn't shown on that site (e.g. some MRPs), so it's left blank — this is expected, not an error
- **failed** — the brand couldn't be fetched at all (investigate)

---

## 4. Running it automatically every month (server, optional)

If you put this on an always-on Linux server, you can schedule it with cron so it runs the first Monday of each month. Full instructions are in **`docs/deployment.md` Sections 6–8** (cron setup, email alerts, cloud upload). For a laptop / on-demand use, you can skip this — just run the command in Section 1.9 whenever you need fresh data.

---

## 5. If something goes wrong

| Symptom | Fix |
|---|---|
| `command not found: python3.11` | Install Python 3.11 (Section 1) |
| `No module named 'playwright'` | You forgot `source .venv/bin/activate` |
| `Executable doesn't exist …` | Run `playwright install chromium` |
| One brand returns 0 rows | Check its credentials are loaded: `env \| grep HYUNDAI` |
| Run hangs 30+ min | Kill it (`ps aux \| grep run_monthly`, then `kill <pid>`) and re-run that one brand |

The full troubleshooting list is in **`docs/deployment.md` Section 10**, and per-brand diagnostics are in **`docs/runbook.md`**.

---

## 6. Where to read more

| Document | Purpose |
|---|---|
| `SPIDER_REFERENCE.docx` | How every one of the 26 spiders works (this handover pack) |
| `docs/deployment.md` | Full deployment guide incl. cron, alerts, cloud upload |
| `docs/runbook.md` | What to do when a spider breaks |
| `docs/per_site_notes.md` | Deepest per-brand technical notes (read before editing a spider) |
| `CLAUDE.md` | Project rules and locked decisions |
| `BRD/` | The original requirement document + field sheet |
