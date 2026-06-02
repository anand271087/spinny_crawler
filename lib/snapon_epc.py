"""SNAP-ON Electronic Parts Catalog shared client (Hyundai, Toyota).

Auth flow (verified 2026-05-18):
  - POST /epc-services/auth/login with `user=X&password=<base64>`
  - Returns sessionJwtToken (JWT, 536 chars).
  - Cookies set: JSESSIONID, AWSALB, AWSALBCORS.

Headers required for ALL data endpoints (post-login):
  - Authorization: Bearer <sessionJwtToken>
  - sbsepc5cs: <anti-CSRF JWT>  (refreshed per request, server-signed HMAC)
  - sbsepc5s:  <session JWT>     (different from sessionJwtToken)
  - amg:       <user UUID>
  - x-client-version: 6.10.1
  - Standard Chrome client hints

CSRF token model:
  - sbsepc5cs and sbsepc5s are signed by the SBS-EPC SDK (sbsepc5acs is pre-login variant).
  - Tokens are NOT forgeable — server verifies signature.
  - Tokens are time-limited (TS field in payload).
  - The only way to obtain valid tokens is to capture them from a live JS session.

Headless detection:
  - Out-of-the-box Playwright headless renders BLANK page after login.
  - With anti-detection (`--disable-blink-features=AutomationControlled` + nav.webdriver
    override), the UI renders correctly.

Navigation API (verified):
  - Year list:  GET /epc-services/datasets/{datasetId}/navigations/filterRequest/{filter_b64}
    where filter_b64 = base64("jobId=1|dataSetId=...|locale=en-US|busReg=IND|priceBookId=...|userId=...")
  - Subsequent navigation: each click adds an opaque state token to the URL:
    /epc-services/datasets/{datasetId}/navigations/{state_token}/filterRequest/{filter_b64}
  - The state token is server-issued; we can't construct it. Must drive the UI.

Cascade verified:
  Year (1998-2026) → Model (ACCENT/CRETA/i10/...) → Equipment Variant (VERNA 24 2022-)
  → Category (ENGINE/TRANSMISSION/CHASSIS/BODY/TRIM/ELECTRIC/WIRE HARNESS REPAIR KIT)
  → Subcategory → Illustration → Parts table

For a parts-leaf extraction:
  - Use Playwright with anti-detection args.
  - Programmatically click year → model → variant → category → subcategory → illustration.
  - On the leaf illustration page, the Parts table shows: Part Number | Qty | Description | MRP.
  - For now this module ships the auth + framework + cascade-aware comments; full crawl
    requires per-click logic that's the largest single piece of remaining v2.0 work.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

log = logging.getLogger("lib.snapon_epc")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--window-size=1920,1080",
]

INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
"""


@dataclass
class SnapOnSession:
    sessionJwtToken: str
    cookies: dict
    user_uuid: str | None = None
    dataset_id: str | None = None


def encode_password(password: str) -> str:
    return base64.b64encode(password.encode()).decode()


def launch_authenticated_context(playwright, username: str, password: str) -> tuple[Browser, BrowserContext, Page, SnapOnSession]:
    """Open a Playwright context, login, return (browser, ctx, page, session).

    Caller must close browser when done. The context stays alive so the SDK can
    inject sbsepc5cs/sbsepc5s on subsequent requests.
    """
    auth_data: dict = {}

    browser = playwright.chromium.launch(headless=True, args=LAUNCH_ARGS)
    ctx = browser.new_context(
        user_agent=UA,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="Asia/Kolkata",
    )
    page = ctx.new_page()
    page.add_init_script(INIT_SCRIPT)

    def on_response(r):
        if "/auth/login" in r.url and r.status == 200:
            try:
                auth_data.update(r.json())
            except Exception:
                pass
        if "/auth/account" in r.url and r.status == 200:
            try:
                d = r.json()
                user = d.get("userDetails", {})
                if user.get("userId"):
                    auth_data["userId"] = user["userId"]
            except Exception:
                pass

    page.on("response", on_response)
    page.goto("https://snaponepc.com/epc/", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(3500)
    page.locator("input[type=text]").first.fill(username)
    page.locator("input[type=password]").first.fill(password)
    page.locator("button:has-text('Login')").first.click()
    page.wait_for_timeout(10_000)

    cookies_list = ctx.cookies()
    cookies = {c["name"]: c["value"] for c in cookies_list}
    session = SnapOnSession(
        sessionJwtToken=auth_data.get("sessionJwtToken", ""),
        cookies=cookies,
        user_uuid=auth_data.get("userId"),
    )
    return browser, ctx, page, session


def list_datasets(page: Page) -> list[dict]:
    """Fetch /datasets via page.request (auto-injects SDK headers)."""
    r = page.request.get("https://snaponepc.com/epc-services/datasets")
    if r.status != 200:
        return []
    try:
        return r.json()
    except Exception:
        return []
