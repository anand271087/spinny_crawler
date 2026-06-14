**Subject:** Spinny OEM Spare-Parts Crawler — handover package (code + install guide + spider reference)

Hi [Stakeholder name],

Please find the complete handover for the Spinny OEM Spare-Parts Crawler. Everything you need to install it on your own machine and run it is included — no cloud service, no recurring cost.

---

### What's attached / shared

1. **`spinny_crawler.zip`** — the full codebase, exported from our Git repository. Unzip it anywhere; that unzipped folder is the "project folder."
2. **`INSTALL_GUIDE.docx`** — step-by-step install + run instructions, with **two paths**:
   - **Way 1 (Manual):** you run the commands yourself in a terminal (~20 min).
   - **Way 2 (Claude Code assisted):** open the folder in Claude Code and let it install + verify the project for you, guided and conversational.
3. **`SPIDER_REFERENCE.docx`** — how every one of the 26 brand spiders works: a summary table plus detailed per-brand notes (site behaviour, the API/selectors used, gotchas, and last verified run results).

> The credentials for the 6 login-protected brands (Hyundai, Toyota, Mahindra, MG, Tata, Ford) are **not** in the code or these documents, by design. I'll share them separately/securely — you'll paste them into a local `.env` file as the guide explains.

---

### 60-second overview

- Monthly batch crawler over **26 OEM / aftermarket brand sites** → per-brand CSV/JSON + one consolidated **master** file.
- **20 brands** need no login; **6** need the dealer credentials I'll send separately.
- A full run of all brands takes roughly **3.5–7 hours** (one site, ZF, is the slow one; there's a cache mode that cuts it to ~15 min after the first run).
- Requires **Python 3.11+** and runs on **macOS, Linux, or Windows** (on Windows, either native PowerShell or WSL2 — both covered in the install guide).

**Fastest way to confirm it works after install:**
```
python -m orchestrator.run_monthly --brands=zip      # ~2 seconds, writes a sample file
```
Then the full run:
```
python -m orchestrator.run_monthly                   # all 26 brands
```

---

### What the data looks like

Each run creates `output/<date>/` with one file per brand plus `spinny_oem_master_<date>.csv` (all brands combined, de-duplicated) and `run_summary_<date>.json` (per-brand status + counts). Fields include item name, item code, MRP, compatible car model, and — for the OEM EPC brands — part description, part structure, and dates where the site exposes them.

---

### Status & a few things to know

- **Fully delivered:** all 26 spiders are working and were run end-to-end recently. The latest consolidated master has ~130K+ de-duplicated rows across all brands.
- **Hyundai MRP:** now captured at ~**76% coverage** (the rest are genuinely price-less on the site).
- **Two known limitations** (not bugs in the crawler):
  - **Toyota MRP** is empty because the dealer account's price book is unpopulated server-side — this needs Spinny's commercial team to enable pricing on that credential.
  - **Maruti "car model"** isn't currently extracted (the public API returns parts without per-model compatibility). Capturing it would need additional per-model iteration — happy to scope this if it's required.

---

### Suggested next steps for your team

1. Follow `INSTALL_GUIDE.docx` (pick Way 1 or Way 2) and do the quick `--brands=zip` smoke test.
2. I'll send the 6 dealer credentials securely; add them to `.env` and run a credentialed test.
3. Do one full run and confirm the `output/<date>/` files look right.
4. (Optional, server) Set up the monthly cron + email alerts + cloud upload — covered in `docs/deployment.md` inside the project.

Happy to jump on a call to walk through the install or any spider in detail. Just let me know.

Best regards,
[Your name]
