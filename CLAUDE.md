# Spinny OEM Spare Parts Crawler — Claude Guidance

Instructions for any Claude session working in this repo. Read this first.

## What this is

Monthly batch crawler that extracts passenger-vehicle spare-parts catalogue data from **19 OEM/aftermarket-brand websites** for Spinny, plus **7 additive OEM EPC sources** added under v2.0 scope (Maruti, Hyundai, Toyota, Mahindra, MG, Tata, Ford). Runs via Linux cron on a Spinny-provided VM. Output → shared cloud folder (S3/GDrive/SharePoint — TBD at kickoff).

## Sources of truth — read these before changing anything

| Document | Location | What it governs |
|---|---|---|
| **BRD (Automation Requirement Document v1.0)** | `BRD/` (PDF) | Scope, fields, SLAs, output format, edge cases, assumptions |
| **Companion xlsx (OES Datasources)** | shared separately | Per-brand navigation steps + exact fields to extract — single source of truth |
| **Implementation plan** | `~/.claude/plans/understand-the-requirements-from-vast-floyd.md` | All decisions (Python stack, Firecrawl rejected, hybrid arch, etc.) |
| **Per-brand config** | [config/sites.yaml](config/sites.yaml) | URL, pattern, fields, dropdowns, gates per brand |
| **Per-site extraction notes** | [docs/per_site_notes.md](docs/per_site_notes.md) | How each spider works, site quirks, breakages, gotchas — read before touching any spider |
| **Operational runbook** | [docs/runbook.md](docs/runbook.md) | How to respond to alerts, read run_summary, fix broken spiders, escalation |
| **Deployment guide** | [docs/deployment.md](docs/deployment.md) | Step-by-step VM install, cron setup, first run, troubleshooting |
| **Kickoff checklist** | [docs/kickoff_checklist.md](docs/kickoff_checklist.md) | Open decisions Spinny must sign off on before production cutover |

If BRD and xlsx conflict on a field → xlsx wins (per BRD §3.1: "single source of truth"). Reflect updates in `config/sites.yaml`.

When a spider is shipped: append its entry to `docs/per_site_notes.md` with Functional + Technical + Site reality vs xlsx flow + Gotchas. This is operational memory the in-house team will rely on for the 24h/5biz-day breakage SLAs.

## Locked decisions — don't relitigate

| Decision | Why | Reference |
|---|---|---|
| **Python 3.11 + Scrapy + Playwright + pdfplumber + pandas** | Best PDF tooling (Bosch), Scrapy middleware saves 40h, pandas for dedup | Plan §3, §6 |
| **Self-hosted on Spinny VM via cron** | $0 infra; monthly batch doesn't need managed service | Plan §3 |
| **Firecrawl is NOT primary** | $333/mo Growth tier is poor ROI for 8h/month workload | Plan §1, §2 |
| **Firecrawl Hobby ($16/mo) only as escape hatch** | If a site genuinely resists Playwright + API interception | Plan §1 (scenario E) |
| **XHR API interception for 6 cascading-dropdown sites** | Avoids combinatorial blowup; 400K → ~5K requests | Plan §3, §12.7, §12.8, §12.11, §12.15, §12.17, §12.18 |
| **Custom Python `smtplib` for alerts** | No SaaS monitoring | Plan §4 |
| **Spinny in-house team owns post-handover maintenance** | Implies readable Python, no black boxes | Plan §3 |
| **Captcha OCR via `ddddocr`** (locally bundled, no SaaS) | Mahindra + MG both have 4-char text captcha. Tesseract failed; ddddocr ~90% per-attempt with retry → effective 100%. Free, offline, no recurring cost. | per_site_notes §V2.4, §V2.5 |
| **WAF-bypass via Sec-Fetch/sec-ch-ua headers** (Schaeffler) | Akamai blocks browser navigation lacking Chrome-131 fingerprint headers. Full header set on `ctx.request.get` reaches backend. No residential proxy needed. | per_site_notes §14 |
| **SAP Commerce Spartacus OCC pattern**: discover via captured `page.goto` network → backend at `/api/<baseSite>/...` | Schaeffler's base site is `Repxpert-IN`. Same pattern generalizes to any future Spartacus storefront. | per_site_notes §14 |
| **Adaptive recursive drill** (MG): when `figno` populated → assemblies; else drill another level | Intelli Catalogue's depth varies per brand. Detection-based drill removes hardcoded level counts. | per_site_notes §V2.5 |
| **SNAP-ON REST replaces AG-Grid Playwright** (Hyundai, Toyota): 15s Playwright login captures `sbsepc5s`+`sbsepc5cs` JWT headers → all data via httpx through `/epc-services/`. Year=2026 production scope. | Legacy AG-Grid clicker crashed 0 rows or hung silently; REST returns 22K-26K parts in 12-13 min per brand, no fragility. MRP via picklist API pending. | per_site_notes §V2.2 |
| **ZF hit-cache** writes productive `(mfr,model,vehicles,ag)` tuples to `output/<date>/zf_hit_cache.json`; subsequent runs with `ZF_USE_CACHE=1` skip the 96.8% known-empty calls | Cuts ZF from 3.5h → ~15 min for monthly cached runs. Quarterly full re-discovery refreshes the cache. | per_site_notes §20 |

## Project layout

```
spinny_crawler/
├── orchestrator/      # Entry point, fan-out, aggregation
├── spiders/           # One file per brand. _base.py defines contract.
├── lib/               # normalize, output, alerts, api_interceptor (shared)
├── config/            # sites.yaml (per-brand), alerts.yaml (SMTP + recipients)
├── cron/              # crontab.txt, run_monthly.sh
├── tests/             # pytest, one fixture HTML per brand
├── docs/              # technical.md, functional.md, runbook.md (Phase 5)
├── BRD/               # Source-of-truth docs (PDF/xlsx)
└── output/            # Date-stamped runs (gitignored)
```

## Conventions

- **Every spider** subclasses `spiders._base.BaseSpider` and overrides `crawl() -> list[Row]`. The base class auto-populates `brand`, `source_website`, `crawl_date`, and `crawl_status`.
- **`brand` column derived from config key**, capitalized. NOT scraped. ("mobil" → "Mobil")
- **`item_name` faithful to site headings.** If the site shows "Exide Epiq EPIQ35L" preserve that — even though `brand` already says "Exide". An analyst should be able to match a row back to what they see on the live site.
- **`crawl_status` rules** (BRD §4, §7):
  - `success` — all required fields present
  - `partial` — required field listed in sheet but not visible on live site → leave blank, mark partial (e.g. Exide MRP)
  - `failed` — fetch/parse error for that row or category (e.g. Bosch corrupted PDF). 0-row run also = `failed`.
- **MRP normalization** (BRD §5): always run through `lib.normalize.clean_mrp()`. Strips ₹/$/commas/whitespace. CSV: unquoted number. JSON: typed number.
- **Politeness** (BRD §7): use `User-Agent: SpinnyOEMCrawler/1.0 (contact@spinny.com)`, AutoThrottle for Scrapy, exponential backoff on 4xx/5xx.
- **Cascading dropdowns** — first attempt: Playwright + `page.on('response')` to capture the XHR API. Replay programmatically via `ctx.request.post()` in the same context (Mobil pattern — token survives, fresh httpx doesn't). UI iteration is fallback only.
- **Vehicle-segment gate** (GABRIEL, Spark Minda, JK TYRE, ZF): reject non-passenger rows in the spider, not downstream. HELLA also gated post-hoc via detail-page breadcrumb.
- **Geography gate** (ZF only): always send `languageID=4&countryID=IND`.
- **Per-brand CSV: drops empty columns; master: keeps full schema.** Implemented in `lib/output.py`. Don't reintroduce empty product columns to per-brand outputs.
- **Master dedup uses two rules per BRD §5**: rows with `item_code` → dedup on `(source_website, item_code)`; rows without → dedup on `(source_website, item_name, compatible_car_model)`. DO NOT use a single rule with NaN-coalescing — pandas collapses all NaNs to one key.
- **Strip referral params** from `cfg["url"]` (`srsltid`, `utm_*`, `gclid`, `fbclid`). See `spiders/monroe.py::_strip_referral`.

## SNAP-ON EPC (Hyundai, Toyota) — locked technique (rewritten 2026-05-30)

**Current implementation = REST API.** Legacy AG-Grid Playwright clicker is preserved at `spiders/hyundai_legacy.py` but should NOT be touched — the production code path is `spiders/snapon_rest.py`, re-exported by `spiders/hyundai.py` and `spiders/toyota.py`.

If touching `spiders/snapon_rest.py`, preserve these — discovered the hard way:

1. **Anti-detection launch args required** for the 15-second Playwright login window (`--disable-blink-features=AutomationControlled` + `navigator.webdriver=undefined`). Without them, post-login body renders empty.
2. **Login is form-encoded with base64 password**: `POST /epc-services/auth/login` with body `user=X&password=BASE64(X)`. Returns `{"sessionJwtToken":"…"}`.
3. **Two custom JWT headers carry auth**: `sbsepc5s` (= sessionJwtToken from login response) and `sbsepc5cs` (derived client-side by the SPA's JS from the JWT's SIG claim — we don't reverse it, we capture it). Both must be extracted from the FIRST `/auth/account` request via `page.on('request')` and replayed on every subsequent httpx call.
4. **Catalog tree is `serializedPath` cursors**: each `/datasets/{ds}/navigations/{sp}/filterRequest/{fr}` returns `children.childNodes[]` where each node has a `serializedPath` (base64) to drill deeper. Levels go: Dataset → Year → Model → Catalog → Group → Section (leaf).
5. **Parts endpoint at the leaf**: `GET /datasets/{ds}/pages/parts/{sp_leaf}/filterRequest/{fr}` returns `partItems[]` with partNumber, formattedPartNumber, manufacturer, description, quantity, dynamicColumns.
6. **Sort years DESCENDING + cap with `i >= max_years`**: SNAP-ON returns years descending natively, but the DFS uses `stack.pop()` (LIFO) — without re-sorting + capping inside the for-loop, MAX_YEARS=1 picks the OLDEST year (1998) instead of the newest (2026). See snapon_rest.py year-handling block.
7. **MRP is NOT in `/pages/parts/`**: requires a separate picklist API call (queued, not yet wired). Rows currently ship as `crawl_status=partial` with item_code/name/compat fully populated.
8. **`x-client-version` header pinned at 6.10.3** (capture from a fresh login if SNAP-ON rev-bumps it).
9. **Production scope = `MAX_YEARS=1` (year=2026 only)**: latest-year catalog already covers all currently-sold Indian models. Older years add discontinued models that aren't relevant to Spinny's active fleet.

## Intelli Catalogue v11.0 (Mahindra, MG) — locked technique

Both Mahindra eCat and MG ServiceConnect run the same Intelli Catalogue v11.0 platform. If touching `spiders/mahindra.py` or `spiders/mg.py`, preserve these:

1. **Captcha bypass via ddddocr**: 4-char text captcha is inline base64 PNG. `ddddocr.classification(bytes)` returns the text at ~90% accuracy. Spider retries 6× → effective ~100% login success.
2. **Mahindra has a User Type dropdown** (second `mat-select`, first is Language). Pick `'Other User (Fleet Owner)'`. MG has no User Type dropdown — skip that step.
3. **Submit via JS, not Playwright `.click()`** — `cdk-overlay-backdrop` intercepts pointer events after closing the dropdown. `page.evaluate("() => document.querySelector('#btnEnter').click()")` bypasses it.
4. **Drill chain (UI-driven)**: each click sends an encrypted `FigureSearchParm` POST. We don't decrypt — we let the SPA encrypt, then capture the response via `page.on('response')`.
5. **Mahindra drill levels**: PV → Fillcategory → Category → FillCategoryCountryModel → Variant → FillCatModelWithOutCountry → SP-Category → FillAssembly → assemblies (`figno` populated).
6. **MG drill levels** (one deeper): Model → Fillcategory → Variant → FillCategoryCountryModel → Sub-Variant → FillCatModelWithOutCountry → Section → ... → assemblies. Use **adaptive recursion**: stop drilling when entries have `figno` populated.
7. **Field map**: `item_name ← categoryname`, `item_code ← figno`. Both brands. MRP not required per xlsx.

## SAP Commerce Spartacus (Schaeffler) — locked technique

Schaeffler India's Vehicle Lifetime Solutions runs on SAP Commerce Cloud with the Spartacus Angular storefront, fronting a RepXpert backend. If touching `spiders/schaeffler.py`:

1. **Akamai WAF bypass**: send full Chrome-131 fingerprint headers on every request — `Sec-Fetch-Dest`, `Sec-Fetch-Mode`, `Sec-Fetch-Site`, `Sec-Fetch-User`, `sec-ch-ua`, `sec-ch-ua-mobile`, `sec-ch-ua-platform`, `Upgrade-Insecure-Requests`, `Accept-Encoding: gzip, deflate, br`. The header set is committed as `WAF_BYPASS_HEADERS` in the spider.
2. **OCC backend prefix is `/api/`** (not `/occ/v2/`). Discovered by running `page.goto` with full headers + watching network — SPA's first XHR is `/api/basesites?fields=baseSites(uid,...)`.
3. **Base site is `Repxpert-IN`**: `/api/basesites` returns 30+ regional Repxpert-* sites; India's is `Repxpert-IN`.
4. **Single endpoint**: `GET /api/Repxpert-IN/products/search?query=:relevance:targetTypes:passengerCar&pageSize=120&currentPage=N`. Server caps pageSize at 120. ~149 pages = ~3 min full crawl.
5. **MRP unavailable**: `priceRange:{}` on all products (catalog policy is "on request"). Rows finalize as `partial`. Same partial-rationale as Exide. User-verified 2026-05-19.

## Spider-build playbook (proven 5× — Mobil, Exide, HELLA, Monroe, Technix)

When tackling a new brand, follow this order — it minimizes rework:

1. **Read the BRD/xlsx entry** + the relevant `docs/per_site_notes.md` section if a similar pattern was already done.
2. **Static fetch first** (`httpx + parsel`). If product cards visible in raw HTML → likely a static spider (Exide, Monroe pattern).
3. **If static returns chrome only**: probe for a JSON API (`/wp-json/...`, `/api/...`, `/.well-known/...`). WordPress sites often expose products as a custom post type at `/wp-json/wp/v2/<type>` (Technix pattern).
4. **If API not available**: open in Playwright, attach `page.on("request")` and `page.on("response")` listeners, navigate, look for XHR responses ≥ a few KB. These are usually the data API (Mobil's Coveo pattern).
5. **Filter scope at the site level if possible** (Mobil: capture the page's filtered request; Exide: PV slug whitelist). If not possible, do it post-fetch via breadcrumb / category metadata (HELLA pattern).
6. **Dedupe early and visibly** — log `total_unique` per page or page-equivalent so progress is observable.
7. **Smoke test on a small subset (first 5 records)** before full run. Verify field types and values.
8. **Run the full crawl**, inspect `run_summary` for status. **Document the spider in [docs/per_site_notes.md](docs/per_site_notes.md)** with site reality vs xlsx flow, selectors used, gotchas.

## Don'ts

- **Don't add Firecrawl as primary path** — it's an escape hatch only. If a site needs it, document why in `docs/technical.md` and add to `config/sites.yaml` under `escape_hatch: firecrawl`.
- **Don't add fields beyond what `config/sites.yaml` lists per brand** — xlsx is authoritative; over-extraction creates a maintenance burden.
- **Don't add error handling for cases that can't happen.** Crash early on contract violations.
- **Don't introduce a new framework** (no FastAPI, no Celery, no Airflow). Cron + Python is the agreed stack.
- **Don't add SaaS captcha-solving / residential proxy** — we have offline alternatives in-tree (ddddocr for text captchas, Sec-Fetch header set for Akamai-style WAFs). If a site needs more than these, escalate to Spinny first (it's a scope-change conversation, not a procurement decision).

## Adding a new spider

1. Pick the closest pattern from these 6: `flat_list`, `multi_level_category`, `cascading_dropdown`, `pdf_brochure`, `hidden_nav`.
2. Add entry to `config/sites.yaml` under `brands.<key>`.
3. Create `spiders/<key>.py` subclassing `BaseSpider`. Implement `crawl() -> list[Row]`.
4. For cascading-dropdown sites: spike Playwright network interception first. Capture the XHR endpoint with `page.on('response', ...)`. Document the discovered API in a docstring.
5. Add fixture `tests/fixtures/<key>/sample.html` and `tests/test_<key>.py` covering schema + partial/failed cases.

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
playwright install chromium
python -m orchestrator.run_monthly --brands=exide --config config/sites.yaml
```

## SLAs to honor (BRD §8)

| Item | Commitment |
|---|---|
| Crawl frequency | Monthly. First Monday 22:00 IST → 06:00 IST Tuesday |
| Output delivery | ≤ 12:00 IST Tuesday |
| Completeness | ≥ 98% brand success per run |
| Breakage notify | ≤ 24 hours |
| Breakage fix | ≤ 5 business days |
| Data retention | ≥ 12 months in shared folder |
