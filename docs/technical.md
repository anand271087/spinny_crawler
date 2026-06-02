# Technical Documentation — Spinny OEM Spare Parts Crawler

## 1. Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Linux VM (Spinny-provided)                                      │
│                                                                  │
│   cron (0 22 1-7 * 1)                                            │
│      │                                                           │
│      ▼                                                           │
│   cron/run_monthly.sh ─► python -m orchestrator.run_monthly      │
│                              │                                   │
│                              │  loads config/sites.yaml          │
│                              │  for each brand:                  │
│                              │    importlib spiders.<brand>      │
│                              │    spider.run() → RunResult       │
│                              │    write_per_brand(...)           │
│                              │                                   │
│                              ▼                                   │
│                          write_master(all_rows)                  │
│                          write_run_summary(summary)              │
│                              │                                   │
│                              ▼                                   │
│                          send_alert(summary) ──► SMTP            │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
        │
        ▼
   output/YYYYMMDD/
   ├── <brand>_<YYYYMMDD>.csv,.json     (19 files)
   ├── spinny_oem_master_<YYYYMMDD>.csv,.json
   └── run_summary_<YYYYMMDD>.json
        │
        ▼
   Shared cloud folder (S3/GDrive/SharePoint — TBD)
```

## 2. Module map

| Module | Responsibility |
|---|---|
| `orchestrator/run_monthly.py` | Entry point. Loads YAML config. Iterates brands. Aggregates. Triggers alerts. |
| `orchestrator/aggregate.py` *(Phase 3)* | Master file dedup, run_summary assembly |
| `orchestrator/dom_change_detector.py` *(Phase 3)* | Selector-output hash vs last good run |
| `spiders/_base.py` | `BaseSpider` contract; `Row` dataclass; auto-populates `source_website`, `crawl_date`, `crawl_status` |
| `spiders/<brand>.py` | Per-brand crawler. 19 files total. |
| `spiders/bosch_pdf.py` | PDF brochure discovery + `pdfplumber` extraction |
| `lib/normalize.py` | `clean_mrp()` — currency/comma/whitespace strip per BRD §5 |
| `lib/output.py` | CSV+JSON writers, dedup, run_summary |
| `lib/alerts.py` | SMTP email alerts via `smtplib` |
| `lib/api_interceptor.py` *(Phase 2)* | Playwright `page.on('response')` helper for XHR capture |
| `config/sites.yaml` | Per-brand: URL, pattern, fields, dropdowns, gates |
| `config/alerts.yaml` | SMTP host, recipient list, trigger toggles |

## 3. Six crawler patterns

The 19 brands map onto 6 patterns. Each pattern has a canonical implementation strategy.

### 3.1 `flat_list` — 3 brands (Technix, Monroe, Mobil)
- **Stack**: Scrapy + httpx (no JS needed except Mobil)
- **Logic**: GET listing page → parse cards → follow `next` link until exhausted.
- **Gotcha**: Monroe URL has `?srsltid=...` Google referral param — strip before fetch.

### 3.2 `multi_level_category` — 8 brands (HELLA, UNO MINDA, ZIP, EXIDE, Spark Minda, VALEO, LUMAX, GABRIEL)
- **Stack**: Scrapy primary; Playwright if category navigation is JS-driven
- **Logic**: Enumerate categories (often hardcoded in `config/sites.yaml` `start_categories`) → for each, walk to leaves → paginate within leaf.
- **Gotcha**: LUMAX has 2W categories to skip — use `skip_categories` regex in config.

### 3.3 `cascading_dropdown` — 6 brands (AMARON, SF SONIC, Schaeffler, JK TYRE, ZF, TVS Girling URL 1)
- **Stack**: Playwright + XHR interception
- **Logic**:
  1. Launch Playwright, navigate to URL
  2. Attach `page.on('response', handler)` listener
  3. Trigger one Make/Manufacturer change manually to capture the API call signature
  4. Extract endpoint, query params, headers from captured response
  5. Iterate every (dropdown1, dropdown2, ...) tuple via `httpx` calls to the captured endpoint
- **Why this matters**: Naive UI iteration on all 6 sites = 400K–600K pages = $333/mo Firecrawl Growth. API replay = ~5K–15K requests = $0.
- **Fallback**: If endpoint is token-gated (anti-replay), fall back to UI iteration with per-combo JSON checkpoint to support resume.

### 3.4 `pdf_brochure` — 1 brand (Bosch)
- **Stack**: Playwright (discovery) + `pdfplumber` (parsing)
- **Logic**: Walk product categories → click Downloads tab → capture PDF URL → download → `pdfplumber.open()` → extract tables; fall back to text + regex for Item Code.
- **`crawl_status=failed`** for broken/missing PDFs per BRD §6.
- **Optimization**: Cache PDFs by hash; skip re-parse if unchanged month-over-month.

### 3.5 `hidden_nav` — 1 brand (AUTOKOI)
- **Stack**: Playwright
- **Logic**: `page.click('button[aria-label=menu]')` → `page.click('text=E-catalogue')` → parse rendered DOM.
- **Gotcha**: BRD §6 explicitly says trigger navigation programmatically — don't rely on default DOM.

### 3.6 Multi-URL same brand — 1 brand (TVS Girling)
- **Stack**: 2 spiders, one cascading_dropdown + one multi_level_category
- **Logic**: URL 1 (partscatalogue.brakesindia.com) and URL 2 (tvsgirling.com) run independently. Outputs merged into single `tvs_girling_<YYYYMMDD>.csv` with `source_website` distinguishing rows. URL 2 rows have only `item_name`; other fields blank → `crawl_status=partial`.

## 4. Data contract

### Output schema (per row)

| Field | Type | Origin | Notes |
|---|---|---|---|
| `item_name` | string | extracted | Required |
| `item_code` | string | extracted | Brand-dependent (not present in JK TYRE, Mobil, TVS Girling URL 2) |
| `mrp` | number | extracted | INR, no FX conversion. Brand-dependent. |
| `compatible_car_model` | string | extracted | Brand-dependent (VALEO, ZF, JK TYRE) |
| `tyre_sizes` | string | extracted | JK TYRE only |
| `vehicle_compatibility` | string | extracted | TVS Girling URL 1 only |
| `source_website` | string | auto | Brand identifier + originating URL |
| `crawl_date` | date (ISO-8601 UTC) | auto | Date of monthly run |
| `crawl_status` | enum | auto | `success` / `partial` / `failed` |

### Files per monthly run (BRD §5)

| File | Format | Pattern | Contents |
|---|---|---|---|
| Per-brand | CSV + JSON | `<brand>_<YYYYMMDD>.{csv,json}` | All rows scraped for that brand |
| Consolidated master | CSV + JSON | `spinny_oem_master_<YYYYMMDD>.{csv,json}` | All 19 brands concatenated; dedupe within-brand on `(source_website, item_code)` if `item_code` present, else `(source_website, item_name, compatible_models)` |
| Run summary | JSON | `run_summary_<YYYYMMDD>.json` | Per-brand row counts, status distribution, error count, runtime, DOM-change list |

### Encoding rules

- UTF-8
- MRP unquoted in CSV, typed as number in JSON
- Currency symbols, commas, whitespace stripped pre-write (`lib.normalize.clean_mrp()`)

## 5. Volume sizing

| Brand pattern | Pages/month (naive UI) | Pages/month (with API interception) |
|---|---|---|
| Flat list (3) | ~1K–5K | same |
| Multi-level category (8) | ~5K–20K | same |
| Cascading dropdown (6) | **400K–600K** | **~5K–15K** ← biggest cost lever |
| PDF (1) | ~50 PDFs | same |
| Hidden nav (1) | ~100–1K | same |
| Multi-URL (1) | ~10K | same |

**Total realistic**: ~50K–100K page-equivalent operations/month with API interception.
**Total naive**: 500K–700K — would require Firecrawl Growth $333/mo and exceed run window.

## 6. Failure handling

| Failure | Detection | Action |
|---|---|---|
| Network 4xx/5xx on a page | Spider retry (Scrapy default), backoff | Row dropped; counted in `errors` |
| Spider crash | `orchestrator.run_monthly` catches, marks brand `failed` | Email alert fired immediately |
| Required field missing | `BaseSpider.finalize()` checks against `required_fields` | Row `crawl_status=partial`; brand status `partial` |
| Bosch PDF malformed | `pdfplumber` exception | Category `crawl_status=failed`; log + continue |
| DOM change on a site | `dom_change_detector` compares selector-output hash | Add to `run_summary.dom_changes`; email alert |
| <98% brand success at end of run | Orchestrator checks final success rate | Email alert summary |

## 7. Testing strategy

| Level | Coverage | Tool |
|---|---|---|
| Unit | `lib/normalize.py`, `lib/output.py`, `BaseSpider` field-status logic | pytest |
| Per-spider | Each `spiders/<brand>.py` against a captured HTML/PDF fixture | pytest + `tests/fixtures/<brand>/` |
| Schema contract | Every spider emits the fields declared in `config/sites.yaml` for that brand | `tests/test_schema.py` |
| Integration | `python -m orchestrator.run_monthly --brands=exide,zf --output-dir tmp/` end-to-end | pytest fixture |
| Live canary | Weekly cron job runs 2–3 spiders against real sites | `cron/canary.sh` |

## 8. Deployment

```bash
# On Spinny VM
sudo mkdir -p /opt/spinny_crawler
sudo chown $USER /opt/spinny_crawler
cd /opt/spinny_crawler

git clone <repo> .
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium

# Install cron entry
cat cron/crontab.txt | crontab -

# Verify
crontab -l
```

### Required env vars
- `SMTP_PASSWORD` — for `lib/alerts.py` email auth (set in `/etc/environment` or systemd unit)
- `S3_BUCKET` — if S3 destination chosen at kickoff (Phase 0)

## 9. Dependencies — why each

| Package | Why |
|---|---|
| `scrapy` | Built-in middleware (AutoThrottle, retries, items pipeline, feed exporters) — saves ~40h of plumbing |
| `playwright` | Headless Chromium for JS-heavy sites; `page.on('response')` for XHR interception |
| `pdfplumber` | Best-in-class Python PDF table extraction (Bosch) |
| `pymupdf` | Fallback PDF text extractor when pdfplumber struggles |
| `pandas` | Dedup + master file assembly in ~20 LOC |
| `pyyaml` | `config/sites.yaml`, `config/alerts.yaml` parsing |
| `httpx` | Fast async HTTP for replaying captured XHR endpoints |
| `tenacity` | Backoff/retry for transient failures |

## 10. Phase status (as of 2026-05-19)

| Phase | Status | Deliverable |
|---|---|---|
| 0. Kickoff | Open | Cloud destination, SMTP host, recipient list, schema mapping — see [kickoff_checklist.md](kickoff_checklist.md) |
| 1. Framework + 3 pilots | ✅ Done | Orchestrator, base spider, output writers, alerts, cron entry, EXIDE + ZF + Bosch pilots |
| 2. v1.0 brand spiders | ✅ Largely done (16/19 shipped or partial) | Pending only JK TYRE, ZF, TVS Girling URL 1 (estimates 14-20h, 12-16h, 4-8h respectively) |
| 2b. v2.0 OEM additive (7 brands) | ✅ Done (5/7 success, 2 partial pending Spinny escalation) | Maruti ✓ Hyundai ✓ Mahindra ✓ MG ✓ Tata ✓ Ford ✓ Toyota ⚠️ partial (MRP gap) |
| 3. Master + alerts + DOM detector | Partial | Master assembly + email alerts done; `dom_change_detector.py` pending |
| 4. UAT | Not started | First "real Monday" run |
| 5. Handover | Not started | `docs/runbook.md` updates (see below), on-call playbook |

**See [docs/per_site_notes.md](per_site_notes.md) for per-spider extraction details, site quirks, and gotchas.**

### Brands shipped — at-a-glance

| Brand | Rows | Status | Pattern | Runtime |
|---|---|---|---|---|
| Mobil | ~410 | success | flat_list / Coveo API capture-replay | ~55s |
| Exide | 48 | partial (no MRP) | multi_level_category / static | ~1s |
| HELLA | 1666 | success | multi_level_category + detail-page filter | ~14m |
| Monroe | 326 | success | flat_list / Magento ?p= pagination | ~17s |
| Technix | 1596 | success | flat_list / WP REST + JetEngine fields | ~4m |
| Gabriel | 2209 | success | pdf_brochure (reclassified from multi_level_category) | ~5s |
| ZIP | 811 | success | multi_level_category / static 6-category endpoint + row-level 4W filter | ~2s |
| LUMAX | 2185 | success | pdf_brochure (reclassified) — 4W consolidated price-list PDF | ~7s |
| VALEO | 0 | **failed** | breakage stub — site has no public product data, see §10 in per_site_notes.md | <1s |
| AMARON | 52 | success | cascading_dropdown / Drupal AJAX + direct-URL bypass | ~18m |
| SF SONIC | ~50-100 (est) | success | cascading_dropdown / direct-URL bypass + fuel short-circuit | ~2h (smoke: 7/Hyundai in 4m) |
| Schaeffler | **17,778 (PV filter)** | **partial** (MRP unavailable — Spinny B6) | **json_api_pagination / SAP Commerce Spartacus (Repxpert-IN)** | **~3m** |
| JK TYRE | 0 | **failed** | breakage stub — Next.js RSC streaming, 14-20h | <1s |
| Bosch | 1426 | partial | pdf_brochure — 4 of 7 catalogue PDFs parsed; column alignment best-effort | ~3m |
| AUTOKOI | 98 | partial | multi_level_category (no MRP on site) | ~3s |
| Spark Minda | 1 (placeholder) | partial | stubbed — needs ProductDetail typeid enumeration | <1s |
| UNO MINDA | 525 | partial | search-results pagination (60 pages, ~12/page) | ~5m |
| ZF | 0 | **failed** | stub — Vue.js SPA + 5-level cascade, 12-16h | <1s |
| TVS Girling | 25 | success | URL 2 multi_level_category; URL 1 stubbed (ASP.NET WebForms) | ~2s |
| **v1.0 subtotal** | **~29,500+** (across 15 working brands; 3 stubbed pending Spinny: JK TYRE, ZF, TVS Girling URL 1) | | | |

### Brands shipped — v2.0 OEM additive (added 2026-05-18 onwards)

| Brand | Rows | Status | Pattern | Runtime |
|---|---|---|---|---|
| Maruti | 29,313 | success | json_api_pagination (public site, no auth) | ~8m |
| Hyundai | 35/35 MRP on smoke | success | snapon_epc (SNAP-ON platform) | ~10h full crawl |
| Toyota | 28 parts; MRP empty | partial (MRP — Spinny B7) | snapon_epc | ~10h |
| Mahindra | 39 ENGINE assemblies on smoke (PV → XUV 7XO → AX7 MT-DSL → ENGINE) | success | cascading_dropdown / Intelli Catalogue v11.0 (captcha bypass via ddddocr) | ~10h sane scope |
| MG | 39 Hector assemblies on smoke | success | cascading_dropdown / Intelli Catalogue v11.0 (same login bypass; adaptive recursion) | ~3h sane scope |
| Tata | 25 ENGINE parts w/ MRP on smoke | success | cascading_dropdown / ASP.NET WebForms + TreeView (`funredirectType('<id>')`) | ~25-50h full |
| Ford | 25 parts on smoke | success | json_api_pagination / SAP Commerce (Microcat) | ~5h |

| **v2.0 subtotal** | **~29,500+ at smoke; significantly more at full scope** | | | |

## 11. Reusable techniques unlocked

Three general-purpose techniques discovered during the v2.0 build that future brand spiders should reuse. Each is committed in-tree (no SaaS, no recurring cost).

### 11.1 Offline captcha OCR — `ddddocr`

When a site gates login behind a 4-character text captcha rendered as an inline base64 PNG (Intelli Catalogue v11.0 — Mahindra, MG), use the [ddddocr](https://github.com/sml2h3/ddddocr) Python library:

```python
import ddddocr, base64
ocr = ddddocr.DdddOcr(show_ad=False)
b64 = captcha_data_url.split(",", 1)[1]
b64 += "=" * (4 - len(b64) % 4)
text = ocr.classification(base64.b64decode(b64)).upper().strip()
```

~90% per-attempt accuracy; spiders retry 6× → effective ~100% login success. Free, fully offline, no third-party API key. See `spiders/mahindra.py` and `spiders/mg.py`.

**When this falls short**: reCAPTCHA v2/v3, hCaptcha, Cloudflare Turnstile, or image-puzzle captchas. Those require a paid solver service (out of BRD scope — escalate to Spinny).

### 11.2 Akamai-style WAF bypass — Chrome 131 fingerprint headers

When a site returns 403 "Access denied" to bare `httpx` / default Playwright requests (Schaeffler), the WAF is checking for a complete browser fingerprint. Send these headers and the WAF lets the request through:

```python
WAF_BYPASS_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "empty",       # or "document" for page-level
    "Sec-Fetch-Mode": "cors",        # or "navigate"
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}
ctx = browser.new_context(extra_http_headers=WAF_BYPASS_HEADERS, ...)
r = ctx.request.get(url)   # 200 instead of 403
```

See `spiders/schaeffler.py::WAF_BYPASS_HEADERS`.

**When this falls short**: WAFs that fingerprint TLS handshake (JA3/JA4) or run JavaScript challenges (e.g. Cloudflare Bot Management Pro). For those: residential-proxy service is needed → escalate to Spinny.

### 11.3 SAP Commerce Spartacus OCC discovery

If `page.goto` with full headers loads a Spartacus storefront, watch the first XHR — it's usually `/<prefix>/basesites?fields=baseSites(uid,...)`. From there:

1. The prefix may be `/occ/v2` (SAP default) OR custom like `/api` (Schaeffler).
2. `/basesites` returns the list of available base sites — pick the one matching the country (`Repxpert-IN` for India).
3. Product search at `/<prefix>/<baseSite>/products/search?query=:relevance&pageSize=120&currentPage=N&fields=DEFAULT`.
4. Filter to passenger cars via `query=:relevance:targetTypes:passengerCar`.

This pattern unlocked Schaeffler from 0 rows → 17,778. Generalizes to any SAP Commerce storefront. See `spiders/schaeffler.py`.
