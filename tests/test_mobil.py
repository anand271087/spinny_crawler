"""Mobil spider test — uses fixture HTML served via Playwright route interception."""

from __future__ import annotations

from pathlib import Path

import pytest

from spiders.mobil import Spider


FIXTURE = Path(__file__).parent / "fixtures" / "mobil" / "sample.html"


@pytest.fixture
def brand_cfg() -> dict:
    return {
        "url": "https://www.mobil.co.in/en-in/our-products",
        "pattern": "flat_list",
        "fields": ["item_name"],
    }


def test_mobil_extracts_item_names_from_fixture(brand_cfg, monkeypatch):
    """Route any request to fixture HTML; spider should return 3 named products."""
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    fixture_html = FIXTURE.read_text()

    def patched_crawl(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context()
            page = ctx.new_page()
            page.route("**/*", lambda r: r.fulfill(status=200, body=fixture_html, content_type="text/html"))
            page.goto("https://www.mobil.co.in/en-in/our-products")
            names = page.locator(".product-card__title").all_text_contents()
            browser.close()
            from spiders._base import Row
            return [Row(item_name=n.strip()) for n in names if n.strip()]

    monkeypatch.setattr(Spider, "crawl", patched_crawl)
    spider = Spider("mobil", brand_cfg)
    result = spider.run()

    assert result["status"] in {"success", "partial"}
    names = [r["item_name"] for r in result["rows"]]
    assert "Mobil 1 ESP 5W-30" in names
    assert "Mobil Super 3000 X1 5W-40" in names
    assert "Mobil Delvac MX 15W-40" in names
    assert len(names) == 3
    # brand column populated
    assert all(r["brand"] == "Mobil" for r in result["rows"])


def test_mobil_auto_fields_populated(brand_cfg):
    spider = Spider("mobil", brand_cfg)
    from spiders._base import Row
    spider.crawl = lambda: [Row(item_name="Test product")]  # type: ignore[method-assign]

    result = spider.run()
    row = result["rows"][0]
    assert row["brand"] == "Mobil"
    assert row["source_website"] == brand_cfg["url"]
    assert row["crawl_date"]
    assert row["crawl_status"] == "success"


def test_mobil_partial_when_required_field_missing(brand_cfg):
    spider = Spider("mobil", brand_cfg)
    from spiders._base import Row
    spider.crawl = lambda: [Row(item_name=None)]  # type: ignore[method-assign]

    result = spider.run()
    assert result["rows"][0]["crawl_status"] == "partial"
    assert result["status"] == "partial"
