"""The app's favicon links resolve to the committed icon assets — a
broken <link rel=icon> is a silent 404 no backend test would catch."""

from __future__ import annotations

import urllib.request

import pytest

pytestmark = pytest.mark.ui


def test_favicon_links_point_at_real_assets(browser, server):
    ctx = browser.new_context()
    pg = ctx.new_page()
    try:
        pg.goto(server)
        hrefs = pg.eval_on_selector_all(
            "link[rel~='icon'], link[rel='apple-touch-icon']",
            "els => els.map(e => e.getAttribute('href'))")
        assert any("winnow" in (h or "") for h in hrefs), hrefs
        for href in hrefs:
            url = href if href.startswith("http") else server.rstrip("/") + href
            with urllib.request.urlopen(url, timeout=5) as r:
                assert r.status == 200, href
                assert int(r.headers.get("Content-Length", "1")) > 0, href
    finally:
        ctx.close()
