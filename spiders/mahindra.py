"""MAHINDRA eCat spider — v2.0 OEM.

Site: https://mahindra-ecat.com/epcview/login (Intelli Catalogue v11.0.0).
xlsx fields: item_name, item_code, compatible_car_model (no MRP required).

Auth env vars: MAHINDRA_USER / MAHINDRA_PASS — supplied separately,
User Type = 'Other User (Fleet Owner)').

CAPTCHA BYPASS (BRD §7 wrinkle):
- 4-character text captcha (140×45 inline base64 PNG) solved offline via `ddddocr`
  (~90% per attempt, no SaaS dependency). With `MAHINDRA_LOGIN_RETRIES=6` the
  effective success approaches 100%.

CRAWL CHAIN (verified 2026-05-19, follows xlsx steps 1-8):

step 1: open login URL                                            ✓ (Playwright)
step 2: click 'quick search' (or figure-search, same data)        ✓ navigate
step 3: click 'Passenger Vehicles'                                ✓ POST Fillcategory
        → response: list of 20 categories (XUV 7XO, THAR ROXX, …)
step 4: click on a car model (Category)                           ✓ POST FillCategoryCountryModel
        → response: list of variants per category (AX7 MT - DSL, AX7 AT - DSL, …)
step 5: click on the variant                                      ✓ POST FillCatModelWithOutCountry
        → response: list of spare-part categories (ENGINE, BRAKES, HVAC, …) ~31 each
step 6: click on the spare part category                          ✓ POST FillAssembly
        → response: list of assemblies — these ARE the items per xlsx field-map.
step 7-8: extract item_name + item_code + car_model from response
        → item_name   ← categoryname (e.g. "ENGINE ASSY - DSL MT (185HP) IEMS")
        → item_code   ← figno (e.g. "W6E010002A")
        → car_model   ← "model | variant | sp_category"

The encrypted POST parameter `FigureSearchParm` is handled implicitly: we do
Playwright UI clicks and let the SPA's JS encrypt + send. Responses are captured
via page.on('response') — no need to decrypt anything.

Env vars — defaults are "representative" scope (production-ready, ~5h runtime,
fits inside BRD §8 8h SLA). Override individual knobs for smoke or full scope.
  MAHINDRA_LOGIN_RETRIES        default 6
  MAHINDRA_MAX_CATEGORIES       default 0 (=all 20 Passenger Vehicle categories)
  MAHINDRA_MAX_VARIANTS         default 1 (=1 representative variant per category)
  MAHINDRA_MAX_SP_CATEGORIES    default 0 (=all 31 spare-part categories per variant)

Scope presets:
  Smoke (CI/dev, ~2 min, ~40 rows):
    MAHINDRA_MAX_CATEGORIES=1 MAHINDRA_MAX_VARIANTS=1 MAHINDRA_MAX_SP_CATEGORIES=1
  Representative (production, ~5h, ~24,800 rows):  ← default
    MAHINDRA_MAX_CATEGORIES=0 MAHINDRA_MAX_VARIANTS=1 MAHINDRA_MAX_SP_CATEGORIES=0
  Full (~34h, exceeds 8h SLA — needs B8 sign-off + split runs):
    MAHINDRA_MAX_CATEGORIES=0 MAHINDRA_MAX_VARIANTS=0 MAHINDRA_MAX_SP_CATEGORIES=0

Volume math: 20 PV categories × N variants × 31 sp-categories × ~40 assemblies
per leaf at ~30s per leaf round-trip (8s sp-click + parse + 4s back-nav,
amortized over login + category clicks).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from lib.credentials import Credentials
from lib.snapon_epc import UA, LAUNCH_ARGS, INIT_SCRIPT
from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.mahindra")

LOGIN_URL = "https://mahindra-ecat.com/epcview/login"
FIGURE_URL = "https://mahindra-ecat.com/epcview/figure-search"


class Spider(BaseSpider):

    def __init__(self, brand_key: str, brand_cfg: dict) -> None:
        super().__init__(brand_key, brand_cfg)
        self.login_retries = int(os.environ.get("MAHINDRA_LOGIN_RETRIES", "6") or "6")
        # Defaults below are "representative" scope (production-ready); see docstring.
        self.max_cats = int(os.environ.get("MAHINDRA_MAX_CATEGORIES", "0") or "0")
        self.max_variants = int(os.environ.get("MAHINDRA_MAX_VARIANTS", "1") or "1")
        self.max_sp = int(os.environ.get("MAHINDRA_MAX_SP_CATEGORIES", "0") or "0")
        # Part-level drill (2026-06-13): click each assembly → accept the prod-date
        # dialog → GetIllustrationPartsJQ returns the parts table (partNo, description,
        # startDate, endDate). Grain becomes per-part. Set MAHINDRA_PART_LEVEL=0 to
        # revert to the cheaper assembly-grain rows. MAHINDRA_MAX_ASSEMBLIES caps
        # assemblies drilled per section (0=all) — the main runtime lever now.
        self.part_level = bool(int(os.environ.get("MAHINDRA_PART_LEVEL", "1") or "1"))
        self.max_assemblies = int(os.environ.get("MAHINDRA_MAX_ASSEMBLIES", "0") or "0")

    def crawl(self) -> list[Row]:
        try:
            import ddddocr
        except ImportError:
            log.error("ddddocr not installed; run `pip install ddddocr`")
            return []

        creds = Credentials.load("mahindra")
        if creds is None or not creds.user:
            log.error("mahindra: missing credentials — set MAHINDRA_USER and MAHINDRA_PASS env vars (see .env.example)")
            return []

        ocr = ddddocr.DdddOcr(show_ad=False)
        rows: list[Row] = []
        seen: set[tuple[str, str]] = set()
        t_start = time.time()

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
            # Capture FigureSearch API responses — these contain category/variant/assembly data.
            # GetIllustrationPartsJQ (Illustration controller) carries the part-level table.
            response_buf: dict[str, list[str]] = {
                "Fillcategory": [], "FillCategoryCountryModel": [],
                "FillCatModelWithOutCountry": [], "FillAssembly": [],
                "GetIllustrationPartsJQ": [],
            }

            def on_resp(r):
                if "/webapi/api/" not in r.url:
                    return
                ct = r.headers.get("content-type", "")
                if not any(t in ct for t in ("json", "text")):
                    return
                try:
                    body = r.text()
                except Exception:
                    return
                for key in response_buf:
                    if key in r.url:
                        response_buf[key].append(body)
                        return

            def open_session():
                """Create a fresh context+page, attach handlers, log in, land on
                FIGURE_URL. Returns (ctx, page) or (None, None). Reused for the
                initial login and for the hard-reset fallback when the SPA session
                gets corrupted by deep part-drilling (re-nav to PV root fails)."""
                new_ctx = browser.new_context(
                    user_agent=UA, viewport={"width": 1920, "height": 1080},
                    locale="en-US", timezone_id="Asia/Kolkata",
                )
                new_page = new_ctx.new_page()
                new_page.add_init_script(INIT_SCRIPT)
                new_page.on("response", on_resp)
                # Assembly click fires a native "search parts with prod date" confirm.
                new_page.on("dialog", lambda d: d.accept())
                if not self._login_with_retry(new_page, creds.user, creds.password, ocr):
                    return None, None
                new_page.goto(FIGURE_URL, wait_until="domcontentloaded", timeout=20_000)
                new_page.wait_for_timeout(8000)
                return new_ctx, new_page

            try:
                ctx, page = open_session()
                if page is None:
                    log.error("mahindra login failed")
                    return rows
                log.info("mahindra login ok")

                # step 3: click Passenger Vehicles
                response_buf["Fillcategory"].clear()
                if not self._click_text(page, "Passenger Vehicles"):
                    log.error("could not click Passenger Vehicles")
                    return rows
                cats = self._parse(response_buf["Fillcategory"])
                # Filter: pick only categories with categoryname (drop nulls/blanks)
                cat_names = [c.get("categoryname") for c in cats if c.get("categoryname")]
                if self.max_cats:
                    cat_names = cat_names[: self.max_cats]
                log.info("PV categories: %d total, crawling %d", len(cats), len(cat_names))

                for cat_idx, cat_name in enumerate(cat_names, start=1):
                    # Re-navigate to root before each category (skip on cat #1 — we're
                    # already on the PV list from the initial click). Breadcrumb-back
                    # is fragile after deep drill; fresh page.goto is reliable.
                    # Added 2026-05-21 after 18+ silent skip-cat failures in a single
                    # second on 2026-05-21 parallel run. See kickoff_checklist §I.I2.
                    if cat_idx > 1:
                        if not self._renavigate_to_pv_root(page, response_buf):
                            # Deep part-drilling can corrupt the SPA session so a plain
                            # re-goto + PV click no longer recovers. Hard-reset: drop the
                            # context and log in fresh, rather than abort the whole crawl.
                            log.warning("[%d/%d] PV root re-nav failed; hard-resetting "
                                        "session (fresh login)", cat_idx, len(cat_names))
                            try:
                                ctx.close()
                            except Exception:
                                pass
                            ctx, page = open_session()
                            if page is None:
                                log.error("[%d/%d] hard-reset re-login failed; aborting",
                                          cat_idx, len(cat_names))
                                return rows
                            response_buf["Fillcategory"].clear()
                            if not self._click_text(page, "Passenger Vehicles", wait=8000):
                                log.error("[%d/%d] PV click failed after hard-reset; aborting",
                                          cat_idx, len(cat_names))
                                return rows

                    response_buf["FillCategoryCountryModel"].clear()
                    if not self._click_text(page, cat_name):
                        log.warning("skip cat %s (click failed after re-nav)", cat_name)
                        continue
                    log.info("[%d/%d] %s — elapsed %.0fs, %d rows so far",
                             cat_idx, len(cat_names), cat_name,
                             time.time() - t_start, len(rows))
                    variants = self._parse(response_buf["FillCategoryCountryModel"])
                    variant_names = [v.get("categoryname") for v in variants if v.get("categoryname")]
                    if self.max_variants:
                        variant_names = variant_names[: self.max_variants]
                    log.info("  category=%s → %d variants, crawling %d",
                             cat_name, len(variants), len(variant_names))

                    for variant_name in variant_names:
                        response_buf["FillCatModelWithOutCountry"].clear()
                        if not self._click_text(page, variant_name):
                            continue
                        sp_cats = self._parse(response_buf["FillCatModelWithOutCountry"])
                        sp_names = [s.get("categoryname") for s in sp_cats if s.get("categoryname")]
                        if self.max_sp:
                            sp_names = sp_names[: self.max_sp]
                        log.info("    variant=%s → %d sp-cats, crawling %d",
                                 variant_name, len(sp_cats), len(sp_names))

                        for sp_name in sp_names:
                            response_buf["FillAssembly"].clear()
                            if not self._click_text(page, sp_name):
                                continue
                            assemblies = self._parse(response_buf["FillAssembly"])
                            log.info("      sp=%s → %d assemblies", sp_name, len(assemblies))

                            compat = f"{cat_name} | {variant_name} | {sp_name}"
                            if self.part_level:
                                self._drill_assemblies(
                                    page, response_buf, assemblies, rows, seen,
                                    cat_name, variant_name, sp_name,
                                )
                            else:
                                for a in assemblies:
                                    item_name = (a.get("categoryname") or "").strip()
                                    item_code = (a.get("figno") or "").strip()
                                    if not item_name or not item_code:
                                        continue
                                    key = (item_code, compat)
                                    if key in seen:
                                        continue
                                    seen.add(key)
                                    rows.append(Row(
                                        item_name=item_name,
                                        item_code=item_code,
                                        compatible_car_model=compat,
                                    ))

                            # Navigate back to sp-category list for next sp_name.
                            # Breadcrumb click on variant_name still usually works at
                            # depth 4 → 3; if it doesn't, the next sp_name click will
                            # fail and we skip-cat (cheap retry).
                            self._click_text(page, variant_name, wait=4000)
                        # Done with all sp-cats for this variant. Don't try
                        # breadcrumb-back to cat_name — re-nav handles that for the
                        # outer loop (see top of `for cat_idx` block).
                        # (Single variant per category is the default scope, so this
                        # rarely matters; if MAHINDRA_MAX_VARIANTS > 1 it's a quick
                        # retry-and-skip in the inner _click_text.)
                    # No back-nav at end of category — outer loop's re-nav handles it.
            finally:
                browser.close()
        elapsed = time.time() - t_start
        log.info("mahindra: %d items extracted in %.0fs (%.1f h)",
                 len(rows), elapsed, elapsed / 3600)
        return rows

    # ---------- part-level drill ----------

    def _drill_assemblies(self, page: Page, response_buf: dict, assemblies: list[dict],
                          rows: list[Row], seen: set, cat_name: str,
                          variant_name: str, sp_name: str) -> None:
        """Click each assembly in the section → accept the prod-date confirm →
        capture GetIllustrationPartsJQ → emit one Row per part.

        Clicking an assembly SWITCHES the view to the illustration/parts detail
        (the assembly grid is hidden). So between assemblies we click the sp-category
        name in the breadcrumb to re-render the grid before clicking the next one.
        """
        asms = [a for a in assemblies if (a.get("categoryname") or "").strip()]
        if self.max_assemblies:
            asms = asms[: self.max_assemblies]
        compat = f"{cat_name} | {variant_name}"
        for idx, a in enumerate(asms):
            asm_name = (a.get("categoryname") or "").strip()
            asm_figno = (a.get("figno") or "").strip()
            # After the first drill we're on a parts-detail view; click the
            # sp-category breadcrumb to return to the assembly grid.
            if idx > 0:
                if not self._click_text(page, sp_name, wait=3000):
                    log.warning("        back-nav to %r grid failed at %d/%d; "
                                "stopping section drill", sp_name, idx + 1, len(asms))
                    break
            response_buf["GetIllustrationPartsJQ"].clear()
            if not self._click_text(page, asm_name, wait=2500):
                continue
            self._accept_confirm(page)
            # parts XHR may arrive a beat after the dialog accept
            if not response_buf["GetIllustrationPartsJQ"]:
                page.wait_for_timeout(2500)
            parts = self._parse(response_buf["GetIllustrationPartsJQ"])
            log.info("        assembly=%s (figno=%s) → %d parts", asm_name, asm_figno, len(parts))
            if not parts:
                continue
            structure = f"{cat_name} > {variant_name} > {sp_name} > {asm_name}"
            if asm_figno:
                structure += f" ({asm_figno})"
            for pt in parts:
                part_no = (pt.get("partNo") or "").strip()
                pdesc = (pt.get("description") or "").strip()
                if not part_no and not pdesc:
                    continue
                key = (part_no, structure)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(Row(
                    item_name=pdesc or asm_name,
                    item_code=part_no,
                    description=pdesc,
                    compatible_car_model=compat,
                    part_structure=structure,
                    start_date=self._fmt_date(pt.get("startDate")),
                    end_date=self._fmt_date(pt.get("endDate")),
                ))

    @staticmethod
    def _accept_confirm(page: Page) -> None:
        """Best-effort click of an in-page Yes/OK confirm (the prod-date dialog is
        sometimes an Angular modal rather than a native dialog). Native dialogs are
        auto-accepted by the page.on('dialog') handler; this covers the modal case."""
        try:
            page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button, a, .btn'));
                const b = btns.find(el => {
                    const t = (el.textContent||'').trim().toLowerCase();
                    return (t === 'yes' || t === 'ok' || t === 'proceed') && el.offsetParent !== null;
                });
                if (b) b.click();
            }""")
        except Exception:
            pass
        page.wait_for_timeout(1500)

    @staticmethod
    def _fmt_date(val) -> str | None:
        """Normalize an ISO datetime ('2025-06-01T00:00:00') to a date ('2025-06-01').
        Returns None for null/blank (legit for in-production parts)."""
        if not val:
            return None
        s = str(val).strip()
        return s.split("T", 1)[0] if "T" in s else (s or None)

    # ---------- helpers ----------

    def _renavigate_to_pv_root(self, page: Page, response_buf: dict) -> bool:
        """Re-navigate to FIGURE_URL and click 'Passenger Vehicles' from a fresh
        DOM state. Used between top-level category transitions to avoid the
        breadcrumb-back fragility observed on 2026-05-21 (18+ skip-cat failures
        in a single second after a few successful drills).
        """
        # ONE quick attempt only. The full 2026-06-13 production run showed the
        # deep part-drill corrupts the SPA session so a plain re-goto + PV click
        # fails on ~15 of 19 transitions — repeated retries reliably fail too and
        # just burn ~30s/transition. So we try once (it does recover on the
        # occasional light transition, e.g. BOLERO/LOGAN-VERITO) and let the
        # caller fall through to the fresh-login hard-reset when it fails.
        try:
            page.goto(FIGURE_URL, wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_timeout(8000)
        except Exception as exc:
            log.warning("  PV root re-nav: page.goto failed: %s", exc)
            return False
        response_buf["Fillcategory"].clear()
        if self._click_text(page, "Passenger Vehicles", wait=8000):
            return True
        log.info("  PV root re-nav: click failed → caller will hard-reset")
        return False

    @staticmethod
    def _parse(payloads: list[str]) -> list[dict]:
        """Pick the last captured payload, parse JSON array. Returns [] on no/bad data."""
        for p in reversed(payloads):
            try:
                arr = json.loads(p)
                if isinstance(arr, list):
                    return arr
            except Exception:
                continue
        return []

    @staticmethod
    def _click_text(page: Page, text: str, wait: int = 8000) -> bool:
        """Click element whose direct text matches exactly; climb to clickable parent."""
        ok = page.evaluate(f"""() => {{
            const els = Array.from(document.querySelectorAll('*'));
            for (const el of els) {{
                if (el.children.length > 0) continue;
                if ((el.textContent||'').trim() === {json.dumps(text)}) {{
                    let c = el;
                    for (let i=0;i<6 && c.parentElement;i++) {{
                        if (c.tagName === 'A' || c.tagName === 'BUTTON' || c.onclick ||
                            c.getAttribute('role') === 'button' ||
                            (c.className||'').includes('card') ||
                            (c.className||'').includes('mat-list-item')) break;
                        c = c.parentElement;
                    }}
                    c.click();
                    return true;
                }}
            }}
            return false;
        }}""")
        if ok:
            page.wait_for_timeout(wait)
        return ok

    # ---------- login ----------

    def _login_with_retry(self, page: Page, user: str, pwd: str, ocr) -> bool:
        for attempt in range(1, self.login_retries + 1):
            log.info("mahindra login attempt %d/%d", attempt, self.login_retries)
            try:
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(6000)
                if self._login_once(page, user, pwd, ocr):
                    return True
            except (PWTimeout, Exception) as e:
                log.warning("attempt err: %s", e)
        return False

    @staticmethod
    def _login_once(page: Page, user: str, pwd: str, ocr) -> bool:
        try:
            captcha_src = page.evaluate("""
                () => Array.from(document.querySelectorAll('img'))
                  .filter(m => m.src.startsWith('data:image') && m.width < 200 && m.height < 60 && m.offsetParent !== null)
                  .map(m => m.src)[0] || ''
            """)
            if not captcha_src:
                return False
            b64 = captcha_src.split(",", 1)[1]
            b64 += "=" * (4 - len(b64) % 4)
            captcha_text = ocr.classification(base64.b64decode(b64)).upper().strip()
            log.info("captcha OCR -> %r", captcha_text)
            if len(captcha_text) != 4:
                return False

            page.locator("mat-select").nth(1).click(timeout=3000)
            page.wait_for_timeout(1500)
            picked = page.evaluate("""
                () => {
                    const o = Array.from(document.querySelectorAll('mat-option, [role=option]'))
                      .find(o => (o.textContent||'').toLowerCase().includes('other user'));
                    if (o) { o.click(); return true; }
                    return false;
                }
            """)
            if not picked:
                return False
            page.wait_for_timeout(1500)
            page.keyboard.press("Escape")
            page.evaluate("() => document.querySelectorAll('.cdk-overlay-backdrop').forEach(b=>b.remove())")
            page.wait_for_timeout(500)

            page.locator("input[name=txtLoginname]").fill(user)
            page.locator("input[name=txtpassword]").fill(pwd)
            page.locator("input[name=captchacode]").fill(captcha_text)

            page.evaluate("() => document.querySelector('#btnEnter').click()")
            page.wait_for_timeout(10_000)
            return "home" in page.url
        except (PWTimeout, Exception) as e:
            log.warning("login_once err: %s", e)
            return False
