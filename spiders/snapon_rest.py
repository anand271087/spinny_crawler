"""SNAP-ON EPC REST spider — replaces the brittle Playwright UI-clicking one.

Background (2026-05-30):
  The legacy hyundai.py spider drove an Angular SPA via Playwright AG-Grid
  clicks. Failure modes hit in production:
    - Page navigation race in _add_all_to_picklist (crashed Hyundai main run)
    - Silent hang after first model (caused Toyota to lose all 3 attempts)
    - Year-tab not clickable when dealer subscription scope changes
    - 20-min session timeouts un-recoverable mid-crawl

Discovery (state/probe_snapon_*.py):
  SNAP-ON's Angular SPA is a thin client over a clean REST API at
  /epc-services/. The auth requires two custom JWT headers (sbsepc5s, sbsepc5cs)
  set by the SPA at runtime — we capture them once via a 15-second Playwright
  login, then make all data calls via httpx.

Architecture:
  1. Playwright opens /epc/, logs in, captures sbsepc5s + sbsepc5cs from the
     first /auth/account request headers. Browser closes immediately after.
  2. httpx.Client carries those headers + AWS ALB stickiness cookies.
  3. Walk the navigation tree DFS: Dataset → Year → Model → Catalog → Group →
     Section (leaf). Each level returns a JSON tree with `childNodes[].serializedPath`
     (base64 cursor) to drill in.
  4. At each leaf section, GET /pages/parts/<sp>/filterRequest/<fr> returns
     `partItems[]` with partNumber, description, manufacturer, quantity, etc.

Performance vs legacy:
  - Legacy Hyundai: ~10-20 min for 168 rows, fragile
  - REST: ~2-3 min for 1000s of rows, no UI dependencies after login
  - 50-100× faster + immune to AG-Grid race conditions
  - Single Playwright login = no 20-min session expiry to recover from

This module is shared by Hyundai and Toyota — they're identical SNAP-ON
deployments under different dataset IDs. The `brand_key` from config picks
the credential and the dataset.

xlsx fields per BRD:
  - Hyundai: item_name, item_code, mrp, compatible_car_model
  - Toyota:  item_name, item_code, mrp, compatible_car_model

MRP STATUS (2026-06-03): MRP fetch is DISABLED in this iteration. Rows ship
without `mrp` populated (crawl_status=partial). What we learned:
  - The MRP comes from a per-part call to /epc-services/picklist/validatePart/
    datasetId/<ds>/filterRequest/<fr>/partId/<pid>/partItemId/<piid>, which
    returns `prices[]` with priceType keys (MOB_LIST, MOB_MRP_A..E, MSRP).
    We want MOB_MRP_A — the standard Hyundai dealer MRP.
  - The validatePart filterRequest MUST include `equipmentRefId=<catalog_id>`
    — the numeric ID of the parent "Catalog" level node from the navigation
    tree (e.g. 7649 = IHMIP0Y24 - VERNA 24).
  - It also requires the `amg: <userId>` header.
  - HOWEVER: even with the right URL, equipmentRefId, amg header, and JWT
    tokens, httpx GETs return 400. The SAME endpoint returns 200 when called
    from inside the live Playwright session (via SPA click). Likely cause is
    server-side TLS/fingerprint or session-state check we haven't isolated.
  - Probe artifacts at state/probe_mrp_*.py, state/snapon_picklist_xhrs.jsonl,
    state/validatepart_headers.json. The catalog-id tracking + parallel-fetch
    plumbing is still in the spider (commented as inactive) so the next
    person can wire MRP fetch quickly once the fingerprint/session piece is
    cracked. See per_site_notes §V2.2 for the open question.
"""
from __future__ import annotations

import base64
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import httpx
from playwright.sync_api import sync_playwright

from spiders._base import BaseSpider, Row

log = logging.getLogger("spiders.snapon_rest")

BASE = "https://snaponepc.com"
LOGIN_TIMEOUT_MS = 30_000
POST_LOGIN_WAIT_MS = 10_000
ACCOUNT_WAIT_MS = 4_000  # let /auth/account fire so we capture its headers

# Credentials MUST be supplied via env vars: HYUNDAI_USER, HYUNDAI_PASS,
# TOYOTA_USER, TOYOTA_PASS. See .env.example for the template.
# No hardcoded fallbacks — public-repo policy.
FALLBACK_CREDS: dict[str, tuple[str, str]] = {}


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _resolve_creds(brand_key: str) -> tuple[str, str]:
    env_u = os.environ.get(f"{brand_key.upper()}_USER")
    env_p = os.environ.get(f"{brand_key.upper()}_PASS")
    if env_u and env_p:
        return env_u, env_p
    if brand_key in FALLBACK_CREDS:
        return FALLBACK_CREDS[brand_key]
    raise RuntimeError(f"no credentials for brand={brand_key}")


def _playwright_login(brand_key: str, user: str, password: str) -> tuple[dict, dict]:
    """Returns (auth_headers_dict, cookies_dict). Opens + closes Playwright in <30s."""
    captured: dict = {}

    def on_request(req):
        # Capture sbsepc5s + sbsepc5cs from the first /auth/account call
        if not captured and req.url.endswith("/epc-services/auth/account"):
            captured.update({k.lower(): v for k, v in req.headers.items()})

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = ctx.new_page()
        page.on("request", on_request)

        log.info("%s: playwright login start", brand_key)
        page.goto(f"{BASE}/epc/", wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS)
        # Sometimes SNAP-ON's bundle takes ~7-10s to render the form
        page.wait_for_selector("input[type=text]", state="visible", timeout=20_000)
        page.wait_for_timeout(1500)
        page.locator("input[type=text]").first.fill(user)
        page.locator("input[type=password]").first.fill(password)
        page.locator("button:has-text('Login')").first.click()
        page.wait_for_timeout(POST_LOGIN_WAIT_MS)

        body = page.locator("body").inner_text()
        if "Logout" not in body:
            browser.close()
            raise RuntimeError(f"{brand_key}: login failed (no Logout link)")

        # Give the SPA a moment to fire /auth/account so we capture headers
        page.wait_for_timeout(ACCOUNT_WAIT_MS)
        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        browser.close()

    auth_headers = {
        k: captured[k]
        for k in ("sbsepc5s", "sbsepc5cs", "x-client-version")
        if k in captured
    }
    if "sbsepc5s" not in auth_headers or "sbsepc5cs" not in auth_headers:
        raise RuntimeError(
            f"{brand_key}: failed to capture session tokens "
            f"(captured keys: {list(captured.keys())[:10]})"
        )
    log.info("%s: tokens captured (sbsepc5s, sbsepc5cs, x-client-version)", brand_key)
    return auth_headers, cookies


class Spider(BaseSpider):
    """Shared REST spider for Hyundai + Toyota. Brand picked from brand_key."""

    def __init__(self, brand_key: str, brand_cfg: dict) -> None:
        super().__init__(brand_key, brand_cfg)
        # Caps useful for smoke tests; default 0 = all (production).
        self.max_years = int(os.environ.get(f"{brand_key.upper()}_MAX_YEARS", "0") or "0")
        self.max_models_per_year = int(
            os.environ.get(f"{brand_key.upper()}_MAX_MODELS", "0") or "0"
        )
        self.max_leaves_per_branch = int(
            os.environ.get(f"{brand_key.upper()}_MAX_LEAVES", "0") or "0"
        )

    def crawl(self) -> list[Row]:
        log.info("%s: REST spider crawl() entered", self.brand_key)
        t_start = time.time()

        user, password = _resolve_creds(self.brand_key)
        log.info("%s: credentials resolved user=%r", self.brand_key, user)

        # 1. Login + extract tokens
        auth_headers, cookies = _playwright_login(self.brand_key, user, password)

        # 2. httpx session for all data calls
        headers = {
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "referer": f"{BASE}/epc/",
            "accept": "application/json, text/plain, */*",
            **auth_headers,
        }
        with httpx.Client(
            headers=headers, cookies=cookies, timeout=45, follow_redirects=True
        ) as client:
            return self._crawl_via_rest(client, t_start)

    def _crawl_via_rest(self, client: httpx.Client, t_start: float) -> list[Row]:
        # 3. Get account info (userId)
        r = client.post(f"{BASE}/epc-services/auth/account")
        r.raise_for_status()
        acct = r.json()
        user_id = acct.get("userDetails", {}).get("userId", "")
        if not user_id:
            raise RuntimeError(f"{self.brand_key}: no userId in /auth/account response")

        # Set the `amg` header (= userId) for all subsequent requests. The SPA
        # adds this on /picklist/* calls — without it, validatePart returns 400
        # (rest of the API tolerates it being missing).
        client.headers["amg"] = user_id

        # 4. Get datasetSettings (has priceBookId per dataset)
        r = client.post(f"{BASE}/epc-services/settings/user")
        r.raise_for_status()
        settings = r.json()
        ds_settings_list = settings.get("datasetSettings", []) or []
        # Map dataset_id → priceBookId
        pb_by_ds = {s["datasetId"]: s.get("priceBookId", "") for s in ds_settings_list}

        # 5. List datasets (just to confirm)
        r = client.get(f"{BASE}/epc-services/datasets")
        r.raise_for_status()
        datasets = r.json()
        if not datasets:
            raise RuntimeError(f"{self.brand_key}: no datasets returned")
        ds = datasets[0]  # SNAP-ON returns one per credential
        ds_id = ds["id"]
        price_book_id = pb_by_ds.get(ds_id, "")
        log.info(
            "%s: dataset=%s priceBook=%s user=%s",
            self.brand_key, ds_id[:8], price_book_id[:8], user_id[:8],
        )

        filter_raw = (
            f"jobId=1|dataSetId={ds_id}|manualFiltersEnabled=true|"
            f"locale=en-US|busReg=IND|priceBookId={price_book_id}|userId={user_id}"
        )
        fr = _b64(filter_raw)

        # 6. DFS walk of navigation tree, accumulating rows at leaf sections
        rows: list[Row] = []
        seen_part_ids: set[str] = set()

        # Stack of (serialized_path, breadcrumb_list, catalog_id)
        # catalog_id = numeric id of the parent "Catalog" level node, required
        # for the validatePart MRP fetch's filterRequest. None until we descend
        # past the Catalog level.
        stack: list[tuple[str, list[str], Optional[str]]] = [("", [], None)]
        years_visited = 0
        leaves_visited = 0
        mrp_lookups = 0
        mrp_hits = 0

        while stack:
            sp, crumb, catalog_id = stack.pop()
            nav = self._get_navigation(client, ds_id, sp, fr)
            if nav is None:
                continue
            kids = nav.get("children", {}) or {}
            child_nodes = kids.get("childNodes", []) or []
            level = kids.get("childLevelTitle", "?")

            if not child_nodes:
                # Empty navigations response — usually means leaf with no UI tree
                # (parts are at the same path under /pages/parts/)
                continue

            # At the Year level, sort DESCENDING (newest year first) so that
            # MAX_YEARS=1 picks 2026 instead of one of the older years. SNAP-ON
            # natively returns descending already but we sort explicitly to be
            # robust against per-tenant ordering changes.
            if level == "Year":
                def _year_key(n):
                    nm = (n.get("name") or "").strip()
                    return -int(nm) if nm.isdigit() else 0
                child_nodes = sorted(child_nodes, key=_year_key)  # newest first
                years_visited += 1

            for i, node in enumerate(child_nodes):
                # Limit how many year branches we push to the stack. With
                # descending sort, i=0 is the newest year. break-at-N keeps
                # only the N newest.
                if level == "Year" and self.max_years and i >= self.max_years:
                    break
                # Cap on models within a year (smoke)
                if (level == "Model"
                    and self.max_models_per_year
                    and i >= self.max_models_per_year):
                    break

                new_crumb = crumb + [node.get("name", "?")]
                node_sp = node.get("serializedPath", "")

                # When entering a Catalog level node, capture its numeric id
                # for the MRP fetch. Pass down through the rest of the walk.
                new_catalog_id = catalog_id
                if level == "Catalog":
                    new_catalog_id = str(node.get("id", "") or "") or catalog_id

                if node.get("leafNode"):
                    if self.max_leaves_per_branch and leaves_visited >= self.max_leaves_per_branch:
                        break
                    leaves_visited += 1
                    parts = self._get_parts(client, ds_id, node_sp, fr)
                    # Filter out parts we've already seen (dedup on partId)
                    new_parts = []
                    for p in parts:
                        pid = p.get("partId", "")
                        if pid and pid in seen_part_ids:
                            continue
                        if pid:
                            seen_part_ids.add(pid)
                        new_parts.append(p)

                    # MRP fetch is DISABLED — endpoint returns 400 outside the
                    # live SPA session (see module docstring "MRP STATUS"). The
                    # plumbing below remains as a launch-point for the next pass.
                    mrp_map: dict[str, Optional[float]] = {}
                    # Re-enable when the SPA-session check is cracked:
                    # if new_parts and new_catalog_id:
                    #     fr_leaf = self._build_filter_request(
                    #         ds_id, price_book_id, user_id, new_catalog_id)
                    #     self._register_parts_for_picklist(
                    #         client, ds_id, node_sp, fr_leaf, new_parts)
                    #     mrp_map = self._fetch_mrps_parallel(
                    #         client, ds_id, fr_leaf, new_parts)
                    #     mrp_lookups += len(new_parts)
                    #     mrp_hits += sum(1 for v in mrp_map.values() if v is not None)

                    for p in new_parts:
                        pid = p.get("partId", "")
                        rows.append(Row(
                            item_name=(p.get("description") or "").strip(),
                            item_code=(p.get("formattedPartNumber") or p.get("partNumber") or "").strip(),
                            mrp=mrp_map.get(pid),
                            compatible_car_model=" > ".join(new_crumb[:5]),  # cap breadcrumb depth
                        ))
                else:
                    # Branch — push to stack to recurse
                    stack.append((node_sp, new_crumb, new_catalog_id))

            # Heartbeat at year boundary
            if level == "Year":
                log.info(
                    "%s: walked level=%s name=%r — elapsed %.0fs, %d unique parts",
                    self.brand_key, level, crumb[-1] if crumb else "(root)",
                    time.time() - t_start, len(rows),
                )

        elapsed = time.time() - t_start
        log.info(
            "%s: REST extracted %d unique parts (%d leaves walked, "
            "%d MRP lookups, %d MRP hits) in %.0fs",
            self.brand_key, len(rows), leaves_visited,
            mrp_lookups, mrp_hits, elapsed,
        )
        return rows

    # ---------- helpers ----------

    @staticmethod
    def _build_filter_request(
        ds_id: str, price_book_id: str, user_id: str, equipment_ref_id: str
    ) -> str:
        """Build the base64-encoded filterRequest including the catalog/equipmentRefId.

        The validatePart endpoint REQUIRES equipmentRefId — the standard navigation
        filterRequest (without it) works for /navigations/ and /pages/parts/ but
        returns 200 with no `prices` field for /picklist/validatePart.
        """
        raw = (
            f"jobId=1|dataSetId={ds_id}|manualFiltersEnabled=true|"
            f"equipmentRefId={equipment_ref_id}|locale=en-US|busReg=IND|"
            f"priceBookId={price_book_id}|userId={user_id}"
        )
        return _b64(raw)

    def _register_parts_for_picklist(
        self,
        client: httpx.Client,
        ds_id: str,
        sp: str,
        fr_leaf: str,
        parts: list[dict],
    ) -> None:
        """POST the parts list to /userContentIndicators to register them in
        the picklist session. Without this prior POST, validatePart returns 400.

        Body = JSON array of minimal part descriptors. We only need partNumber,
        partId, manufacturer (the other fields can be null).
        """
        url = (
            f"{BASE}/epc-services/datasets/{ds_id}/pages/parts/{sp}/"
            f"filterRequest/{fr_leaf}/userContentIndicators"
        )
        payload = [
            {
                "partNumber": (p.get("partNumber") or "").strip(),
                "partId": (p.get("partId") or "").strip(),
                "manufacturer": (p.get("manufacturer") or "").strip(),
                "indicators": None,
                "partNotes": None,
                "partNoteType": None,
            }
            for p in parts
            if p.get("partId")
        ]
        try:
            r = client.post(url, json=payload, headers={"content-type": "application/json"})
        except Exception as e:
            log.debug("userContentIndicators POST err: %s", e)
            return
        if r.status_code not in (200, 204):
            log.debug("userContentIndicators POST → %d", r.status_code)

    def _fetch_mrps_parallel(
        self,
        client: httpx.Client,
        ds_id: str,
        fr_leaf: str,
        parts: list[dict],
        max_workers: int = 8,
    ) -> dict[str, Optional[float]]:
        """For each part in `parts`, call /picklist/validatePart and extract
        the MOB_MRP_A price. Returns {partId: mrp_value_or_None}.

        Parallelized via ThreadPoolExecutor. httpx.Client is thread-safe for
        sync usage so we can share the same client across worker threads. The
        validatePart endpoint can be slow (~400-700ms) so 8 workers give a
        ~5-7x speedup vs serial.
        """
        result: dict[str, Optional[float]] = {}

        def fetch_one(part: dict) -> tuple[str, Optional[float]]:
            pid = part.get("partId", "")
            piid = part.get("partItemId", "")
            if not pid or not piid:
                return pid, None
            url = (
                f"{BASE}/epc-services/picklist/validatePart/datasetId/{ds_id}/"
                f"filterRequest/{fr_leaf}/partId/{pid}/partItemId/{piid}"
            )
            try:
                r = client.get(url)
            except Exception as e:
                log.debug("validatePart err %s: %s", pid, e)
                return pid, None
            if r.status_code != 200:
                return pid, None
            try:
                data = r.json()
            except Exception:
                return pid, None
            for p in data.get("prices", []) or []:
                # MOB_MRP_A is the canonical Hyundai dealer-zone MRP. For
                # Toyota the same key is populated when the price book is
                # populated; if it's blank (current TKM_TOY dealer's known
                # issue) the part ships partial. MSRP can be a fallback but
                # for now we strictly match MOB_MRP_A.
                if p.get("priceType") == "MOB_MRP_A":
                    amt = p.get("amount")
                    if amt is None:
                        return pid, None
                    try:
                        return pid, float(amt)
                    except (TypeError, ValueError):
                        return pid, None
            return pid, None

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(fetch_one, p) for p in parts]
            for fut in as_completed(futures):
                try:
                    pid, mrp = fut.result()
                    if pid:
                        result[pid] = mrp
                except Exception as e:
                    log.debug("validatePart future err: %s", e)
        return result

    # ---------- HTTP helpers ----------

    def _get_navigation(
        self, client: httpx.Client, ds_id: str, sp: str, fr: str
    ) -> Optional[dict]:
        url = (
            f"{BASE}/epc-services/datasets/{ds_id}/navigations/"
            f"{sp}/filterRequest/{fr}"
            if sp
            else f"{BASE}/epc-services/datasets/{ds_id}/navigations/filterRequest/{fr}"
        )
        try:
            r = client.get(url)
        except Exception as e:
            log.warning("nav GET err %s: %s", url[:100], e)
            return None
        if r.status_code != 200:
            log.warning("nav GET %s → status=%d", url[:100], r.status_code)
            return None
        try:
            return r.json()
        except Exception as e:
            log.warning("nav GET %s → JSON err: %s", url[:100], e)
            return None

    def _get_parts(
        self, client: httpx.Client, ds_id: str, sp: str, fr: str
    ) -> list[dict]:
        """GET /pages/parts/<sp>/filterRequest/<fr> → list of partItems."""
        url = (
            f"{BASE}/epc-services/datasets/{ds_id}/pages/parts/"
            f"{sp}/filterRequest/{fr}"
        )
        try:
            r = client.get(url)
        except Exception as e:
            log.warning("parts GET err %s: %s", url[:100], e)
            return []
        if r.status_code != 200:
            log.warning("parts GET %s → status=%d", url[:100], r.status_code)
            return []
        try:
            return (r.json() or {}).get("partItems", []) or []
        except Exception as e:
            log.warning("parts GET %s → JSON err: %s", url[:100], e)
            return []
