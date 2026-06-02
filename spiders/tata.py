"""TATA eCats spider — v2.0 OEM.

Site: https://www.tatamotorsecats.com (ASP.NET WebForms, requires login).
xlsx fields: item_name, item_code, mrp, compatible_car_model.

Auth (env vars TATA_USER / TATA_PASS — credentials supplied separately, see .env.example).

Crawl strategy (verified 2026-05-19):
  1. POST login at /frmTATALogin.aspx
  2. Cascade through 7 dropdowns at /frmTATAModelSearch.aspx:
       Division (PV-INDIA) → ModelCategory → Model → Chassis → VC → Description → EngineType
     Each level triggers an ASP.NET partial postback; programmatically iterate combinations.
  3. Click 'Go to Catalogue' → lands at /frmtataadminmodelnew.aspx?ID=<modelId>
  4. Catalogue page shows 21 categories via TreeView (00-ENGINE … 83-AIRCON).
     For each category, fire __doPostBack('ctl00$ContentPlaceHolder1$trvEPCDetails', 't<m>\\<c>')
     to load that category's illustrations.
  5. Each illustration is rendered as <a href="javascript:funredirectType('<illId>')">.
     That maps to direct URL:  /frmtataartbomnew.aspx?ID=1_<illId>
     (no postback needed beyond category expand).
  6. Parts table on the artbom page has columns:
       ITEM | PART NO. (= item_code) | PART DESCRIPTION (= item_name)
       | QTY | RATE (= mrp) | REMARKS

compatible_car_model is derived from breadcrumb:
  "ARIA 2.2L DICOR BS-III | 28700328R | 614001 | 00-ENGINE | 00.00.01A - ENGINES"

Env vars (small defaults for first runs):
  TATA_MAX_MODELS_PER_CATEGORY  default 1 (0 = all). Limits Model dropdown iteration.
  TATA_MAX_VCS_PER_MODEL        default 1.
  TATA_MAX_CATEGORIES           default 1.
  TATA_MAX_ILLUSTRATIONS        default 5.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

from parsel import Selector
from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from lib.credentials import Credentials
from lib.snapon_epc import UA, LAUNCH_ARGS, INIT_SCRIPT  # reuse anti-detection args
from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.tata")

LOGIN_URL = "https://www.tatamotorsecats.com/frmTATAModelSearch.aspx"
ARTBOM_URL = "https://www.tatamotorsecats.com/frmtataartbomnew.aspx?ID=1_{illust_id}"
SETTLE_MS = 4500


@dataclass
class _Ctx:
    model_id: str
    breadcrumb: str  # "ARIA 2.2L DICOR BS-III | 28700328R | 614001"


class Spider(BaseSpider):

    def __init__(self, brand_key: str, brand_cfg: dict) -> None:
        super().__init__(brand_key, brand_cfg)
        self.max_models = int(os.environ.get("TATA_MAX_MODELS_PER_CATEGORY", "1") or "1")
        self.max_vcs = int(os.environ.get("TATA_MAX_VCS_PER_MODEL", "1") or "1")
        # Default to 0 (all 21 parts categories: ENGINE, CLUTCH, GEARBOX,
        # SUSPENSION, BRAKES, etc.) — 2026-05-26 bumped from 1.
        self.max_cats = int(os.environ.get("TATA_MAX_CATEGORIES", "0") or "0")
        self.max_ills = int(os.environ.get("TATA_MAX_ILLUSTRATIONS", "5") or "5")

    def crawl(self) -> list[Row]:
        creds = Credentials.load("tata")
        if creds is None or not creds.user:
            log.error("tata: missing credentials — set TATA_USER and TATA_PASS env vars (see .env.example)")
            return []
        rows: list[Row] = []
        seen: set[tuple[str, str]] = set()  # (item_code, compatible_car_model)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
            ctx = browser.new_context(
                user_agent=UA, viewport={"width": 1920, "height": 1080},
                locale="en-US", timezone_id="Asia/Kolkata",
            )
            page = ctx.new_page()
            page.add_init_script(INIT_SCRIPT)
            try:
                if not self._login(page, creds.user, creds.password):
                    log.error("login failed")
                    return rows
                log.info("login ok")

                # Walk cascade. For smoke runs, only pick the first option at each level.
                # 2026-05-26: removed all redundant `_select` calls before `_iter_dropdown` —
                # `_iter_dropdown` itself selects the parent dropdown, so the explicit `_select`
                # before it caused double postbacks. Symptom: catalogue tree only rendered
                # 1 of 21 categories (verified by `state/probe_tata_double_select.py`).
                self._select(page, "drpDivision", "PASSENGER VEHICLES - INDIA")
                model_categories = self._dropdown_opts(page, "drpModelCategory")
                log.info("model categories: %d", len(model_categories))

                for mc in model_categories:
                    self._select(page, "drpModelCategory", mc)
                    models = self._dropdown_opts(page, "drpModel")
                    if self.max_models:
                        models = models[: self.max_models]
                    log.info("  category=%s → models=%d", mc, len(models))

                    for model in models:
                        self._select(page, "drpModel", model)
                        chassis_list = self._dropdown_opts(page, "drpChassis")
                        log.info("    model=%s → chassis=%d", model, len(chassis_list))

                        for chassis in chassis_list:
                            self._select(page, "drpChassis", chassis)
                            vcs = self._dropdown_opts(page, "drpVC")
                            if self.max_vcs:
                                vcs = vcs[: self.max_vcs]

                            for vc in vcs:
                                self._select(page, "drpVC", vc)
                                # Description + EngineType — pick first; description may have many,
                                # but typically all map to the same model.
                                descs = self._dropdown_opts(page, "drpDescription")
                                if not descs:
                                    continue
                                self._select(page, "drpDescription", descs[0])
                                engs = self._dropdown_opts(page, "drpEnginetype")
                                if not engs:
                                    continue
                                # EngineType select can be touchy — use raw event dispatch
                                self._select_via_event(page, "drpEnginetype", engs[0])

                                if not self._go_to_catalogue(page):
                                    continue

                                model_id = self._extract_model_id(page.url)
                                if not model_id:
                                    log.warning("could not extract model_id from %s", page.url)
                                    self._back_to_search(page)
                                    continue
                                bc = f"{model} | {vc} | {chassis} | {descs[0]}"
                                _ctx = _Ctx(model_id=model_id, breadcrumb=bc)
                                log.info("      catalogue model_id=%s for %s", model_id, bc)

                                cats = self._catalogue_categories(page)
                                if self.max_cats:
                                    cats = cats[: self.max_cats]
                                log.info("      categories on catalogue: %d", len(cats))

                                for (cat_text, cat_arg) in cats:
                                    ill_ids = self._expand_and_get_illustration_ids(page, cat_arg, cat_text)
                                    if self.max_ills:
                                        ill_ids = ill_ids[: self.max_ills]
                                    log.info("        cat=%s → ills=%d", cat_text, len(ill_ids))

                                    for (ill_id, ill_text) in ill_ids:
                                        car_model = f"{bc} | {cat_text} | {ill_text}"
                                        for part in self._fetch_parts(page, ill_id):
                                            code = part["item_code"]
                                            key = (code, car_model)
                                            if not code or key in seen:
                                                continue
                                            seen.add(key)
                                            rows.append(Row(
                                                item_name=part["item_name"] or code,
                                                item_code=code,
                                                mrp=part["mrp"],
                                                compatible_car_model=car_model,
                                            ))
                                self._back_to_search(page)
            finally:
                browser.close()
        log.info("tata: extracted %d unique parts", len(rows))
        return rows

    # ---------- login + cascade ----------

    @staticmethod
    def _login(page: Page, user: str, pwd: str) -> bool:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(3000)
        try:
            page.locator("input[name=txtUserName]").fill(user)
            page.locator("input[name=txtPassword]").fill(pwd)
            page.locator("input#btnLogin").click()
        except PWTimeout:
            return False
        page.wait_for_timeout(8_000)
        if "ModelSearch" not in page.url:
            try:
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(4000)
            except PWTimeout:
                return False
        return "drpDivision" in page.content()

    @staticmethod
    def _back_to_search(page: Page) -> None:
        """Return to Model Search after one combination crawl."""
        # Broader exception catch (was just PWTimeout). chrome-error navigation
        # interrupts surface as plain Playwright Error, not PWTimeout. Returning
        # quietly here lets the outer loop carry on to the next VC/chassis.
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(4000)
        except Exception as e:
            log.debug("back_to_search err (continuing): %s", e)

    @staticmethod
    def _dropdown_opts(page: Page, dd_name: str) -> list[str]:
        return page.evaluate(f"""() => {{
            const s = document.querySelector('select[name="ctl00$ContentPlaceHolder1${dd_name}"]');
            if (!s) return [];
            return Array.from(s.options).map(o => o.text.trim()).filter(t => t && t !== '--SELECT--');
        }}""") or []

    def _iter_dropdown(self, page: Page, parent_dd: str, target_dd: str, first_label: str) -> list[str]:
        """After selecting parent, return target's option list."""
        if first_label:
            try:
                page.select_option(f'select[name="ctl00$ContentPlaceHolder1${parent_dd}"]', label=first_label)
                page.wait_for_timeout(SETTLE_MS)
            except (PWTimeout, Exception):
                pass
        return self._dropdown_opts(page, target_dd)

    @staticmethod
    def _select(page: Page, dd_name: str, label: str) -> None:
        try:
            page.select_option(f'select[name="ctl00$ContentPlaceHolder1${dd_name}"]', label=label)
            page.wait_for_timeout(SETTLE_MS)
        except (PWTimeout, Exception) as e:
            log.debug("select %s=%s failed: %s", dd_name, label, e)

    @staticmethod
    def _select_via_event(page: Page, dd_name: str, label: str) -> None:
        page.evaluate(f"""() => {{
            const s = document.querySelector('select[name="ctl00$ContentPlaceHolder1${dd_name}"]');
            if (!s) return;
            for (let i = 0; i < s.options.length; i++) {{
                if (s.options[i].text.trim() === {json.dumps(label)}) {{
                    s.selectedIndex = i;
                    s.dispatchEvent(new Event('change', {{bubbles: true}}));
                    break;
                }}
            }}
        }}""")
        page.wait_for_timeout(SETTLE_MS)

    @staticmethod
    def _go_to_catalogue(page: Page) -> bool:
        for sel in ["input[name*='btnGo' i]", "input[id*='btnGo' i]",
                    "input[value*='Go to Catalogue' i]"]:
            try:
                page.locator(sel).first.click(timeout=3000)
                page.wait_for_timeout(10_000)
                return "adminmodelnew" in page.url
            except (PWTimeout, Exception):
                continue
        return False

    # ---------- catalogue + tree ----------

    @staticmethod
    def _extract_model_id(url: str) -> str | None:
        m = re.search(r"[?&]ID=(\d+)", url)
        return m.group(1) if m else None

    @staticmethod
    def _catalogue_categories(page: Page) -> list[tuple[str, str]]:
        """Return [(category_text, anchor_id), ...] for the 21 top-level categories.

        Returns the anchor's DOM id (not the postback arg) — caller clicks the
        anchor by id to invoke the inline onclick handler with the correctly
        escaped postback arg (browser-managed JS string parsing).
        """
        return page.evaluate("""() => {
            const out = [];
            const seenText = new Set();
            const anchors = Array.from(document.querySelectorAll('a[href*="trvEPCDetails"]'));
            for (const a of anchors) {
                const m = a.href.match(/__doPostBack\\('[^']+','([^']+)'\\)/);
                if (!m) continue;
                const arg = m[1];
                const txt = (a.textContent || '').trim();
                // Categories: text matches "NN - UPPERCASE NAME" and arg is "s<parentId>\\<childId>"
                if (/^\\d{2}\\s*-\\s*[A-Z][A-Z &/]+$/.test(txt) && arg.startsWith('s') && arg.indexOf('\\\\') > 0) {
                    if (seenText.has(txt)) continue;
                    if (!a.id) continue;
                    seenText.add(txt);
                    out.push([txt, a.id]);  // return the anchor's id; caller clicks it
                }
            }
            return out;
        }""") or []

    @staticmethod
    def _expand_and_get_illustration_ids(page: Page, anchor_id: str, cat_text: str) -> list[tuple[str, str]]:
        """Click the category anchor by DOM id; parse the delta response for
        illustration IDs.

        Returns [(illustration_id_str, illustration_text), ...] for this category.

        2026-05-26: clicks anchor element directly via JS .click() instead of
        synthesizing __doPostBack with a captured arg. The captured arg from
        a.href has 2 literal backslashes which ASP.NET event-validation rejects;
        clicking the anchor invokes the inline onclick handler with the
        correctly-escaped string (1 backslash at runtime).
        """
        delta_body: dict[str, str] = {}

        def on_response(r):
            try:
                ajax = r.request.headers.get("x-microsoftajax", "")
            except Exception:
                ajax = ""
            if ajax == "Delta=true" and "adminmodelnew" in r.url:
                try:
                    delta_body["body"] = r.text()
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            clicked = page.evaluate(
                "id => { const a = document.getElementById(id); if (!a) return false; a.click(); return true; }",
                anchor_id,
            )
            if not clicked:
                log.warning("expand %s: anchor #%s not found", cat_text, anchor_id)
                return []
            page.wait_for_timeout(6_000)
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass

        body = delta_body.get("body", "")
        if not body:
            log.warning("expand %s: no delta response captured", cat_text)
            return []
        # 2026-05-26 rewrite: the old single-regex pattern
        #   funredirectType\(\s*'([^']+)'\s*\)[^>]*>([^<]+)</a>
        # never matched against the UpdatePanel delta body — extra attributes
        # / whitespace / closing quotes between the `funredirectType(NNN)` call
        # and the anchor's text broke `[^>]*`. New approach:
        #   1. Find all funredirectType(<id>) occurrences (loose, verified to work
        #      against real bodies: 27 hits per ENGINE category).
        #   2. For each, walk backwards to the anchor's opening tag, then forward
        #      to its closing </a> to extract the displayed text.
        loose_pattern = re.compile(r"funredirectType\(\s*'([^']+)'\s*\)")
        ill_pairs: list[tuple[str, str]] = []
        for m in loose_pattern.finditer(body):
            ill_id = m.group(1)
            # Walk back from the funredirectType match to the <a tag opening
            a_open = body.rfind("<a ", 0, m.start())
            if a_open < 0:
                continue
            # Forward to the closing > of the opening tag
            tag_end = body.find(">", m.end())
            if tag_end < 0:
                continue
            # Forward to next </a>
            close = body.find("</a>", tag_end)
            if close < 0:
                continue
            ill_text = body[tag_end + 1:close].strip()
            # Strip any nested tags from the text (rare; defensive)
            ill_text = re.sub(r"<[^>]+>", "", ill_text).strip()
            if not ill_text:
                continue
            ill_pairs.append((ill_id, ill_text))

        # No category-offset filter: clicking the category anchor returns a
        # delta scoped to that category's illustrations only (verified: 27
        # funredirectType anchors per ENGINE category, all engine-specific).
        return ill_pairs

    # ---------- parts table ----------

    @staticmethod
    def _fetch_parts(page: Page, ill_id: str) -> list[dict]:
        """GET frmtataartbomnew.aspx?ID=1_<ill_id> and parse the parts table.

        2026-05-26: switched from page.goto to ctx.request.get to avoid
        chrome-error navigation interrupts. The artbom URL is a plain ASP.NET
        page with parts data in HTML — no JS rendering needed. Using the
        request context preserves auth cookies but skips browser navigation.

        Columns: ITEM | PART NO. | PART DESCRIPTION | QTY | RATE | REMARKS
        """
        url = ARTBOM_URL.format(illust_id=ill_id)
        try:
            r = page.context.request.get(url, timeout=30_000)
        except Exception as e:
            log.warning("fetch_parts ill=%s request err: %s", ill_id, e)
            return []
        if r.status != 200:
            log.warning("fetch_parts ill=%s status=%d", ill_id, r.status)
            return []
        html = r.text()
        sel = Selector(text=html)
        # Find tables containing the parts header (cells: ITEM, PART NO., PART DESCRIPTION, QTY, RATE, REMARKS).
        # The artbom page has nested layout tables; same parts rows can appear in multiple tables.
        # Dedupe within the page on (part_no, desc).
        parts: list[dict] = []
        seen: set[tuple[str, str]] = set()
        # 2026-05-26: parts-table detection switched from `tr:first-child`-only
        # header check to full-text-of-table check. The artbom layout now wraps
        # the parts table inside extra layout rows, so the header is no longer
        # the table's first <tr>. Searching the table's normalized text reliably
        # finds it (verified: 73 illustrations × 3-4 parts each).
        for table in sel.css("table"):
            full_text = table.xpath("normalize-space(.)").get() or ""
            if "PART NO." not in full_text or "PART DESCRIPTION" not in full_text:
                continue
            for tr in table.css("tr"):
                cells = [c.strip() for c in tr.css("td").xpath("normalize-space(.)").getall() if c is not None]
                row = [c for c in cells if c]
                if len(row) < 5:
                    continue
                # Part number = first 8-14 digit cell
                pn_idx = next((i for i, c in enumerate(row) if re.fullmatch(r"\d{8,14}", c)), None)
                if pn_idx is None or pn_idx + 3 >= len(row):
                    continue
                part_no = row[pn_idx]
                desc = row[pn_idx + 1]
                if desc in ("PART DESCRIPTION", "Description"):
                    continue
                qty = row[pn_idx + 2]
                rate_raw = row[pn_idx + 3] if pn_idx + 3 < len(row) else ""
                key = (part_no, desc)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    mrp = float(rate_raw.replace(",", "")) if rate_raw else None
                except ValueError:
                    mrp = None
                parts.append({
                    "item_code": part_no,
                    "item_name": desc,
                    "qty": qty,
                    "mrp": mrp,
                })
        return parts
