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
            ctx = browser.new_context(
                user_agent=UA, viewport={"width": 1920, "height": 1080},
                locale="en-US", timezone_id="Asia/Kolkata",
            )
            page = ctx.new_page()
            page.add_init_script(INIT_SCRIPT)

            # Capture FigureSearch API responses — these contain category/variant/assembly data
            response_buf: dict[str, list[str]] = {
                "Fillcategory": [], "FillCategoryCountryModel": [],
                "FillCatModelWithOutCountry": [], "FillAssembly": [],
            }

            def on_resp(r):
                if "/webapi/api/FigureSearch/" not in r.url:
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

            page.on("response", on_resp)

            try:
                if not self._login_with_retry(page, creds.user, creds.password, ocr):
                    log.error("mahindra login failed")
                    return rows
                log.info("mahindra login ok")

                page.goto(FIGURE_URL, wait_until="domcontentloaded", timeout=20_000)
                page.wait_for_timeout(8000)

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
                            log.error("[%d/%d] PV root re-nav failed; aborting Mahindra crawl",
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

    # ---------- helpers ----------

    def _renavigate_to_pv_root(self, page: Page, response_buf: dict) -> bool:
        """Re-navigate to FIGURE_URL and click 'Passenger Vehicles' from a fresh
        DOM state. Used between top-level category transitions to avoid the
        breadcrumb-back fragility observed on 2026-05-21 (18+ skip-cat failures
        in a single second after a few successful drills).
        """
        log.info("  re-nav to PV root")
        try:
            page.goto(FIGURE_URL, wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_timeout(8000)
        except Exception as exc:
            log.warning("  PV root re-nav: page.goto failed: %s", exc)
            return False
        response_buf["Fillcategory"].clear()
        if not self._click_text(page, "Passenger Vehicles", wait=8000):
            log.warning("  PV root re-nav: 'Passenger Vehicles' click failed")
            return False
        return True

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
