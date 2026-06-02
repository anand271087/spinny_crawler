"""FORD Microcat spider — v2.0 OEM.

Site: https://microcat-apac.superservice.com (Infomedia Microcat MARKET SPA).
xlsx fields: item_name, item_code, compatible_car_model (NO MRP).
Auth env vars: FORD_USER / FORD_PASS — credentials supplied separately (see .env.example).

Crawl strategy (verified 2026-05-19):

1. Login via login.superservice.com (email + password) → redirects back to Microcat,
   which sets a JWT in localStorage AND attaches it as `Authorization: Bearer <jwt>`
   on every backend call via an Angular HTTP interceptor. We capture this JWT from
   the first authenticated request and reuse it via page.request.get.

2. APIs (all under https://microcat-apac.superservice.com/ver/microcat/epc-html/):

   a) /v2/history/vehicles?market=IN&page=0&size=40
      → list of vehicles the account has previously identified. Each entry has a
      `vehicleId` (base64-encoded protobuf) which is the entry-point token.
      Spinny has ~15-40 vehicles in history (FIGO EC, ECOSPORT BW, FIGO CDU, …).

   b) /v3/section/<vehicleId>/children?market=IN&language=en&id=<sectionId>
      → list of sections under a node. id=-1 returns top-level (11 sections for FIGO EC:
      A BODY, B FRONT AXLE, … Z ACCESSORIES). Each entry has `illustrated:bool` —
      false = expandable group, true = leaf with parts.

   c) /v1/part/<vehicleId>/sectionparts/<leafId>?imageIndex=0&language=en&market=IN
      → parts for a single illustrated leaf. Response has
      `catalogWithParts.parts[]` — each part has `label`, `partIdentifications.partFormats`,
      `qty`, etc.

3. Per part:
     item_name   ← part.label   (e.g. "BODYSHELL - PRIMED - LESS CLOSURES")
     item_code   ← partFormats[key=partnumber].value, fallback to finis or engineering
     compatible_car_model ← "<catalogName> | <parent section path> | <leaf label>"

4. NO MRP — xlsx confirms Ford does not require MRP. priceData.partPriceList is empty
   for this credentials' subscription anyway.

SCOPE — credentials are dealer-scoped:
- /v1/catalog/list returns 15 Ford catalogs (including MUSTANG, MONDEO, ENDEAVOUR, FIGO,
  FIESTA, ECOSPORT, IKON, FUSION, ESCORT). But you need a vehicleId to enter the section
  tree — vehicleId is VIN-derived and only available via vehicle history (or VIN lookup).
- Vehicle History covers ~3-4 catalogs effectively (FIGO EC, ECOSPORT BW, FIGO CDU,
  variants thereof). The other ~11 catalogs are unreachable without VINs.
- Spinny can either (a) accept this 4-catalog subset, (b) feed a curated VIN list,
  or (c) negotiate broader catalog access.

Env vars (small defaults for first runs):
  FORD_MAX_VEHICLES         default 1 (0 = all in history)
  FORD_MAX_LEAVES_PER_CAT   default 3 (0 = all illustrated leaves per top-section)
  FORD_MAX_TOP_SECTIONS     default 1 (0 = all 11)
"""

from __future__ import annotations

import json
import logging
import os
from urllib.parse import quote

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from lib.credentials import Credentials
from lib.snapon_epc import UA, LAUNCH_ARGS, INIT_SCRIPT
from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.ford")

URL = ("https://microcat-apac.superservice.com/content/microcat-epc/#/identify"
       "?appName=Microcat_EPC&subscription=DYN000000000ED944F"
       "&subscriptionAssignment=DYN000000001563B26")
BASE = "https://microcat-apac.superservice.com/ver/microcat/epc-html"
AFX = "f331ff7e"
SETTLE_MS = 8000


class Spider(BaseSpider):

    def __init__(self, brand_key: str, brand_cfg: dict) -> None:
        super().__init__(brand_key, brand_cfg)
        self.max_vehicles = int(os.environ.get("FORD_MAX_VEHICLES", "1") or "1")
        self.max_top_sections = int(os.environ.get("FORD_MAX_TOP_SECTIONS", "1") or "1")
        self.max_leaves = int(os.environ.get("FORD_MAX_LEAVES_PER_CAT", "3") or "3")

    def crawl(self) -> list[Row]:
        creds = Credentials.load("ford")
        if creds is None or not creds.user:
            log.error("ford: missing creds — set FORD_USER and FORD_PASS env vars (see .env.example)")
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

            auth_headers: dict[str, str] = {}

            def on_request(req):
                if "/ver/microcat/" in req.url and "authorization" in req.headers and not auth_headers:
                    auth_headers.update({
                        k: v for k, v in req.headers.items()
                        if k.lower() in {"authorization", "x-ifm-sid", "x-ifm-session-id",
                                         "x-ifm-franchise", "accept"}
                    })

            page.on("request", on_request)

            try:
                if not self._login(page, creds.user, creds.password):
                    log.error("ford login failed")
                    return rows
                if not auth_headers:
                    log.error("ford: no Bearer JWT captured after login")
                    return rows
                log.info("ford login + auth captured")

                def api_get(path: str) -> tuple[int, str]:
                    r = page.request.get(f"{BASE}{path}", headers=auth_headers)
                    return r.status, r.text()

                # 1. Vehicle history → list of usable vehicles
                st, body = api_get(
                    f"/v2/history/vehicles?market=IN&language=en&page=0&size=40&afx={AFX}"
                )
                if st != 200:
                    log.error("ford history failed: %s", st)
                    return rows
                vehicles = json.loads(body).get("content", [])
                if not vehicles:
                    log.error("ford: empty vehicle history")
                    return rows
                if self.max_vehicles:
                    vehicles = vehicles[: self.max_vehicles]
                log.info("ford: %d vehicles to crawl", len(vehicles))

                for v in vehicles:
                    vid = v["vehicleId"]
                    cat_name = v["catalogName"]
                    vin = v.get("searchCriteria", "")
                    vid_q = quote(vid, safe="")
                    log.info("vehicle %s (%s) %s", cat_name, vin, vid[:40])

                    # 2. Top-level sections
                    st, body = api_get(self._section_path(vid_q, "-1"))
                    if st != 200:
                        log.warning("section/-1 for %s: %s", vin, st)
                        continue
                    top_sections = json.loads(body)
                    if self.max_top_sections:
                        top_sections = top_sections[: self.max_top_sections]

                    for top in top_sections:
                        # Recursive DFS to illustrated leaves
                        leaves_collected = 0
                        for leaf, path in self._iter_illustrated_leaves(api_get, vid_q, top, path_prefix=top["label"]):
                            if self.max_leaves and leaves_collected >= self.max_leaves:
                                break
                            leaves_collected += 1
                            # Fetch parts
                            st, body = api_get(
                                f"/v1/part/{vid_q}/sectionparts/{quote(leaf['id'])}"
                                f"?imageIndex=0&language=en&market=IN"
                                f"&showNonApplicable=false&showOrbSupersession=false"
                                f"&interpretationAttributes=&afx={AFX}"
                            )
                            if st != 200:
                                continue
                            parts_resp = json.loads(body)
                            parts = parts_resp.get("catalogWithParts", {}).get("parts", []) or []
                            log.info("  %s → %d parts", path, len(parts))

                            compat = f"{cat_name} | {path}"
                            for pt in parts:
                                code = self._part_code(pt)
                                name = pt.get("label") or pt.get("tooltipLabel") or ""
                                if not code:
                                    continue
                                key = (code, compat)
                                if key in seen:
                                    continue
                                seen.add(key)
                                rows.append(Row(
                                    item_name=name.strip() or code,
                                    item_code=code.strip(),
                                    compatible_car_model=compat,
                                ))
            finally:
                browser.close()

        log.info("ford: %d unique parts", len(rows))
        return rows

    # ---------- helpers ----------

    @staticmethod
    def _section_path(vid_q: str, section_id: str) -> str:
        return (
            f"/v3/section/{vid_q}/children?market=IN&language=en"
            f"&showNonApplicable=false&useLegacySectionUserNote=false"
            f"&id={quote(section_id)}&afx={AFX}"
        )

    def _iter_illustrated_leaves(self, api_get, vid_q, node, path_prefix, depth=0):
        """DFS: yield (leaf_node, label_path) for every illustrated:true descendant."""
        if depth > 6:
            return
        if node.get("illustrated"):
            yield node, path_prefix
            return
        # Expand this node's children
        st, body = api_get(self._section_path(vid_q, node["id"]))
        if st != 200:
            return
        children = json.loads(body) or []
        for c in children:
            new_path = f"{path_prefix} > {c['label']}"
            yield from self._iter_illustrated_leaves(api_get, vid_q, c, new_path, depth + 1)

    @staticmethod
    def _part_code(pt: dict) -> str:
        """Extract item_code from part.partIdentifications.partFormats[]."""
        formats = pt.get("partIdentifications", {}).get("partFormats") or []
        by_key = {f.get("key"): f.get("value") for f in formats if f.get("value")}
        # Preference: partnumber > finis > engineering
        return (by_key.get("partnumber") or by_key.get("finis") or by_key.get("engineering") or "").strip()

    @staticmethod
    def _login(page: Page, user: str, pwd: str) -> bool:
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(5000)
            page.locator("input[type=email]").first.fill(user)
            page.locator("input[type=password]").first.fill(pwd)
            page.locator("button:has-text('SIGN IN'), button[type=submit]").first.click()
            page.wait_for_timeout(15_000)
            return "microcat-apac" in page.url
        except (PWTimeout, Exception) as e:
            log.error("ford login error: %s", e)
            return False
