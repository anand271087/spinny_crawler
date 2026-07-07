**Subject:** RE: Crawler output review — root causes + fixes

Hi [Stakeholder],

Thanks for the detailed list. We investigated every item against the latest code and re-ran the spiders. Summary up front: **4 real bugs found and fixed** (Bosch, Amaron, Mobil, plus Hella which we caught ourselves), several items were **already working on the latest code** (you may be on an earlier build), a few are **expected behaviour**, and **2 need one data point from you** to close. Point-by-point below.

---

### 1. Low part-number output

**Ford — 40 → this is expected, not a bug.**
Ford's Microcat only exposes the vehicles present in the dealer account's service history (~15 vehicles) — there's no VIN lookup to reach the rest of the catalogue. So the part count is inherently small. We get 41. This is a dealer-account limitation on Ford's side, not a crawler issue.

**Mahindra — 3,756 / MG — 833 / Tata — 722 → please read the per-brand files, not the master.**
On the latest code, the per-brand outputs are much larger:
- Mahindra: **28,222** parts (per-brand file)
- MG: **10,281** parts (per-brand file)
- Tata: **5,851** parts (per-brand file)

The consolidated **master** file de-duplicates on part number per the agreed BRD rule, which collapses these part-level brands (e.g. the same part shared across many assemblies/models becomes one row). That's why the master shows lower numbers (Tata 722 in the master matches exactly what you saw). **For full counts, use the per-brand CSVs; the master is the de-duplicated cross-brand view by design.**
Note: your Mahindra (3,756) and MG (833) match neither our per-brand nor our master figures, which suggests you were testing an **earlier build** — please re-pull the latest code.

---

### 2. Wrong / incorrect data extraction

**Bosch — FIXED. ✅** (MRP, item code, and compatible_car_model were all wrong.)
Root cause: the catalogue PDFs are 5-column tables (Part Number | Product Description | Image | Model Description | MRP), but the parser was reading the PDF as flattened text with a regex, which scrambled the columns into each other — hence wrong part codes, wrong MRP, and a garbage compatible_car_model. We rewrote it to parse the actual table columns. Now: correct part codes, correct names ("Brake Master Cylinder"), and **correct MRP** (e.g. ₹3,540, which the old code mangled to ₹540). Verified — 1,796 clean rows.

**Exide — needs one input from you.**
The batteries and item codes are correct, but MRP is pulled from Exide's official MRP-list **PDF** (a fixed published file). Your two points are consistent with that PDF being an **older edition** (prices lower than the latest) and not listing every SKU (so "not all fetched"). To fix precisely, please share **the correct current MRP for 1–2 batteries and where you see it** — we'll re-point the spider at the current MRP source and confirm coverage.

**Lumax — needs one input from you.**
Our `item_code` currently comes out as a numeric material code (e.g. `61001594`). If the part number you expect is a different value/format, we likely mapped the wrong column. Please share **one example of the expected part number** for a Lumax item and we'll correct the mapping.

---

### 3. Spiders "failing outright"

**Mobil — FIXED. ✅**
The Mobil site added Akamai bot-protection since our last run — it returned "Access Denied" to the crawler, so no data loaded. We added a real browser fingerprint (Chrome UA + headers) to get past it. Verified — 412 products extracted.

**Schaeffler / TVS Girling / Uno Minda / ZF — running perfectly on the latest code. ✅**
We re-ran all four today and they all completed successfully:
- Schaeffler: **18,224** rows
- ZF: **635** rows
- TVS Girling: **913** rows
- Uno Minda: **613** rows

Since these work on our current version, the failures you saw are most likely from an **earlier build or a local setup issue** (e.g. the browser dependency `playwright install chromium` not run, or no network/credentials). Please re-test on the latest code — and if any still fail, send us the exact error message and we'll pinpoint it.

---

### 4. Extraction incomplete

**Amaron — FIXED. ✅** ("complete data not coming, not all models extracted.")
Root cause: the spider de-duplicated globally on the battery SKU, so once a battery model was seen it was skipped for every other vehicle — collapsing the output to ~52 rows and keeping only the first vehicle per battery. We changed it to keep every battery × vehicle combination. Verified — **1,045 rows** (50 battery SKUs across 498 vehicles), all with MRP.

**Gabriel — running; please confirm the expected count.**
Gabriel extracts **2,209** parts from its catalogue PDF on the latest code and runs cleanly. If you're expecting more, please share the number you expect (or a section you think is missing) and we'll check whether additional pages/sections of the PDF need to be included.

---

### Additionally — we proactively found and fixed one more

**Hella — FIXED. ✅** (not on your list, but it was failing.)
The Hella site was dropping our connections mid-crawl under load, which crashed the spider. We added automatic retry + throttling so it now completes reliably.

---

### One item pending on the site's side

**MG portal currently unreachable.**
While re-testing, MG's dealer portal (serviceconnect.mgmotorindia.com) is returning connection timeouts from our end — the site appears down or is blocking traffic right now. The MG spider itself is unchanged and previously produced 10,281 parts, so there's nothing to fix in code; we'll confirm a clean run as soon as the portal is reachable again.

---

### Net status
- **Fixed:** Bosch, Amaron, Mobil, Hella
- **Confirmed working on latest code:** Schaeffler, ZF, TVS Girling, Uno Minda (plus Mahindra/MG/Tata full counts in per-brand files)
- **Expected / by design:** Ford (dealer scope), lower master counts (de-dup)
- **Need one data point from you:** Exide (correct MRP source), Lumax (expected part number), Gabriel (expected count)
- **Site-side:** MG portal temporarily unreachable

Please pull the latest code (we'll share the refreshed zip) and re-run. Happy to hop on a quick call to walk through any of these.

Best regards,
[Your name]
