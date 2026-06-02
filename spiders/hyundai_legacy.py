"""SNAP-ON EPC spider — Hyundai entry point + shared cascade walker for Toyota.

Plan ref: v2.0 Hyundai + Toyota sheets. Both use snaponepc.com.
The Spider class is brand-agnostic — it uses `self.brand_key.upper()` as the env-var
prefix (HYUNDAI_USER / HYUNDAI_PASS / HYUNDAI_YEARS, or TOYOTA_USER / TOYOTA_PASS /
TOYOTA_YEARS). `spiders/toyota.py` simply re-exports this class.

URL: https://snaponepc.com/epc/  (dataset auto-selected by credentials)
xlsx fields: item_name, item_code, mrp, car_model.

Cascade verified:
  Login → Year → Model (auto-expands to variant when single)
        → Category (ENGINE/TRANSMISSION/...)
        → Illustration (20-201A etc.)
        → Parts AG-Grid (col-ids: calloutLabel=PNC, renderedDescription=Part Name,
          formattedPartNumber=Part Number, original_qty=Qty, FROMDATE, remarks, NOTES)

MRP is NOT in the parts grid — only in the picklist (cart) grid. xlsx requires MRP →
rows finalise as `partial` per BRD §7.

Per-brand MRP availability (verified 2026-05-19):
  - Hyundai: 100% MRP via col-id MOB_MRP_A (current dealer credential).
  - Toyota: MRP always empty server-side. /picklist/validatePart returns `prices:[]`
    for every part. Dealer's TKM_TOY price book is empty. Catalog browsing works;
    MRP requires a different Toyota dealer credential with populated TKM_TOY
    entries (Spinny escalation). See docs/per_site_notes.md §V2.3.

Env-var scoping (small defaults to keep first runs sane):
  HYUNDAI_YEARS                    default "2024"  — comma-sep
  HYUNDAI_MAX_MODELS_PER_YEAR      default 0 = all
  HYUNDAI_MAX_CATEGORIES           default 0 = all
  HYUNDAI_MAX_ILLUSTRATIONS        default 0 = all
"""

from __future__ import annotations

import logging
import os

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from lib.credentials import Credentials
from lib.snapon_epc import UA, LAUNCH_ARGS, INIT_SCRIPT
from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.hyundai")

# Defaults: last 5 years, all models / categories / illustrations.
# Override via env vars: <BRAND>_YEARS (CSV), <BRAND>_MAX_MODELS_PER_YEAR,
# <BRAND>_MAX_CATEGORIES, <BRAND>_MAX_ILLUSTRATIONS   (0 = all)
# Default years narrowed to the two latest model years (2026-05-25 user
# instruction: "always pick the latest years on. keep only 2026 and 2025
# only"). Toyota's full 5-year walk was taking ~5h and old years are
# rarely needed for refurb-cost estimation. Override via env var
# <BRAND>_YEARS to widen.
DEFAULT_YEARS = "2025,2026"
SETTLE_MS = 3500

# LEGACY MODULE — kept only as fallback reference; production uses snapon_rest.py.
# Credentials MUST be supplied via env vars (HYUNDAI_USER/PASS, TOYOTA_USER/PASS).
# No hardcoded fallbacks — public-repo policy.
FALLBACK_CREDS: dict[str, tuple[str, str]] = {}


class Spider(BaseSpider):

    def __init__(self, brand_key: str, brand_cfg: dict) -> None:
        super().__init__(brand_key, brand_cfg)
        prefix = brand_key.upper()
        self.years = [y.strip() for y in os.environ.get(f"{prefix}_YEARS", DEFAULT_YEARS).split(",") if y.strip()]
        self.max_models = int(os.environ.get(f"{prefix}_MAX_MODELS_PER_YEAR", "0") or "0")
        self.max_cats = int(os.environ.get(f"{prefix}_MAX_CATEGORIES", "0") or "0")
        self.max_ills = int(os.environ.get(f"{prefix}_MAX_ILLUSTRATIONS", "0") or "0")

    def crawl(self) -> list[Row]:
        # Startup breadcrumbs — these MUST log BEFORE any try/except so a silent
        # crash anywhere downstream still leaves a trace (added 2026-05-21 after
        # Toyota produced zero log output across a full 74-min parallel run; see
        # kickoff_checklist §I.I4).
        log.info("%s: spider crawl() entered", self.brand_key)
        fb_user, fb_pass = FALLBACK_CREDS.get(self.brand_key, ("", ""))
        creds = Credentials.load(self.brand_key) or Credentials(
            user=fb_user, password=fb_pass, brand=self.brand_key,
        )
        log.info(
            "%s: credentials resolved user=%r (pw_len=%d, source=%s)",
            self.brand_key,
            creds.user or "<empty>",
            len(creds.password or ""),
            "env" if os.environ.get(f"{self.brand_key.upper()}_USER") else "fallback",
        )
        if not creds.user:
            log.error("%s: no credentials available (set %s_USER / %s_PASS env vars)",
                      self.brand_key, self.brand_key.upper(), self.brand_key.upper())
            return []
        rows: list[Row] = []
        seen: set[str] = set()

        log.info("%s: launching playwright chromium", self.brand_key)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
            log.info("%s: chromium launched, creating context", self.brand_key)
            ctx = browser.new_context(
                user_agent=UA, viewport={"width": 1920, "height": 1080},
                locale="en-US", timezone_id="Asia/Kolkata",
            )
            page = ctx.new_page()
            page.add_init_script(INIT_SCRIPT)
            try:
                log.info("%s: attempting login as %r", self.brand_key, creds.user)
                if not self._login(page, creds.user, creds.password):
                    log.error("%s: login failed for user=%r", self.brand_key, creds.user)
                    return rows
                log.info("%s: login ok; years=%s", self.brand_key, self.years)

                for year in self.years:
                    self._reset(page)
                    if not self._click_text(page, year):
                        log.warning("year %s not clickable", year)
                        continue
                    models = self._thumbnails(page)
                    if self.max_models:
                        models = models[: self.max_models]
                    log.info("year=%s models=%d", year, len(models))

                    for model in models:
                        # Re-click year to fresh-state each model (cheaper than guessing back paths)
                        self._reset(page)
                        self._click_text(page, year)
                        if not self._click_thumbnail(page, model):
                            continue
                        # After model click, may auto-jump to variant; categories visible if
                        # ENGINE etc appear in the thumbnails list
                        post = self._thumbnails(page)
                        cats = [t for t in post if self._is_category(t)]
                        if not cats:
                            # Try clicking the first variant thumbnail to expose categories
                            if post and self._click_thumbnail(page, post[0]):
                                cats = [t for t in self._thumbnails(page) if self._is_category(t)]
                        if self.max_cats:
                            cats = cats[: self.max_cats]
                        log.info("  model=%s cats=%d", model[:30], len(cats))

                        # remember the model-page state to return to per category
                        cat_root_snapshot = self._snapshot(page)

                        for cat in cats:
                            if not self._click_thumbnail(page, cat):
                                continue
                            ills = self._thumbnails(page)
                            if self.max_ills:
                                ills = ills[: self.max_ills]

                            for ill in ills:
                                if not self._click_thumbnail(page, ill):
                                    continue
                                # Wait until parts-grid is rendered with at least one row
                                if not self._wait_parts_grid_ready(page):
                                    continue
                                parts = self._read_parts_grid(page)
                                if not parts:
                                    continue
                                # Add each part to picklist to get MRP
                                self._add_all_to_picklist(page)
                                mrp_by_code = self._read_picklist_mrps(page)
                                self._clear_picklist(page)

                                compat = f"{self.brand_label} {year} {model}".replace("\n", " ").strip()
                                for pt in parts:
                                    code = (pt.get("formattedPartNumber") or "").strip()
                                    name = (pt.get("renderedDescription") or "").strip()
                                    if not code or code in seen:
                                        continue
                                    seen.add(code)
                                    rows.append(Row(
                                        item_name=name or code,
                                        item_code=code,
                                        mrp=mrp_by_code.get(code),
                                        compatible_car_model=f"{compat} | {ill}",
                                    ))
                                # back to ill list
                                if not self._breadcrumb_up(page, count=1):
                                    self._click_thumbnail(page, cat) or self._restore(page, cat_root_snapshot)
                            # back to categories
                            self._breadcrumb_up(page, count=1)
                        # next model iteration resets via _reset+year+model
            finally:
                browser.close()
        log.info("hyundai: extracted %d unique parts", len(rows))
        return rows

    # ---------- helpers ----------

    @staticmethod
    def _login(page: Page, user: str, pwd: str) -> bool:
        page.goto("https://snaponepc.com/epc/", wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(SETTLE_MS)
        try:
            page.locator("input[type=text]").first.fill(user)
            page.locator("input[type=password]").first.fill(pwd)
            page.locator("button:has-text('Login')").first.click()
        except PWTimeout:
            return False
        page.wait_for_timeout(10_000)
        return "Logout" in page.locator("body").inner_text()

    @staticmethod
    def _click_text(page: Page, text: str) -> bool:
        """Click ANY element with given text (used for plain Year buttons)."""
        try:
            page.locator(f"text='{text}'").first.click(timeout=4_000)
            page.wait_for_timeout(SETTLE_MS)
            return True
        except (PWTimeout, Exception):
            return False

    @staticmethod
    def _click_thumbnail(page: Page, text: str) -> bool:
        """Click an <a[class*=thumbnail]> that contains exactly this text."""
        try:
            # text may contain ZWSP/special chars — use partial match if exact fails
            sel = page.locator(f"a[class*=thumbnail]").filter(has_text=text).first
            sel.click(timeout=4_000)
            page.wait_for_timeout(SETTLE_MS)
            return True
        except (PWTimeout, Exception):
            try:
                page.locator(f"text='{text}'").first.click(timeout=4_000)
                page.wait_for_timeout(SETTLE_MS)
                return True
            except (PWTimeout, Exception):
                return False

    @staticmethod
    def _thumbnails(page: Page) -> list[str]:
        return page.evaluate("""
            () => Array.from(document.querySelectorAll('a[class*=thumbnail]'))
                .map(a => a.textContent.replace(/\\s+/g, ' ').trim())
                .filter(t => t && t.length < 300)
        """)

    @staticmethod
    def _is_category(text: str) -> bool:
        """Categories — two known styles across SNAP-ON datasets:

        Hyundai: pure uppercase, letters + spaces only.
           e.g. ENGINE, TRANSMISSION, BODY, WIRE HARNESS REPAIR KIT.
        Toyota:  "N - UPPERCASE TEXT" with digit prefix.
           e.g. '1 - TOOL/ENGINE/FUEL GRP', '2 - PWRTRAIN/CHASSIS GRP', '3 - BODY GROUP'.

        Examples that FAIL:
          - 'ASV7#,AXVA70,AXVH71 (287320)' (sub-model — mixed chars + parens)
          - '20-201A - SUB ENGINE ASSY' (illustration — has lowercase 'CC' or digits embedded mid-word)
          - 'IHMIP0Y24 - VERNA 24 (2022-)' (variant — has parens)
        """
        if not text or not (2 < len(text) < 60):
            return False
        import re as _re
        # Hyundai style
        if _re.fullmatch(r"[A-Z][A-Z\s&/]+", text):
            return True
        # Toyota style: "1 - TEXT GRP"
        if _re.fullmatch(r"\d+\s*-\s*[A-Z][A-Z\s&/]+", text):
            return True
        return False

    @staticmethod
    def _breadcrumb_up(page: Page, count: int = 1) -> bool:
        """Click the breadcrumb segment 'count' positions before the last one."""
        try:
            crumbs = page.locator(".breadcrumb a, [class*=breadcrumb] a, nav a").all()
            if len(crumbs) >= count + 1:
                crumbs[-(count + 1)].click(timeout=3_000)
                page.wait_for_timeout(SETTLE_MS)
                return True
        except (PWTimeout, Exception):
            pass
        # Fallback: browser back
        try:
            for _ in range(count):
                page.go_back(timeout=4_000)
                page.wait_for_timeout(SETTLE_MS)
            return True
        except (PWTimeout, Exception):
            return False

    def _reset(self, page: Page) -> None:
        """Click brand breadcrumb to return to year list."""
        try:
            page.locator(f"text='{self.brand_label}'").first.click(timeout=3_000)
            page.wait_for_timeout(SETTLE_MS)
        except (PWTimeout, Exception):
            page.goto("https://snaponepc.com/epc/#/", timeout=10_000)
            page.wait_for_timeout(SETTLE_MS)

    @staticmethod
    def _snapshot(page: Page) -> str:
        return page.url

    @staticmethod
    def _restore(page: Page, url: str) -> bool:
        try:
            page.goto(url, timeout=10_000)
            page.wait_for_timeout(SETTLE_MS)
            return True
        except Exception:
            return False

    @staticmethod
    def _wait_parts_grid_ready(page: Page, timeout_ms: int = 10_000) -> bool:
        """Poll until parts-grid has at least one addToPicklist gridcell rendered."""
        sel = "ag-grid-angular.parts-grid add-to-picklist-renderer img"
        try:
            page.wait_for_selector(sel, timeout=timeout_ms, state="visible")
            page.wait_for_timeout(800)  # AG-Grid sometimes adds rows after initial render
            return True
        except (PWTimeout, Exception):
            return False

    @staticmethod
    def _add_all_to_picklist(page: Page) -> None:
        """Click each add-to-picklist <img>.

        AG-Grid uses virtual scrolling — rows below the fold have no renderer in DOM.
        So we click visible imgs, scroll the parts-grid viewport down, click new ones,
        repeat until no more new imgs appear.
        """
        sel = "ag-grid-angular.parts-grid add-to-picklist-renderer img"
        clicked_codes: set[str] = set()
        # Loop: scroll → click newly-rendered imgs → repeat
        for _ in range(20):  # safety cap on scroll iterations
            imgs = page.locator(sel)
            n = imgs.count()
            new_clicks = 0
            for i in range(n):
                try:
                    el = imgs.nth(i)
                    el.scroll_into_view_if_needed(timeout=1_500)
                    # Identify by sibling part-number cell text so we dedup across scrolls
                    row_code = el.evaluate("""(img) => {
                        const cell = img.closest('[role=gridcell]');
                        if (!cell) return '';
                        const row = cell.closest('[role=row]');
                        if (!row) return '';
                        const pn = row.querySelector('[col-id=formattedPartNumber]');
                        return pn ? pn.textContent.trim() : '';
                    }""")
                    if row_code in clicked_codes or not row_code:
                        continue
                    el.click(timeout=2_000)
                    clicked_codes.add(row_code)
                    new_clicks += 1
                    page.wait_for_timeout(900)  # SDK debounce — too fast = no-op
                except (PWTimeout, Exception):
                    continue
            # Scroll parts-grid viewport down to load more rows
            scrolled = page.evaluate("""() => {
                const grid = document.querySelector('ag-grid-angular.parts-grid');
                if (!grid) return false;
                const vp = grid.querySelector('.ag-body-viewport');
                if (!vp) return false;
                const before = vp.scrollTop;
                vp.scrollTop = before + 300;
                return vp.scrollTop !== before;
            }""")
            page.wait_for_timeout(600)
            if not scrolled and new_clicks == 0:
                break

    @staticmethod
    def _read_picklist_mrps(page: Page) -> dict[str, float]:
        """Read picklist grid → {part_number: MRP}.

        AG-Grid uses virtual scrolling for the picklist too — only visible rows are
        in the DOM. We scroll the picklist viewport from top to bottom, collecting
        rows along the way.
        """
        # MRP col-id varies by SNAP-ON tenant:
        #   Hyundai/Mobis: MOB_MRP_A
        #   Toyota:        MSRP (Suggested Retail) — may also have DEALER_COST
        rows = page.evaluate("""
            async () => {
                const g = document.querySelector('ag-grid-angular.picklist-grid');
                if (!g) return [];
                const vp = g.querySelector('.ag-body-viewport');
                if (!vp) return [];
                const PRICE_COLS = ['MOB_MRP_A', 'MSRP', 'DEALER_COST'];
                const collected = {};
                vp.scrollTop = 0;
                await new Promise(r => setTimeout(r, 300));
                for (let step = 0; step < 30; step++) {
                    const rows = Array.from(g.querySelectorAll('[role=row]'));
                    for (const r of rows) {
                        const cells = r.querySelectorAll('[col-id]');
                        const obj = {};
                        for (const c of cells) obj[c.getAttribute('col-id')] = (c.textContent||'').replace(/\\s+/g,' ').trim();
                        const code = obj.footerRowTotalColumnAlternate;
                        if (!code || code === 'Part Number') continue;
                        // Pick first non-empty price column
                        let price = '';
                        for (const col of PRICE_COLS) {
                            if (obj[col]) { price = obj[col]; break; }
                        }
                        collected[code] = price;
                    }
                    const before = vp.scrollTop;
                    vp.scrollTop = before + 50;
                    await new Promise(r => setTimeout(r, 200));
                    if (vp.scrollTop === before) break;
                }
                return Object.entries(collected);
            }
        """) or []
        out: dict[str, float] = {}
        for code, mrp in rows:
            if not code or not mrp or mrp in {"MRP A", "Suggested", "Dealer", ""}:
                continue
            try:
                out[code] = float(mrp.replace(",", ""))
            except (ValueError, AttributeError):
                continue
        return out

    @staticmethod
    def _clear_picklist(page: Page) -> None:
        """Click 'Clear' button to reset the picklist between illustrations."""
        try:
            # Look for the Clear button at the top of the picklist area
            page.locator("button:has-text('Clear')").first.click(timeout=2_000)
            page.wait_for_timeout(800)
            # Confirm dialog may appear
            try:
                page.locator("button:has-text('Yes')").first.click(timeout=1_500)
                page.wait_for_timeout(800)
            except (PWTimeout, Exception):
                pass
        except (PWTimeout, Exception):
            pass

    @staticmethod
    def _read_parts_grid(page: Page) -> list[dict]:
        """Read AG-Grid rows from the parts-grid (NOT the picklist-grid)."""
        return page.evaluate("""
            () => {
                const grid = document.querySelector('ag-grid-angular.parts-grid')
                          || document.querySelector('#partsGrid');
                if (!grid) return [];
                const rows = Array.from(grid.querySelectorAll('[role=row]'));
                const out = [];
                for (const r of rows) {
                    const cells = r.querySelectorAll('[col-id]');
                    if (!cells.length) continue;
                    const obj = {};
                    for (const c of cells) {
                        const col = c.getAttribute('col-id');
                        if (col) obj[col] = (c.textContent || '').replace(/\\s+/g, ' ').trim();
                    }
                    // Skip header row (calloutLabel === 'PNC')
                    if (obj.calloutLabel === 'PNC') continue;
                    // Real parts have a non-empty formattedPartNumber
                    if (!obj.formattedPartNumber) continue;
                    out.push(obj);
                }
                return out;
            }
        """) or []
