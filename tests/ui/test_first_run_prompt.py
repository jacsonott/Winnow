"""The one-time first-run offer to enable Remote session mode."""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui


@pytest.fixture(autouse=True)
def reset_remote_setting(server):
    """Accepting the offer persists remote mode MACHINE-side now — reset
    it after each test or the next test's prompt never fires (the offer
    honours an already-answered machine)."""
    yield
    req = urllib.request.Request(
        server.rstrip("/") + "/api/settings/app",
        data=json.dumps({"remote_session": False}).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    urllib.request.urlopen(req, timeout=5).read()


def _fresh_page(browser, server):
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    pg = ctx.new_page()
    pg.goto(server, wait_until="networkidle")
    pg.wait_for_selector(".row", timeout=30_000)
    return ctx, pg


def test_first_run_offers_remote_mode_once(browser, server):
    ctx, pg = _fresh_page(browser, server)
    pg.wait_for_selector(".confirm-overlay")
    assert "remote desktop" in pg.locator(".confirm-card").inner_text().lower()
    pg.locator(".confirm-card .btn", has_text="Enable remote mode").click()
    pg.wait_for_function("() => document.documentElement.classList.contains('remote')")
    # Stored machine-side now, not in localStorage (which kept resetting).
    pg.wait_for_function("() => __winnow.S.appSettings.remote_session === true")
    # answered once — a reload must not ask again
    pg.reload(wait_until="networkidle")
    pg.wait_for_selector(".row")
    pg.wait_for_timeout(400)
    assert pg.locator(".confirm-overlay").count() == 0
    assert pg.evaluate("() => document.documentElement.classList.contains('remote')")
    ctx.close()


def test_declining_also_only_asks_once(browser, server):
    ctx, pg = _fresh_page(browser, server)
    pg.wait_for_selector(".confirm-overlay")
    pg.locator(".confirm-card .btn", has_text="No, local session").click()
    pg.wait_for_timeout(200)
    assert not pg.evaluate("() => document.documentElement.classList.contains('remote')")
    pg.reload(wait_until="networkidle")
    pg.wait_for_selector(".row")
    pg.wait_for_timeout(400)
    assert pg.locator(".confirm-overlay").count() == 0
    ctx.close()


def test_a_machine_with_stored_appearance_is_not_nagged(browser, server):
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    ctx.add_init_script("localStorage.setItem('winnow.appearance', JSON.stringify({ density: 'compact' }))")
    pg = ctx.new_page()
    pg.goto(server, wait_until="networkidle")
    pg.wait_for_selector(".row")
    pg.wait_for_timeout(400)
    assert pg.locator(".confirm-overlay").count() == 0
    ctx.close()
