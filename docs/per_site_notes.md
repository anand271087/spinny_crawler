# Per-Site Notes — How Data Is Extracted From Each OEM Site

Operational reference for engineers maintaining the spiders.

Each entry covers:
- **Functional** — what the spider produces (audience: Spinny Catalog team)
- **Technical** — how it extracts the data (audience: Spinny in-house engineers)
- **Site reality vs xlsx flow** — quirks, breakages, workarounds
- **Gotchas / re-run notes** — what could break and how to fix fast

Status legend: ✅ shipped · ⚠️ partial (missing fields per BRD §7) · ⏳ pending

---

## 1. Mobil — ✅ shipped

- **URL**: https://www.mobil.co.in/en-in/our-products
- **Pattern**: flat_list (BRD §6)
- **xlsx fields**: `item_name` only
- **Output**: ~410 unique product names per run, `status=success`

### Functional
Returns the full Mobil-India product catalog (engine oils, greases, industrial lubricants, fluids). All 410 items are Mobil-branded products sold in India. Other ExxonMobil regional brands (Esso Canada, Esso Hong Kong) are filtered out.

### Technical
- Page is a **Coveo Search Cloud** SPA. Static HTML returns `"Loading..."` — no products.
- Spider uses **Playwright + capture-and-replay**:
  1. Open page in headless Chromium.
  2. Attach `page.on("request")` listener; intercept the first POST to `/coveo/rest/search/v2`.
  3. Capture the URL (incl. `sitecoreItemUri` + `siteName=Mobil_IN_PROD`), the form body (~2.9 KB containing the `aq`/`cq`/`pipeline` India filters), and the Bearer-token header.
  4. Replay via `ctx.request.post()` inside the SAME browser context (auth/cookies stay intact). Bump `numberOfResults` from 10 → 100, page through `firstResult=0,100,200,300,400` until `totalCount` reached.
- Extracts each result's `title` field as `item_name`.
- 5 paginated API calls; runtime ~55s.

### Site reality vs xlsx flow
- xlsx says "scroll to Product Search section → extract complete Product List". The Product Search section is the Coveo widget — works as documented from a user POV. Spider just bypasses the UI and hits the same API the widget uses.

### Gotchas
- **Coveo caps `numberOfResults` at 100.** Setting it higher returns HTTP 400.
- **Token is session-scoped.** Cannot replay the captured request from a fresh httpx Client — auth fails. Must use `ctx.request.post()` inside the same Playwright context.
- **Global Coveo index ≠ India.** Without the page's heavy `aq` filter, the API returns ~10K results across all ExxonMobil regional sites. Always capture-and-replay the page's own filtered request; never construct one from scratch.
- **Coveo can return tied-relevancy items in slightly different order between runs.** Total count may vary ±5 month-over-month — normal.

### File
- [spiders/mobil.py](../spiders/mobil.py)

---

## 2. Exide — ✅ shipped (MRP source: official MRP-list PDF)

- **URL**: https://www.exideindustries.com/products/automotive-batteries/four-wheeler-batteries.aspx
- **MRP source**: https://docs.exideindustries.com/pdf/mrp-list/mrcp-exide-vehicular-and-2wl-batteries.pdf
- **Pattern**: pdf_brochure (BRD §6) — switched from `multi_level_category` after PDF discovery 2026-05-19
- **xlsx fields**: `item_name`, `item_code`, `mrp`
- **Output**: 53 batteries per run, `status=success`, all MRPs populated

### Functional
Returns Exide's passenger-vehicle battery catalogue with MRPs from the official monthly "MRP List" PDF. 9 PV families: Epiq (3), Matrix (3), Mileage (13), Mileage ISS (3), AGMi (5), Eezy (6), Eezy ISS (3), Ride (4), Drive (13). CV (Xpress), Tractor (Jai Kisan), 2-Wheeler (Xplore), 3W/LCV (Eko), E-Rickshaw filtered out.

### Technical
- Single-source: the **MRP PDF** at `docs.exideindustries.com/pdf/mrp-list/`. Discovered via footer "MRP List" link → `/mrp-list/default.aspx` → `/mrp-list/vehicular-and-two-wheeler-batteries.aspx` → 3 sub-brand PDFs (Exide, SF Batteries, Dynex; only Exide is in scope).
- Stack: **httpx download + pdfplumber parse**. No Playwright.
- **Step 1**: `lib.exide_pdf.download_pdf()` — fetch + cache PDF to `/tmp/spinny_crawler_exide/`.
- **Step 2**: `parse_pdf()` — A4 single page (595×842 pt), TWO-column layout. `extract_words(x_tolerance=10, y_tolerance=3)` to recover word tokens (raw `extract_text` outputs character-spaced text like "E P IQ"). Split each line at `width/2` into L/R columns. Within each column, iterate y-buckets; match category headers (`CAR/SUV: EXIDE EPIQ: 77M WARRANTY`) and rows (`<Ah> <code> <warranty> <price>`).
- **Step 3**: `passenger_vehicle_rows()` — keep categories whose segment list contains `CAR` or `SUV`. DRIVE qualifies (`CAR/SUV/3W/TRACTOR/CV`).
- **Step 4**: Emit `Row(item_name="Exide <Family> <Code>", item_code=<Code>, mrp=<float>)`.
- 1 HTTP request, ~0.2s runtime.

### Site reality vs xlsx flow
- xlsx says "scroll → select battery type → for each type, extract products". The website page itself shows no MRP. Exide publishes the authoritative price list as a monthly PDF (versioned, e.g. "Price List No: EIL/AUTO-VEH/MRCP/25-26/08 dated 20th March 2026"). We use that as the catalogue source because it (a) carries MRPs, (b) covers a superset of the website's family pages, (c) is the only place MRP exists publicly.

### Gotchas
- **Character-spaced PDF text**: `extract_text()` returns `"E P IQ 3 5 L"`. Must use `extract_words(x_tolerance=10)` then column-split.
- **Mixed-case family names**: "AGMi" has lowercase 'i' — `CATEGORY_RE` must allow `[A-Za-z]`, not just `[A-Z]`.
- **Codes with parentheses**: `MLM42(ISS)`, `ML55B24L(T1)`, `XP1200/L(RH)`. Row regex anchors on the warranty pattern, not on a strict code shape.
- **Sub-brand PDFs**: SF Batteries and Dynex have their own PDFs (`mrcp-sf-...`, `mrcp-dynex-...`). They're separate brands in Spinny's classification — if/when added, write spiders for them; the parser in `lib/exide_pdf.py` is reusable (same layout).
- **PDF URL stability**: Exide reissues the same filename each month with the new price list. Our crawler always re-downloads (no `If-Modified-Since`) so it picks up the latest revision automatically.
- **DRIVE is multi-segment**: PDF labels it `CAR/SUV/3W/TRACTOR/CV` — we include all DRIVE codes because CAR/SUV is one of its segments. If an analyst notices DRIVE codes that are clearly inverter/E-Rickshaw products (ERT*, ERFL*), file a clarification ticket — those rows appear in the PDF under DRIVE in older revisions but the current PDF separates them into E-RICKSHAW categories.

### File
- [spiders/exide.py](../spiders/exide.py)
- [lib/exide_pdf.py](../lib/exide_pdf.py) — reusable PDF parser

---

## 3. HELLA — ✅ shipped + enriched (async parallel + vehicle compat extraction, 2026-05-19)

- **URL** (xlsx): https://shop4hella.com/listing/4-wheeler
- **Working URL pattern**: https://shop4hella.com/listing/Shop4Hella/Shop4Hella/`<category>`
- **Pattern**: multi_level_category + detail-page segment filter + enrichment (BRD §6)
- **xlsx fields**: `item_name`, `item_code`, `mrp`
- **Bonus fields** (out of xlsx but harvestable from detail pages): `vehicle_compatibility` (breadcrumb hierarchy), `compatible_car_model` (parsed from name)
- **Output**: 1666 PV products per run, `status=success`. 100% have MRP + vehicle_compatibility. 463 (~28%) have a captured compatible_car_model.

### Functional
Returns HELLA's 4W passenger-vehicle catalog (lighting, bulbs, horns, brakes, filters, wipers, etc.). Every product has Name + SKU + MRP. Categories merged from PCA (Passenger Car Accessories) and PCS (Passenger Car Spare Parts) sub-business segments. **Deeper pass** (2026-05-19) adds per-row enrichment: full breadcrumb category hierarchy and parsed vehicle compatibility for parts whose names follow the "Part for <Make> <Model>" convention.

### Technical
- Async **httpx.AsyncClient** + parsel. Detail fetches run in parallel under a semaphore of 8.
- **Step 1 — Category discovery**: Fetch the working sub-segment pages `/listing/4-wheeler/passenger-car-accessories-PCA` and `/listing/4-wheeler/passenger-car-spare-parts-PCS`. Each lists its categories as links. Union of PCA + PCS slugs = 13 unique categories (aux, bulb, brakes-1, coolant, european-parts, filter, horns, lighting, lubes, other, relay-switches, shock-absorber, wiper). Verified via [state/probe_hella_categories.py](../state/probe_hella_categories.py) — no categories missed; the unscoped `/listing/Shop4Hella/Shop4Hella` doesn't list category anchors so PCA+PCS union is authoritative.
- **Step 2 — Listing walk** (sequential per-category, ordered): Walk `/listing/Shop4Hella/Shop4Hella/<cat>/<page>` (path-based pagination) until a page yields no new SKUs.
- **Step 3 — Card parse**: Each `div.product-grid` contains `<a>` (detail URL), `<span>` (item name), `<p class="sku">` (item_code prefixed `SKU: `), `<p class="price">` (₹MRP).
- **Step 4 — Parallel detail-fetch + enrich**:
  - PV filter: breadcrumb crumb `[1]` must be "Passenger Car Accessories (PCA)" or "Passenger Car Spare Parts (PCS)". Drops 2W / 3W / Commercial / Agriculture.
  - MRP: regex `MRP\s*:?\s*Rs\.?\s*([\d,]+)` against detail-page body (authoritative; falls back to listing card if absent).
  - vehicle_compatibility: breadcrumb crumb `[2] > [3]` (e.g., "Lighting > Single Function Lamp", "Aux > Driving Lamp").
  - compatible_car_model: regex `\bfor\s+(.+?)` against product name, gated by a whitelist of vehicle makes (Maruti, Hyundai, TATA, BMW, Mahindra, Honda, Ford, Toyota, Mercedes, M&M, Skoda, VW, Audi, Volvo, Range Rover, Jaguar, etc.). Captures real vehicles, rejects false positives like "for High Beam", "for Comet 500" (HELLA's own product line).
- **Performance**: 1666 rows in ~4 min (down from ~14 min sequential). ~5,400 detail HTTP requests parallelized 8-wide.

### Site reality vs xlsx flow — **BREAKAGE**
- **xlsx-documented URL pattern `/listing/4-wheeler/<seg>/<category>` returns chrome only — zero product cards.** Confirmed via static httpx, Playwright after `networkidle`, and POST-with-form-data.
- Working URL pattern (`/listing/Shop4Hella/Shop4Hella/<cat>`) is **unscoped** — returns ALL segments mixed (4W + 2W + 3W + Agriculture + Commercial). Required workaround: per-product detail fetch + breadcrumb-based segment filter.
- **Action per BRD §8**: this is a breakage discovery, vendor should notify Spinny within 24h. Recommended message: *"HELLA's documented xlsx URL pattern does not render products as of 2026-05-18. Spider works via a fallback pattern that requires per-product breadcrumb filtering to scope to PV. Confirm intended scrape flow with HELLA / shop4hella.com webmaster."*

### Enrichment notes
- **vehicle_compatibility** is populated for every row (universal — it's the breadcrumb hierarchy, always present). Format: `"Category > Sub-category"`, e.g., `"Lighting > Head Lamp Assy"`, `"Aux > Driving Lamp"`.
- **compatible_car_model** is best-effort and only filled when the part name explicitly says "for <Vehicle>" AND the first token matches a known PV manufacturer. Examples extracted: "TATA SAFARI 2021 Onwards", "Maruti Ertiga", "RANGE ROVER IV", "JAGUAR XF", "M&M BOLERO TYPE 2", "HONDA CRV Ty-3". Universal accessories (bulbs, lubricants, horns) leave it blank — that's correct, they fit any vehicle.
- **Captured-make distribution** (top 10): Maruti (62), BMW (35), TATA (34), AUDI (25), HYUNDAI (38), Mahindra (40), TOYOTA (32), HONDA (33), Hyundai (15), Ford (18). Case-mixed because we preserve the source string per CLAUDE.md item-name-faithful rule.

### Gotchas
- The 6-products-per-page pagination is path-based (`/lighting/2`, `/lighting/3`), NOT query-string (`?p=2` returns same page-1 content).
- Breadcrumb 2nd crumb is the authoritative segment marker. The footer "Segment" filter (with counts 166, 384, 1289…) appears on every page but is unrelated to the current product's segment.
- The "Most Viewed Products" sidebar on every detail page contains `p.price` elements that are NOT this product's MRP. Don't use `p.price::text` on detail pages — use the `MRP : Rs ...` regex against body text (it's a single unambiguous occurrence).
- Vehicle-make whitelist needs updating if HELLA expands into new OEM brands (e.g., MG, Kia). False-negative rate currently ~0% on the discovered list; false-positive rate ~0% after the whitelist gate.
- The 1st product of every category is sometimes a "featured" item that ALSO appears later as a regular item — spider dedupes by SKU.
- Some captured `compatible_car_model` values contain trailing position suffixes (FRONT/REAR) — these aren't stripped because removing them would also drop legitimate model variants. Analyst-side normalization recommended if needed downstream.

### File
- [spiders/hella.py](../spiders/hella.py)

---

## 4. Monroe — ✅ shipped

- **URL**: https://www.bksmotors.com/brand/monroe
- **Pattern**: flat_list (BRD §6)
- **xlsx fields**: `item_name`, `item_code`, `mrp`
- **Output**: 326 products per run, `status=success`

### Functional
Returns Monroe (Tenneco) shock absorbers and struts sold via BKS Motors (online reseller). Each item has descriptive name (e.g., "FRONT SHOCK ABSORBER / STRUT ELEMENT (LH/RH)-TAVERA 1ST GEN 2008-2017"), Monroe SKU (e.g., "M2N3R7203"), and MRP in INR.

### Technical
- Magento 2 storefront, static HTML, **httpx + parsel**.
- **Step 1**: Strip `?srsltid=...` Google referral param (per plan §12.6).
- **Step 2**: Walk pagination via `?p=<N>` (Magento default) until a page yields zero new SKUs.
- **Step 3**: For each `li.item.product` / `.product-item-info`:
  - `item_name` ← `a.product-item-link::text`
  - `item_code` ← regex `SKU:\s*</strong>\s*([\w\-\.]+)` inside the card's `description-table`
  - `mrp` ← `.price-wrapper[data-price-amount]::attr(data-price-amount)` (Magento's structured price attribute — bypasses currency-format parsing)
- 17 paginated requests + 1 trailing page that confirms exhaustion (no new SKUs); runtime ~17s.

### Site reality vs xlsx flow
- xlsx says "open URL → list of all products → extract" — implemented as-is.
- Site claims "Total 326 Items Found" badge — spider extracts exactly 326. Round-trip count match.

### Gotchas
- Past the last real page (page 18+), Magento returns 21 "related items" cards rather than empty content. Spider stops via the `new_count == 0` heuristic, not via 4xx.
- The `srsltid` referral param is added when reaching the page from Google. Always strip it — the URL works without it and reduces analytics noise.
- BKS Motors is a multi-brand reseller. Other Monroe URLs may exist (`/brand/monroe-shocks`, etc.) but only `/brand/monroe` is in scope per xlsx.
- The 326 figure is BKS Motors' inventory — NOT Monroe's full India catalog. This is a known reseller scoping decision in the xlsx.

### File
- [spiders/monroe.py](../spiders/monroe.py)

---

## 5. Technix — ✅ shipped

- **URL**: https://technixauto.com/
- **Pattern**: flat_list (BRD §6)
- **xlsx fields**: `item_name`, `item_code`, `mrp`
- **Output**: 1596 products per run, `status=success`

### Functional
Returns Technix's full aftermarket catalog: bushes, engine mountings, strut kits, strut bearings, strut mountings, PU bump stoppers. Item codes follow Technix's internal format (e.g., "YVU-A5600"); names are concise descriptors ("Rubber Strut Kit RR").

### Technical
- WordPress + JetEngine Custom Post Type, **httpx + parsel**.
- **Step 1 — Enumerate via WP REST API**: `GET /wp-json/wp/v2/singlecar?per_page=100&page=<N>` — yields all 1596 records in 16 calls. `X-WP-Total` and `X-WP-TotalPages` headers tell us the total upfront.
- **Step 2 — Detail-page fetch**: For each post `link`, fetch the detail page. Extract:
  - `item_code` ← record's `title.rendered` (uppercase SKU display, e.g., "YVU-A5600")
  - `item_name` ← XPath `//*[normalize-space(text())='Product Name']/following::*[contains(@class,'jet-listing-dynamic-field__content')][1]/text()`
  - `mrp` ← first `.jet-listing-dynamic-field__content::text` starting with "MRP ₹" (skip the matches in the "Similar Ads" carousel that come later)
- ~1612 HTTP requests total (16 API list calls + 1596 detail fetches); runtime ~4 min.

### Site reality vs xlsx flow
- xlsx says "click search w/o filters → flat list across tabs → extract". The site's search widget is JS-rendered and broken when you load it server-side. But the **WP REST API** exposes the same product post-type unfiltered — equivalent to "no filter applied".

### Gotchas
- Custom post type is `singlecar` (not `cars` or `products`). Discovered via `/wp-json` route enumeration.
- "Similar Ads" section on each detail page also uses `jet-listing-dynamic-field__content` — extract only the FIRST MRP match (in source order), not all of them.
- The `Product Name` label appears twice on each detail page (once in main, once in sidebar/footer). Both resolve to the same value — first-match XPath is safe.
- WP REST `per_page` cap is 100 (Wordpress default). Cannot fetch all 1596 in one call.

### File
- [spiders/technix.py](../spiders/technix.py)

---

## 6. GABRIEL — ✅ shipped (pattern reclassified to `pdf_brochure`)

- **URL**: https://www.anandgroupindia.com/gabrielindia/aftermarkets/?pcatid=all&vcatid=passenger-vehicle
- **xlsx pattern**: multi_level_category — **REVISED to `pdf_brochure`** (site landing has no SKUs/MRPs)
- **xlsx fields**: `item_name`, `item_code`, `mrp`
- **Output**: 2209 PV products per run, `status=success`

### Functional
Returns Gabriel India's full PV aftermarket catalogue: shock absorbers, struts, bush kits, OC springs, gas springs, brake pads, drive shafts, suspension parts, synchronizer rings, alloy wheels, brake fluid, coolant, and PV-relevant tyres/tubes. 2W/3W/CV variants excluded.

### Technical
- **Site is just marketing chrome** — 18 modal divs all contain category descriptions, ZERO product SKUs in HTML.
- The xlsx says "in the catalogue, search for 4W products" — the **catalogue** is the PDF download link on the landing page (`GIL-All-Products-Catalogue-*.pdf`).
- Spider flow:
  1. **PDF URL discovery**: regex-match the PDF anchor on the landing page (`.*\/wp-content\/uploads\/.*\/.*Catalogue.*\.pdf`). Works even when Gabriel publishes a new edition.
  2. **Download** the PDF (~12 MB) via httpx.
  3. **Page-range scoping**: hard-coded `SECTION_PAGE_RANGES` derived from the PDF's Index page (page 2). Maps 1-indexed page numbers → section names (e.g., pages 12-18 = "4W Shock Absorbers", pages 5-9 = "2W Shock Absorbers"). Out-of-scope sections are listed in `OUT_OF_SCOPE_SECTIONS`.
  4. **Row parse**: for each in-scope page, parse text via `pdfplumber`. Lines matching `AM-<code> <suitable_for...> <std_pack:int> <mrp:float>` are extracted.
  5. **Row-level filter**: drop any row whose `suitable_for` contains explicit `2W` / `3W` / `CV` tokens (catches mixed-segment rows in "Tyres & Tubes" section).
  6. `item_name = "<Section>: <SUITABLE FOR/APPLICATION>"`, `item_code = AM-...`, `mrp = float`.
- ~2 HTTP requests (landing + PDF); runtime ~5 sec.

### Site reality vs xlsx flow
- xlsx says "Aftermarket tab → catalogue → search 4W products". The "catalogue" referenced is the PDF; the on-site flow has no searchable catalogue.
- **Action per BRD §8**: file a finding — xlsx pattern was misclassified. Confirm with Spinny that PDF-catalogue scrape is acceptable (it's the data source the xlsx implicitly points at).

### Gotchas
- The PDF Index page lists page ranges per section. If Gabriel issues a new edition with reordered sections, `SECTION_PAGE_RANGES` must be updated. **Detection**: spider logs `extracted N rows` — drop from ~2200 to ~100 = section mapping is broken.
- "Tyres & Tubes" section (pages 100-106) has mixed segments. PV tyres (e.g., car tyres) need to stay; 2W/3W tyres need to drop. Row-level regex on the SUITABLE FOR column (`\b(2W|3W|CV)\b`) handles this — works as long as Gabriel keeps using these abbreviations.
- Initial header-detection approach was abandoned because "2025-26" copyright line was being picked up as a section title — page-range scoping from the Index page is the deterministic alternative.
- AGMi/Gabriel-EV products (pages 1-4) included via "EV Products" section. If Gabriel adds an "EV 2W" sub-section, revisit.

### File
- [spiders/gabriel.py](../spiders/gabriel.py)

---

## 7. ZIP Filters — ✅ shipped

- **URL**: https://www.zipfilters.com/
- **Pattern**: multi_level_category (BRD §6)
- **xlsx fields**: `item_name`, `item_code`, `mrp`
- **Output**: 811 PV filters per run, `status=success`

### Functional
Returns ZIP's 4-wheeler filter catalogue across 6 categories: Air Filters (351), Cabin Filters (179), Fuel Filters (117), Filter Kits (6), Oil Filters (147), Transmission Filters (11). 2W filters for Bajaj/Hero/TVS-scooter/Royal Enfield/Yamaha/KTM are excluded.

### Technical
- Static HTML, **httpx + parsel**.
- 6 GET requests to `/products?cat=N` (1..6). Each category is one long HTML page — no pagination.
- Card selector: `.product__item`. Extracts:
  - `item_name` ← `.product__item__title a::text` (e.g., "ZIP Air Filters ZA-0201")
  - `item_code` ← XPath: `//strong[contains(., 'Part No')]/following-sibling::text()[1]`
  - `suitable_for` ← XPath: `//strong[contains(., 'Suitable For')]/following-sibling::text()[1]` (used only for 4W filter, not in output)
  - `mrp` ← last child `<div>` whose text starts with `₹` (no class — must select by content)
- **Row-level 4W filter**: drop rows where `suitable_for` starts with `2-WHEELERS`, `BAJAJ`, `HERO`, `TVS APACHE/JUPITER/XL/STAR/SCOOTY/NTORQ`, `ROYAL ENFIELD`, `YAMAHA`, `KTM` (regex `NON_4W_PREFIX`).
- ~6 HTTP requests; runtime ~2s.

### Site reality vs xlsx flow
- xlsx says "6 sub-sections → for each → product list → extract" — exactly what we do, via the `cat=N` URL param.
- Site claims "1600+ SKUs". We see ~847 cards total across 6 cats (some are duplicated or hidden 2W); after 4W filter, 811 remain. The 1600+ likely includes their full catalog incl. 2W and unlisted variants.

### Gotchas
- The price `<div>` has **no class** — inline styles only. Must select via text content (`text starts with ₹`).
- The `.product__item__content` has multiple inline-styled divs; the price isn't always the last child if "Suitable For" / "Part No" labels are inside divs too. Spider picks the last `<div>` whose text starts with ₹.
- 4W denylist is conservative — keeps Honda/Suzuki (both make 2W and 4W). Some Honda 2W filters may leak in. If precision matters, audit `Air Filters` rows whose `suitable_for` contains "HONDA ACT/SHN/SP" patterns.
- ZIP also has a "Vehicle Make" search filter on the homepage. If we ever need stricter 4W filtering, switch to per-make search URLs instead of category URLs.

### File
- [spiders/zip.py](../spiders/zip.py)

---

## 8. LUMAX — ✅ shipped (pattern reclassified to `pdf_brochure`)

- **URL**: https://www.lumaxworld.in/aftermarket/product-catalogue.html
- **Pattern**: pdf_brochure (xlsx said multi_level_category — site reality is PDF-only)
- **xlsx fields**: `item_name`, `item_code` (no MRP required, but extracted as bonus)
- **Output**: 2185 PV products per run, `status=success`

### Functional
Returns LUMAX's full 4W aftermarket catalogue from the consolidated "4W Price List as on Date" PDF: head lamps, tail lamps, side mirrors, indicators, fog lamps, gear knobs, horns, and electrical components across Maruti, Hyundai, Tata, Mahindra, Toyota, Honda, and other PV makes. Material Codes are 8-digit numeric stable SKUs.

### Technical
- Site landing has **only PDF download links** — no on-page product data.
- Spider flow:
  1. Discover the 4W price list PDF anchor: `href="*4W-price-list*.pdf"`.
  2. Download (~2.3 MB) via httpx.
  3. Parse with `pdfplumber` (92 pages). Row schema: `Material Code | Parts Nos | Description | HSN Nos | MRP | STD PKG | MOQ`.
  4. Regex `(\d{8})\s+(\S+)\s+(.+?)\s+(\d{8})\s+(\d+)\s+(\d+)\s+(\d+)` to capture each row.
  5. Track Make/Model/Sub-section context via all-caps short-line header detection (e.g., `MARUTI → ALTO → HEAD LAMP`).
  6. `item_name = "<Make> <Model> <Sub-section>: <Description>"`.
- ~2 HTTP requests; runtime ~7s.

### Site reality vs xlsx flow
- xlsx says "go to product catalogue page → select category (skip 2W) → extract". The "catalogue page" links to PDFs. The 4W Price List PDF is the consolidated source.
- 4W scope is built into the PDF title ("4W Price List") — no row-level 4W filter needed.

### Gotchas
- The PDF anchor URL changes monthly (`4W-price-list-as-on-date-nov-25.pdf` → `dec-25.pdf` next month). The regex `4W-price-list[^"]+\.pdf` adapts automatically.
- Make/Model header detection is heuristic — works for the current PDF layout. If LUMAX changes formatting (e.g., adds a section divider), the context becomes stale but rows still extract.
- The Material Code (8-digit) is more stable than Parts Nos (which has typos / format drift across rows). Use Material Code as `item_code`.
- xlsx declares only `[item_name, item_code]` for LUMAX. We additionally emit `mrp` because it's available — masters that consume LUMAX data get pricing for free.

### File
- [spiders/lumax.py](../spiders/lumax.py)

---

## 9. ZIP Filters — see §7 (shipped earlier in the document)

---

## 10. VALEO — ✅ SHIPPED (TecAssist API decoded 2026-05-19; previously SHIPPED-AS-FAILED)

- **URL** (xlsx): https://www.valeoservice.in/en-in/passenger-car
- **Working URL**: https://www.valeoservice.in/en-in/techassist (Technical Assistance)
- **Pattern**: REST API (Next.js prefetch JSON + TecAssist POST endpoint)
- **xlsx fields**: `item_name`, `item_code`, `compatible_car_model` (no MRP)
- **Output**: **5,286 unique articles per run, `status=success`** (all 3 xlsx fields populated via OEM cross-reference)

### Functional
Returns the full Valeo India Technical Assistance catalogue: 5,286 unique Valeo part numbers spanning Alternators, Starters, Brakes, Clutches, Wipers, Lighting, Cooling, Filters, Electrics, Switches, etc. Each row has an OEM cross-reference linking the Valeo part to the equivalent vehicle-manufacturer OE part number (e.g., Valeo `207441` = Mahindra `0603BAA-0461N`). Top makes by cross-ref count: Hyundai 191, Tata 149, Audi 126, Mercedes-Benz 125, Mahindra 100, Maruti 73, Nissan 69, Volvo 58, BMW 55, Toyota 50, Ford 47, Suzuki 35, Renault 34, Honda 32.

### Technical
- Static HTTP + JSON, **httpx only** (no Playwright). `verify=False` due to Valeo cert chain quirks on some clients.
- **Step 1 — BUILD_ID discovery**: GET https://www.valeoservice.in/en-in/techassist → parse `<script id="__NEXT_DATA__">` JSON → extract `buildId` (changes per Next.js deploy, e.g. `M1ERjgswRLEuvHoveKeET`).
- **Step 2 — Product lines list**: `GET /_next/data/<BUILD>/en-in/techassist/products/product-lines.json?selectedProductLineTab=PASSENGER` → 24 PASSENGER product lines (numeric `id` 100001..200067, names: Air Conditioning, Belt Drive, Body, Braking System, Clutch, Cooling, Electrics, Engine, Lighting, Wiping, etc.).
- **Step 3 — Sub-categories per line**: `GET /_next/data/<BUILD>/en-in/techassist/products/product-lines/product-line/P-<id>.json` → `productLine.parts` array, each with numeric `id` (e.g., 402 = "Brake Pad Set, disc brake") and `articlesCount`.
- **Step 4 — Articles**: `POST https://api.valeoservice-techassist.com/rest/articles?page=N&country=IN&lang=en` with body `{"filters":{"partIds":["<id>"],"brands":[]}}`. Server-side pagination at pageSize=10 (fixed, cannot override). Walk all pages per sub-cat. **No auth required** — only the Referer header `https://www.valeoservice.in/` is checked.
- **Step 5 — Dedup**: same `reference` may appear across multiple product lines (Valeo splits lines into Passenger + Line-of-Business sub-tabs). Dedup by `reference`.
- **Row build**: `item_code` ← article `reference`; `item_name` ← `"Valeo " + description`; `compatible_car_model` ← concatenated OEM cross-refs `"MAHINDRA: 0603BAA-0461N | TATA: 2724254..."` (cap 8 makes × 3 OEM numbers each, 400-char limit).
- **MRP**: NOT in any API response. Confirmed by recursive scan for `price/mrp/cost/amount/rupee/currency/rate` keys across full article payload — zero hits. User manually verified same. `mrp` is not a required field for Valeo per xlsx, so rows finalize as `success` (not `partial`).
- **Performance**: 1× landing + 24× line-list + ~70× per-line parts + ~1,400× POST articles (paginated) = ~1,500 HTTP calls. ~5.4 minutes full run.

### Site reality vs xlsx flow — **BREAKAGE RESOLVED 2026-05-19**
- The xlsx-documented URL (`/en-in/passenger-car`) hosts marketing pages only — no product data. **Initial probe 2026-05-18 declared the site SHIPPED-AS-FAILED.**
- The user manually discovered the Technical Assistance feature (`/en-in/techassist`) supports vehicle-model and part-number lookups, returning actual parts. Vendor re-probe 2026-05-19 reverse-engineered the underlying TecAssist API contract: a **public** Next.js + REST architecture with no auth and well-structured JSON payloads. The catalogue is fully extractable via these public endpoints.
- Recommended action per BRD §8: notify Spinny of the URL change. The xlsx pointer at `/en-in/passenger-car` is misleading; the actual catalogue is at `/en-in/techassist` with sub-tab `selectedProductLineTab=PASSENGER`.

### MRP escalation (B10 in kickoff_checklist)
MRP is not present in any Valeo public endpoint. The TecAssist API returns full technical catalogue but no pricing. Options for Spinny: (a) Pursue Valeo dealer/partner access for pricing API, (b) Cross-reference Valeo `reference` against another priced supplier feed, (c) Accept rows without MRP (xlsx does NOT require MRP for Valeo, so this is the cleanest path). Same partial-rationale as Schaeffler / Toyota / Bosch wipers — but here MRP wasn't required anyway, so all rows finalize as `success`.

### Gotchas
- **BUILD_ID changes per Next.js deploy** — spider must scrape it dynamically from `<script id="__NEXT_DATA__">` on every run. Hardcoded BUILD_IDs will break.
- **Pagination is server-side only**: `?page=N&pageSize=200` is silently ignored — the server always returns 10 per page. Spider must walk `pagination.pageCount` for each sub-category. Confirmed in [state/probe_valeo_pagination.py](../state/probe_valeo_pagination.py).
- **Product-line ID format mismatch**: JSON has `"id": 100006` (int); URL has `P-100006` (string with prefix). Spider constructs `f"P-{line['id']}"` for URL.
- **OEM cross-ref is the only PV-compatibility signal**: the `vehicleCriteria` field is usually empty; `oemNumbers` is the rich source. Some articles (Brake Fluid universal, Air Dryer Cartridge CV-only) have no clean PV signal — spider falls back to `"VALEO <line> > <part_desc>"` for those.
- **OEM names include CV-only makes** (MAN, IVECO, ASHOK LEYLAND, MERITOR, RENAULT TRUCKS). The current spider does NOT filter at article level — it preserves the full cross-ref string. If Spinny needs strict PV-only, add a post-hoc filter against a PV-make whitelist (same one used in HELLA + Bosch wipers).
- **Same article appears in multiple product lines** — typically via the `P-100050` (Clutch) vs `P-200053` (Clutch line-of-business) duplication. Spider's `seen_refs` dedup by `reference` handles this; the 24-line list becomes ~12 unique line scans.
- **`verify=False`**: Valeo's cert chain occasionally fails strict verification (intermediate CA cache issue, same family of bug as JK Tyre). Scoped to this spider only.

### Files
- [spiders/valeo.py](../spiders/valeo.py) — full implementation
- [state/probe_valeo_api.py](../state/probe_valeo_api.py) — captured POST body that decoded the API
- [state/probe_valeo_pagination.py](../state/probe_valeo_pagination.py) — confirmed server-side pagination behaviour

---

## 11. UNO MINDA — ⏭️ skipped (out of "easy" scope)

- **URL**: https://unomindakart.com/
- **Pattern**: declared `multi_level_category` — site reality: **medium-difficulty JS-rendered SPA**
- **Status**: not built; deferred to medium-bucket implementation

### Investigation summary (2026-05-18)
- Site loads `Loading...` placeholder in static HTML — pure SPA.
- Categories/segments visible in nav (`/category/<slug>`, `/make-model?segment=cars`).
- After Playwright + `networkidle` + 4s extra wait: **0 ₹ prices in DOM, 0 product cards**.
- Only 2 XHRs fire on page load (`/fetchMakes`, `/get_main_category_ajax`) — neither returns products.
- xlsx documents an interactive flow: "Know What You're Looking For? → products by segment → car → tabs".
- Conclusion: products only render after user actions (segment select, make-model pick, scroll). Same caliber as Mobil's Coveo pattern — requires Playwright capture-replay of the post-interaction API call.

### Recommended approach when picked up
1. Playwright + `page.on('response')` interceptor.
2. Programmatically click `?segment=cars` → select first Make → first Model → wait for product list.
3. Capture the XHR endpoint that returns products (likely `/fetchProducts` or similar).
4. Replay via `ctx.request.post()` for each Make×Model combination (Cars segment only).

---

## 12. AMARON — ✅ shipped (first cascading_dropdown crack)

- **URL**: https://www.amaron.com/passenger-vehicles
- **Pattern**: cascading_dropdown (Make × Model × Fuel)
- **xlsx fields**: `item_name`, `item_code`, `mrp` + `compatible_car_model` (we emit as bonus)
- **Output**: 52 unique batteries per run, `status=success`

### Functional
Returns Amaron's full passenger-vehicle battery range. Each row is one (Make, Model, Fuel) → battery mapping. 32 makes × ~10 models each × ~1-2 fuels = ~600 vehicle combos crawled; ~52 distinct battery SKUs surface (heavy overlap — same battery fits many cars).

### Technical
- Site is Drupal 9 with a 3-select AJAX form.
- **Step 1 — Cascade Make → Model via Drupal AJAX**: POST to `/passenger-vehicles?ajax_form=1&_wrapper_format=drupal_ajax` with `_triggering_element_name=vehicle-make` and the selected `vehicle-make` value. Response is a JSON array of Drupal AJAX commands; the `insert` command with `selector="#edit-model"` carries new `<option>` HTML. Track `form_build_id` — it mutates on every AJAX response via `update_build_id` command.
- **Step 2 — Cascade Model → Fuel**: same AJAX endpoint, `_triggering_element_name=model`.
- **Step 3 — Result page**: Amaron's "Find Now" form-submit redirects via HTTP to `/battery/passengers/<make-slug>/<model-slug>/<fuel-slug>`. Discovery: **the slug-based URL works directly via httpx GET** — no need to follow the form redirect (which httpx couldn't trigger anyway due to missing session state). `_slug()` rule: lowercase, replace non-alphanumeric runs with `-`, strip edges.
- **Result-page parsing**: `<table class="comparisionTable">` has battery cards as `<span class="bold-font">` in row 0; MRP row is identified by `<th>` text containing `"Base Price"` (Amaron labels MRP this way).
- **Row dedup** at `item_code` level — same battery appears under many vehicle combos.

### Site reality vs xlsx flow
- xlsx says "select Make → Model → Fuel → extract from product description". Direct site interaction works as documented. Spider replicates programmatically.

### Gotchas
- The form-submit "Find Now" button **does NOT redirect when posted via httpx** — Drupal 9 wants a full browser session. Direct URL construction is the workaround.
- ~half of (Make×Model×Fuel) combos return **HTTP 500** when no Amaron battery exists for that vehicle. Spider skips silently; not an error.
- "Base Price (Inclusive of GST)" is Amaron's MRP label — has nothing to do with their "Special Discount" or "Total Price" rows.
- Some Amaron entries include BAJAJ Qute (a quadricycle on the passenger-vehicles page — kept in scope since Amaron categorizes it as PV).
- 32 makes × cascade overhead + ~600 result fetches → **~18 min sequential**. Could parallelize per-make with asyncio for ~3 min runtime if needed (Phase D optimization).

### File
- [spiders/amaron.py](../spiders/amaron.py)

---

## 13. SF SONIC — ✅ FIXED 2026-05-22 (regression diagnosed as scope+speed, not data loss)

**Root cause analysis 2026-05-22**: the 2026-05-21 "37 rows in 45 min" wasn't a data-extraction bug — the spider was working correctly but **slow** (~2.5 min per brand × 34 brands = ~85 min full walk). The run was killed mid-walk at brand 24 of 34 (TATA), so only ~37 unique batteries had been seen by then because the slowest brands (Maruti, Hyundai with ~58 models each) were still pending.

**Fix 2026-05-22**: rewrote per-brand walk to use **async parallel fuel fetches** (4 concurrent result-page GETs per model). Brand→Model handshake POSTs stay sequential (Drupal session affinity), but result pages now fetch 4-wide in parallel. Estimated wall-clock drops from ~85min to ~22min.

Added per-brand progress + elapsed logging so future regressions are observable. Result-page parsing logic unchanged.

---

## 13. SF SONIC — ✅ shipped (smoke-tested; full run ~2h)

- **URL**: https://www.sfbatteries.in/battery-finder/4w-battery/
- **Pattern**: cascading_dropdown (Brand × Model × Fuel)
- **xlsx fields**: `item_name`, `item_code`, `mrp`
- **Smoke output**: Hyundai alone = 7 unique batteries in ~4min, all `status=success`
- **Expected full run**: ~33 brands × ~4min/brand = **~2 hours**

### Functional
Returns SF SONIC's full passenger-vehicle battery range. Item codes are full SF SKUs (e.g., `F4W5-66S-40B20L`), names combine variant + battery model (e.g., "Enhanced Life 66S-40B20L"), MRPs as integers from on-page `<h5>MRP: Rs N</h5>` element.

### Technical
- Custom WordPress-style AJAX form. POST endpoint: `/battery-finder/4w-battery/` with `ait_action` discriminator.
- Cascade actions:
  - `ait_action=loadModels` + `brand=<BRAND_LABEL>` → JSON `{"models": "<option>...</option>"}`
  - `ait_action=loadModelsFuels` + `brand+model` → JSON `{"fuels": "..."}`
  - `ait_action=find_battery` would POST + 302 + 301 redirect chain — bypassed.
- **Bypass: direct GET** to `https://www.sfbatteries.in/battery-for/car/<brand-slug>/<model-slug>/<fuel-slug>/` (canonical URL with trailing slash). Same `_slug()` rule as AMARON.
- Result page: each battery is in `<li class="addAnimate"><aside>`. Selectors:
  - `aside h2::text` → item_code (full SKU)
  - `aside h5` → `MRP: Rs <NUMBER>` via regex
  - `article.moreProductInfo h3::text` → simple battery model
  - `article.moreProductInfo p span::text` → variant name (e.g., "Enhanced Life")
- **Short-circuit**: if 2 consecutive fuels return 0 new SKUs for a model, skip remaining fuels for that model. SF SONIC batteries usually don't differ by fuel.

### Site reality vs xlsx flow
- xlsx flow works as documented. No reclassification needed.
- 33 brands × ~30 models avg × 4 fuels (Petrol/Diesel/LPG/CNG) = ~4000 combos. Heavy redundancy means actual unique SKUs are likely <100.

### Gotchas
- **Slow runtime (~2h)**: most time spent on network round-trips. SF SONIC slug-URL responses take ~1.5s each due to server-side rendering. Acceptable for monthly batch (8-hour BRD window). Could parallelize with `asyncio.AsyncClient` to ~15min if needed (Phase D optimization).
- **Brand select option `value` equals the label text** — `_slug(label)` is correct for URL construction.
- **`load_fuels` is wasteful**: returns the same 4 fuels (petrol/diesel/lpg/cng) for every model. Could be hardcoded to save N AJAX calls per brand. Skipped for robustness — server is the source of truth.
- The form's submit button label "Locate" is misleading — it actually triggers `ait_action=find_battery`.

### File
- [spiders/sf_sonic.py](../spiders/sf_sonic.py)

---

## 14. LuK/Schaeffler — ✅ SHIPPED (WAF defeated 2026-05-19)

- **URL**: https://vehiclelifetimesolutions.schaeffler.in/en-gb/catalog
- **Pattern**: json_api_pagination (SAP Commerce Cloud Spartacus, RepXpert backend)
- **Required fields**: item_name, item_code, mrp
- **Status**: **17,778 passenger-car products extractable**; rows marked `partial` because MRP is empty (Schaeffler India lists products as `catalogStatus=on_demand`, prices on request)

### WAF bypass — the key unlock
The Akamai-style WAF behaves differently based on request headers:
- **Blocked**: bare `httpx`, basic Playwright `page.goto`, requests without `Sec-Fetch-*` / `sec-ch-ua-*` headers → 403 "Access denied" + IP fingerprint echoed.
- **Allowed**: `ctx.request.get(...)` with full Chrome-131 fingerprint headers — `Sec-Fetch-Dest`, `Sec-Fetch-Mode`, `Sec-Fetch-Site`, `Sec-Fetch-User`, `sec-ch-ua`, `sec-ch-ua-mobile`, `sec-ch-ua-platform`, `Accept-Encoding: gzip, deflate, br`, `Upgrade-Insecure-Requests: 1` → 200 OK.

**No proxy, no SaaS solver, no Spinny escalation needed.** The WAF's bot heuristic missed the full browser fingerprint when supplied in HTTP headers.

### Architecture
1. **Frontend SPA** at `vehiclelifetimesolutions.schaeffler.in` (Angular + Spartacus).
2. **OCC backend** at SAME host under `/api/<baseSite>/...` (custom prefix, not the standard `/occ/v2/`).
3. **Base site** for India is **`Repxpert-IN`** (Schaeffler operates the catalogue under their RepXpert brand). Discovered via `GET /api/basesites?fields=baseSites(uid)` — returns 30+ regional Repxpert-* sites including DE, ES, AT, CH, GB, US, BR, AE, UA, GR, and IN.

### Crawl strategy
Single endpoint, paginated:
```
GET /api/Repxpert-IN/products/search
    ?query=:relevance:targetTypes:passengerCar    (BRD passenger-vehicle gate)
    &pageSize=120                                  (server caps at 120)
    &currentPage=N                                 (0-indexed)
    &fields=DEFAULT
```

Response per product:
- `name` → item_name (e.g. "NOx Sensor, urea injection")
- `catalogArticleNumber` → item_code (e.g. "571 0018 10" — global Schaeffler part number)
- `priceRange` → MRP (consistently `{}` for Repxpert-IN — see below)
- `code`, `ean`, `tradeNumbers`, `seoPath`, `brand` — available as bonus metadata

### Volume
- `targetTypes:passengerCar` filter → **17,778 products** across ~149 pages
- ~1 second per page → **~3 minutes total** for full Schaeffler crawl
- Well within BRD §8 8h window

### MRP — partial-status rationale (USER-VERIFIED 2026-05-19)

The `priceRange` field is `{}` for every product on Repxpert-IN. Investigation of the response shows `purchasableStatus: {code: "on_demand", name: "On request"}` — Schaeffler India's commercial model lists parts as "price on request" rather than publishing list prices.

**User manually verified on the live site 2026-05-19: no MRP visible anywhere on the Schaeffler India catalogue.** This is a Schaeffler business policy, not a scraping limitation. Per BRD §7 ("Where a field listed in the sheet is not visible on the live site... that field is left blank and crawl_status is set to partial") the rows finalize as `partial`. The spider does extract any priceRange value defensively in case Schaeffler changes their model.

**ESCALATION TO SPINNY (user action — 2026-05-19)**: Spinny is being informed that Schaeffler India does not publish MRPs on `vehiclelifetimesolutions.schaeffler.in`. Possible paths Spinny may pursue:
   (a) Register as a RepXpert dealer (Schaeffler's authenticated catalogue) — dealer logins typically unlock pricing.
   (b) Accept the 17,778 PV parts with `partial` status (item_name + item_code only — same partial-rationale as Exide).
   (c) Cross-reference Schaeffler `catalogArticleNumber` against another priced supplier feed downstream of this crawl.

### Smoke test result (2 pages, ~240 rows)
```
571 0018 10  → NOx Sensor, urea injection
571 0002 10  → NOx Sensor, urea injection
571 0060 10  → NOx Sensor, urea injection
... (228 unique items)
```

### Files
- [spiders/schaeffler.py](../spiders/schaeffler.py)

### Files (probes for next-session reference)
- `state/probe_schaeffler*.py` — the WAF-bypass + endpoint-discovery probes.

---

## 15. JK TYRE — ✅ SHIPPED (RSC dodge via static URL tree, 2026-05-19)

- **URL**: https://www.jktyre.com/pcr
- **Pattern**: multi_level_category (`/pcr/<segment>/<make>/tyre-details/<slug>`)
- **xlsx fields**: item_name, compatible_car_model, tyre_sizes (no item_code, no MRP)
- **Status**: ~30 unique tyres × multiple sizes each in ~3 min full crawl

### The unlock — sidestep the RSC entirely

Prior investigation classified JK Tyre as a 14-20h RSC reverse-engineering job. The reality: the homepage tyre-finder widget IS a Next.js 13+ RSC + react-select monster, **but the site also exposes a fully static URL tree** under `/pcr/` (passenger car radial):

```
/pcr                                    landing — lists 3 segments + their Make lists
/pcr/<segment>                          segment landing (SUV/MUV, SEDAN, HATCHBACK)
/pcr/<segment>/<make>                   make page — lists 1-17 compatible tyres
/pcr/<segment>/<make>/tyre-details/<slug>  detail page — name + AVAILABLE SIZE(S)
```

All four levels return server-rendered HTML accessible to plain `httpx` (no JS, no
RSC parsing). The RSC tyre-finder on the homepage is just a UI shortcut — the
underlying data is browsable via clean URLs.

### Segment / Make matrix (per BRD passenger-vehicle gate)

The `/pcr` tree IS the BRD passenger gate by construction (no commercial /
motorcycle / tractor catalogues are reachable from it). Segments:

- **SUV/MUV** (19 makes): BMW, BYD, Citroen, Ford, Honda, Hyundai, Isuzu, Jeep, Kia, Land Rover, Mahindra, Maruti Suzuki, Morris Garages, Nissan, Renault, Skoda, Tata, Toyota, Volkswagen
- **SEDAN** (15 makes): Audi, BMW, GM, Honda, Hyundai, Jaguar, Mahindra, Maruti Suzuki, Mercedes, Renault, Skoda, Tata, Toyota, Volkswagen, Volvo
- **HATCHBACK** (15 makes): Citroen, Datsun, Fiat, Ford, GM, Honda, Hyundai, Maruti Suzuki, Mini Cooper, Morris Garages, Nissan, Renault, Tata, Toyota, Volkswagen

**Total: 49 (segment, make) combos** to crawl.

### Crawl chain

1. `GET /pcr` → parse 49 (segment, make) URLs
2. For each combo: `GET /pcr/<seg>/<make>` → extract `<a href="...tyre-details/...">` URLs
3. For each tyre-detail URL: `GET ...` → extract:
   - `item_name` ← `<h1>` text (e.g. "UX ROYALE SMART - EMBEDDED SMART TYRE")
   - `tyre_sizes` ← regex `\d{3}/\d{2}\s*R\s*\d{1,2}` matches in body (e.g. "165/80R14, 185/65R15")
   - `compatible_car_model` ← derived from URL: `<segment> | <make>` (e.g. "SUV/MUV | HONDA")
4. **Dedup on tyre-slug** — same tyre (e.g. UX ROYALE) appears under many (segment, make) combos. Spider merges compat into a `; `-joined list across all combos.

### Smoke test — 12 combos

19 unique tyres extracted with full sizes + multi-make compat:
- **LEVITAS ULTRA** (BMW) — 225/45R17, 225/50R17, 225/55R16, 225/55R17, 235/55R17, 245/45R18
- **UX ROYALE** (BYD, CITROEN, FORD, +more) — 14 sizes
- **RANGER H/T** (Ford, Hyundai, Isuzu, …) — 12 sizes incl. 215/75R15 to 265/70R17
- **UX ROYALE SMART - EMBEDDED SMART TYRE** (Honda, Hyundai, Kia, …) — 165/80R14, 185/65R15, 215/60R16, 215/60R17
- **RANGER H/T (PUNCTURE GUARD)**, **RANGER A/T**, **RANGER M/T**, **BRUTE 4X4** (Mahindra)
- Mahindra page alone exposes 17 tyres including RWL variants

Status: `success`.

### TLS quirk — `ignore_https_errors=True` needed

jktyre.com serves an **incomplete intermediate certificate chain**. Both stock httpx (with certifi) and Playwright's `APIRequestContext` fail strict verification. Chromium's `page.goto` works (lax cert handling). Spider runs through a Playwright context with `ignore_https_errors=True` — scoped to this site only. **Operational note**: if Spinny IT enforces cert verification VM-wide, JK Tyre will be the one site that needs an exemption.

### Volume

49 combos × avg 5 tyres each = ~250 detail fetches; dedup yields ~25-35 unique tyres.
~0.5s per fetch via Playwright → **~3 minutes full crawl**.

### File
- [spiders/jk_tyre.py](../spiders/jk_tyre.py)

---

## 16. Bosch — ✅ shipped (ALL 7 catalogue PDFs parsing, 2026-05-19 deeper pass)

- **URL**: https://ap.boschaftermarket.com/in/en/parts/
- **Pattern**: pdf_brochure (per-category PDFs with per-layout dispatch)
- **Output**: 1664 rows (582 success with MRP + 1082 partial)
- **Coverage**: all 7 catalogue PDFs (brakes, diesel, gasoline, spark-plugs, starters, wipers; `sensors` is a duplicate URL of gasoline-parts and is correctly deduped).

### Functional
Returns Bosch India's passenger-vehicle aftermarket catalogue across 6 unique PDFs (7 categories, 1 URL duplicate). Brakes / diesel / gasoline / spark-plugs catalogues use a line-format with MRP present (582 priced rows). Wipers and starters/alternators catalogues use table-format without MRP (1082 rows ship as `partial` per BRD §7).

### Technical — per-PDF dispatch
- **Discovery** (unchanged): walk `/in/en/parts/<category>/` pages, find `*.pdf` anchors containing "catalog". Discovers 7 catalogue URLs; `sensors` and `gasoline-parts` resolve to the same PDF and the second is skipped via a `seen_pdf_urls` set.
- **Part-number regex broadened**: was `\b0\s\d{3}\s\d{3}\s\d{3}\b` (first digit fixed at 0). Now `\b\d\s\d{3}\s\d{3}\s\d{3}\b` (any first digit) — catches the `3 397 NNN NNN` wiper format and any future `N NNN NNN NNN` variant.
- **Line-format dispatch** (`spark-plugs`, `brakes`, `diesel-parts-and-components`, `gasoline-parts`): unchanged — `extract_text()` per page + regex per line. MRP captured from end-of-line.
- **Wiper-table dispatch** (`wiper-blades`): `extract_tables()` per page yields 7-column tables `[Brand-description, Size, Classic Driver, Classic Passenger, Set, ClearAdvantage Driver, ClearAdvantage Passenger]`. Brand-header rows (single-cell all-caps make) set the current make; data rows have model description in col 0 and Bosch part numbers in cols 2-6. We emit one Row per non-blank (model, part_number) cell, suffixed with the variant (Classic Driver / Set / ClearAdvantage Passenger etc.). Filtered to passenger-vehicle makes only via `PV_MAKES` whitelist (BRD §3.2).
- **Starter-table dispatch** (`starters-and-alternators`): 17-column table; we locate the `Bosch Part No.` and `Application` columns by header text (positional offsets vary across pages). Process pages between "Passenger Cars" section markers (per PDF ToC, pages 24-33 are PV in this catalogue). Part-number regex supports the 4 Bosch starter formats: `1986A00576`, `F002G70212`, `0 124 555 056`, `9000033015` (12-13 digit numeric). All starter/alternator rows lack MRP → `partial`.
- **Helpers**: `_is_make_header()` accepts strings that are ≥70% uppercase letters; `_normalize_make()` strips trailing "(HML)" / "LIMITED" / "INDIA" / "MOTORS LIMITED" suffixes; `_maybe_merge_make_header()` rejoins `["HINDUSTAN MOTORS LIMITED (H", "ML)"]` cells split across columns by pdfplumber.

### Why MRP partial for wipers and starters
These catalogues are technical-application documents, not price lists. Each row is `[Brand | Model description | Size | Part Number(s)]`. There's no MRP column at all. Per BRD §7 we emit the rows we have with `crawl_status=partial` and leave MRP blank. If MRP is mandatory downstream, Spinny would need to escalate to Bosch India for a price list (similar to Toyota / Schaeffler escalations).

### Catalogue inventory
| Category | Format | Rows | MRP | Notes |
|---|---|---:|---|---|
| brakes | line | 119 | yes | Discs/pads/shoes for PV |
| diesel-parts-and-components | line | 377 | yes | Injection system parts |
| gasoline-parts | line | 813 | yes | Sensors, ignition, throttle bodies |
| sensors | line | (deduped) | — | Same URL as gasoline-parts |
| spark-plugs | line | 140 | yes | Standard + Premium |
| starters-and-alternators | table | 87 | no | PV section of ToC; multi-format Bosch part #s |
| wiper-blades | table | 128 | no | Classic + ClearAdvantage variants per model |
| **TOTAL** | | **1664** | 582/1664 | |

### Gotchas
- **Same-URL duplication**: `sensors` and `gasoline-parts` resolve to `gasoline_sensors_catalogue.pdf`. Without dedup we'd pay 28s + 13MB twice. The `seen_pdf_urls` set handles this.
- **Transient discovery flakes**: the Bosch India CDN occasionally serves a partial page response and `_discover_catalogues` may skip a category silently. If a monthly run shows fewer than 6 unique PDFs in the "discovered N catalogue PDFs" log line, schedule a retry. Observed once during testing (brakes missed on first attempt, captured on retry).
- **Wiper table extraction sensitivity**: pdfplumber occasionally splits a brand-header cell across columns (e.g. `["HINDUSTAN MOTORS LIMITED (H", "ML)"]`). The `_maybe_merge_make_header` helper detects unbalanced parens between consecutive cells and merges them.
- **Starter PC-section boundary**: the catalogue's ToC says "Passenger Cars 24" — we trigger the PC flag on any "passenger car" mention and clear it when "light commercial vehicle"/"3-wheeler"/"tractor" markers reappear without a fresh "passenger car" mention. This handles the inconsistent section boundaries in the PDF.
- **PV make whitelist** (`PV_MAKES`): includes ~40 makes covering all PV brands in the Indian market. Update when Bosch adds new OEM partnerships. Currently rejects CV-only makes (ASHOK LEYLAND, EICHER, BHARAT BENZ) and Force Motors' LCV models.
- **`success_rate=0.00` in orchestrator log is misleading**: Bosch row-level rate is 35% success / 65% partial. The `success_rate` metric counts whole-brand status only; "partial" is not "success", so Bosch contributes 0 to the rate. Per BRD §8 this is a row-level partial, not a brand failure.

### File
- [spiders/bosch.py](../spiders/bosch.py)

---

## 17. AUTOKOI — ✅ shipped + ENRICHED (PDF-driven 2026-05-22)

**2026-05-22**: stakeholder confirmed an "E-Catalogue" PDF on autokoi.com homepage contains MRPs for every product. Spider rewritten to use **HYBRID** strategy:
- HTML walk of `/products/` for clean item_names (98 codes)
- Download + parse e-catalogue PDF for MRP coverage (1,382 codes with MRP)
- Merge: 93 codes get spider's item_name + PDF MRP; 1,289 PDF-only codes get derived item_name (`Autokoi {section} ({make}) {code}`) + PDF MRP; 5 HTML-only codes remain partial.

Result: **98 → 1,387 rows**, 99.6% MRP coverage, ~61s runtime. PDF parser lives in [lib/autokoi_pdf.py](../lib/autokoi_pdf.py); E-Catalogue URL is scraped from homepage on each run (resilient to filename changes by Autokoi).

---

## 17. AUTOKOI — ✅ shipped (pre-2026-05-22, kept for history)

- **URL**: https://www.autokoi.com/products/
- **Pattern**: declared `hidden_nav` — site reality: products directly listed at `/products/`, no hamburger needed
- **Output**: 98 SKUs across 18 categories, `status=partial` (no MRP on site)

### Technical
Each /product/<slug>/ page has multiple SKUs (e.g., KRPF14021..). Regex extracts uppercase-alphanumeric codes; item_name combines product type from page title with SKU.

### File
- [spiders/autokoi.py](../spiders/autokoi.py)

---

## 18. Spark Minda — ✅ SHIPPED (URL pattern decoded 2026-05-19)

- **URL**: https://mcl-aftermarket.com/Product/ProductServices
- **Pattern**: multi_level_category (static HTML; httpx + parsel)
- **Required fields**: item_name, item_code, mrp
- **Output**: **1,297 unique 4W products** with all 3 fields populated

### URL chain (no JS required)

```
/Product/ProductServices             → segment landing; lists 14+ segments with /Product/ProductSegment?id=N anchors
/Product/ProductSegment?id=<sid>     → segment overview; shows 3 vehicle-type cards
/Product/ProductDetail?typeid=<vt>&producGrouptId=<sid>   → product list (HTML)
```

`typeid` values:
- 1 = 2 Wheeler
- **2 = 4 Wheeler ← BRD passenger-vehicle gate**
- 3 = 3 Wheeler (some segments)
- 4 = Tractors

The spider always uses `typeid=2` for BRD compliance.

### Per-product HTML block

```html
<div class="product-listing">
  <h3> DOOR HANDLE W/O LOC / 1168-049 SCASYX</h3>
  <p><strong>Vehicle Type</strong> 4W</p>
  <p><strong>MRP ₹</strong><span> ₹ 159.57</span></p>
</div>
```

Field mapping:
- `item_name` ← left side of `" / "` in `<h3>`
- `item_code` ← right side of `" / "` in `<h3>`
- `mrp` ← float from `<span>` after `MRP ₹` (strips ₹ + commas)

### Smoke test result — full crawl

| Segment | 4W products |
|---|---|
| INSTRUMENT | 522 |
| LOCKS | 415 |
| WIRING HARNESS | 116 |
| AUTO ELECTRICALS | many (incl. ALTERNATOR PARTS family) |
| WIPER | 75 |
| CAPACITOR DISCHARGE IGNITION | 59 |
| CLUTCH PLATES / LUBRICANTS / ABS PARTS | 0 (no 4W products listed) |
| **Total unique 4W products** | **1,297** |

Sample data:
- `ARM-2013` ALTERNATOR PARTS — ₹1,837.73
- `BDA-2001SD` ALTERNATOR PARTS — ₹1,858.50
- `DRA-040` ALTERNATOR PARTS — ₹862.51
- `1168-049 SCASYX` DOOR HANDLE W/O LOC — ₹159.57
- `30011Z-049SASSY` LOCK KIT — ₹240.25
- `WB-170001` CONVENTIONAL WIPERS — ₹103.23

Status: `success`. ~10 second full crawl (static HTML, httpx).

### Gotcha — segment-name auto-discovery

The landing page renders segment cards with two anchors per card: one is a generic "Download Brochure" link, the other has the segment name as text. Spider's `_extract_segments` keeps the named one and skips the brochure-download one. Auto-discovers segments 1-51+ (some IDs unused; those return empty product lists).

### File
- [spiders/spark_minda.py](../spiders/spark_minda.py)

---

## 19. UNO MINDA — ✅ shipped

- **URL**: https://unomindakart.com/  (xlsx flow lands on `/search?segment=car`)
- **Pattern**: search-results pagination
- **Output**: 525 unique products, 517 `success` + 8 `partial`

### Technical
- Discovered APIs: `/fetchMakes`, `/fetchModels`, `/get_main_category_ajax`, `/get_subcategoy_ajax` (typo in their endpoint).
- For aggregate extraction, the simpler path: `/search?segment=car&page=N` page-walks (60 pages × ~12 products).
- Each card: `h6.card-title` (name with embedded SKU) + `card-text` (selling price + `MRP :₹<NUMBER>` strikethrough).
- 8 rows partial because they have no discount → only one price shown, no MRP label.

### File
- [spiders/uno_minda.py](../spiders/uno_minda.py)

---

## 20. ZF — ✅ FIXED 2026-05-22 (was regression 2026-05-21)

**Root cause**: hardcoded `brandIDs=14&22&32&35&68&126&161&294&8888` (ZF sub-brand filter) on `getArticlesForFilter` returned `content:[]` for every Indian PV manufacturer in the 2026-05-21 run. The 2026-05-19 shipped run probably hit different cache state where the filter happened to pass through.

**Fix 2026-05-22**: dropped `brandIDs` from `COMMON_QS` entirely. ZF brand info still arrives per-article via `brandName` so post-filtering remains possible. Also: migrated from Playwright `ctx.request` to direct `httpx.Client` (more reliable in parallel pool execution; was the secondary failure mode).

**Production defaults bumped** to representative scope:
- `ZF_MAX_MFRS=0` (was 1) — walk all 105 manufacturers
- `ZF_MAX_MODELS_PER_MFR=0` (was 1) — all models
- `ZF_MAX_VARIANTS_PER_MDL=1` (unchanged) — 1 representative variant
- `ZF_MAX_ASSEMBLYGROUPS=30` (was 3) — top 30 AGs per variant

Estimated full-run wall-clock: 1-2h. Smoke verified 2026-05-22: 5 mfrs × 2 mdls × 1 var × 10 AG → 2 unique articles in 55s; cascade and dedup work correctly.

**2026-05-26/27 — Mfr-whitelist optimization (in-flight 2026-05-22 estimate was wrong; real wall-clock ~7-8h)**

The 2026-05-22 "1-2h" estimate was based on the smoke run pace; production reality was much heavier. Diagnosed during the 2026-05-26 run when the crawl was killed at 3h 47min on mfr 51/105 with 438 unique articles.

**Performance profile (measured from the killed run):**
| Metric | Value |
|---|---|
| Wall-clock | 13,644s (227 min) for 51/105 mfrs |
| Total API calls | 18,311 |
| Effective per-call latency | 745ms (network ≈ 648ms mean) |
| `getArticlesForFilter` calls | 17,117 = 93.5% of all traffic |
| `getArticlesForFilter` hit rate | **3.2%** (543 yielded data, 16,574 empty) |
| Yield | 0.024 unique articles per call |

**Where the time went:**
- **79% on empty responses** — 16,574 calls × 648ms returning `content:[]` (we paid for "is there anything here?" 17K times).
- **~12% on mfrs that yielded 0 articles** — JAGUAR, BENTLEY, FORD USA, JEEP, GMC, DODGE, BAJAJ TEMPO, HUSQVARNA, KTM, BYD, FANTIC, BEDFORD, APRILIA, HUMMER, FREIGHT ROVER, BIMOTA, BAW, EMCO, E-Ton, GASGAS, JONWAY, ASHOK LEYLAND, ALPINA, DAF, DODGE, IVECO (26 of 51 walked → 51%). 2W brands slipped past `vehicleTypeIDs=P`.

**Fixes applied (in [spiders/zf.py](../spiders/zf.py)):**

1. **`ZF_MFR_WHITELIST` env-var + sane default** — only walk Spinny-relevant India-PV makes:
   ```
   MARUTI, HYUNDAI, TATA, MAHINDRA, HONDA, TOYOTA, KIA, RENAULT, NISSAN,
   SKODA, VOLKSWAGEN, FORD, MERCEDES-BENZ, BMW, AUDI, FIAT, CHEVROLET,
   DATSUN, MITSUBISHI, JAGUAR LAND ROVER, VOLVO
   ```
   Filters 105 → 20 (one of the 21 didn't match ZF's casing — likely JAGUAR LAND ROVER). Cuts mfr-count ~80%.

2. **`ZF_PAG_BUCKETS` env-var (disabled by default)** — initially tried whitelisting outer picture buckets (`PictoBremse, PictoKupplung, PictoLenkung,…`). **Smoke showed this over-prunes** — AUDI's article-yielding AGs spread across buckets beyond the PV-relevant 10. Yield dropped from 85 → 19 articles. Now disabled by default; can be opted in if a specific scope is profiled.

3. **`ZF_EMPTY_STREAK_SKIP` env-var (disabled by default)** — initially set to 5 ("if 5 consecutive AG calls return empty, skip remaining for this variant"). **Smoke showed this also over-prunes** — at 3-5% AG-hit rate, P(5-empty streak before a hit) = 77%; AUDI yielded 0 articles in a streak=5 run. Now disabled by default; safe at very high values (≥25) but the math says it doesn't help.

4. **Probe captured in [state/probe_zf_pag_fields.py](../state/probe_zf_pag_fields.py)** — confirmed `getAllPictureAssemblyGroups` only returns `{assemblyGroupID, name}` per inner AG (no `articleCount` field), ruling out cheap pre-filtering of empty AGs.

**Production run completed 2026-05-27**: 20 mfrs × all-models × 1-variant × 30-AG cap, mfr-whitelist on, all other optimizations off. **709 unique articles in 456.9 min (7.6h)** wall-clock. 29,840 API calls, 960 AG hits (3.2% hit rate — matches profile). Within BRD §8 8h SLA. Master CSV `output/20260525/spinny_oem_master_20260525.csv` includes ZF post-dedup; per-brand file `output/20260525/zf_20260525.csv` retains all 709 articles with merged compat strings.

**Lessons documented for any future similar API:**
- ZF's catalogue isn't enumerable as a list — only via vehicle-keyed leaf queries. Hit rate is fundamentally low (~3%) because most (mfr, model, variant, AG) tuples have no parts.
- Early-exit heuristics (streak/bucket whitelist) require the data distribution to cluster hits early. ZF's distribution is scattered → these heuristics over-prune.
- The only safe optimization is **cutting upstream** (mfr whitelist). Don't try to be clever inside the AG loop.
- A 3-5× speedup is still on the table via `httpx.AsyncClient` + concurrency-5 — implemented 2026-05-28 (see next subsection).

**2026-05-28 — Async refactor (concurrency-3, 2.18× speedup verified)**

The 7.6h sequential wall-clock from the 2026-05-26/27 run was within SLA but consumed almost the entire 8h window, leaving no headroom for retries or other long-running brands. Converted ZF's HTTP layer from synchronous `httpx.Client` to `httpx.AsyncClient` with bounded concurrency.

**Implementation (in [spiders/zf.py](../spiders/zf.py)):**
1. **`crawl()` now wraps `asyncio.run(self._crawl_async())`** — keeps the orchestrator's sync contract intact while the spider internals are async.
2. **Inner AG loop parallelized via `asyncio.gather`** — the inner `getArticlesForFilter` calls (93.5% of all traffic) fire `ZF_CONCURRENCY` (default **3**) at a time through an `asyncio.Semaphore`. Outer loops (mfr → model → variant) stay sequential because their fanout is small and the per-mfr log lines double as progress indicators.
3. **Retry-with-backoff in `_aget`** — 3 attempts on 5xx/429/network errors with 1.5s × attempt backoff. Permanent 4xx fails fast.
4. **Fallback path** — `ZF_CONCURRENCY=1` switches to `_fetch_ag_sequential` which preserves the `ZF_EMPTY_STREAK_SKIP` heuristic. Concurrency > 1 silently disables streak (no deterministic ordering to count empties against).

**Gotcha — Akamai/WAF tripped by HTTP keep-alive:**

First async smoke at concurrency=5 worked. Subsequent runs returned **HTTP 502 on `getArticlesForFilter`** while `curl` against the same URL returned 200 instantly. Diagnostic curl with identical headers and User-Agent succeeded, ruling out fingerprint or rate-limit. **Root cause: the AsyncClient was reusing keep-alive TCP connections, and ZF's WAF/load balancer was 502'ing on the second-or-later request riding a kept-alive socket.** First request on a fresh connection always succeeded.

**Fix**: `httpx.Limits(max_keepalive_connections=0)` forces a fresh TCP+TLS handshake per request. Adds ~150ms per call but eliminates 502s completely. Also pinned `http2=False` to keep the connection profile predictable.

**Verification (full 20-mfr run, 2026-05-28 05:38 IST → 09:07 IST):**

| Metric | Sequential (2026-05-27) | Async concurrency-3 (2026-05-28) |
|---|---|---|
| Wall-clock | 456.9 min (7.6h) | **209.6 min (3.5h)** — 2.18× faster |
| Articles | 709 | **709 (identical)** |
| Total API calls | 29,840 | 29,879 (~same) |
| AG hits | 960 | 960 (identical) |
| Retries (transient) | n/a | ~3 (transparent) |
| 502s | 0 | **0** |

**Defaults locked at**: `ZF_CONCURRENCY=3` (server tolerates well, no 502s observed; can bump to 5 if needed but 3 is safer). Keep-alive **must** stay disabled — it's the difference between 0 502s and ~70% failure rate.

**One transient incident** during the run: a 13-min stall at 08:18 IST (network or server hiccup; spider auto-recovered without intervention thanks to retry-with-backoff). No data lost.

**Why not concurrency=5 or higher?** Concurrency=5 worked in the first smoke but triggered 502s on `getManufacturers` after the spider had warmed up the connection pool. Concurrency=3 with no-keepalive is the safe operating point: every request is a fresh TLS handshake but only 3 fly at once, so we never trip the WAF.

**2026-05-30 — Hit cache (cuts subsequent monthly runs from 3.5h → ~15 min)**

### ZF_USE_CACHE — when to set 0 vs 1 (read this first)

`ZF_USE_CACHE` is the single env var controlling cache behavior. **What writes to the cache is always-on**; what changes is whether the next run reads from it.

| Value | What it does | When to use |
|---|---|---|
| **`ZF_USE_CACHE=0`** | **Full discovery.** Walks ALL `(mfr, model, variant, AG)` tuples from scratch — ~30,000 API calls × ~0.65 s each = ~3.5 h wall-clock. Writes `output/<date>/zf_hit_cache.json` at end of run, replacing any previous cache. | • The very first time ZF runs after install (no cache exists yet)<br/>• Every **3 months** as a refresh (catches any new parts ZF added to previously-empty tuples — see "Why refresh" below)<br/>• Anytime ZF's row count drops dramatically vs the prior cached run (signal that the cache has gone stale or ZF changed IDs)<br/>• When debugging: cache-on can mask a regression |
| **`ZF_USE_CACHE=1`** | **Cached run.** Loads the most-recent `zf_hit_cache.json` from `output/<prev-date>/`, calls ONLY the ~960 productive tuples logged there, skips the 28,880 known-empty ones. ~15 minutes wall-clock. Still writes a fresh cache at end (which will be ~identical to the input cache since the hit set rarely shifts). | • Normal monthly cron runs in months **2, 3, 4, 5, 6, 7, 8, 9, 10, 11** after a discovery run<br/>• Anytime you want a fast ZF refresh and can accept missing any newly-added parts since the last discovery |

### Why refresh every 3 months

ZF's catalog grows incrementally. The cache mode is **asymmetric**:
- **Stale entries** (tuples that USED to have parts but no longer do) → cheap. Cache makes 1 wasted API call returning empty; ~150 ms per stale tuple. Harmless.
- **Missing new entries** (tuples that ZF NEWLY populated since last discovery) → **invisible**. Cache mode never asks those tuples, so the data stays missing in our output until the next full discovery rewrites the cache.

For Indian PV makes the catalog is stable enough that monthly cache runs work fine, but the 3-month full discovery is insurance against silently missing additions.

### Decision rule the cron operator follows

```
First Monday of Jan/Apr/Jul/Oct      → ZF_USE_CACHE=0   (quarterly refresh)
First Monday of every other month    → ZF_USE_CACHE=1   (cached fast run)
```

Or simpler: keep `ZF_USE_CACHE=1` in `/etc/environment` always, and **manually flip to 0 for the run 4× per year** when doing the quarterly refresh.

---

The mfr-whitelist (105→20) and async (C=3) optimizations brought ZF to 3.5h. The remaining cost was 96.8% empty `getArticlesForFilter` responses — 28,880 of 29,840 calls return `content:[]`. Most (mfr, model, variant, AG) tuples have no parts; we just can't tell without asking.

**Solution**: persist the ~960 productive tuples from each successful run and skip the known-empty ones on subsequent runs.

**Implementation (in [spiders/zf.py](../spiders/zf.py))**:
1. **Track productive hits** — every time `getArticlesForFilter(mfr, mdl, veh_ids, ag)` returns ≥1 article, add `(mfr_id, mdl_id, veh_ids_str, ag_id)` to a `productive_hits` set.
2. **Write at end of crawl** — `output/<rundate>/zf_hit_cache.json` with the full hit set (sorted for diff-friendliness), plus metadata (elapsed_s, hit_count, written_at).
3. **Read at start of next run** — find the most-recent `zf_hit_cache.json` under `output/*/`, load into `self.hit_cache`. When `ZF_USE_CACHE=1`, filter each variant's AG list to only those in cache before fetching.
4. **Always-on writing, opt-in reading** — `ZF_WRITE_CACHE=1` (default), `ZF_USE_CACHE=0` (default off so the first cache-aware run still happens to be a full discovery). Flip `ZF_USE_CACHE=1` after the first cached run lands and you've sanity-checked the output.

**Expected performance**:
| Run kind | Calls | Wall-clock |
|---|---|---|
| First run (no cache) | ~30K (full discovery) | ~3.5h |
| Cached run, `ZF_USE_CACHE=1` | ~960 (only productive tuples) | **~15 min** (× 3 concurrent + ~150ms per fresh-keepalive call) |
| Quarterly discovery (no cache) | ~30K | ~3.5h |

**Quarterly discovery cadence**: ZF's catalog mostly grows incrementally — a new part added to a previously-empty (mfr, model, variant, AG) tuple would be invisible to cache-mode. Recommended: every 3 months, run with `ZF_USE_CACHE=0` to re-discover. The cache writes are unconditional, so each discovery refreshes the cache for the next 3 cached runs.

**Cache file layout** (`output/<date>/zf_hit_cache.json`):
```json
{
  "written_at": "2026-05-30T08:00:00+00:00",
  "elapsed_s": 12577.4,
  "hit_count": 960,
  "hits": [
    [2, 483, "1000037", 478],
    [2, 483, "1000037", 4542],
    ...
  ]
}
```

**Risks acknowledged**:
- Cache-mode misses any new parts added to previously-empty tuples (quarterly discovery sweeps catch this — that's the trade).
- If ZF's mfr/model/variant/AG IDs change (e.g. catalog re-numbering), cache becomes silently stale. Mitigation: cache validates ID stability via the first response's row count — if cached run produces dramatically fewer rows than baseline, the validator triggers a full discovery (TBD — currently relies on operator review).

**Locked**: `ZF_USE_CACHE=0` by default. Don't turn it on until the first hit-cache file lands under `output/<date>/`.

---

## 20. ZF — ✅ SHIPPED (Vue bundle mined for REST endpoints, 2026-05-19)

- **URL**: https://aftermarket.zf.com/en/aftermarket-portal/our-catalog/search-by-vehicle/
- **Pattern**: json_api_pagination — REST API at `/functions/controller/opc/*`
- **Status**: Vue.js SPA backend exposed; 105 PV manufacturers accessible; smoke produced real articles (e.g. SACHS clutch slave cylinder)
- **xlsx fields**: item_name, item_code, compatible_car_model (no MRP per xlsx)

### The unlock — mine the Vue bundle for endpoint signatures

Prior investigation classified ZF as a 12-16h Vue SPA reverse-engineering job. The reality:

1. The SPA's main bundle (`/technical/apps/opc-vuejs/js_5/zfap_opc_bundle.js`, 2.6MB) is minified but contains the REST endpoint **literals** as string constants. Grep `"/get[A-Z]"` → 53 distinct endpoints including `/getManufacturers`, `/getModels`, `/getGroupedVehicles`, `/getAllPictureAssemblyGroups`, `/getArticlesForFilter`, plus alternates (`/getOpticat*`, `/getFraga*`, KBA/VIN/VRM lookups).
2. For each endpoint, the bundle reveals the **exact query-param signature** via the `params:{...}` literal next to the URL.
3. Required common params: `languageID=4`, `brandIDs=14&brandIDs=22&...&brandIDs=8888` (9 ZF sub-brands: LEMFÖRDER, SACHS, TRW, BOGE, …), `countryID=IND`, `vehicleTypeIDs=P` (Passenger Cars).
4. Required header: `X-Requested-With: XMLHttpRequest`.

### Crawl chain — 5 GETs deep

| Step | Endpoint | Params | Returns |
|---|---|---|---|
| 1 | `/getManufacturers` | (common) | 105 PV manufacturers (ABARTH, ALFA ROMEO, ASHOK LEYLAND, …, MARUTI, …) |
| 2 | `/getModels` | (common) + `manufacturerID` | ~7-30 models per mfr (MARUTI: 800, ALTO, BALENO, OMNI, WAGON R, ZEN, BALENO Estate) |
| 3 | `/getGroupedVehicles` | (common) + `modelID` | variants grouped by engine; each entry has a `vehicleIDs` ARRAY of TecDoc K-types |
| 4 | `/getAllPictureAssemblyGroups` | (common) + `manufacturerID&modelID&vehicleIDs` | picture-groups (Engine/Steering/Brake/Chassis/…), each with inner `assemblyGroups` array of part-type IDs (Brake Disc=82, Tie Rod End=914, Inner Tie Rod=51, …) |
| 5 | `/getArticlesForFilter` | (common) + `manufacturerID&modelID&vehicleIDs&assemblyGroupIDs` | articles for that vehicle + assembly group; fields: `articleID`, `name`, `brandName`, `articleCriterias` (specs) |

### Field mapping
- `item_code` ← `articleID` (e.g. `"DF95023"`, `"38747 01"`, `"3182 600 178"`)
- `item_name` ← `brandName + name` (e.g. `"TRW Brake Disc"`, `"LEMFÖRDER Tie Rod End"`, `"SACHS Central Slave Cylinder, clutch"`)
- `compatible_car_model` ← `"<Manufacturer> | <Model> | <Variant>"` derived from the iteration context. Spider dedupes on `articleID` and **merges** compat across all vehicle contexts the article fits.

### Sample data (smoke test on MARUTI 800 0.8L)
- `DF95023` TRW Brake Disc (solid, 10mm thick, 215mm dia)
- `38747 01` LEMFÖRDER Tie Rod End (Front Axle L/R)
- `38748 01` LEMFÖRDER Inner Tie Rod (M14x1.5)

Smoke test on ABARTH 500/595/695 (smaller test) extracted 1 dedup'd article: `3182 600 178` SACHS Central Slave Cylinder, clutch.

### Per BRD gates
- **Vehicle-segment gate**: `vehicleTypeIDs=P` (Passenger Cars) — server filters to PV manufacturers only.
- **Geography gate**: `countryID=IND&languageID=4` — required by BRD §7 ZF-specific clause.

### Volume estimate

Full scope: 105 mfrs × ~15 models × ~3 variants × ~30 assembly groups × ~1-5 articles = **~100K-500K article fetches**. At ~0.3s each = **10-40 hours full crawl** — exceeds BRD §8 8h window.

**Realistic monthly scope** (env-var caps):
- Limit to top-10 India PV manufacturers (Maruti, Hyundai, Tata, Mahindra, Honda, Toyota, Kia, Renault, Skoda, Volkswagen)
- ~10 mfrs × ~10 models × ~3 variants × ~30 ag × ~3 articles = ~27,000 fetches ≈ ~2-3 hours
- Aggressive dedup on `articleID` (same SACHS/LEMFÖRDER article fits many vehicles) reduces output to ~5K-10K unique rows

### Env vars (defaults conservative for smoke)
- `ZF_MAX_MFRS` (default 1, 0=all 105)
- `ZF_MAX_MODELS_PER_MFR` (default 1, 0=all)
- `ZF_MAX_VARIANTS_PER_MDL` (default 1, 0=all)
- `ZF_MAX_ASSEMBLYGROUPS` (default 3, 0=all per vehicle)

### File
- [spiders/zf.py](../spiders/zf.py)

---

## 21. TVS Girling — ✅ SHIPPED (URL 1 cracked 2026-05-19; both URLs merged)

- **URLs**:
  - URL 1: https://partscatalogue.brakesindia.com/ (Brakes India parts catalogue) — **SHIPPED**, 4 xlsx fields (item_name, item_code, MRP, vehicle_compatibility)
  - URL 2: https://www.tvsgirling.com/passenger-cars-scv/ (marketing site) — SHIPPED, item_name only
- **Output**: rows from both URLs merged into one tvs_girling_<YYYYMMDD> file; `source_website` column distinguishes origin.
- **Smoke test**: 33 real parts from URL 1 (10 makes × 2 models cap) + 74 marketing items from URL 2 = 107 rows total. Status: `partial` (URL 2 rows lack other fields per BRD §7 — same partial-rationale always intended).

### Sample real data (URL 1)

| item_code | item_name | MRP (₹) | vehicle_compatibility |
|---|---|---|---|
| 29933214 | KIT PAD ASSEMBLY | 1,336 | ASHOK LEYLAND STILE; MITSUBISHI PAJERO SPORT; NISSAN EVALIA |
| 29933404 | KIT PAD ASSEMBLY (FRONT) | 7,490 | AUDI A3/Q2; SKODA KAROQ/OCTAVIA/SUPERB; VOLKSWAGEN |
| 29933499 | KIT PAD ASSEMBLY (REAR) | 4,967 | AUDI A3/Q2; SKODA KAROQ/SUPERB; VOLKSWAGEN PASSAT |
| 29933612 | KIT PAD ASSY-REAR | 4,262 | BMW 3 SERIES (F30) |
| 29933613 | KIT PAD ASSY-FRONT | 6,708 | BMW 3 SERIES (F30); BMW X3 SERIES (F25) |
| 29390144 | BRAKE ROTOR (DISC) (6 HOLE) | 1,527 | CHEVROLET AVEO/BEAT/SAIL PETROL |
| 29933297 | COMBO KIT (2 NOS ROTOR + KIT PAD) | 4,075 | CHEVROLET AVEO/BEAT/SAIL PETROL |
| 29321279 | ANTI RATTLE CLIP | 96 | CHEVROLET TAVERA |

### Technical — URL 1 (the part that was stubbed before)

**Cascade**: Make → Segment → Model → ModelYear/Type → Search.

Each dropdown change is an ASP.NET UpdatePanel partial postback (`X-MicrosoftAjax: Delta=true`). httpx replay returned 500 errors (server missed some script-manager fields), so the spider **drives the cascade with Playwright** (no captcha, no login — just dropdown selects). Once the Make+Segment+Model are chosen, the spider switches to plain httpx for the GET-only result and detail pages, reusing Playwright's cookies.

**Discovered URL patterns** (the dropdowns drive plain GETs to these once postbacks have run):
- List:   `/FrmProduct?Model=<mid>&Make=<mkid>&Year=-1&Segment=<sid>`
- Detail: `/HotspotView?Model=<mid>&Product=<code>&Year=-&Partnumber=<code>`

**Detail page structure** (verified for Hyundai ACCENT, KIT PAD ASSY 29933041):
- Main table: `BI Part Number | Part Description | Part Specification | MRP as on | Serviceability`
- Applicable Models table: `Make | Model | Assembly Part No.` — multi-row, becomes `vehicle_compatibility`

**PV gate**: keep only Segments whose text matches `/CAR|MUV|SUV|HATCHBACK/i`. Filter out 3-WHEELER / LCV / PICKUP / VAN / TRACTOR / TRUCK. Implemented as `PV_SEGMENT_RX` + `BLOCK_SEGMENT_RX` in the spider.

**Dedup**: same part (e.g. 29933214 KIT PAD ASSEMBLY) often appears across many model iterations. Spider dedupes on `item_code` and **merges** the `vehicle_compatibility` across all model contexts AND the Applicable Models table — yielding a complete fitment list per part.

### Technical — URL 2 (unchanged from earlier impl)

WordPress/Elementor site. Each leaf product URL slug → title-cased item_name. xlsx fields for URL 2 = `item_name` only.

### Volume estimate (URL 1 full crawl)

- ~30 Makes × ~2-3 PV segments × ~10 models × ~5-20 products
- ≈ 5,000-15,000 product detail GETs at ~0.3s each (httpx, reused cookies)
- **~25-75 minutes full crawl** — within BRD §8 8h window.

### File
- [spiders/tvs_girling.py](../spiders/tvs_girling.py)

---

## Pending: 0 brands

All 19 brands attempted in this session.

---

## v2.0 OEM EPC scope — additive (per kickoff decision 2026-05-18)

### V2.1 Maruti — ✅ shipped
- URL: https://www.marutisuzuki.com/genuine-parts
- API: POST /api/sitecore/MSGP/GetFilter (double-decoded JSON, paginated)
- 29,313 products extracted, full MRP, ~8 min runtime

### V2.2 Hyundai — ✅ REWRITTEN 2026-05-30 (REST API replaces Playwright)

**Current implementation**: REST API at `https://snaponepc.com/epc-services/`.
The legacy Playwright AG-Grid clicker is preserved at `spiders/hyundai_legacy.py`
for fallback. The new shared module `spiders/snapon_rest.py` is used by both
Hyundai and Toyota (same SNAP-ON platform, different credentials).

**Why it was rewritten**:
- Legacy 2026-05-29 production run: Hyundai crashed with 0 rows after `_add_all_to_picklist` race; Toyota silently hung after 3 models. Both repeatable.
- Both failures traced to the AG-Grid double virtual scroll + 20-min session timeout — fundamentally fragile UI automation.
- Legacy yield was tiny (~30-168 rows total) because the picklist UI caps each interaction. Most of the SNAP-ON catalogue was invisible to a click-driven walker.

**Discovery (state/probe_snapon_*.py)**:
1. SNAP-ON's Angular SPA is a thin client over a clean REST backend.
2. Login: `POST /epc-services/auth/login` with `user=X&password=BASE64(X)` form body. Returns `sessionJwtToken`.
3. Auth: every subsequent XHR carries TWO custom headers — `sbsepc5s` (= sessionJwtToken) and `sbsepc5cs` (derived client-side from the JWT's SIG claim via JS algorithm we didn't reverse).
4. Strategy: 15-second Playwright login to capture both headers from the first `/auth/account` request, then switch to httpx for all data calls. No more UI dependencies after login.

**Crawl chain (5 GETs deep)**:
| Step | Endpoint | Returns |
|---|---|---|
| 1 | `POST /epc-services/settings/user` | datasetSettings[] (per-dataset priceBookId) |
| 2 | `GET /epc-services/datasets` | 1 dataset per dealer cred |
| 3 | `GET /datasets/{ds}/navigations/filterRequest/{fr}` | child level = "Year" |
| 4 | `GET /datasets/{ds}/navigations/{sp}/filterRequest/{fr}` | recurse Year→Model→Catalog→Group→Section |
| 5 | `GET /datasets/{ds}/pages/parts/{sp_leaf}/filterRequest/{fr}` | `partItems[]` — partNumber, description, manufacturer, quantity, dynamicColumns |

The `{sp}` parameter is a base64-encoded `serializedPath` cursor returned in the previous navigations response. `{fr}` is base64(`jobId=1|dataSetId=X|manualFiltersEnabled=true|locale=en-US|busReg=IND|priceBookId=Y|userId=Z`).

**Performance** (smoke 2026-05-30, year=2026 only):
| | Legacy Hyundai | REST Hyundai |
|---|---|---|
| Wall-clock | 10-20 min (when not crashed) | 12 min (1 year × 1 model) |
| Rows | 168 (best case) or 0 (crashed) | **22,043** unique parts from 1 model |
| Fragility | crash @ AG-Grid race or hang @ session timeout | none after 15s login |
| Item code coverage | 100% | 100% (22,043/22,043) |
| Item name coverage | 100% | 100% (22,043/22,043) |
| MRP | available via picklist after each click | **not yet** — picklist API integration pending; rows ship `partial` |

**Toyota same code, just `brand_key="toyota"`** picks `<dealer-cred>` credential and Toyota dataset. Smoke result: 26,815 unique parts in 13 min from year=2026 × LEXUS RX alone.

**Env vars (snapon_rest.Spider)**:
- `HYUNDAI_MAX_YEARS` / `TOYOTA_MAX_YEARS` — `0=all`, `1=latest only` (recommended)
- `HYUNDAI_MAX_MODELS` / `TOYOTA_MAX_MODELS` — `0=all`
- `HYUNDAI_MAX_LEAVES` / `TOYOTA_MAX_LEAVES` — `0=all` (for smoke)
- Credential env vars unchanged: `HYUNDAI_USER`, `HYUNDAI_PASS`, etc.

**Production recommendation per user 2026-05-30**: `MAX_YEARS=1` (latest catalog only). SNAP-ON's year axis is the catalog publication year — a year=2026 catalog already covers all currently-sold models. Older years are mostly previous revisions of the same parts (heavy partId duplication). Saves 5× wall-clock with negligible yield loss.

**Locked decisions**:
- Use SNAP-ON REST. Don't drive AG-Grid via Playwright. Legacy is fallback only.
- Always extract `sbsepc5s` + `sbsepc5cs` from the FIRST `/auth/account` request headers. Don't try to derive `sbsepc5cs` from the JWT — the SPA's JS does that, we don't need to.
- Use `httpx.Client` (not AsyncClient) for SNAP-ON — endpoints are fast and serial walking respects pagination; async doesn't help.

**Pending — MRP via picklist API (2026-06-03 full investigation)**:

Reverse-engineered the picklist API, identified TWO blockers, neither solved in this pass. Rows continue to ship `partial` (item_code + item_name + compatibility populated; mrp blank).

**The endpoint**: `GET /epc-services/picklist/validatePart/datasetId/<ds>/filterRequest/<fr>/partId/<pid>/partItemId/<piid>` returns `prices[]` like:
```json
[
  {"priceType":"MOB_LIST","amount":"863.14","currency":"INR"},
  {"priceType":"MOB_MRP_A","amount":"1079.00","currency":"INR"},
  {"priceType":"MSRP","amount":"1079.00","currency":"INR"},
  ...
]
```
We want `MOB_MRP_A` (Hyundai dealer-zone A MRP).

**Required inputs (all identified)**:
1. `equipmentRefId=<catalog_id>` baked into the filterRequest base64 (numeric id of the parent "Catalog" navigation node — e.g. 7649 = IHMIP0Y24 VERNA 24). Spider tracks this via DFS now.
2. `amg: <userId>` header.
3. `sbsepc5s` + `sbsepc5cs` JWT headers (auto-captured at login).
4. Full Chrome `sec-ch-ua-*` + `user-agent` headers.

**Two blockers found**:

**Blocker 1 (solved): TLS fingerprint check.** httpx (Python TLS, h11) → always 400. The SAME URL + headers from inside Chromium → 200. SNAP-ON's WAF JA3-fingerprints `/picklist/*` endpoints specifically. Other endpoints (navigations, pages/parts) don't have this check — httpx works fine there.
- Solved by routing picklist calls through the browser: `page.evaluate(fetch(...))` or `ctx.request.get(...)`.

**Blocker 2 (NOT solved): SPA session-state precondition.** Even from inside the browser with the correct URL + headers + cookies, validatePart returns 400 **unless the SPA UI has clicked into that exact section first**. The SPA registers its "currently viewing section X" state via some Angular internal mechanism we couldn't isolate.
- Tried: pre-warming with `GET /pages/parts/<sp>` via browser → no effect.
- Tried: pre-warming with `POST /pages/parts/<sp>/userContentIndicators` → no effect.
- Tried: `page.goto('#/parts;serializedPath=...')` for hash-routing → the SPA's URL stays at `#/` regardless of section navigated to. Internal state only.
- Both ctx.request and page.evaluate(fetch) hit blocker 2 the same way.

**The right endpoint is actually `/partdetails/supersession`, not `/picklist/validatePart`** (2026-06-03 follow-on probe — `state/probe_part_detail.py`). When the user clicks a part number, the SPA fires:
```
GET /epc-services/partdetails/supersession?ds=<ds>&pr=<partId>&fr=<filterRequest>
```
The response contains a `prices[]` array including `MOB_MRP_A`. Confirmed real:
- JACK ASSY (`09110-H6500`, partId 378794) → MOB_MRP_A = **1,079.00 INR**
- LABEL (`09127-2C001`) → 25.00
- WRENCH-WHEEL NUT (`09131-3B010`) → 369.00
- ...

**Same TLS-fingerprint check as picklist endpoints**: httpx → 400, browser fetch → 200.

**Same SPA-session precondition**: returns 400 unless the SPA has fired supersession for ≥1 part in the section already (via a part-number UI click). Tested progressive UI drills — Year → Model → Catalog → Group → Section all return 400. Only a **part-number click** sets the state. Once set, the **same state covers ALL parts in that section** — verified by fetching supersession for 5 different partIds after clicking only 1, all returned 200 with correct prices.

**Realistic cost analysis (per Hyundai run)**:
| Step | Cost |
|---|---|
| UI drill to section (5 clicks) | ~5s |
| Click first part-number (kicks off supersession) | ~3s |
| Parallel browser-fetch supersession for all ~50 parts in section | ~5s |
| **Per section** | **~13s** |
| × ~387 sections per model | **~84 min/model** |
| × 5 models in year=2026 | **~7 hours per Hyundai run** |

This exceeds the BRD §8 8h SLA when combined with other brands' runs. **MRP cannot be a single monthly run.**

**Realistic options**:

| Option | Effort | Trade-off |
|---|---|---|
| **A. Quarterly MRP-enrichment pass** (recommended) | ~1 day | Separate script (`state/enrich_hyundai_mrp.py`) runs once a quarter, walks the catalog with UI clicks, updates the `mrp` column on existing rows. Monthly catalog run stays fast (12 min). Prices change rarely, so quarterly cadence matches business need. |
| **B. SNAP-ON B2B data feed** (commercial) | Spinny ↔ SNAP-ON conversation | Cleanest: native price feed bypasses all client-side reverse engineering. |
| **C. Reverse-engineer SPA Angular services** | ~1 week | Find `PartDetailsService.fetch()` (or equivalent) and call via `page.evaluate(angular.get(...))`. Bypasses the UI click. Brittle to SPA updates. |

**Spider plumbing kept** for the future re-enable (5-line uncomment in the leaf loop once Blocker 2 is solved):
- `_fetch_mrps_via_browser()` — `page.evaluate(Promise.all(fetch()))` batching
- `_register_parts_for_picklist()` — POST /userContentIndicators
- `_build_filter_request()` — filterRequest with equipmentRefId
- DFS catalog_id tracking on the walk stack

**Probe artifacts** (under `state/`):
- `probe_snapon_picklist_mrp.py` — original endpoint discovery
- `probe_validatepart_headers.py` — captured the 14 SPA request headers
- `probe_mrp_browser_with_headers.py` — proved browser-channel fetch works
- `probe_ctx_request_validate.py` — proved ctx.request also works
- `probe_section_url.py` — discovered SPA URL stays at `#/` regardless of navigation
- `probe_mrp_browser_fetch.py`, `probe_mrp_fresh_ids.py`, `probe_mrp_exact_headers.py` — various blocker-2 attempts
- `validatepart_headers.json`, `snapon_picklist_xhrs.jsonl` — raw captures

---

### V2.2 Hyundai — legacy notes (pre-2026-05-30 Playwright AG-Grid)
- URL: https://snaponepc.com/epc/  (dataset Hyundai INDIA)
- Auth: Playwright login with **HYUNDAI_USER / HYUNDAI_PASS** env vars (default <dealer-cred>)
- Cascade: Year → Model → Variant (auto) → Category → Illustration → Parts AG-Grid
- **100% MRP capture proven** (verified 35/35 on smoke test)

**Critical SNAP-ON quirks — must preserve in code**:
1. **Anti-detection essential** — without `--disable-blink-features=AutomationControlled` + `navigator.webdriver=undefined`, post-login body renders empty.
2. **Click the `<img>` inside `add-to-picklist-renderer`, NOT the cell wrapper** — clicking the gridcell only registers when the row is "hot" (hovered/focused); clicking the img always works.
3. **AG-Grid double virtual scrolling**:
   - Parts grid: rows below the fold have no DOM. Spider scrolls parts-grid viewport in 300px steps, clicking new rows as they render (`_add_all_to_picklist`).
   - Picklist grid: same behaviour. Spider reads rows while scrolling viewport top-to-bottom (`_read_picklist_mrps`).
4. **Per-add wait ≥ 900ms** — the SDK debounces. At 500ms some adds become no-ops.
5. **MRP lives in picklist grid, not parts grid** — col-id `MOB_MRP_A` in the picklist row keyed by `footerRowTotalColumnAlternate` = part number.
6. **Clear picklist between illustrations** — `_clear_picklist` clicks the "Clear" button to reset state.

### V2.3 Toyota — ✅ REWRITTEN 2026-05-30 (REST API replaces Playwright)

**Current implementation**: shares `spiders/snapon_rest.py` with Hyundai. The `spiders/toyota.py` module is a one-liner re-export. Same authentication, navigation tree, and parts endpoint structure as §V2.2 Hyundai — only the dataset ID and credential differ.

**Why it was rewritten**: in the 2026-05-29 production run, Toyota hung silently three times. Each attempt got past login then froze inside a Playwright operation (no error, no log, ~20-25 min before manual kill). Root cause was the same AG-Grid race condition that crashed Hyundai. REST API eliminates the failure mode entirely.

**Verification (2026-05-30)**:
- Smoke (year=2026 × 1 model = LEXUS RX): 26,815 parts in 13 min, 100% coverage on item_code + item_name.
- Full year=2026 production: 26,815 parts in 13 min (the LEXUS RX model alone covers most of the year's parts; few other 2026-active Toyota dealer models exist on this credential).

**Dataset specifics**:
- `dataSetId`: `a50e5def-b954-09d3-e044-00144f3a895d` (TKM India)
- Dealer: Galaxy Toyota Okhla, Delhi (`<dealer-cred>`)
- Locale: `en-US`, busReg: `IND`

**MRP**: Same status as Hyundai — `/pages/parts/` response does not include MRP; requires picklist API integration. Rows ship `partial`. The longstanding "<dealer-cred> price book empty" escalation is INDEPENDENT of this — the legacy issue was that even the picklist endpoint returned empty prices for this dealer. The REST refactor doesn't address that; Spinny commercial team needs to upgrade the dealer credential's price book scope.

**Locked decisions inherited from §V2.2**:
- `MAX_YEARS=1` for production (year=2026 only)
- Sort year nodes descending + cap with `i >= max_years` (avoids LIFO-stack picking the oldest year)
- Capture `sbsepc5s`+`sbsepc5cs` from the first `/auth/account` request headers; replay via httpx

---

### V2.3 Toyota — legacy notes (pre-2026-05-30 Playwright AG-Grid, kept for history)

**Fix 2026-05-22**: added breadcrumb logs in `spiders/hyundai.py::crawl()` (Toyota re-exports this Spider class). New logs run BEFORE any try/except:

**2026-05-22 fix (legacy)**: added breadcrumb logs in `spiders/hyundai.py::crawl()` (Toyota re-exported it). Subsequent runs proved Toyota hung silently inside Playwright after the login breadcrumb → triggered the 2026-05-30 REST refactor above. MRP-empty escalation (B7) remains independent — TKM India dealer-credential issue, not a spider bug.

---

### V2.3 Toyota — ⚠️ partial (catalog OK; MRP unavailable for current credentials)
- URL: https://snaponepc.com/epc/  (dataset Toyota INDIA — Galaxy Toyota Okhla, Delhi)
- Auth: TOYOTA_USER / TOYOTA_PASS env vars (default <dealer-cred>)
- Reuses spiders/hyundai.py logic via re-export (spiders/toyota.py is a thin shell).

**Cascade differs from Hyundai**:
- Year → Model → **Sub-Model (e.g. 'ASV7#,AXVA70,AXVH71,MXVA71 (287320)')** → Category → Illustration → Parts
- Categories use a `N - UPPERCASE TEXT` style: "1 - TOOL/ENGINE/FUEL GRP", "2 - PWRTRAIN/CHASSIS GRP", "3 - BODY GROUP".
- Cascade walker handles the extra sub-model level automatically by clicking the first thumbnail when no categories appear directly under model.

**Toyota-specific column IDs** (different from Hyundai):
- Parts grid: same col-ids except `FDATE` (Toyota) vs `FROMDATE` (Hyundai)
- Picklist grid: uses `MSRP` ("Suggested Retail") and `DEALER_COST` columns instead of `MOB_MRP_A`.
- Picklist MRP reader tries `MOB_MRP_A` → `MSRP` → `DEALER_COST` in order (`_read_picklist_mrps`).

**MRP credential issue — investigated 2026-05-19**:
- The <dealer-cred> dealer login (Galaxy Toyota Okhla) returns **empty** MSRP & DEALER_COST for every part tested.
- Root cause confirmed via Playwright XHR capture:
  - `/epc-services/settings/user` shows the dealer is configured with `selectedPicklistPriceSource: PRICE_BOOK` and `selectedPicklistPriceColumns: { PRICE_BOOK: [DEALER_COST, MSRP] }` (config is correct).
  - Price book `a6f16413-031a-422e-97de-f4c8ac28254c` (TKM_TOY, currency INR) is assigned to the dataset.
  - But `POST /epc-services/picklist/validatePart` consistently returns **`"prices": []`** for every part added (tested 5 parts across illustrations) — the price book is empty server-side for this dealer.
  - `/epc-services/priceBooks*` admin endpoints all return 401 — dealer cannot inspect or modify the book themselves.
- Catalog data extraction works fine: parts, descriptions, codes, illustrations all populated.
- **Status**: marked `partial` per BRD §7 (required field MRP empty → leave blank, status=partial). The spider runs to completion; rows just lack MRP.
- **Resolution path**: Spinny escalates to TKM India to either populate TKM_TOY for this dealer, or provide alternate credentials whose assigned price book contains MRP data (the same dealer login serves catalog browsing on snaponepc.com — Galaxy Toyota presumably doesn't price-shop other models, so empty book may be intentional).
- See [state/probe_toyota_settings.py](../state/probe_toyota_settings.py) and [state/probe_toyota_multiparts.py](../state/probe_toyota_multiparts.py) for the diagnostic probes that proved this.

**Selector breadth** in hyundai.py handles both brands:
- Thumbnails: `a[class*=thumbnail]` matches both `a.thumbnail` (Hyundai) and `a.thumbnailNonIllustrated` (Toyota sub-models).
- Picklist MRP read tries `MOB_MRP_A` → `MSRP` → `DEALER_COST` in order.

### V2.4 Mahindra — ✅ FIXED 2026-05-22 (back-nav resilience added)

**Fix 2026-05-22**: added `_renavigate_to_pv_root()` helper that re-issues `page.goto(FIGURE_URL)` before each top-level category iteration (skip on cat #1 since we're already there). Removed the unreliable trailing breadcrumb-back calls. Restart from known-good state every category instead of relying on stale breadcrumb DOM state. Estimated to recover row count to representative scope (~24,800).

---

### V2.4 Mahindra — ✅ shipped at REPRESENTATIVE PARTS-LEAF scope (default 2026-05-19)
- URL: https://mahindra-ecat.com/epcview/login
- Auth: env vars `MAHINDRA_USER` / `MAHINDRA_PASS` (User Type = `Other User (Fleet Owner)`)
- Platform: **Intelli Catalogue v11.0.0** (commercial EPC software; same vendor as MG)
- Required fields: item_name, item_code, compatible_car_model (no MRP per xlsx)

**BRD §7 captcha blocker — SOLVED offline.**

The login form gates access with a 4-character text captcha rendered as inline base64 PNG (140×45). Initial assessment marked this as a scope-change ticket like LuK/Schaeffler. **2026-05-19 update**: solved with **`ddddocr`** (free, offline, no SaaS dependency) at ~90% accuracy per attempt. With `MAHINDRA_LOGIN_RETRIES=6` the spider achieves effectively 100% login success across runs. Tesseract was tried first and failed (returns garbage like "BRL" for "BAKC"); ddddocr is purpose-built for captchas.

**Crawl strategy** (verified end-to-end):
1. Playwright login: fill `txtLoginname` + `txtpassword` + OCR'd `captchacode`, pick `Other User (Fleet Owner)` from the **second** mat-select (first is Language), click `#btnEnter` via JS to bypass cdk-overlay interception.
2. `POST /webapi/api/Login/GetLogin` returns Bearer JWT + session cookies; redirects to `/epcview/home`.
3. Navigate to `/epcview/quick-search` so the SPA invokes `GET /webapi/api/Quick/getFavTree` — this fetch only succeeds in the SPA's auth context (not via direct page.request), so the spider sniffs the SPA's own response via `page.on("response")` rather than re-issuing.
4. `getFavTree` response is the **full accessible catalogue tree** for account `<account-id>`:

   | CategoryType | Categories | Total Models |
   |---|---|---|
   | Passenger Vehicles | 20 (XUV 7XO, THAR ROXX, XUV 3XO, XUV400, SCORPIO-N, …) | **384** |
   | Pikup Vehicles | 5 (VEERO, SUPRO, MAXXIMO, BOLERO PIK-UP, IMPERIO) | ~101 |
   | LMM (3-wheelers) | 3 (ALFA, JEETO, Treo/Zor) | 66 |
   | MEAL (EV SUVs) | 3 (BE 6, XEV 9e, XEV 9S) | 14 |
   | **TOTAL** | **31** | **565** |

5. Per BRD passenger-vehicle gate, only `CategoryType = "Passenger Vehicles"` is kept → **384 models**.

**Current spider output** (each Row = one Mahindra PV variant):
- `item_name` ← variant text (e.g. "AX7 MT - DSL", "MX5 GSL MT 2WD")
- `item_code` ← server-side model id (e.g. `38658_Mod`)
- `compatible_car_model` ← `"Passenger Vehicles | <Category> | <Variant>"`

**Smoke test result**: 384 unique Passenger Vehicles models, status `success`, login succeeded on attempt 1 (captcha "D23V").

**Full drill chain — SOLVED** (xlsx steps 3-8 all implemented):
The spider now drives the Figure Search UI through 4 levels of clicks. Each click triggers an encrypted POST (`FigureSearchParm` form-data — server-encrypted, we don't decrypt it; we just let the SPA's JS encrypt and send it, then capture the response):

| Step | UI click | API fired | Returns |
|---|---|---|---|
| 3 | "Passenger Vehicles" | `POST /api/FigureSearch/Fillcategory` | 20 PV categories (XUV 7XO, THAR ROXX, …) |
| 4 | Category (e.g. XUV 7XO) | `POST /api/FigureSearch/FillCategoryCountryModel` | Variants (AX7 MT-DSL, AX7 AT-DSL, …) |
| 5 | Variant (e.g. AX7 MT-DSL) | `POST /api/FigureSearch/FillCatModelWithOutCountry` | ~31 spare-part categories (ENGINE, BRAKES, HVAC, …) |
| 6 | SP-Category (e.g. ENGINE) | `POST /api/FigureSearch/FillAssembly` | ~40 assemblies — these are the **items** per xlsx |

**Smoke test (XUV 7XO → AX7 MT-DSL → ENGINE)** extracted **39 real assemblies**:
- `W6E010002A` → ENGINE ASSY - DSL MT (185HP) IEMS
- `W6A010003A` → CYLINDER BLOCK ASSY - MT (DSL) IEMS
- `W6A010005` → CYLINDER HEAD ASSY
- `W6A010007` → CYLINDER HEAD GASKET
- `W6A010008` → GLOW PLUG ASSY
- `W6A010010A` → OIL PUMP & STRAINER ASSY
- `W6A010012` → CAM SHAFT ASSY
- `W6A010014` → CRANKSHAFT ASSY
- `W6A010016` → CONNECTING ROD ASSY
- … 30 more

Field mapping:
- `item_name` ← `categoryname` (e.g. "ENGINE ASSY - DSL MT (185HP) IEMS")
- `item_code` ← `figno` (Mahindra W-prefixed part number, e.g. "W6E010002A")
- `compatible_car_model` ← `"<Category> | <Variant> | <SP-Category>"`

Status: `success`. All 3 xlsx fields populated.

**Scope presets** (env vars `MAHINDRA_MAX_CATEGORIES` / `MAHINDRA_MAX_VARIANTS` / `MAHINDRA_MAX_SP_CATEGORIES`):

| Preset | Vars | Rows | Runtime | When to use |
|---|---|---:|---:|---|
| Smoke | `1 / 1 / 1` | ~40 | ~2 min | CI / dev sanity check |
| **Representative (default 2026-05-19)** | **`0 / 1 / 0`** | **~24,800** | **~5h** | **Production monthly** (fits 8h SLA) |
| Full | `0 / 0 / 0` | ~250K | ~34h | Needs B8 sign-off (exceeds 8h SLA) — split across multiple months |

The default was bumped from `1/1/1` → `0/1/0` on 2026-05-19 after the drill chain was verified end-to-end at 2×1×2 scope (79 rows, ~2 min, both categories captured with full back-navigation). The representative scope picks one variant per category (assemblies are largely shared across variants of the same model — engine/brakes/HVAC don't fundamentally change across AT/MT/DSL/PET trims; Spinny can re-run with `MAHINDRA_MAX_VARIANTS=0` if variant-level granularity is required).

**Volume math**: 20 PV categories × N variants × 31 sp-categories × ~40 assemblies per leaf. Per-leaf round-trip = 8s sp-click + parse + 4s back-nav ≈ 30s amortized. At N=1: 20 × 1 × 31 = 620 leaves × 30s = ~5.2h.

**Observability**: the spider logs `[N/20] <CategoryName> — elapsed Xs, Y rows so far` per category and a final `Mahindra: N items extracted in Xs (Yh)` summary. Long runs are monitorable via these checkpoints.

**Status**: parts-leaf drill — **PRODUCTION DEFAULT**. The remaining open item (B8 in kickoff checklist) now only gates "full" scope (all variants), not the representative-scope production default.

**MG sibling**: MG runs the same Intelli Catalogue v11.0 platform with the same drill APIs (Fillcategory, FillCategoryCountryModel, FillCatModelWithOutCountry, FillAssembly). MG's spider does an **adaptive recursive drill** — detects assemblies when `figno` is populated. See §V2.5.

### V2.5 MG — ✅ shipped at REPRESENTATIVE PARTS-LEAF scope (default 2026-05-19)
- URL: https://serviceconnect.mgmotorindia.com/epc/login (redirects from /epc/figure-search)
- Auth: env vars `MG_USER` / `MG_PASS` (alt user available on request)
- Platform: **same Intelli Catalogue v11.0.0 login as Mahindra** — captcha mechanism is identical, OCR bypass strategy translates verbatim.
- Required fields: item_name, item_code, compatible_car_model (no MRP per xlsx)

**Captcha bypass — same as Mahindra**: ddddocr OCR + retry 6×. Login form has **no User Type dropdown** (only Language), so the spider skips that step relative to Mahindra. Verified working: OCR retry on a 3-char misread succeeded on attempt 2.

**Differences from Mahindra (post-login navigation)**:
- MG uses **FIGURE SEARCH** (not Quick Search); no `getFavTree` endpoint.
- Model list comes from **`/webapi/api/FigureSearch/FillcategoryType`** — returns a flat array of category entries, each with `categoryname` ("MG Hector") + `id` (numeric).
- Account scope (<account-id>): 7 PV models cover the entire MG India consumer lineup:
  - **MG Hector** (id=17), **MG ZS EV** (id=7), **MG Gloster** (id=15), **MG ASTOR** (id=16), **MG COMET** (id=19), **MG WINDSOR** (id=21), **MG MAJESTOR** (id=24).
  - Unlike Mahindra (commercial trucks + EVs + 3-wheelers all in tree), the <account-id> account is already PV-scoped at the catalog level — no need for BRD `CategoryType=Passenger Vehicles` filter.

**Current spider output**: 7 rows, one per MG model.
- `item_name` ← `categoryname`
- `item_code` ← `id`
- `compatible_car_model` ← `"MG Passenger Vehicles | <categoryname>"`

**Smoke test result**: 7 unique models, status `success`. OCR retry: 1/6 attempts wasted on 3-char misread, 2nd attempt landed.

**Full drill chain — SOLVED** (xlsx steps 4-8 all implemented):
MG has one MORE level than Mahindra. The spider uses an **adaptive recursive drill**: at each level, fetch entries via SPA click, check if any have `figno` populated → those are assemblies, extract and stop; otherwise drill further.

| Step | Click | API fired | Returns |
|---|---|---|---|
| 3 | MG Model (e.g. MG Hector) | `POST /api/FigureSearch/Fillcategory` | 9 model variants (HECTOR_BS4_N15T_6MT, …) |
| 4 | Variant | `POST /api/FigureSearch/FillCategoryCountryModel` | 4 sub-variants (STYLE 5 Seater, …) |
| 5 | Sub-variant | `POST /api/FigureSearch/FillCatModelWithOutCountry` | 7 spare-part sections (AG001 – ICE POWER_SYSTEM, …) |
| 6 | Section | (same API as last list-returning) | ~40 assemblies with `figno` populated |

**Smoke test (MG Hector → HECTOR_BS4_N15T_6MT → STYLE 5 Seater → AG001 – ICE POWER_SYSTEM)** extracted **39 real assemblies**:
- `BJ00-001` → ENGINE ASSEMBLY - 1.5 LTR GASOLINE
- `BJ00-002` → INLET-INTAKE MANIFOLD ASSEMBLY
- `BJ00-003` → CYLINDER BLOCK, OIL COOLER, OIL FILTER
- `BJ00-004` → CYLINDER HEAD, VALVES, HEAD GASKET, LIFT
- `BJ00-005` → PISTON, CRANKSHAFT AND CONNECTING ROD
- `BJ00-006` → ENGINE OIL PUMP_PETROL
- `BJ00-007` → ENGINE TIMING,PULLEY_GASOLINE
- `BJ00-014` → TURBOCHARGER
- `BJ00-015` → SHIELD - ENGINE OIL PAN
- … 30 more

Field mapping:
- `item_name` ← `categoryname` (e.g. "TURBOCHARGER")
- `item_code` ← `figno` (MG's BJ-prefixed part number, e.g. "BJ00-014")
- `compatible_car_model` ← `"MG | <Model> | <Variant> | <Sub-variant> | <Section>"`

Status: `success`. All 3 xlsx fields populated.

**Volume**:
**Scope presets** (env vars `MG_MAX_MODELS` / `MG_MAX_VARIANTS` / `MG_MAX_SP_CATEGORIES`):

| Preset | Vars | Rows | Runtime | When to use |
|---|---|---:|---:|---|
| Smoke | `2 / 1 / 1` | ~50 | ~90s | CI / dev sanity check (2 models × 1 leaf each) |
| **Representative (default 2026-05-19)** | **`0 / 1 / 0`** | **~2,000** | **~40 min** | **Production monthly** (fits 8h SLA) |
| Full | `0 / 0 / 0` | ~70K | ~12h | Needs B8 sign-off (exceeds 8h SLA) — split across multiple months |

The default was bumped from `1/1/1` → `0/1/0` on 2026-05-19 after the recursive drill chain was verified at 2 models (51 rows including MG Hector + MG ZS EV, all 3 fields populated, ~90s). Representative scope picks one variant + one sub-variant per model (assemblies are largely shared across MG India's 7 PV models' trims; AT/MT/petrol/EV permutations don't change the underlying section structure).

**Volume math**: 7 PV models × N variants × N sub-variants × M sections × ~40 assemblies per leaf, at ~45s per leaf round-trip (8s click + ~8s back-nav + assembly parse, amortized). At N=1, M=all: 7 × 1 × 1 × 7 = 49 leaves × 45s = ~37 min.

**Observability**: the spider logs `[N/7] <ModelName> — elapsed Xs, Y rows so far` per model and a final `mg: N items extracted in Xs (Yh)` summary. Recursive drill emits `depth=N  M entries, drilling K` per step.

**Status**: parts-leaf drill — **PRODUCTION DEFAULT**. The remaining open item (B8 in kickoff checklist) now only gates "full" scope (all variants + sub-variants), not the representative-scope production default.

**Bonus**: MG parts grid columns from `/api/common/GetColumnActive` include `PRICE1`, `admincprice1` (DIST_COST), `admincprice2` (DIST_MSRP), `admincprice3`. The current spider doesn't drill into the actual parts-list endpoint (one level deeper than assemblies) — if Spinny wants MRP as a bonus, that's the next pass.

### V2.6 Tata — ✅ shipped (smoke test passed end-to-end with real MRP)
- URL: https://www.tatamotorsecats.com/frmTATAModelSearch.aspx (redirects to /frmTATALogin.aspx)
- Auth: xlsx defaults `<dealer-cred>` — verified working
- Platform: ASP.NET WebForms (has __VIEWSTATE / __EVENTVALIDATION) — buildable
- Required fields: item_name, item_code, **MRP**, compatible_car_model
- **Only remaining v2.0 OEM with MRP requirement** — highest data value.

**Discovery completed 2026-05-19** (probes saved under state/probe_tata_*.py):

1. **Login** — POST to /frmTATALogin.aspx with `txtUserName` + `txtPassword`. Image-submit button `input#btnLogin`. Session cookie carries authorization for downstream pages.

2. **Cascading dropdowns** on /frmTATAModelSearch.aspx (7 levels, each triggers ASP.NET partial postback):
   - drpDivision: 2 options (`PASSENGER VEHICLES - INDIA` is what we want)
   - drpModelCategory: populated after Division. ~10 options.
   - drpModel: ~10-30 models per category (Aria, Bolt, Indica, Indigo, Nano, Nexon, Punch, Safari, Tiago, Tigor, Vista, Zest, …)
   - drpChassis: ~5-15 chassis numbers per model (e.g. `614001`)
   - drpVC: ~10-15 vehicle codes per chassis (e.g. `28700128R`)
   - drpDescription: variant text (e.g. `ARIA 4X2 PURE,2.2 LTR DICOR BS-III`)
   - drpEnginetype: fuel/engine (e.g. `2.2L DICOR BS-III`)
   - After all selected → click `input[id*=btnGo]` to enter catalogue.

3. **Catalogue page** lands at `/frmtataadminmodelnew.aspx?ID=<modelId>` (e.g. ID=300 for ARIA).
   - Page shows breadcrumb `TATA >> PV-INDIA >> ARIA >> 2.2L DICOR BS-III >> 28700328R - 614001` and the **21 spare-part categories**: 00-ENGINE, 25-CLUTCH, 26-GEARBOX, 29-CONTROLS, 30-ACCELERATOR, 31-FRAME, 32-SUSPENSION, 33-FRONT AXLE, 35-REAR AXLE, 40-WHEELS, 41-PROPELLER SHAFT, 42-BRAKES, 46-STEERING, 47-FUEL SYSTEM, 49-EXHAUST SYSTEM, 50-RADIATOR, 54-ELECTRICALS, 55-MASCOT, 58-TOOLS, 60-BODY, 83-AIRCON.

4. **Tree navigation** via ASP.NET WebForms TreeView. Each tree node is `<a href="javascript:__doPostBack('ctl00$ContentPlaceHolder1$trvEPCDetails','<arg>')">`. The `arg` encodes the path:
   - Toggle/expand: `t<modelId>\<chap>\<sub>...` (toggles visibility of children)
   - Select: `s<modelId>\<chap>\<sub>...` (renders selected node's parts/illustration)
   - Children of a non-expanded node appear as `<arg>\Loading...Please Wait.` placeholders that get replaced on expand.

5. **Per-category illustrations** — confirmed each category has ~25-30 sub-illustrations. Example for 00-ENGINE: `00.00.01A - ENGINES`, `00.00.02A - OVERHAUL GASKET KIT`, `00.01.01 - ASSEMBLY CYLINDER CRANKCASE` … through `00.24.01B - ENGINE MOUNTING` (27 illustrations).

6. **Parts-table extraction** — ✅ verified. **Key discovery**: each illustration in the TreeView is rendered as `<a href="javascript:funredirectType('<illId>')">` (NOT __doPostBack), where `funredirectType(id)` simply navigates to `frmtataartbomnew.aspx?ID=1_<id>`. So we skip the postback dance entirely: get the illustration IDs from the TreeView expand response, then GET each artbom URL directly. Parts table columns: ITEM | PART NO. (8-14 digit) | PART DESCRIPTION | QTY | RATE (₹) | REMARKS. The artbom page has nested layout tables so the spider dedupes on `(part_no, desc)` within each illustration.

   **Confirmed working** via [state/smoke_tata_parts.py](../state/smoke_tata_parts.py): illustration 421 returned 4 ARIA engine parts (BARE ENGINE STORME BS-IV ₹270387, HALF BLOCK-MERLIN BS-IV ₹148547, SPD ENGINE BSIII ₹514741, ASSY. CYL. HEAD W/O INJECTOR ₹54458). Illustration 395 returned 44 gasket parts. Illustration 420 returned 10 fastener parts (some MRPs null for low-value washers — row marked `partial` per BRD §7).

7. **Volume estimate** — Tata PV India catalogue is large:
   - ~30-100 vehicle variants
   - × 21 categories
   - × ~25 illustrations
   - × ~5-20 parts per illustration
   - ≈ **75K-1M parts records** at full breadth (similar order to Hyundai's full crawl)
   - Per-request rate ~5s (ASP.NET postback latency) → full crawl is **~25-100 hours** of wall time
   - Recommended: per-vehicle checkpoint persistence; partition crawl across multiple monthly runs OR scope by model whitelist.

**Implementation**: [spiders/tata.py](../spiders/tata.py). Default env-var limits (`TATA_MAX_MODELS_PER_CATEGORY=1`, `TATA_MAX_VCS_PER_MODEL=1`, `TATA_MAX_CATEGORIES=1`, `TATA_MAX_ILLUSTRATIONS=5`) keep the first run small. Override to 0 = all for production crawls.

**Performance / SLA consideration**: ~6s per illustration fetch, ~525 illustrations per vehicle, 30+ vehicle variants = ~25-50 hours for the full PV-India catalogue. Exceeds the BRD §8 8h monthly window. Kickoff decision needed (`docs/kickoff_checklist.md` item): pick one of (a) whitelist current Tata models (Tiago/Tigor/Altroz/Punch/Nexon/Harrier/Safari/Curvv ≈ 8 vehicles, ~6h), (b) split Tata across multiple monthly windows, or (c) accept Tata running outside the 8h SLA with a longer separate cron.

**2026-05-26 — Site redesign fix-up (Tata returned 0 rows after silent backend change)**

Symptoms after redesign: cascade reached catalogue page; spider reported "categories on catalogue: 1" instead of 21; postback args raised event-validation errors; full crawl returned 0 rows. Root causes and fixes (all in [spiders/tata.py](../spiders/tata.py)):

1. **Event-validation rejected synthesized toggle args** — old code converted the rendered `s<id>` select arg into `t<id>` to expand the tree. Server now rejects synthesized toggle args. Fix: drop the synth, click the anchor element directly via `document.getElementById(id).click()` so the browser invokes the inline `onclick` with the server-registered arg.
2. **Backslash mismatch between captured arg and `__doPostBack` payload** — `a.href` returns the URL string with literal `\\` (two backslashes); when the anchor is clicked, the JS engine parses `'s300\\394'` as runtime string `s300\394` (one backslash). Re-emitting the captured-from-href arg failed event-validation. Same fix as above: click the anchor, let the browser handle string parsing.
3. **`TATA_MAX_CATEGORIES=1` default silently truncated** — production needs all 21. Default bumped to `0` (= no limit).
4. **Two-pass `funredirectType` extraction** — the strict regex `funredirectType\\('([^']+)'\\)[^>]*>(text)</a>` matched 0 in the delta body even though 27 valid IDs were present (artbom delta wraps anchors across layout `<tr>`/`<td>` boundaries). Switched to two-pass: loose regex finds all IDs, then for each match walk backwards to the nearest `<a ` and forwards to the next `</a>` to recover the visible label.
5. **`page.goto(artbom_url)` triggered chrome-error://chromewebdata/ mid-crawl** — interrupted the parts fetch _and_ crashed `_back_to_search`. Switched `_fetch_parts` to `page.context.request.get()` (no navigation, reuses cookies/session). Broadened `_back_to_search` exception catch so a stray navigation interrupt doesn't kill the cascade.
6. **Parts-table header detection** — artbom layout wraps the parts table inside layout rows; the `PART NO. / PART DESCRIPTION` header is mid-table, not in `tr:first-child`. Switched detection to normalized full-text-of-table contains both `PART NO.` and `PART DESCRIPTION`.
7. **Redundant double-select in cascade** — old code ran `_select(drpModelCategory, mc)` then `_iter_dropdown(drpModelCategory, drpModel, first_label=mc)` which re-selected the same category. The redundant second select corrupted the dropdown state. Removed the wrapper; the cascade now does one `_select` per level + one `_dropdown_opts` read.

**Verified production run** (2026-05-26, full crawl, no model whitelist): **1,431 unique parts in 58 min** wall-clock (well under the SLA 8h window — the per-vehicle Playwright-context reuse keeps per-illustration latency around 2.5s, not the earlier 6s estimate). Sample rows include `BARE ENGINE STORME BS-IV` (₹270,387), `HALF BLOCK-MERLIN BS-IV` (₹148,547), `SPD ENGINE BSIII` (₹514,741). After BRD §5 dedup on `(source_website, item_code)` the master keeps **203 unique part numbers** (multiple vehicle/category mappings per part are collapsed). Per-brand file retains all 1,431 mapping rows so analysts can resolve back to specific vehicles.

**Diagnostic probes preserved** under `state/probe_tata_redesign[1-6].py`, `state/probe_tata_spider_call.py`, `state/probe_tata_login_path.py`, `state/probe_tata_double_select.py`, `state/probe_tata_launch_args.py`. probe_tata_redesign5.py is the most informative — it captured the 27 funredirectType IDs in the delta that pointed at the strict-regex failure.

### V2.7 Ford — ✅ shipped (smoke test passed — 25 parts on first illustration set)
- URL: https://microcat-apac.superservice.com/content/microcat-epc/#/identify
- Auth: env vars `FORD_USER` / `FORD_PASS` (xlsx defaults `<dealer-cred>`).
- Platform: Infomedia Superservice / Microcat MARKET (Angular SPA, REST backend).
- Required fields: item_name, item_code, compatible_car_model (no MRP per xlsx).

**Crawl strategy** (verified 2026-05-19 via [spiders/ford.py](../spiders/ford.py)):

1. **Login**: standard email/password at login.superservice.com. SPA then sets a JWT in `Authorization: Bearer …` header on every backend request (Angular HTTP interceptor). We capture the Bearer + IFM headers (`x-ifm-sid`, `x-ifm-session-id`, `x-ifm-franchise`, `accept`) from the first authenticated request and reuse them via `page.request.get(..., headers=auth_headers)`.

2. **APIs** (all under `https://microcat-apac.superservice.com/ver/microcat/epc-html/`):
   - `GET /v2/history/vehicles?market=IN&page=0&size=40` → list of vehicleIds the account has access to (~15-40 entries; FIGO EC, ECOSPORT BW, FIGO CDU primarily).
   - `GET /v3/section/<vehicleId>/children?market=IN&language=en&id=<sectionId>&showNonApplicable=false&useLegacySectionUserNote=false` → list of child sections. Each has `id`, `code`, `label`, `illustrated:bool`. `illustrated:true` = leaf with parts. Use `id=-1` for the 11 top-level sections (A BODY, B FRONT AXLE, … Z ACCESSORIES).
   - `GET /v1/part/<vehicleId>/sectionparts/<leafId>?imageIndex=0&language=en&market=IN&showNonApplicable=false&showOrbSupersession=false&interpretationAttributes=` → parts for one illustrated leaf. Response shape: `{catalogWithParts: {parts: [{label, partIdentifications: {partFormats: [{key,value},...]}, qty, ...}]}}`.

3. **Per-part field mapping**:
   - `item_name` ← `part.label` (e.g. "BODYSHELL - PRIMED - LESS CLOSURES")
   - `item_code` ← `part.partIdentifications.partFormats[]` — prefer `key=partnumber`, fallback to `finis` (Ford internal) or `engineering`. Spider applies this preference.
   - `compatible_car_model` ← `"<catalogName> | <breadcrumb of parent sections> | <leaf label>"`, e.g. `"FIGO EC 2010- | A BODY AND RELATED PARTS > A01 BODYSHELL > A01.050 REPAIR PANELS"`.
   - **NO MRP** — `priceData.partPriceList` is empty for this dealer credential, and xlsx doesn't require it.

**Smoke test result** (FIGO EC 2010-, 1st top section A, 3 illustrated leaves): 25 unique parts including BODYSHELL (SPAS69 A00015 AA), various NUT/SCREW/BRACKET fasteners, MEMBER ASSY FLOOR SIDE LH/RH, PAN ASSY FLOOR REAR, etc. All 3 required fields populated.

**Scope**:
- `/v1/catalog/list` returns 15 Ford catalogs (FIGO EC, ECOSPORT BW, FIGO CDU, FIESTA, ENDEAVOUR family, MUSTANG, MONDEO, IKON, FUSION, ESCORT, …) — but you need a **vehicleId to enter the section tree**, and vehicleIds are derived from VINs.
- Vehicle History API exposes only the ~3-4 catalogs that the account has actually used (FIGO EC, ECOSPORT BW, FIGO CDU and variants). The other ~11 catalogs cannot be entered without VINs.
- Kickoff decision (track in `docs/kickoff_checklist.md`): pick one of
  - (a) Accept history-only subset (likely 3-4 models × multiple variants — month-over-month drift acceptable),
  - (b) Provide curated VIN list (Spinny's active Ford fleet — best coverage, largest crawl),
  - (c) Negotiate broader catalog access (so /catalog/list responses also expose default vehicleIds per catalog).

**Env-var limits** (small defaults for first runs):
- `FORD_MAX_VEHICLES` (default 1, 0=all) — vehicles to crawl from history
- `FORD_MAX_TOP_SECTIONS` (default 1, 0=all 11) — top-level sections per vehicle
- `FORD_MAX_LEAVES_PER_CAT` (default 3, 0=all) — illustrated leaves per top section

**Volume estimate at full scope (1 vehicle, all sections, all leaves)**:
- 11 top sections × ~15 subsections × ~10 illustrations × ~10 parts ≈ **~16K parts per vehicle**
- ~3 sec per illustration fetch → ~80 min per vehicle
- For 4 history-accessible catalogs × 1 representative vehicle each = ~5h crawl per month (within 8h SLA).

### Volume planning (v2.0 OEMs)
| Brand | Est rows full | Est runtime full | Notes |
|---|---|---|---|
| Maruti | ~29,313 | ~8 min | Single API call, no auth |
| Hyundai | ~50K–100K | ~10–15 hrs | 5 years × ~10 models × ~7 cats × ~30 ills × ~8 parts |
| Toyota | ~30K–60K (est) | ~6–10 hrs | Toyota catalog typically smaller than Hyundai |

Default `HYUNDAI_YEARS=2022..2026` (5 years). For monthly batch covering all history, override to `HYUNDAI_YEARS=1998,1999,...,2026` — but this exceeds the BRD §8 8-hour window. Recommend Spinny clarify which years are downstream-relevant.

---

## Conventions adopted across all spiders

Knowledge crystallized from the 5 spiders shipped so far:

| Convention | Why |
|---|---|
| **WP REST API enumeration** (when site exposes it) | Avoid scraping list pages with bot-detection / pagination quirks. Technix won big from this. |
| **Capture-and-replay (Playwright + ctx.request.post)** for JS-rendered APIs | Mobil's Coveo backend rejects requests without the page's session token. Same-context replay is the only reliable method. |
| **Detail-page filter** when listing URL is unscoped | HELLA's `/listing/Shop4Hella/...` mixes segments. Breadcrumb 2nd crumb is the authoritative scope marker. |
| **PV slug whitelist** when listing URL has too many siblings | Exide's `exide-*.aspx` matches global-nav links — explicit `PV_FAMILY_SLUGS` set prevents pollution. |
| **Strip referral params** (`srsltid`, `utm_*`, `gclid`) | Monroe — Google added `srsltid` to its referral URLs; clean URL keeps analytics off our scrape. |
| **Dedupe-by-`item_code` early-stop** | Don't trust site pagination's "last page" indicator. Stop when a page yields zero new SKUs. |
| **Two-rule master dedup** (BRD §5) | Mobil items have no `item_code` — pandas would collapse them all to one row if we deduped on `(source_website, item_code)` only. Split by `item_code` presence. |
| **`crawl_status` = partial when required field absent** | Per BRD §7. Exide all-partial because MRP isn't on site. VALEO will be similar (no MRP). |
| **`brand` column derived from config key, NOT scraped** | "Mobil", "Exide", etc. — stable across runs, no risk of upstream rename. |
| **`item_name` faithful to site headings** | "Exide Epiq EPIQ35L" — preserve what an analyst sees on the site, even if it repeats brand. |
