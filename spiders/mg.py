"""MG (Morris Garages) ServiceConnect spider — v2.0 OEM.

Site: https://serviceconnect.mgmotorindia.com/epc/login
Platform: Intelli Catalogue v11.0.0 — same login + drill chain as Mahindra
(spiders/mahindra.py), with two differences:
- Login form has NO User Type dropdown (only Language).
- The top-level model list is exposed via FillcategoryType (account is already
  PV-scoped — 7 MG India models: Hector, ZS EV, Gloster, ASTOR, COMET, WINDSOR,
  MAJESTOR), so we click a model directly (skipping the "Passenger Vehicles" step).

xlsx fields: item_name, item_code, compatible_car_model (no MRP).
Auth env vars: MG_USER / MG_PASS — supplied separately.

CAPTCHA: same offline ddddocr bypass as Mahindra.

Crawl chain (same FigureSearch APIs as Mahindra):
1. Login (captcha OCR + #btnEnter).
2. Navigate /epc/figure-search → SPA calls /api/FigureSearch/FillcategoryType
   which returns the 7 MG models directly.
3. For each model: click → POST FillCategoryCountryModel  → variants
4. For each variant: click → POST FillCatModelWithOutCountry → spare-part categories
5. For each sp-category: click → POST FillAssembly → assemblies (the items per xlsx)
6. Each assembly row: item_name=categoryname, item_code=figno, car_model="model|variant|sp"

Env vars — defaults are "representative" scope (production-ready, ~40 min,
fits inside BRD §8 8h SLA). Override individual knobs for smoke or full scope.
  MG_LOGIN_RETRIES        default 6
  MG_MAX_MODELS           default 0 (=all 7 MG India PV models)
  MG_MAX_VARIANTS         default 1 (=1 variant + 1 sub-variant per recursion)
  MG_MAX_SP_CATEGORIES    default 0 (=all 7 sections per sub-variant)

Scope presets:
  Smoke (CI/dev, ~90s for 2 models, ~50 rows):
    MG_MAX_MODELS=2 MG_MAX_VARIANTS=1 MG_MAX_SP_CATEGORIES=1
  Representative (production, ~40 min, ~2,000 rows):  ← default
    MG_MAX_MODELS=0 MG_MAX_VARIANTS=1 MG_MAX_SP_CATEGORIES=0
  Full (~12h, exceeds 8h SLA — needs B8 sign-off + split runs):
    MG_MAX_MODELS=0 MG_MAX_VARIANTS=0 MG_MAX_SP_CATEGORIES=0

Volume math: 7 PV models × N variants × N sub-variants × M sections × ~40
assemblies per leaf at ~45s per leaf round-trip (8s click + ~8s back-nav
+ assembly parse time amortized). At N=1, M=all: 7 × 1 × 1 × 7 = 49 leaves.
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

log = logging.getLogger("spiders.mg")

LOGIN_URL = "https://serviceconnect.mgmotorindia.com/epc/login"
FIGURE_URL = "https://serviceconnect.mgmotorindia.com/epc/figure-search"


class Spider(BaseSpider):

    def __init__(self, brand_key: str, brand_cfg: dict) -> None:
        super().__init__(brand_key, brand_cfg)
        self.login_retries = int(os.environ.get("MG_LOGIN_RETRIES", "6") or "6")
        # Defaults below are "representative" scope (production-ready); see docstring.
        self.max_models = int(os.environ.get("MG_MAX_MODELS", "0") or "0")
        self.max_variants = int(os.environ.get("MG_MAX_VARIANTS", "1") or "1")
        self.max_sp = int(os.environ.get("MG_MAX_SP_CATEGORIES", "0") or "0")
        # Part-level drill (2026-06-13): same GetIllustrationPartsJQ mechanism as
        # Mahindra. Set MG_PART_LEVEL=0 to revert to assembly-grain rows.
        self.part_level = bool(int(os.environ.get("MG_PART_LEVEL", "1") or "1"))
        self.max_assemblies = int(os.environ.get("MG_MAX_ASSEMBLIES", "0") or "0")

    def crawl(self) -> list[Row]:
        try:
            import ddddocr
        except ImportError:
            log.error("ddddocr not installed; run `pip install ddddocr`")
            return []

        creds = Credentials.load("mg")
        if creds is None or not creds.user:
            log.error("mg: missing credentials — set MG_USER and MG_PASS env vars (see .env.example)")
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

            # MG step-API names differ from Mahindra. Track ALL FigureSearch responses
            # and use the most-recent non-empty list-response as the "level result".
            # Always-skipped helper endpoints that we don't want to confuse for data.
            SKIP = {"GetUserNote", "GetModelDate"}
            last_list_response: dict[str, str] = {"body": ""}
            top_models: dict[str, str] = {"body": ""}
            parts_buf: dict[str, str] = {"body": ""}

            def on_resp(r):
                if "/webapi/api/" not in r.url:
                    return
                ct = r.headers.get("content-type", "")
                if not any(t in ct for t in ("json", "text")):
                    return
                # endpoint name (last URL segment without query)
                ep = r.url.split("?")[0].rsplit("/", 1)[-1]
                # Part-level table (Illustration controller) — capture separately.
                if ep == "GetIllustrationPartsJQ":
                    try:
                        parts_buf["body"] = r.text()
                    except Exception:
                        pass
                    return
                if "/webapi/api/FigureSearch/" not in r.url:
                    return
                if ep in SKIP:
                    return
                try:
                    body = r.text()
                except Exception:
                    return
                # Stash FillcategoryType (the top-level model list) separately
                if ep == "FillcategoryType":
                    top_models["body"] = body
                    return
                # Anything else with a JSON array of dicts containing 'categoryname'
                # is a level-data response.
                try:
                    arr = json.loads(body)
                    if isinstance(arr, list) and arr and any("categoryname" in (x or {}) for x in arr):
                        last_list_response["body"] = body
                except Exception:
                    pass

            page.on("response", on_resp)
            # Assembly click fires a native "search parts with prod date" confirm.
            page.on("dialog", lambda d: d.accept())

            try:
                if not self._login_with_retry(page, creds.user, creds.password, ocr):
                    log.error("mg login failed")
                    return rows
                log.info("mg login ok")

                page.goto(FIGURE_URL, wait_until="domcontentloaded", timeout=20_000)
                page.wait_for_timeout(10000)

                models_raw = self._parse_one(top_models["body"])
                model_names = [m.get("categoryname") for m in models_raw
                               if m.get("categoryname") and not m.get("inactive")]
                if self.max_models:
                    model_names = model_names[: self.max_models]
                log.info("MG models: %d total, crawling %d", len(models_raw), len(model_names))

                def drill_click(label: str) -> list[dict]:
                    last_list_response["body"] = ""
                    if not self._click_text(page, label):
                        return []
                    return self._parse_one(last_list_response["body"])

                def recurse_to_assemblies(entries: list[dict], path: list[str], depth: int):
                    """Recursive depth-first drill. When figno is populated, entries are
                    assemblies — extract them. Otherwise drill into each.

                    `depth` is the current level count; cap at MG_MAX_DEPTH (default 8)
                    to avoid runaways.
                    """
                    if depth > int(os.environ.get("MG_MAX_DEPTH", "8") or "8"):
                        return
                    # If any entry has figno populated, these ARE assemblies.
                    if entries and any(e.get("figno") for e in entries):
                        if self.part_level:
                            self._drill_assemblies(page, parts_buf, entries, rows,
                                                   seen, path)
                        else:
                            for a in entries:
                                name = (a.get("categoryname") or "").strip()
                                code = (a.get("figno") or "").strip()
                                if not name or not code:
                                    continue
                                compat = " | ".join(["MG"] + path)
                                key = (code, compat)
                                if key in seen:
                                    continue
                                seen.add(key)
                                rows.append(Row(item_name=name, item_code=code, compatible_car_model=compat))
                        return
                    # Otherwise drill into each entry (respecting limits per level)
                    limit_map = {1: self.max_variants, 2: self.max_variants,
                                 3: self.max_sp, 4: self.max_sp}
                    limit = limit_map.get(depth, self.max_sp)
                    children_names = [e.get("categoryname") for e in entries if e.get("categoryname")]
                    if limit:
                        children_names = children_names[:limit]
                    log.info("%s  depth=%d  %d entries, drilling %d",
                             "  " * depth, depth, len(entries), len(children_names))
                    for child in children_names:
                        sub = drill_click(child)
                        recurse_to_assemblies(sub, path + [child], depth + 1)
                        # navigate back up — click parent (last in path)
                        if path:
                            self._click_text(page, path[-1], wait=3500)

                for model_idx, model_name in enumerate(model_names, start=1):
                    log.info("[%d/%d] %s — elapsed %.0fs, %d rows so far",
                             model_idx, len(model_names), model_name,
                             time.time() - t_start, len(rows))
                    variants = drill_click(model_name)
                    recurse_to_assemblies(variants, [model_name], 1)
                    # Back to model list for next model
                    self._click_text(page, "FIGURE SEARCH", wait=3500)
                    # If above fails, re-navigate
                    page.goto(FIGURE_URL, wait_until="domcontentloaded", timeout=20_000)
                    page.wait_for_timeout(6000)
            finally:
                browser.close()
        elapsed = time.time() - t_start
        log.info("mg: %d items extracted in %.0fs (%.1f min)",
                 len(rows), elapsed, elapsed / 60)
        return rows

    # ---------- part-level drill ----------

    def _drill_assemblies(self, page: Page, parts_buf: dict, assemblies: list[dict],
                          rows: list[Row], seen: set, path: list[str]) -> None:
        """Click each assembly → accept the prod-date confirm → capture
        GetIllustrationPartsJQ → emit one Row per part. Clicking an assembly
        switches to the parts-detail view, so between assemblies we click the
        section breadcrumb (path[-1]) to re-render the assembly grid.
        """
        section = path[-1] if path else None
        asms = [a for a in assemblies if (a.get("categoryname") or "").strip()]
        if self.max_assemblies:
            asms = asms[: self.max_assemblies]
        compat = " | ".join(["MG"] + path)
        for idx, a in enumerate(asms):
            asm_name = (a.get("categoryname") or "").strip()
            asm_figno = (a.get("figno") or "").strip()
            if idx > 0 and section:
                if not self._click_text(page, section, wait=3000):
                    log.warning("      back-nav to %r grid failed at %d/%d; "
                                "stopping section drill", section, idx + 1, len(asms))
                    break
            parts_buf["body"] = ""
            if not self._click_text(page, asm_name, wait=2500):
                continue
            self._accept_confirm(page)
            if not parts_buf["body"]:
                page.wait_for_timeout(2500)
            parts = self._parse_one(parts_buf["body"])
            log.info("      assembly=%s (figno=%s) → %d parts", asm_name, asm_figno, len(parts))
            if not parts:
                continue
            structure = " > ".join(["MG"] + path + [asm_name])
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
        """Best-effort click of an in-page Yes/OK confirm (modal variant). Native
        dialogs are auto-accepted by the page.on('dialog') handler."""
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
        if not val:
            return None
        s = str(val).strip()
        return s.split("T", 1)[0] if "T" in s else (s or None)

    # ---------- helpers ----------

    @staticmethod
    def _parse_one(body: str) -> list[dict]:
        if not body:
            return []
        try:
            arr = json.loads(body)
            return arr if isinstance(arr, list) else []
        except Exception:
            return []

    @staticmethod
    def _click_text(page: Page, text: str, wait: int = 8000) -> bool:
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
            log.info("mg login attempt %d/%d", attempt, self.login_retries)
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

            # NO User Type dropdown on MG (unlike Mahindra).
            page.locator("input[name=txtLoginname]").fill(user)
            page.locator("input[name=txtpassword]").fill(pwd)
            page.locator("input[name=captchacode]").fill(captcha_text)
            page.evaluate("() => document.querySelector('#btnEnter').click()")
            page.wait_for_timeout(10_000)
            return "/login" not in page.url
        except (PWTimeout, Exception) as e:
            log.warning("login_once err: %s", e)
            return False
