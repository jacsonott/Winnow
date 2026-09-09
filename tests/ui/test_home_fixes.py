"""Home / quick-look fixes: "Open existing case file…" has a Browse…
picker, new-case paths join with the directory's own separator (no
C:\\Cases/x on Windows), the quick-look banner spans the bar and stays put
when the sidebar toggles, and a fresh origin adopts the machine's saved
look instead of the default skin."""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui


def _post(server, route, body):
    req = urllib.request.Request(
        server.rstrip("/") + route, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def test_join_path_uses_the_directory_s_own_separator(page):
    jp = lambda d, n: page.evaluate("([d, n]) => __winnow.joinPath(d, n)", [d, n])
    assert jp("C:\\Cases", "acme.db-winnow") == "C:\\Cases\\acme.db-winnow"
    assert jp("C:\\Cases\\", "acme.db-winnow") == "C:\\Cases\\acme.db-winnow"
    assert jp("/srv/cases", "acme.db-winnow") == "/srv/cases/acme.db-winnow"
    assert jp("cases", "x.db-winnow") == "cases/x.db-winnow"


def test_open_existing_case_has_a_browse_button(page):
    page.evaluate("() => __winnow.openExistingCasePrompt()")
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator("#modalTitle").inner_text().lower() == "open existing case file"
    assert page.locator("#modal button", has_text="Browse…").count() == 1
    page.locator("#modal button", has_text="Browse…").click()
    # The server-disk picker, in file mode.
    page.wait_for_selector("#modal:not([hidden])")
    assert "add files" in page.locator("#modalTitle").inner_text().lower()
    page.locator("#modal button", has_text="Cancel").click()
    page.wait_for_function("() => /open existing/i.test(document.getElementById('modalTitle').textContent)")
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_quicklook_banner_spans_the_bar_and_ignores_the_sidebar(page):
    page.evaluate("() => __winnow.paintTempBanner(true)")
    page.wait_for_selector("#tempBanner:not([hidden])")
    box = page.locator("#tempBanner").bounding_box()
    vw = page.evaluate("() => window.innerWidth")
    assert box["x"] < 1 and box["width"] >= vw - 2          # full width, not column 2
    page.locator("#btnTabJump").click()                      # hide the sidebar
    page.wait_for_selector("#sidebar", state="hidden")
    box2 = page.locator("#tempBanner").bounding_box()
    assert abs(box2["x"] - box["x"]) < 1 and abs(box2["width"] - box["width"]) < 1
    page.locator("#btnTabJump").click()
    page.wait_for_selector("#sidebar", state="visible")
    page.evaluate("() => __winnow.paintTempBanner(false)")


def test_fresh_origin_adopts_the_machine_s_saved_look(browser, server):
    before = _post(server, "/api/settings/app", {})   # current, unchanged
    _post(server, "/api/settings/app", {"appearance": {
        "style": "phosphor", "themeMode": "dark", "accent": "#39e881",
        "accentCustomized": False, "splash": False}})
    ctx = browser.new_context(viewport={"width": 1200, "height": 800})
    # No stored appearance at all — a brand-new origin, like a quick-look.
    ctx.add_init_script("localStorage.setItem('winnow.remotePrompt', 'seen')")
    pg = ctx.new_page()
    try:
        pg.goto(server, wait_until="networkidle")
        pg.wait_for_function("() => document.documentElement.getAttribute('data-style') === 'phosphor'",
                             timeout=10_000)
        assert pg.evaluate("() => __winnow.S.appearance.style") == "phosphor"
    finally:
        ctx.close()
        # An origin WITH its own stored look keeps it (the shared fixture
        # contexts store one) — and put the machine back as we found it.
        restore = before.get("appearance") or {"style": "harvest", "themeMode": "dark",
                                                "accent": "#d9a441", "accentCustomized": False, "splash": False}
        _post(server, "/api/settings/app", {"appearance": restore})
