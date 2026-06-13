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
  - Two blockers found (2026-06-03 deep dive):
    a) TLS fingerprint check — httpx → 400 always. Browser-channel fetch (via
       Playwright page.evaluate or ctx.request) with the same URL + headers
       returns 200. Solved by routing picklist calls through the browser.
    b) SPA session-state precondition — even via the browser, validatePart
       returns 400 unless the SPA UI has been clicked into that exact section
       (year→model→catalog→group→section). Warming via GET /pages/parts/ does
       NOT help. The SPA registers section-view state via internal JS that we
       could not isolate or replicate outside its click flow.
  - To unlock MRP in a future pass: either (a) drive Playwright UI clicks
    per leaf section before calling validatePart (slow — ~5s/leaf × 387
    leaves/model ≈ 30 min/model just for nav — but functionally works), or
    (b) reverse-engineer the SPA's Angular PicklistService to call its
    internal addPart() method via page.evaluate().
  - Plumbing stays in this module (commented out): _fetch_mrps_via_browser,
    _register_parts_for_picklist, _build_filter_request, and DFS catalog_id
    tracking. Re-enable is a 5-line uncomment once the SPA-session piece is
    solved. See per_site_notes §V2.2 for the operator-level summary and a
    full list of probe artifacts.
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


def _dyn_col(part: dict, code: str) -> Optional[str]:
    """Pull a dynamicColumns value by its `code` (e.g. FDATE/TDATE). SNAP-ON
    returns part-validity dates here, not as top-level fields. Returns None when
    the column is absent or blank (legitimate for in-production parts)."""
    for col in (part.get("dynamicColumns") or []):
        if isinstance(col, dict) and col.get("code") == code:
            v = (col.get("value") or "").strip()
            return v or None
    return None


def _resolve_creds(brand_key: str) -> tuple[str, str]:
    env_u = os.environ.get(f"{brand_key.upper()}_USER")
    env_p = os.environ.get(f"{brand_key.upper()}_PASS")
    if env_u and env_p:
        return env_u, env_p
    if brand_key in FALLBACK_CREDS:
        return FALLBACK_CREDS[brand_key]
    raise RuntimeError(f"no credentials for brand={brand_key}")


def _playwright_login(brand_key: str, user: str, password: str):
    """Logs in to SNAP-ON. Returns (playwright_ctx_manager, browser, context, page,
    auth_headers_dict, cookies_dict).

    The browser is kept OPEN for the MRP fetch path. Caller is responsible for
    calling `pw_cm.__exit__()` (or using as a context manager) to clean up.

    auth_headers contains: sbsepc5s, sbsepc5cs, x-client-version — captured
    from the first /auth/account XHR the SPA fires post-login. cookies is the
    BrowserContext cookies (used by ctx.request automatically).
    """
    captured: dict = {}

    def on_request(req):
        # Capture sbsepc5s + sbsepc5cs from the first /auth/account call
        if not captured and req.url.endswith("/epc-services/auth/account"):
            captured.update({k.lower(): v for k, v in req.headers.items()})

    # NOTE: we explicitly do NOT use a `with sync_playwright()` block here so
    # the browser stays alive after this function returns. Caller calls
    # pw_cm.__exit__() to clean up.
    pw_cm = sync_playwright()
    p = pw_cm.__enter__()
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
        pw_cm.__exit__(None, None, None)
        raise RuntimeError(f"{brand_key}: login failed (no Logout link)")

    # Give the SPA a moment to fire /auth/account so we capture headers
    page.wait_for_timeout(ACCOUNT_WAIT_MS)
    cookies = {c["name"]: c["value"] for c in ctx.cookies()}

    auth_headers = {
        k: captured[k]
        for k in ("sbsepc5s", "sbsepc5cs", "x-client-version")
        if k in captured
    }
    if "sbsepc5s" not in auth_headers or "sbsepc5cs" not in auth_headers:
        browser.close()
        pw_cm.__exit__(None, None, None)
        raise RuntimeError(
            f"{brand_key}: failed to capture session tokens "
            f"(captured keys: {list(captured.keys())[:10]})"
        )
    log.info("%s: tokens captured (sbsepc5s, sbsepc5cs, x-client-version)", brand_key)
    return pw_cm, browser, ctx, page, auth_headers, cookies


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
        # MRP fetch (UI-driven, slow): drill UI into each section + click first
        # part to warm SPA state + parallel-fetch supersession for all parts.
        # Off by default; enable per-run via HYUNDAI_FETCH_MRP=1.
        self.fetch_mrp = bool(int(
            os.environ.get(f"{brand_key.upper()}_FETCH_MRP", "0") or "0"
        ))

    def crawl(self) -> list[Row]:
        log.info("%s: REST spider crawl() entered", self.brand_key)
        t_start = time.time()

        user, password = _resolve_creds(self.brand_key)
        log.info("%s: credentials resolved user=%r", self.brand_key, user)

        # 1. Login — keep Playwright alive: MRP /picklist/validatePart calls
        # require browser TLS fingerprint (httpx 400s on those even with
        # identical headers — see module docstring "MRP STATUS").
        pw_cm, browser, pw_ctx, pw_page, auth_headers, cookies = _playwright_login(
            self.brand_key, user, password
        )
        # Stash for the helpers
        self._pw_ctx = pw_ctx
        self._auth_headers = auth_headers

        # 2. httpx session for /navigations and /pages/parts (fast — no fingerprint check)
        headers = {
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "referer": f"{BASE}/epc/",
            "accept": "application/json, text/plain, */*",
            **auth_headers,
        }
        try:
            with httpx.Client(
                headers=headers, cookies=cookies, timeout=45, follow_redirects=True
            ) as client:
                return self._crawl_via_rest(client, t_start)
        finally:
            # Always clean up Playwright (even on exception)
            try:
                browser.close()
            except Exception:
                pass
            try:
                pw_cm.__exit__(None, None, None)
            except Exception:
                pass

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

                    # MRP fetch: UI-drill the SPA into the section (warms server-
                    # side session state), then parallel browser-fetch supersession
                    # for all parts. See module docstring "MRP STATUS" for details.
                    mrp_map: dict[str, Optional[float]] = {}
                    if new_parts and new_catalog_id and self.fetch_mrp:
                        log.info("MRP: leaf crumb=%r catalog_id=%s, parts=%d",
                                 new_crumb[:5], new_catalog_id, len(new_parts))
                        fr_leaf = self._build_filter_request(
                            ds_id, price_book_id, user_id, new_catalog_id
                        )
                        # UI drill to set SPA section state — one click trail per leaf
                        warmed = self._warm_section_ui(new_crumb[:5])
                        if warmed:
                            mrp_map = self._fetch_mrps_via_browser(
                                ds_id, fr_leaf, new_parts, user_id, leaf_sp=node_sp,
                            )
                            mrp_lookups += len(new_parts)
                            mrp_hits += sum(1 for v in mrp_map.values() if v is not None)

                    for p in new_parts:
                        pid = p.get("partId", "")
                        desc = (p.get("description") or "").strip()
                        rows.append(Row(
                            item_name=desc,
                            item_code=(p.get("formattedPartNumber") or p.get("partNumber") or "").strip(),
                            mrp=mrp_map.get(pid),
                            description=desc,  # explicit Description column (= item_name source)
                            compatible_car_model=" > ".join(new_crumb[:5]),  # cap breadcrumb depth
                            # FDate/TDate live in dynamicColumns (Toyota: start/end of part
                            # validity). Blank for parts with no date range — left as None.
                            start_date=_dyn_col(p, "FDATE"),
                            end_date=_dyn_col(p, "TDATE"),
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

    def _warm_section_ui(self, breadcrumb: list[str]) -> bool:
        """Drive Playwright UI clicks to set the SPA's section state, then click
        the first part-number to trigger supersession (which warms the per-section
        state for all subsequent supersession fetches).

        breadcrumb = [year, model, catalog, group, section]. We re-navigate from
        /epc/#/ each time — simpler than diffing against the prior section, ~5s
        per nav. Returns True if drill succeeded all the way to section.

        After this returns True, supersession can be called for any partId
        belonging to this section via in-browser fetch (see _fetch_mrps_via_browser).
        """
        page = self._pw_ctx.pages[0]
        try:
            # Reset to home so the next click sequence starts from a known state.
            page.goto(f"{BASE}/epc/#/", wait_until="domcontentloaded", timeout=15_000)
            page.wait_for_timeout(4000)
        except Exception as e:
            log.warning("warm: home goto err: %s", e)
            return False
        # Click the dataset tile (Hyundai/Toyota) — first thumbnail on home.
        try:
            page.locator('[class*="thumbnail"]').first.click(timeout=5000)
            page.wait_for_timeout(4000)
        except Exception:
            pass
        # Drill through breadcrumb levels by matching visible text. Names can
        # include special chars (zero-width spaces in model names like
        # 'ACCENT/​ACCENT BLUE/​VERNA/​PONY'); we try exact text first, then a
        # partial-text fallback.
        for level_idx, name in enumerate(breadcrumb):
            clicked = False
            # Strip zero-width chars for partial-match fallback
            short = ''.join(c for c in name if c.isalnum() or c in ' -/')[:24]
            for sel in (
                f"text='{name}'",
                f'[class*="thumbnail"]:has-text("{short}")',
                f'text=/{short}/',
            ):
                try:
                    el = page.locator(sel).first
                    if el.count() == 0:
                        continue
                    el.click(timeout=5000)
                    page.wait_for_timeout(4500)
                    clicked = True
                    log.info("warm: clicked level %d (%r) via %s", level_idx, name[:30], sel[:40])
                    break
                except Exception:
                    continue
            if not clicked:
                log.warning("warm: failed at level %d (%r)", level_idx, name[:50])
                return False
        # Click the part-number — opens part-detail view — SPA fires supersession.
        # Capture the SPA's exact supersession URL + headers; we'll re-use the URL
        # verbatim, only swapping pr=<partId> for each part we want to price.
        # (Verified working pattern: state/probe_supersession_per_section.py.)
        captured = {"url": None, "headers": None}
        def on_super(req):
            if not captured["url"] and "/partdetails/supersession" in req.url:
                captured["url"] = req.url
                captured["headers"] = dict(req.headers)
        page.on("request", on_super)
        try:
            page.locator('.ag-cell[col-id="formattedPartNumber"] a').first.click(timeout=8000)
            page.wait_for_timeout(5000)
            page.remove_listener("request", on_super)
            if captured["url"] and captured["headers"]:
                self._last_spa_super_url = captured["url"]
                self._spa_super_headers = captured["headers"]
                log.info("warm: captured SPA supersession URL + headers (%d hdrs)",
                         len(captured["headers"]))
                return True
            log.warning("warm: no supersession XHR captured after part-number click")
            return False
        except Exception as e:
            page.remove_listener("request", on_super)
            log.warning("warm: part-number click err: %s", e)
            return False

    def _fetch_mrps_via_browser(
        self,
        ds_id: str,
        fr_leaf: str,
        parts: list[dict],
        user_id: str,
        leaf_sp: str = "",
        batch_size: int = 50,
    ) -> dict[str, Optional[float]]:
        """For each part, call /picklist/validatePart via Playwright (browser
        TLS) and extract MOB_MRP_A. Returns {partId: mrp_or_None}.

        Why via browser: SNAP-ON's WAF JA3-checks /picklist/* endpoints. httpx
        (Python TLS) returns 400; the SAME URL + headers from inside the
        browser returns 200.

        Implementation: page.evaluate() with Promise.all() — single JS round-
        trip, browser fires up to ~6 concurrent HTTP/2 streams natively. Way
        faster than serial ctx.request, and sync Playwright isn't thread-safe
        so a Python ThreadPool wouldn't work anyway.

        Batched at 50 parts per evaluate to keep the JSON arg/result manageable
        and to bound the per-call wall-clock (each batch is ~5-8 seconds).
        """
        result: dict[str, Optional[float]] = {}
        if not parts:
            return result

        # Use the FRESHLY-CAPTURED SPA headers from _warm_section_ui — those
        # include sbsepc5s/cs that may have been refreshed since login, plus
        # all the sec-ch-ua/user-agent quirks the WAF expects.
        if getattr(self, "_spa_super_headers", None):
            req_headers = {k: v for k, v in self._spa_super_headers.items()
                           if not k.lower().startswith("content-length")}
        else:
            # Fallback: synthesize from login-time headers (may fail if stale)
            req_headers = {
                "accept": "application/json, text/plain, */*",
                "amg": user_id,
                "cache-control": "no-cache,no-store",
                "expires": "0",
                "pragma": "no-cache",
                "referer": f"{BASE}/epc/",
                "user-agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                **self._auth_headers,
            }

        # JS: take the SPA's captured baseUrl, swap `pr=<partId>` for each
        # request, fire in parallel. MRP comes from prices[].priceType==MOB_MRP_A.
        # Pattern proven working in state/probe_supersession_per_section.py.
        js = """async ({baseUrl, partIds, headers}) => {
            const out = {};
            const debug = {firstStatus: null, firstBody: null, errors: 0};
            const results = await Promise.all(partIds.map(async (pid, i) => {
                const u = new URL(baseUrl);
                u.searchParams.set('pr', pid);
                try {
                    const r = await fetch(u.toString(), {credentials: 'include', headers});
                    if (i === 0) {
                        debug.firstStatus = r.status;
                        debug.firstBody = (await r.clone().text()).slice(0, 300);
                    }
                    if (!r.ok) return [pid, null];
                    const data = await r.json();
                    // Supersession returns supersessionData[] or similar; look for
                    // prices[] anywhere in the response by JSON-walk.
                    const txt = JSON.stringify(data);
                    const m = txt.match(/"priceType":"MOB_MRP_A","amount":"([^"]+)"/);
                    return [pid, m ? m[1] : null];
                } catch (e) {
                    debug.errors++;
                    return [pid, null];
                }
            }));
            for (const [pid, amt] of results) out[pid] = amt;
            return {out, debug};
        }"""

        # Use the SPA's exact captured supersession URL as the base. Only swap
        # `pr=<partId>` per part — keeps every byte of the SPA-built fr (with
        # equipmentRefId), every query-param order, etc.
        base_url = getattr(self, "_last_spa_super_url", None)
        if not base_url:
            log.warning("MRP: no SPA-captured URL — skipping leaf")
            return result

        # Process in batches to avoid huge single page.evaluate args.
        for start in range(0, len(parts), batch_size):
            chunk = parts[start : start + batch_size]
            pairs = []
            for p in chunk:
                pid = p.get("partId", "")
                if not pid:
                    continue
                pairs.append([pid])  # only partId — URL is built JS-side from base
            if not pairs:
                continue
            try:
                page = self._pw_ctx.pages[0]
                # Pairs is [[partId], ...] — extract flat partIds list
                part_ids_chunk = [p[0] for p in pairs]
                batch_result = page.evaluate(js, {
                    "baseUrl": base_url,
                    "partIds": part_ids_chunk,
                    "headers": req_headers,
                })
            except Exception as e:
                log.warning("supersession batch err: %s", e)
                continue
            if start == 0:
                debug = (batch_result or {}).get("debug", {})
                log.info("MRP first-batch debug: status=%s firstBody=%r errors=%s",
                         debug.get("firstStatus"),
                         (debug.get("firstBody") or "")[:200],
                         debug.get("errors"))
            for pid, amt in (batch_result or {}).get("out", {}).items():
                try:
                    result[pid] = float(amt) if amt is not None else None
                except (TypeError, ValueError):
                    result[pid] = None
        return result

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
