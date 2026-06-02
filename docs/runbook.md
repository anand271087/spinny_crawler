# Runbook — Spinny OEM Spare Parts Crawler

Operational guide for the in-house team that takes over the crawler post-handover.

**Audience**: Spinny engineers responsible for the BRD §8 SLAs (24-hour breakage notify, 5-business-day fix).

**One-line responsibility**: ensure that every first Monday of the month, the crawler runs successfully and the consolidated master file lands in the shared cloud folder by 12:00 IST Tuesday.

---

## 1. What you check every month

The monthly run is fully automated via cron. You only intervene on failure. Routine ops:

| When | What | Where |
|---|---|---|
| Tuesday morning after first Monday | Email subject `[Spinny Crawler]` arrives only if something is wrong | Inbox of the recipient list in `config/alerts.yaml` |
| If email arrives | Open `run_summary_<YYYYMMDD>.json` in shared cloud folder | path TBD at kickoff |
| If no email arrives | Quick spot-check: master CSV exists, row count is in expected range (~12K rows once all 19 brands ship) | shared cloud folder |

---

## 2. Reading `run_summary_<YYYYMMDD>.json`

This is the single artefact that tells you whether the run succeeded.

```json
{
  "run_date": "2026-05-18",
  "brands": {
    "mobil":   {"status": "success", "rows": 410,  "errors": 0},
    "exide":   {"status": "partial", "rows": 48,   "errors": 0},
    "hella":   {"status": "success", "rows": 1666, "errors": 0},
    ...
  },
  "dom_changes": [
    {"brand": "monroe", "kind": "row_count_drop",
     "current": "245 (was 326, -25%)", "baseline": 326, "severity": "medium"}
  ]
}
```

### Status meanings
| Status | Meaning | Action |
|---|---|---|
| `success` | All required fields per the xlsx were captured for every row | None |
| `partial` | Some rows had blank required fields (field missing on live site for that product) — BRD §7 expected behavior | None unless % is unusually high |
| `failed` | Spider crashed or returned 0 rows | **Investigate within 24h per BRD §8** |

### `dom_changes` severity levels
| Severity | Examples | Action |
|---|---|---|
| `high` | status regression, brand missing, zero rows | **Investigate within 24h**; baseline NOT auto-advanced (so re-runs still trigger alerts) |
| `medium` | row count dropped >25% | Investigate this week; usually a site adding/removing products |

---

## 3. Common failure scenarios

### 3.1 A spider failed (`status: failed`)

1. Check the log: `/var/log/spinny_crawler/cron.log` on the VM (most recent run).
2. Find the brand: `grep brand=<brandname> /var/log/spinny_crawler/cron.log`.
3. Common root causes:
   - **Site HTML changed**: spider's selector or regex no longer matches. Fix the relevant spider file.
   - **Site offline**: 4xx/5xx; usually transient. Re-run the brand: `python -m orchestrator.run_monthly --brands=<brandname>`.
   - **Anti-bot triggered**: Cloudflare/Akamai may have been added — out of BRD §7 scope; raise scope-change ticket.

### 3.2 Row count dropped >25%

1. Open the previous month's per-brand file. Compare to current.
2. Check the live site — has the catalog actually shrunk? (Brand discontinued products.)
3. If site count matches our number → real change, no fix needed. Manually advance baseline:
   ```bash
   cp output/<YYYYMMDD>/run_summary_<YYYYMMDD>.json state/last_successful.json
   ```
4. If site count is much higher than ours → spider missed products. Investigate selectors.

### 3.3 Brand missing from summary

Means the spider crashed before producing any output (e.g., Python exception in the spider's first call). Check log for stacktrace. Most common: dependency missing or VM env drift.

### 3.4 DOM change detected (specific patterns)

#### v1.0 brands

| Brand-specific pattern | Likely cause | Fix |
|---|---|---|
| Mobil zero rows | Coveo widget changed; `aq` filter or sitecoreItemUri may have shifted | Re-run Mobil's capture-replay sequence; if changed, the page's own request body is auto-captured each run so this is usually self-healing |
| HELLA zero rows | shop4hella.com URL structure changed (xlsx URL was already broken; we use a workaround) | Inspect `/listing/Shop4Hella/Shop4Hella/<cat>` URLs and breadcrumbs; update if needed |
| Exide row count change | Exide added/removed a 4W family | Update `PV_FAMILY_SLUGS` in `spiders/exide.py` |
| Gabriel row count change | New PDF edition published; section page-ranges shifted | Verify PDF Index page 2 + update `SECTION_PAGE_RANGES` in `spiders/gabriel.py` |
| LUMAX row count change | New monthly price list PDF; auto-discovered by URL pattern — usually self-heals | Check PDF anchor regex matches the new filename |
| Monroe row count change | BKS Motors added/removed Monroe stock | No fix; site is canonical |
| Technix row count change | Technix added new products via WordPress | Usually clean — site has duplicate posts that we dedupe; if dedup logic changes, audit `spiders/technix.py::_enumerate_posts` |
| ZIP row count change | ZIP added/removed filters, OR a new 2W brand appears in the catalog | If new 2W brand: add it to `NON_4W_PREFIX` regex in `spiders/zip.py` |
| Schaeffler 403 / status=failed | Akamai WAF tightened its fingerprint check | Update `WAF_BYPASS_HEADERS` in `spiders/schaeffler.py` with current Chrome version's `sec-ch-ua` value. Re-test by running `python state/probe_schaeffler.py`. |
| Schaeffler row count change | RepXpert catalogue refreshed — products added/discontinued | Site canonical; no fix |
| VALEO status changes | Site still has no public catalog (known issue) | No fix; remains `failed` until Spinny clarifies the data source |

#### v2.0 OEM additive

| Brand-specific pattern | Likely cause | Fix |
|---|---|---|
| Hyundai/Toyota login fails or `sbsepc5s` token not captured | SNAP-ON SDK changed detection signature OR header naming | Verify `--disable-blink-features=AutomationControlled` + `webdriver=undefined` init script in `spiders/snapon_rest.py::_playwright_login`. If header names changed, capture a fresh login XHR via `state/probe_snapon_headers.py`. |
| Hyundai/Toyota 401 on `/datasets` or `/pages/parts/` | Captured tokens expired OR `x-client-version` skewed | Restart the spider (login is part of crawl). If persistent, re-capture `x-client-version` via the headers probe and update fallback in `spiders/snapon_rest.py`. |
| Hyundai/Toyota empty rows | Year-sort regression — picked the wrong (oldest) catalog year | Verify `MAX_YEARS=1` is set and snapon_rest.py sorts year nodes descending. Sample row's `compatible_car_model` must begin with `2026 > …`. |
| Hyundai/Toyota MRP still empty | Picklist API integration deferred (see V2.2 notes); rows ship `partial` | Not a bug — pending next-phase work. Item code, name, compatibility unaffected. |
| Mahindra/MG login failure ("captcha length != 4") | OCR misread; spider auto-retries up to 6× | No fix unless retry budget is exhausted. If consistent failure: re-test ddddocr with a captured captcha image; site may have changed captcha length/font. |
| Mahindra/MG "user-type 'Other User' option not found" | Mahindra renamed the option | Inspect dropdown options at runtime via Playwright debug. Update the option-text matcher in `_login_once`. |
| Mahindra "category click → 0 entries" | UI tree structure changed | Tree response is captured via `page.on('response')`. Inspect `state/mahindra_*.json` after a debug run to confirm response shape. |
| Tata `Go to Catalogue` click never lands on `frmtataadminmodelnew.aspx` | ASP.NET cascade flow broke | Re-run probes under `state/probe_tata_*.py` to identify which dropdown changed. The 7-level cascade order is locked; only postback URLs/IDs typically rotate. |
| Ford 401 on `/v3/section/<vid>/children` | Bearer JWT expired or new IFM header required | The spider captures auth headers from a live SPA request on each run — so JWT refresh is automatic. If header set changed, update the `auth_headers` filter in `spiders/ford.py`. |
| Maruti row count >10% drop | Maruti added a new vehicle the public API doesn't expose, OR site moved to filtered pagination | Manually verify `/api/sitecore/MSGP/GetFilter` still returns ~29K. If not: re-discover the page's request body. |

See [per_site_notes.md](per_site_notes.md) for full investigation context per brand.

---

## 4. VM access + log locations

**VM**: Spinny-provided Linux box; details at kickoff.

| Resource | Path |
|---|---|
| Project root | `/opt/spinny_crawler` |
| Python venv | `/opt/spinny_crawler/.venv` |
| Cron log | `/var/log/spinny_crawler/cron.log` |
| Weekly canary log | `/var/log/spinny_crawler/canary.log` |
| State (DOM-change baseline) | `/opt/spinny_crawler/state/last_successful.json` |
| Latest output | `/opt/spinny_crawler/output/<YYYYMMDD>/` |
| Cron entry | `crontab -l` |

### Useful commands on the VM

```bash
# View last 200 lines of cron log
tail -n 200 /var/log/spinny_crawler/cron.log

# Manually trigger a full monthly run
cd /opt/spinny_crawler && source .venv/bin/activate
python -m orchestrator.run_monthly --output-dir output/

# Trigger one brand for debugging
python -m orchestrator.run_monthly --brands=monroe --output-dir output/

# Verify cron is scheduled
crontab -l | grep spinny_crawler

# Force a baseline reset (after a legitimate scope change)
rm state/last_successful.json
# Next run will re-establish baseline from its own results
```

---

## 4a. Parallelism (added 2026-05-20)

The orchestrator runs brand spiders in parallel via `ProcessPoolExecutor`.

### Default behaviour

- **4 workers** in parallel (set in `cron/run_monthly.sh` via `CRAWLER_MAX_WORKERS=4`).
- Each brand crawls in its own subprocess — one crash never affects others.
- Per-brand `elapsed_s` recorded in `run_summary.brands.<brand>.elapsed_s`.
- Wall-clock total in the final orchestrator log line: `all brands done — wall-clock Xs`.

### Tuning

| Knob | Where | Default | When to change |
|---|---|---|---|
| `CRAWLER_MAX_WORKERS` | env var | `4` | Raise to 6-8 on VMs with ≥8 GB RAM. Drop to 1 for debugging. |
| `--max-workers N` | CLI flag | from env | One-off override (e.g., `--max-workers=1` for sequential debug). |

### Why 4 by default

| Workers | Approx peak RAM | Per-site politeness | Full-run wall-clock |
|---:|---:|---|---:|
| 1 (sequential) | ~200 MB | ✓ | ~11h (ZF 3.5h + Tata 1h + Mahindra + serial others) |
| **4** | **~800 MB** | ✓ (each brand = different domain) | **~3.5h** (gated by ZF) |
| 8 | ~1.6 GB | ✓ | ~3.5h (diminishing returns) |
| 26 (all parallel) | ~5 GB | ✓ | ~3.5h (ZF remains the gate) |

The hard floor is **ZF's runtime (~3.5h with async-concurrency-3 default, 2026-05-28)** — no amount of brand-level parallelism shrinks below that, because ZF is one brand. To go faster than 3.5h, ZF itself must be sliced (mfr-chunked runs); see §7 ZF entry and `per_site_notes.md §20` for the technique.

**Comparative wall-clocks for the long tail** (informational — most other brands finish in < 30 min):
| Brand | Approx wall-clock | Bottleneck |
|---|---|---|
| **ZF** | **~3.5h** (was 7.6h before async refactor) | 30K API calls, ZF WAF blocks high concurrency |
| Tata | ~58 min | ASP.NET artbom navigation, ~21 categories × ~25 illustrations |
| Maruti | ~30 min | Pagination depth (29K rows) |
| Mahindra | ~10 min | Captcha solve + drill chain |
| Schaeffler | ~3 min | Single OCC endpoint, server-paged at 120/page |
| Other 21 brands | < 5 min each | Various |

### Per-brand politeness in parallel mode

Parallelism is at the **brand level**, not the request level. Each brand has its own domain, so when 4 brands run at once we're hitting 4 *different* sites with 1 worker each — no site sees a higher request rate than in sequential mode. BRD §7 politeness requirements are preserved.

### Debugging

Single-brand reproduction is always sequential per-brand internally. To debug an orchestrator issue, force sequential mode:

```bash
python -m orchestrator.run_monthly --max-workers=1 --brands=<brand> --config config/sites.yaml
```

### What's NOT parallelized

- Within a single brand — Mahindra/MG drill chains and most cascading-dropdown spiders are sequential by design (each click depends on prior server state). HELLA detail fetches are already 8-wide async inside the spider.
- Aggregation (master CSV, run_summary, dom_change_detector) — runs serially after all spiders complete. Fast (~seconds).

---

## 5. Updating a spider when a site breaks

The most common maintenance task.

### Workflow

1. **Investigate**: Use the diagnostic recipe from [CLAUDE.md "Spider-build playbook"](../CLAUDE.md) — static fetch first, then Playwright if needed.
2. **Reproduce**: Run only that brand: `python -m orchestrator.run_monthly --brands=<brandname>`.
3. **Locate the issue**: Open `spiders/<brand>.py`. The selectors / regexes are at the top; the parse logic is in `_parse_cards()` or equivalent.
4. **Fix**: Update the selector / regex. Test interactively first with a Python REPL or a one-off `python -c` script.
5. **Verify**: Re-run the brand. Confirm row count is in the expected range.
6. **Document**: Update `docs/per_site_notes.md` with the change and date.
7. **Commit**: `git commit -m "fix: <brand> selector after site update <YYYY-MM-DD>"`
8. **Update CLAUDE.md** if the breakage taught us something reusable (e.g., a new anti-pattern).
9. **Notify Spinny stakeholders**: status email closing the BRD §8 24h notification thread.

---

## 6. Escalation path

| Issue | Who to contact |
|---|---|
| A spider broke and 5-business-day fix is at risk | Vendor on-call (you) — fix or request scope-change |
| A new site needs to be added (out of BRD scope) | Spinny Catalog / OES Datasources team — change-request flow per BRD §8 |
| Site requires CAPTCHA / login (BRD §7 assumption violated) | Spinny + Vendor jointly — formal scope-change ticket |
| VM unavailable / disk full / OS issues | Spinny IT / DevOps |
| Cloud destination upload failing | Spinny IT + Vendor (check credentials + path) |

---

## 7. Per-spider quick reference

### Per-brand wall-clock + row count (production reality as of 2026-05-28)

Sorted by wall-clock descending — the long tail dictates total run time (`CRAWLER_MAX_WORKERS=4` makes ZF the gate).

| # | Brand | Pattern | Rows | Wall-clock | Status | Key dependency |
|---|---|---|---:|---:|---|---|
| 1 | **ZF** | json_api_pagination / Vue REST | **709** | **3h 30m** (first run) / **~15m** with `ZF_USE_CACHE=1` | success | httpx.AsyncClient + no-keepalive (WAF) |
| 2 | **Tata** | cascading_dropdown / ASP.NET TreeView + artbom | **5,785** | **3h 23m** | partial | Playwright |
| 3 | Maruti | json_api_pagination | 29,313 | ~8m | success | httpx |
| 4 | **Hyundai** | json_api_pagination / SNAP-ON REST (year=2026) | **22,043** | **~12m** | partial | Playwright (15s login) + httpx |
| 5 | Mahindra | cascading_dropdown / Intelli Catalogue v11.0 | 4,180 | ~10m (representative scope) | success | Playwright + ddddocr |
| 6 | **Toyota** | json_api_pagination / SNAP-ON REST (year=2026) | **26,815** | **~13m** | partial | Playwright (15s login) + httpx |
| 7 | MG | cascading_dropdown / Intelli Catalogue v11.0 | 833 | ~5m | success | Playwright + ddddocr |
| 8 | HELLA | multi_level_category / detail enrichment | 1,666 | ~4m | success | httpx 8-wide async |
| 9 | Technix | flat_list / WordPress wp-json | 1,225 | ~4m | success | httpx only |
| 10 | Schaeffler | json_api_pagination / SAP Spartacus + WAF bypass | 17,749 | ~3m | partial | httpx (manual WAF headers) |
| 11 | Bosch | pdf_brochure | 1,664 | ~3m | partial | pdfplumber |
| 12 | Spark Minda | multi_level_category | 1,296 | ~3m | success | Playwright |
| 13 | AMARON | cascading_dropdown / Drupal | 52 | ~3m | success | Playwright |
| 14 | SF SONIC | cascading_dropdown / Drupal async | 1,530 | ~3m | success | httpx async |
| 15 | UNO MINDA | search pagination | 525 | ~3m | partial | Playwright |
| 16 | VALEO | json_api_pagination / TecAssist | 5,286 | ~3m | success | httpx |
| 17 | Ford | json_api_pagination / Microcat | 18 | ~2m (history-only scope) | success | Playwright (auth capture) |
| 18 | AUTOKOI | pdf_brochure (post-2026-05-22) | 1,387 | ~1m | partial | httpx + pdfplumber |
| 19 | Mobil | flat_list / Coveo capture-and-replay | 410 | ~55s | success | Playwright |
| 20 | TVS Girling | multi_level_category (2 URLs merged) | 909 | ~30s | partial | httpx |
| 21 | JK Tyre | static URL tree (RSC dodge) | 23 | ~30s | success | httpx |
| 22 | Lumax | pdf_brochure | 2,185 | ~7s | success | pdfplumber |
| 23 | Gabriel | pdf_brochure (post-2026-05-19 reclassification) | 2,209 | ~5s | success | pdfplumber |
| 24 | Monroe | flat_list / Magento | 326 | ~3s | success | httpx |
| 25 | Exide | multi_level_category + MRP PDF | 53 | ~3s | success | httpx + pdfplumber |
| 26 | ZIP | multi_level_category | 811 | ~2s | success | httpx |
| | **TOTAL (per-brand row sum)** | | **127,605** | | | |
| | **MASTER (after BRD §5 dedup)** | | **120,575** | **~3h 30m**\* | | |

\*With `CRAWLER_MAX_WORKERS=4` — the 25 non-ZF brands all finish within ZF's window. Sequential total would be ~6h. With `ZF_USE_CACHE=1` on the second-and-onwards run, the whole cycle drops to ~15-20 min total because ZF (the long pole) shrinks to 15 min.

**Notes:**
- "Partial" status = some BRD-required fields blank because the live site doesn't expose them (e.g. Schaeffler MRP "on request", Hyundai/Toyota MRP via picklist API not yet wired, Tata low-value parts with null MRP). Honors BRD §7 — row preserved with `crawl_status=partial`.
- Mahindra/MG defaults are "representative scope" (`MAX_VARIANTS=1`) per per_site_notes §V2.4/V2.5. Full variant walk would be ~10h/~3h respectively but is out of monthly SLA — rerun with env overrides if needed.
- Hyundai/Toyota are **SNAP-ON REST** (rewritten 2026-05-30 per per_site_notes §V2.2). 100× more data per run vs legacy AG-Grid clicker. MRP fetch via picklist API is the next-phase upgrade — until then rows ship as `partial` with item_code/name/compat fully populated.
- ZF was 7.6h before the 2026-05-28 async refactor → 3.5h after; with hit-cache (2026-05-30) cached runs drop to ~15 min. See per_site_notes §20 for the cache cadence.

**Full extraction details for each**: [docs/per_site_notes.md](per_site_notes.md)

---

## 7a. Cross-cutting techniques the runbook depends on

Three techniques recur across multiple spiders. Read these before diagnosing related failures.

### 7a.1 Captcha OCR (Mahindra, MG)

**Library**: [ddddocr](https://github.com/sml2h3/ddddocr) — installed via `pip install ddddocr`.

**Failure mode**: spider logs `captcha OCR -> 'XYZ'` followed by `captcha length 3 != 4, retrying`. This is benign — auto-retry handles it.

**True failure**: 6 consecutive misreads → spider returns 0 rows. If you see this:
1. Run `python state/probe_mahindra_captcha.py` (or `_mg_*`) to capture a fresh captcha PNG.
2. Open the PNG to verify it's actually a 4-char text captcha. If the site switched to reCAPTCHA/hCaptcha — escalate to Spinny (scope-change).
3. If still text-based but ddddocr accuracy dropped, increase `MAHINDRA_LOGIN_RETRIES` to 10.

### 7a.2 Akamai WAF bypass (Schaeffler)

**Failure mode**: spider logs `schaeffler: first page fetch failed (WAF?)`.

**Diagnosis**:
1. Check if the IP is rate-limited: run `curl https://vehiclelifetimesolutions.schaeffler.in/robots.txt` from the VM. If 200 → IP fine, headers wrong. If 403 → IP-flagged, wait 30 min and retry.
2. If headers wrong: open `spiders/schaeffler.py`, verify `WAF_BYPASS_HEADERS` includes a current Chrome version's `sec-ch-ua` string. Update if Chrome major-version has advanced significantly (e.g. from 131 → 140+).

### 7a.3 SAP Commerce Spartacus OCC discovery (Ford, Schaeffler)

**Failure mode**: 401 / 404 from a `/api/...` endpoint that used to work.

**Diagnosis**:
1. Capture the live SPA's request headers: spider uses `page.on('request')` to grab the current Bearer JWT. JWT expiry is handled automatically each run.
2. If a new IFM header appears in the live SPA's request that isn't in our `auth_headers` filter, add it.

---

## 8. What this runbook deliberately does NOT cover

- **Adding new brands**: that's a BRD §8 change-request task. Follow the [CLAUDE.md spider-build playbook](../CLAUDE.md) and write a per-site-notes entry first.
- **Changing the schema** (adding/removing columns): also a change-request — affects downstream consumers.
- **Cron scheduling changes** (e.g., switching to bi-monthly): out-of-cycle change per BRD §8. Edit `cron/crontab.txt`, install on VM, document the new SLA window.
- **Master file dedup logic**: locked per BRD §5. If you suspect dedup is wrong, read `lib/output.py::write_master` — it implements the BRD's two-rule dedup verbatim.
