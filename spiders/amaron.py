"""AMARON spider — cascading_dropdown pattern, all via httpx (no Playwright needed).

Plan ref §12.7. URL: https://www.amaron.com/passenger-vehicles
xlsx steps: Make × Model × Fuel cascade → extract battery details.
xlsx fields: item_name, item_code, mrp.

Site reality (verified 2026-05-18):
- The landing page is a Drupal form with 3 selects + a "Find Now" submit.
- Drupal AJAX endpoint cascades Make → Model → Fuel:
  POST https://www.amaron.com/passenger-vehicles?ajax_form=1&_wrapper_format=drupal_ajax
  Response: JSON command array; `insert` commands carry HTML <option> lists for the next select.
- The form's "Find Now" submit POSTs to /passenger-vehicles (non-AJAX) and 302-redirects to:
  https://www.amaron.com/battery/passengers/<make-slug>/<model-slug>/<fuel-slug>
- That result page has a `proComparisionTable` with battery cards.
- Each battery card has: <span class="bold-font">AMARON FLO Automotive Battery - 40B20L (AAM-FL-00040B20L)</span>
  → item_name = text before `(`, item_code = text inside `()`.
- MRP rows in the same comparison table.

Strategy:
1. GET landing page → extract `form_build_id`, `vehicle-type`, and all 32 Makes.
2. For each Make: AJAX POST → parse Model options.
3. For each Model: AJAX POST → parse Fuel options.
4. For each Fuel: form POST with op=Find Now → follow 302 redirect → parse battery rows.

Volume: 32 makes × ~10 models avg × ~2 fuels ≈ 640 final page fetches + ~330 cascade calls.
Runtime: ~3-4 min sequential.
"""

from __future__ import annotations

import logging
import re

import httpx
from parsel import Selector

from lib.normalize import clean_mrp
from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.amaron")

USER_AGENT = "SpinnyOEMCrawler/1.0 (contact@spinny.com)"
BASE = "https://www.amaron.com"
LANDING_URL = f"{BASE}/passenger-vehicles"
AJAX_URL = f"{BASE}/passenger-vehicles?ajax_form=1&_wrapper_format=drupal_ajax"
RESULT_URL_TPL = f"{BASE}/battery/passengers/{{make}}/{{model}}/{{fuel}}"

OPTION_RE = re.compile(r'<option[^>]*value=[\'"](\d+)[\'"][^>]*>([^<]+)</option>')
# Battery card label: "AMARON FLO Automotive Battery - 40B20L (AAM-FL-00040B20L)"
BATTERY_LABEL_RE = re.compile(r"^(.*?)\s*\(([A-Z0-9\-_]+)\)\s*$")
SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return SLUG_RE.sub("-", text.lower()).strip("-")


class Spider(BaseSpider):

    def crawl(self) -> list[Row]:
        rows: list[Row] = []
        seen_codes: set[str] = set()
        with httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=30,
        ) as client:
            hidden, makes = self._bootstrap(client)
            log.info("amaron: %d makes found", len(makes))
            for make_value, make_label in makes:
                try:
                    models = self._cascade(client, hidden, "vehicle-make",
                                            {"vehicle-make": make_value, "model": "", "fuel": ""},
                                            "edit-model")
                except Exception as exc:
                    log.warning("amaron: make=%s failed at model fetch: %s", make_label, exc)
                    continue
                log.info("amaron: make=%s models=%d", make_label, len(models))
                for model_value, model_label in models:
                    try:
                        fuels = self._cascade(client, hidden, "model",
                                              {"vehicle-make": make_value, "model": model_value, "fuel": ""},
                                              "edit-fuel")
                    except Exception as exc:
                        log.warning("amaron: %s/%s failed at fuel fetch: %s",
                                    make_label, model_label, exc)
                        continue
                    for fuel_value, fuel_label in fuels:
                        try:
                            new_rows = self._fetch_batteries(
                                client, hidden, make_value, model_value, fuel_value,
                                make_label, model_label, fuel_label, seen_codes,
                            )
                            rows.extend(new_rows)
                        except Exception as exc:
                            log.warning("amaron: %s/%s/%s failed at result fetch: %s",
                                        make_label, model_label, fuel_label, exc)
                            continue
            log.info("amaron: %d unique batteries extracted", len(rows))
        return rows

    @staticmethod
    def _bootstrap(client: httpx.Client) -> tuple[dict, list[tuple[str, str]]]:
        r = client.get(LANDING_URL)
        r.raise_for_status()
        sel = Selector(r.text)
        fbi = sel.css('input[name="form_build_id"]::attr(value)').get()
        vt = sel.css('input[name="vehicle-type"]::attr(value)').get() or "5618"
        hidden = {
            "vehicle-type": vt,
            "form_build_id": fbi,
            "form_id": "batteries_info",
        }
        make_select_html = sel.css('select[name="vehicle-make"]').get() or ""
        makes = [
            (v, t.strip()) for v, t in OPTION_RE.findall(make_select_html)
            if v and t.strip().lower() not in ("select", "")
        ]
        return hidden, makes

    @staticmethod
    def _cascade(client: httpx.Client, hidden: dict, triggering: str,
                 fields: dict, target_select_id: str) -> list[tuple[str, str]]:
        """POST AJAX cascade; return option (value, label) list for target select."""
        payload = dict(hidden)
        payload.update(fields)
        payload["_triggering_element_name"] = triggering
        payload["_drupal_ajax"] = "1"
        payload["ajax_page_state[theme]"] = "amaron_theme"
        payload["ajax_page_state[libraries]"] = ""
        resp = client.post(
            AJAX_URL, data=payload,
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
        )
        resp.raise_for_status()
        target_html = ""
        for cmd in resp.json():
            if cmd.get("command") == "update_build_id":
                hidden["form_build_id"] = cmd.get("new", hidden["form_build_id"])
            elif f"#{target_select_id}" in cmd.get("selector", ""):
                target_html = cmd.get("data", "") or ""
        return [
            (v, t.strip()) for v, t in OPTION_RE.findall(target_html)
            if v and t.strip().lower() != "select"
        ]

    def _fetch_batteries(self, client: httpx.Client, hidden: dict,
                         make_value: str, model_value: str, fuel_value: str,
                         make_label: str, model_label: str, fuel_label: str,
                         seen_codes: set[str]) -> list[Row]:
        """GET the direct battery result URL from labels (lowercase, hyphenated)."""
        url = RESULT_URL_TPL.format(
            make=_slug(make_label),
            model=_slug(model_label),
            fuel=_slug(fuel_label),
        )
        try:
            resp = client.get(url)
        except httpx.HTTPError:
            return []
        # 500 = no battery for this combo (Amaron's server returns 500 instead of empty); skip
        if resp.status_code != 200:
            return []
        sel = Selector(resp.text)
        # Each card label is in <span class="bold-font"> within the comparison table.
        labels = [s.strip() for s in sel.css("span.bold-font::text").getall() if "AAM-" in s]
        # MRPs sit in a tr whose first cell text is "MRP" or "Best Online Price" etc.
        mrps = self._extract_mrp_row(sel)
        rows: list[Row] = []
        compat = f"{make_label} {model_label} ({fuel_label})"
        for i, label in enumerate(labels):
            m = BATTERY_LABEL_RE.match(label)
            if not m:
                continue
            item_name = m.group(1).strip()
            item_code = m.group(2).strip()
            # Dedup per (battery, vehicle) — NOT globally on item_code. A battery
            # SKU fits many vehicles; the old global dedup kept only the first
            # vehicle per SKU (~52 rows total) and discarded the full compatibility
            # matrix. Keep every (SKU × make × model × fuel) combination.
            key = (item_code, compat)
            if key in seen_codes:
                continue
            seen_codes.add(key)
            mrp = mrps[i] if i < len(mrps) else None
            rows.append(Row(
                item_name=item_name,
                item_code=item_code,
                mrp=clean_mrp(mrp),
                compatible_car_model=compat,
            ))
        return rows

    @staticmethod
    def _extract_mrp_row(sel) -> list[str]:
        """Find the 'Base Price (Inclusive of GST)' row in the comparison table.

        Amaron labels MRP as 'Base Price (Inclusive of GST)'. We return the per-battery
        td texts in order, filtered to those starting with ₹.
        """
        for tr in sel.css("table.comparisionTable tr"):
            label = (tr.css("th").xpath("string(.)").get() or "").strip().lower()
            if "base price" in label or label.startswith("mrp"):
                cells = [t.xpath("string(.)").get("").strip() for t in tr.css("td")]
                return [c for c in cells if c.startswith("₹")]
        return []
