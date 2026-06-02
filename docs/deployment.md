# Deployment & Operations Guide

End-to-end instructions for installing and running the Spinny OEM Spare-Parts Crawler **on a laptop (development / one-off runs)** or **on a Linux server (production / monthly cron)**.

**Audience**: Spinny in-house engineer or DevOps person taking over post-handover.

**Time to complete a fresh install**:
- Laptop: ~20 min
- Server: ~30-45 min (extra steps for cron + cloud upload)

---

## Quick decision tree

> **"I just want to run it once and see the output."** → Sections 1, 2, 3, 5
> **"I want to set up the production monthly cron."** → Sections 1, 2, 3, 4, 6, 7, 8, 9
> **"Something broke."** → Section 10 (troubleshooting)
> **"I want to understand the output files."** → Section 5.3

---

## 1. System requirements

### Hardware
| Resource | Minimum | Recommended | Notes |
|---|---|---|---|
| CPU | 2 cores | 4 cores | More cores ≠ faster (gated by single brands like ZF) |
| RAM | 2 GB | 4 GB | Each parallel worker uses ~200 MB; 4 workers = ~800 MB |
| Disk | 10 GB free | 20 GB free | Chromium 1 GB + monthly outputs ~500 MB × 12-month retention |
| Network | unrestricted HTTPS | unrestricted | Spider talks to ~30 different .in / .com domains |

### Software
| Component | Required version | How to verify |
|---|---|---|
| OS | macOS 11+ (laptop) OR Ubuntu 22.04+ / Debian 12+ (server) | `uname -a` |
| Python | **3.11 or 3.12** | `python3.11 --version` |
| Git | any recent | `git --version` |
| pip | latest | `pip --version` |

> **Important**: Python 3.10 and earlier will NOT work — the codebase uses 3.11+ syntax (e.g. PEP 604 `X \| Y` type unions).

### Installing Python 3.11

**macOS (laptop)** — easiest is Homebrew:
```bash
# If Homebrew is not installed:
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Then:
brew install python@3.11
```

**Ubuntu / Debian (server)**:
```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev git curl
```

**Verify**:
```bash
python3.11 --version
# Should print: Python 3.11.x
```

---

## 2. Get the codebase

You'll be handed the project as a **git repository URL** (preferred) or as a **zip file**.

### 2a. From a git repository (preferred)
```bash
# Choose a location. On a server, /opt is conventional. On a laptop, anywhere works.
# Server:
sudo mkdir -p /opt/spinny_crawler
sudo chown $USER:$USER /opt/spinny_crawler
cd /opt/spinny_crawler

# Laptop (e.g. macOS):
mkdir -p ~/Desktop/spinny_crawler
cd ~/Desktop/spinny_crawler

# Clone:
git clone <repo-url> .
```

### 2b. From a zip handed over
```bash
unzip spinny_crawler.zip -d /opt/spinny_crawler   # or your chosen path
cd /opt/spinny_crawler
```

### Verify
```bash
ls
# Expected: orchestrator/  spiders/  lib/  config/  cron/  docs/  BRD/  state/  pyproject.toml  CLAUDE.md
```

If any of the top-level directories are missing, the handover is incomplete — ask for a fresh export.

---

## 3. First-time install (~10 min)

### Step 3.1 — Create a Python virtual environment

A venv keeps the project's dependencies isolated from system Python (avoids "works on my machine" issues).

```bash
cd /opt/spinny_crawler   # or your laptop path
python3.11 -m venv .venv
```

A `.venv/` directory will appear.

### Step 3.2 — Activate the venv

You must do this in EVERY new terminal session before running the crawler.

```bash
source .venv/bin/activate
```

You'll see your prompt change to `(.venv) $ …`. From now on, `python` and `pip` point to the venv.

> **Tip**: To make this automatic, add `cd /opt/spinny_crawler && source .venv/bin/activate` to your `~/.bashrc` (server) or `~/.zshrc` (laptop).

### Step 3.3 — Upgrade pip
```bash
pip install --upgrade pip
```

### Step 3.4 — Install Python dependencies

```bash
pip install -e .[dev]
```

This reads `pyproject.toml` and installs everything: httpx, Playwright, pdfplumber, pandas, scrapy, parsel, ddddocr, etc. Takes ~3-5 minutes.

### Step 3.5 — Install Playwright's Chromium browser

The crawler uses headless Chromium for the brands that require a real browser (Hyundai, Toyota, Mahindra, MG, Tata, AMARON, Schaeffler).

```bash
playwright install chromium
```

This downloads ~150 MB. On a server you also need the system libraries Chromium depends on:

```bash
# Ubuntu / Debian only:
playwright install-deps chromium
```

On macOS this step is not needed (Apple's frameworks ship the libraries).

### Step 3.6 — Verify the install

Run this self-check:
```bash
python -c "
from playwright.sync_api import sync_playwright
import pdfplumber, pandas, scrapy, httpx, yaml, parsel, ddddocr
print('python deps: OK')
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=['--no-sandbox'])
    b.close()
print('playwright chromium: OK')
"
```

Expected output:
```
python deps: OK
playwright chromium: OK
```

If this fails, see **Section 10 (troubleshooting)** before proceeding.

### Step 3.7 — Verify all 26 spiders import cleanly

```bash
python -c "
import importlib
brands = ['amaron','autokoi','bosch','exide','ford','gabriel','hella','hyundai',
          'jk_tyre','lumax','mahindra','maruti','mg','mobil','monroe','schaeffler',
          'sf_sonic','spark_minda','tata','technix','toyota','tvs_girling',
          'uno_minda','valeo','zf','zip']
fails = []
for b in brands:
    try: importlib.import_module(f'spiders.{b}')
    except Exception as e: fails.append((b, str(e)[:80]))
print(f'{len(brands)-len(fails)}/{len(brands)} spiders import cleanly')
for b,e in fails: print(f'  FAIL {b}: {e}')
"
```

Expected: `26/26 spiders import cleanly`

If any spider fails to import, do not proceed — investigate with the runbook.

---

## 4. Credentials and secrets (~5 min)

Six of the 26 brands require dealer credentials (the other 20 have public catalogs). The codebase ships with **NO hardcoded credentials** — public-repo policy. All credentials must be supplied via environment variables. Spinny provides the actual values out-of-band; the [.env.example](../.env.example) file at the repo root is the template.

### 4.1 Credentials currently in scope

The codebase does NOT ship with any hardcoded credentials (public-repo policy). Credentials are supplied via environment variables — see `.env.example` at the repo root for the template.

| Brand | Env vars |
|---|---|
| Hyundai | `HYUNDAI_USER`, `HYUNDAI_PASS` |
| Toyota | `TOYOTA_USER`, `TOYOTA_PASS` |
| Tata | `TATA_USER`, `TATA_PASS` |
| Mahindra | `MAHINDRA_USER`, `MAHINDRA_PASS` |
| MG | `MG_USER`, `MG_PASS` |
| Ford | `FORD_USER`, `FORD_PASS` |

The other 20 brands have public catalogs — no credentials needed.

**Spinny will provide the actual credential values out-of-band** (they're not in this repo and never should be). Ask the project owner for the real values.

### 4.2 Setting credentials (laptop, temporary)

Copy the template:
```bash
cp .env.example .env
# Edit .env with your actual credentials (Spinny provides these)
```

Then load them into the current shell before running:
```bash
set -a; source .env; set +a
```

Or export individually:
```bash
export HYUNDAI_USER=<your-username>
export HYUNDAI_PASS=<your-password>
```

`.env` is in `.gitignore` so it never gets committed.

### 4.3 Setting credentials (server, persistent)

Add to `/etc/environment` so they survive reboots and are visible to cron:
```bash
sudo tee -a /etc/environment <<'EOF'
HYUNDAI_USER=<your-username>
HYUNDAI_PASS=<your-password>
TOYOTA_USER=<your-username>
TOYOTA_PASS=<your-password>
TATA_USER=<your-username>
TATA_PASS=<your-password>
SMTP_PASSWORD=<from-spinny-it>
EOF

# Reload:
source /etc/environment
```

> **Security note**: `/etc/environment` is world-readable on Linux by default. For higher-security environments, use a secrets manager (Vault, AWS Secrets Manager, etc.) and have `cron/run_monthly.sh` fetch them at runtime.

### 4.4 Verify credentials work

```bash
# Quick login test for Hyundai (takes ~25 seconds):
HYUNDAI_MAX_YEARS=1 HYUNDAI_MAX_MODELS=1 python -m orchestrator.run_monthly --brands=hyundai
```
Expected: terminates after ~12-15 min with a `hyundai_<YYYYMMDD>.csv` containing ~22K rows. If you want a faster sanity check (just login + one model leaf), use:
```bash
HYUNDAI_MAX_YEARS=1 HYUNDAI_MAX_MODELS=1 HYUNDAI_MAX_LEAVES=5 python -m orchestrator.run_monthly --brands=hyundai
```

---

## 5. Running the crawler (laptop or interactive)

### 5.1 Activate the venv (every new terminal)
```bash
cd /opt/spinny_crawler   # or your laptop path
source .venv/bin/activate
```

### 5.2 Single-brand runs (for testing)

The orchestrator accepts `--brands=<name>` for a one-brand run:

```bash
# Smallest spider (~2 seconds):
python -m orchestrator.run_monthly --brands=zip

# Medium (~1 minute, exercises Playwright):
python -m orchestrator.run_monthly --brands=mobil

# Two brands:
python -m orchestrator.run_monthly --brands=mobil,exide

# Pass scope caps via env (smoke test Hyundai):
HYUNDAI_MAX_MODELS=1 HYUNDAI_MAX_LEAVES=5 python -m orchestrator.run_monthly --brands=hyundai
```

### 5.3 Full monthly run (all 26 brands)

```bash
# Default: 4 parallel workers (laptop or small server)
python -m orchestrator.run_monthly

# Or set workers explicitly:
CRAWLER_MAX_WORKERS=4 python -m orchestrator.run_monthly

# Sequential mode (for debugging — slow):
python -m orchestrator.run_monthly --max-workers=1
```

**Expected wall-clock** with 4 workers: **~3.5h** (gated by ZF — see runbook §4a).

### 5.4 Output directory structure

A successful run creates `output/<YYYYMMDD>/` with:

```
output/
└── 20260530/
    ├── amaron_20260530.csv         # one per brand
    ├── amaron_20260530.json
    ├── autokoi_20260530.csv
    ├── autokoi_20260530.json
    │   ...
    ├── zip_20260530.csv
    ├── zip_20260530.json
    ├── spinny_oem_master_20260530.csv     # deduped master across all 26 brands
    ├── spinny_oem_master_20260530.json
    ├── run_summary_20260530.json          # per-brand status + counts
    └── zf_hit_cache.json                  # ZF productive-tuples cache (next-run optimization)
```

**File contents:**

| File | Schema | Used by |
|---|---|---|
| `<brand>_<date>.csv` / `.json` | Per-brand rows with brand-specific columns | Analysts inspecting one brand |
| `spinny_oem_master_<date>.csv` / `.json` | Unified schema across all 26 brands, deduped per BRD §5 | Downstream consumers (refurbishment, dealer settlement) |
| `run_summary_<date>.json` | Run metadata: per-brand `{status, rows, errors}`, dom_changes | On-call team for triage |
| `zf_hit_cache.json` | Internal optimization for next ZF run | Spider only — don't edit |

### 5.5 Useful environment-variable knobs

| Env var | Default | Effect |
|---|---|---|
| `CRAWLER_MAX_WORKERS` | 4 | Parallel brand workers |
| `ZF_USE_CACHE` | 0 | Use last month's ZF hit cache (after first successful run) |
| `ZF_CONCURRENCY` | 3 | ZF in-flight requests (don't exceed 5 — WAF risk) |
| `HYUNDAI_MAX_YEARS` | 0 | 1 = year=2026 only (recommended), 0 = all years |
| `HYUNDAI_MAX_MODELS` | 0 | 0 = all models per year |
| `TOYOTA_MAX_YEARS` | 0 | same as Hyundai |
| `TOYOTA_MAX_MODELS` | 0 | same as Hyundai |
| `MAHINDRA_MAX_VARIANTS` | 1 | "representative scope" — 1 variant per model |
| `MG_MAX_VARIANTS` | 1 | same as Mahindra |

Production recommended config:
```bash
export CRAWLER_MAX_WORKERS=4
export HYUNDAI_MAX_YEARS=1
export TOYOTA_MAX_YEARS=1
export ZF_USE_CACHE=1   # only after the first successful run created a cache file
```

### 5.6 Deactivate the venv when done
```bash
deactivate
```

---

## 6. Setting up production cron (server only)

### 6.1 Make the runner executable
```bash
cd /opt/spinny_crawler
chmod +x cron/run_monthly.sh
```

### 6.2 Inspect the runner
```bash
cat cron/run_monthly.sh
```
You should see it `cd /opt/spinny_crawler`, `source .venv/bin/activate`, and call `python -m orchestrator.run_monthly`. If the path differs (you installed elsewhere), edit the `PROJECT_DIR` variable at the top.

### 6.3 Create the log directory
```bash
sudo mkdir -p /var/log/spinny_crawler
sudo chown $USER:$USER /var/log/spinny_crawler
```

### 6.4 Install the cron entry
```bash
crontab cron/crontab.txt
```

### 6.5 Verify cron is active
```bash
crontab -l
```
You should see lines like:
```
0 22 1-7 * 1 /opt/spinny_crawler/cron/run_monthly.sh >> /var/log/spinny_crawler/cron.log 2>&1
```
(That's "first Monday of every month at 22:00 local time".)

### 6.6 Confirm cron daemon is running
```bash
# Ubuntu / Debian:
sudo systemctl status cron
# Should show: active (running)
```

### 6.7 Trigger a manual test run
You don't need to wait for the first Monday. Run it manually to confirm the cron entry-point works:
```bash
/opt/spinny_crawler/cron/run_monthly.sh
```

This should complete in ~3.5h and write `output/<today>/`. Check `/var/log/spinny_crawler/cron.log` for any errors.

---

## 7. Configure email alerts (server only)

### 7.1 Edit `config/alerts.yaml`
```yaml
smtp:
  host: smtp.spinny.internal       # or smtp.ses.us-east-1.amazonaws.com for AWS SES
  port: 587
  username: spinny-crawler
  password_env: SMTP_PASSWORD

from_address: spinny-crawler@spinny.com

recipients:
  - catalog-ops@spinny.com
  - data-eng-oncall@spinny.com

triggers:
  per_spider_failure: true
  end_of_run_below_98: true
  dom_change: true
```

### 7.2 Set the SMTP password
Add to `/etc/environment` (see §4.3).

### 7.3 Test alert delivery
```bash
source .venv/bin/activate
python -c "
from pathlib import Path
from lib.alerts import send_alert
send_alert(
    {'brands': {'_test_': {'status': 'failed', 'rows': 0, 'errors': 1}}, 'dom_changes': []},
    Path('config/alerts.yaml')
)
"
```
Check the recipient inboxes — should see a `[Spinny Crawler] test failed` email.

If it fails:
- Verify SMTP host is reachable: `nc -zv smtp.spinny.internal 587`
- Verify `SMTP_PASSWORD` is set: `echo $SMTP_PASSWORD | wc -c` (should be > 1)

---

## 8. Configure cloud upload (server only)

After each monthly run, the `output/<date>/` directory needs to land in Spinny's shared cloud folder. Three options:

### Option A — S3
```bash
sudo apt install -y awscli
aws configure   # set access key, region

# Append to cron/run_monthly.sh after the python invocation:
echo 'aws s3 sync output/ s3://spinny-oem-crawl/' >> cron/run_monthly.sh
```

### Option B — Google Drive (rclone)
```bash
sudo apt install -y rclone
rclone config   # interactive — set up GDrive remote

# Append to cron/run_monthly.sh:
echo 'rclone sync output/ spinny-gdrive:spinny-oem-crawl/' >> cron/run_monthly.sh
```

### Option C — SharePoint
Use Microsoft Graph CLI or `m365` CLI per Spinny's tenant. See Spinny IT for the exact setup.

### Local-only retention
Output stays locally for **90 days** then is auto-purged (see §11). The cloud copy is the canonical 12-month archive per BRD §8.

---

## 9. Production cutover checklist

Run this checklist on the day before your first scheduled run:

- [ ] `python3.11 --version` works
- [ ] `cd /opt/spinny_crawler && source .venv/bin/activate && python -c "from playwright.sync_api import sync_playwright; print('ok')"` passes
- [ ] All 26 spiders import (§3.7)
- [ ] Credentials set in `/etc/environment` (§4.3)
- [ ] `crontab -l` shows the monthly entry
- [ ] `sudo systemctl status cron` is active
- [ ] Manual test run completed within last 7 days (§6.7) — produced `output/<date>/spinny_oem_master_<date>.csv` ≥ 100K rows
- [ ] Email alert delivered to inbox (§7.3)
- [ ] Cloud upload succeeded — files visible in S3 / GDrive / SharePoint
- [ ] On-call rotation has the [runbook.md](runbook.md) link
- [ ] Spinny stakeholder has been told the first run will start at 22:00 IST on the first Monday and finish around 01:30 IST Tuesday

---

## 10. Troubleshooting

### "command not found: python3.11"
Install Python (§1).

### "ModuleNotFoundError: No module named 'playwright'"
The venv is not activated. Run `source .venv/bin/activate`.

### "playwright._impl._errors.Error: Executable doesn't exist..."
Chromium binary not installed. Run:
```bash
playwright install chromium
# On Linux also:
playwright install-deps chromium
```

### `pip install -e .[dev]` fails with "no module named build"
Upgrade pip first: `pip install --upgrade pip`.

### Single brand returns 0 rows
- Verify credentials are set: `env | grep -E 'HYUNDAI|TOYOTA|TATA'`
- Run the spider isolated with `--brands=<name>` and tail the output — look for `ERROR` / `Traceback` lines
- See [runbook.md §3](runbook.md) for per-brand failure patterns
- For SNAP-ON (Hyundai/Toyota): check that `compatible_car_model` of sample rows starts with `2026 > …` — if it starts with an older year, the year-sort regressed (see runbook troubleshooting)

### Run hangs / no progress for 30+ minutes
- Check the log file (`/var/log/spinny_crawler/cron.log` on server, or stdout on laptop)
- If the last line is a Playwright operation: usually a session timeout. Kill the process (`ps aux | grep run_monthly` then `kill <pid>`) and re-run the affected brand alone.

### Cron didn't fire
- `sudo systemctl status cron` — daemon must be active
- `crontab -l` — entry must be present
- `tail /var/log/syslog | grep CRON` — should show the trigger attempt
- Check `cron/run_monthly.sh` has execute bit (`ls -la cron/run_monthly.sh`)

### Out of disk space
```bash
df -h /opt
# Manually prune old runs:
find /opt/spinny_crawler/output -maxdepth 1 -type d -mtime +60 -exec rm -rf {} \;
```

### SMTP errors in cron.log
The orchestrator catches SMTP failures and continues (alerts are non-fatal). To fix the alert delivery:
- See §7

### ZF takes 6+ hours (regression)
- Verify `ZF_USE_CACHE=1` is set AND `output/<prev-date>/zf_hit_cache.json` exists
- If the cache file is missing, do a one-time full run, then enable the cache for next month

### Hyundai/Toyota MRP rows show empty `mrp`
**This is expected** — the REST refactor (2026-05-30) doesn't yet wire MRP fetch via the picklist API. Rows ship as `crawl_status=partial`. Catalog data (item_code, item_name, compatibility) is 100% populated. MRP fetch is queued as next-phase work.

---

## 11. Routine operations

| Frequency | Task | Command |
|---|---|---|
| First Monday → Tuesday of each month | Auto-runs via cron; intervene only on email alert | n/a |
| Weekly | Canary script runs 2-3 spiders to catch DOM changes early | `cron/canary.sh` |
| Monthly (post-run) | Confirm cloud upload finished + share link with stakeholders | manual |
| Quarterly | Update Python dependencies | `source .venv/bin/activate && pip install -U scrapy playwright pdfplumber pandas` |
| Quarterly | Re-run ZF without cache to refresh `zf_hit_cache.json` | `ZF_USE_CACHE=0 python -m orchestrator.run_monthly --brands=zf` |
| Annually | Renew SMTP/SES credentials, audit credential file access | manual |
| Annually | Sample-validate output against live sites (10 rows per brand) | manual |

---

## 12. Where to find more docs

| Document | Purpose |
|---|---|
| [docs/runbook.md](runbook.md) | What to do when something breaks (per-brand diagnostics, escalation paths) |
| [docs/per_site_notes.md](per_site_notes.md) | Deep technical notes per brand — read before touching any spider |
| [docs/kickoff_checklist.md](kickoff_checklist.md) | Open commercial / decision items still pending |
| [CLAUDE.md](../CLAUDE.md) | High-level project rules and locked decisions |
| [BRD/](../BRD/) | The original requirement document |

---

## 13. Quick reference — commands you'll use often

```bash
# Activate venv (every new shell)
cd /opt/spinny_crawler && source .venv/bin/activate

# Smoke test — one fast brand
python -m orchestrator.run_monthly --brands=zip

# Full production run (4 workers)
python -m orchestrator.run_monthly

# Tail today's log
tail -f /var/log/spinny_crawler/cron.log

# See per-brand status of last run
cat output/$(date -u +%Y%m%d)/run_summary_$(date -u +%Y%m%d).json | python -m json.tool

# Count rows in the master
wc -l output/$(date -u +%Y%m%d)/spinny_oem_master_$(date -u +%Y%m%d).csv

# Trigger an ad-hoc rerun of one brand (useful when one brand failed)
python -m orchestrator.run_monthly --brands=valeo
```
