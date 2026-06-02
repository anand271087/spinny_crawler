# Functional Documentation — Spinny OEM Spare Parts Crawler

For Spinny Catalog / OES Datasources team. Describes what the system does — not how.

## 1. Purpose

Replace the current manual process of browsing 19 OEM/aftermarket-brand websites tab-by-tab and copying spare-parts data. Automate it into a single deterministic monthly job that produces clean, structured catalogue files.

**Why it matters:** Spare-parts data (names, codes, MRPs, vehicle compatibility) drives downstream business workflows:
- Refurbishment cost estimation
- Service-quote generation
- Dealer settlement
- Parity checks against in-house catalogue and pricing

The manual process is slow, error-prone, and cannot keep pace with how often OEMs revise their catalogues. This crawler converts a multi-day manual task into an overnight automated one.

## 2. In-scope brands (19 v1.0 + 7 v2.0 OEM additive)

### Phase 1 — v1.0 aftermarket brand websites (19)

| # | Brand | Type |
|---|---|---|
| 1 | HELLA | Lighting/electrical |
| 2 | UNO MINDA | Lighting/horns/sensors |
| 3 | Technix | Aftermarket aggregator |
| 4 | GABRIEL | Suspension |
| 5 | ZIP | Filters |
| 6 | MONROE | Suspension (via BKS Motors reseller) |
| 7 | AMARON | Batteries |
| 8 | SF SONIC | Batteries |
| 9 | EXIDE | Batteries |
| 10 | Spark Minda | Lighting/electrical |
| 11 | LuK / Schaeffler | Clutch/transmission (SAP Commerce / RepXpert backend) |
| 12 | AUTOKOI | Aftermarket aggregator |
| 13 | Bosch | Multi-category (PDF brochures) |
| 14 | VALEO | Engine/climate |
| 15 | ZF | Drivetrain/suspension |
| 16 | LUMAX | Lighting |
| 17 | JK TYRE | Tyres |
| 18 | TVS Girling | Brakes (two source URLs) |
| 19 | Mobil | Lubricants |

### v2.0 OEM additive (7) — added per kickoff decision 2026-05-18

Vehicle-manufacturer EPCs that supplement the aftermarket brands above. Most require login (BRD §7's "no auth" assumption was relaxed for these per the v2.0 decision — see §8 below):

| # | Brand | EPC platform | Auth |
|---|---|---|---|
| 20 | Maruti Suzuki | marutisuzuki.com Genuine Parts | none (public) |
| 21 | Hyundai | SNAP-ON EPC (snaponepc.com) | dealer login |
| 22 | Toyota | SNAP-ON EPC (same platform as Hyundai) | dealer login |
| 23 | Mahindra | Intelli Catalogue v11.0 | login + 4-char text captcha (offline OCR via ddddocr) |
| 24 | MG | Intelli Catalogue v11.0 (same platform as Mahindra) | login + 4-char text captcha (offline OCR) |
| 25 | Tata | TATA eCats (ASP.NET WebForms + TreeView) | dealer login |
| 26 | Ford | Infomedia Microcat (SAP Commerce) | Infomedia SSO login |

**Out of scope:** Two-wheeler, three-wheeler, commercial-vehicle, tractor parts. Where a brand surfaces a vehicle-segment filter, the crawler picks passenger-car / 4-wheeler before extracting.

## 3. Monthly schedule

| Item | Commitment |
|---|---|
| Frequency | Once per calendar month |
| Run window | Night of the **first Monday**, 22:00 IST → 06:00 IST Tuesday |
| Output delivery | Files in shared folder by **12:00 IST Tuesday** |

Operations team receives the run summary email by 12:00 IST Tuesday (sooner if any brand fails).

## 4. Output files

Every monthly run produces, in a date-stamped sub-folder of the shared cloud folder:

### 4.1 Per-brand files (19 files × 2 formats = 38)
- `<brand>_<YYYYMMDD>.csv`
- `<brand>_<YYYYMMDD>.json`

Each contains all spare-parts rows scraped that month for that brand.

**Special case — TVS Girling:** has 2 source URLs but produces a **single** combined file. Each row's `source_website` field shows which URL it came from.

### 4.2 Consolidated master (2 files)
- `spinny_oem_master_<YYYYMMDD>.csv`
- `spinny_oem_master_<YYYYMMDD>.json`

All 19 brands concatenated, deduplicated within each brand. Deduplication rule:
- If `item_code` is present → dedupe on `(source_website, item_code)`
- Else → dedupe on `(source_website, item_name, compatible_models)`

### 4.3 Run summary (1 file)
- `run_summary_<YYYYMMDD>.json`

Contains:
- Per-brand row counts
- Per-brand `crawl_status` (success / partial / failed)
- Per-brand error count and runtime
- List of sites whose DOM appeared to have changed

This file is the single artefact ops needs to verify a run succeeded.

## 5. Data fields

Core fields vary by brand. The exact list per brand is in the companion OES Datasources spreadsheet — that sheet is the **single source of truth**.

### 5.1 Common business fields

| Field | Type | Notes |
|---|---|---|
| Item Name | string | Required for all 19 brands |
| Item Code | string | 16 of 19 brands. Missing for JK TYRE, Mobil, TVS Girling URL 2. |
| MRP | number | 13 of 19 brands. INR only — no FX conversion. Currency symbols/commas stripped. |
| Compatible vehicle / car model | string | VALEO, ZF, JK TYRE, TVS Girling URL 1 |
| Tyre sizes | string | JK TYRE only |
| Vehicle Information (Compatibility) | string | TVS Girling URL 1 only |

### 5.2 Auto-populated metadata (every row)

| Field | Type | Purpose |
|---|---|---|
| `source_website` | string | Identifies which brand + which URL the row came from |
| `crawl_date` | date (ISO-8601 UTC) | When the row was scraped |
| `crawl_status` | enum | `success` / `partial` / `failed` |

### 5.3 `crawl_status` meaning

- **success** — all required fields for that brand were captured
- **partial** — some required field was listed for the brand but was not visible on the live site for that product (field left blank)
- **failed** — the row or its source category could not be parsed (e.g. Bosch broken PDF, page returned 4xx/5xx after retries)

## 6. Service-level agreements (BRD §8)

| Item | Commitment |
|---|---|
| Per-run completeness | ≥ 98% of in-scope brands return `crawl_status = success` per monthly run |
| Site breakage detection | Auto-detected each run (DOM change, filter change, blocked response) |
| Breakage notification | Spinny notified within **24 hours** of detection (email) |
| Breakage resolution | Broken site restored to `success` within **5 business days** of detection |
| Data retention | All monthly outputs kept in shared folder ≥ **12 months** |

## 7. Edge cases the crawler explicitly handles (BRD §6)

| Pattern | Brands | Behaviour |
|---|---|---|
| Cascading dropdowns (Make → Model → Variant/Fuel) | AMARON, SF SONIC, LuK/Schaeffler, JK TYRE, ZF, TVS Girling (URL 1) | Iterates the full combination space; checkpoints progress so a run can resume after interruption |
| Multi-level category iteration | HELLA, UNO MINDA, ZIP, EXIDE, Spark Minda, VALEO, LUMAX, GABRIEL | Walks every (category × sub-category); paginates within each leaf |
| Multi-URL same brand | TVS Girling | Both URLs crawled; merged into one file with `source_website` preserving origin |
| Geography / region gate | ZF | Selects India + Passenger cars + LCV before search; rejects any result that bypasses the gate |
| Vehicle-segment filter | GABRIEL, Spark Minda, JK TYRE, ZF | Picks 4W/passenger-car as step 1; rejects non-passenger rows |
| Catalogue in PDF brochures | Bosch | Opens each product category, fetches the linked brochure PDF, parses it. Broken/missing PDF → category marked `failed`. |
| Hidden / off-canvas navigation | AUTOKOI | Triggers the hamburger menu programmatically — does not rely on default homepage DOM |
| Flat list with pagination | MONROE, Technix, Mobil | Iterates the full list; verifies completeness by comparing extracted count against any total-count indicator on the page |

## 8. Assumptions (BRD §7) — updated 2026-05-19

These conditions are assumed true. If they change, raise a scope-change ticket:

1. **v1.0 brands** are publicly accessible without authentication. **v2.0 OEM EPCs** require login (the additive scope explicitly accepted this — credentials supplied per the OES Datasources Master Tab).
2. **Text captchas accepted as solvable** — offline OCR (ddddocr) handles 4-char text captchas for Mahindra + MG without third-party services. reCAPTCHA, hCaptcha, Cloudflare Turnstile, or image-puzzle captchas remain out of scope (would need a paid solver).
3. **WAF / bot-management** — Akamai-style WAFs that gate on browser-fingerprint headers (Schaeffler) are bypassable in-tree with our Chrome 131 header set. WAFs that fingerprint TLS handshake (JA3/JA4) or run JavaScript challenges remain out of scope (would need residential-proxy service).
4. MRP, where extracted, is in INR. No FX conversion is performed.
5. Only 4W passenger-vehicle parts are in scope.
6. The OES Datasources spreadsheet's "Relevant Data Parameters" column for each brand is the authoritative list of fields to extract — no more, no less.
7. If a field listed in the sheet is not visible on the live site for a given product, the field is left blank and `crawl_status` is set to `partial`. **Several v1.0/v2.0 brands hit this case for MRP** — Exide, Schaeffler, AUTOKOI, UNO MINDA, Toyota — because their public sites don't publish list prices (catalog policy is "price on request" or dealer-only). The rows ship with `partial` status per this rule.
8. Site DOM, URL structure, and filter behaviour are reasonably stable between monthly runs. Structural changes are flagged via the SLA process.
9. `robots.txt` and the site's terms of use permit polite, low-frequency crawling at this cadence and volume.

## 9. Change-request flow

Out-of-cycle changes — adding a new site, changing data fields, or changing crawl frequency — follow a change-request flow with mutually agreed effort estimate and timeline. Default monthly cadence and the 19-brand scope are governed by the BRD and require formal amendment.

## 10. Operations playbook (summary)

| When | What happens | Who acts |
|---|---|---|
| First Monday 22:00 IST | Cron triggers monthly run automatically | Crawler |
| During run (overnight) | Spider failures generate immediate emails | On-call ops |
| Tuesday 12:00 IST | All output files delivered to shared folder. Run summary email sent. | Crawler |
| If <98% success | Email alert with details of failed brands | Vendor / on-call |
| If DOM change detected | Email alert with affected sites listed | Vendor begins 5-biz-day fix |
| Monthly | Ops verifies run_summary.json shows ≥98% success; consumes the master file downstream | Spinny Catalog team |

## 11. Glossary

| Term | Meaning |
|---|---|
| BRD | Automation Requirement Document v1.0 — the contract governing scope, SLAs, fields |
| OES | Original Equipment Supplier — the brand websites being crawled |
| OEM | Original Equipment Manufacturer — context for downstream Spinny workflows |
| MRP | Maximum Retail Price (INR) |
| 4W | Four-wheeler / passenger car |
| 2W | Two-wheeler — explicitly out of scope |
| LCV | Light commercial vehicle — included only for ZF per their site's filter taxonomy |
| Cascading dropdown | A UI pattern where selecting an option in dropdown A reloads the options in dropdown B (Make → Model → Variant) |
| DOM | Document Object Model — the page structure. "DOM change" = the site's HTML structure changed, breaking selectors. |
| Source of truth | The OES Datasources spreadsheet — overrides this doc if they ever conflict |
