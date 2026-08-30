"""Where settings live: machine-wide ones reachable without a case, and
case-scoped ones on the Session menu.

The split matters because Settings is now openable from the home screen,
where there is no case for a "this case" control to describe."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_settings_opens_from_the_case_list(page):
    """The gear lives on the app bar, which the home screen doesn't have —
    so before this there was no way in without opening a case first."""
    # showHome() only unhides the panel — refreshCases() is what builds the
    # header the button lives in.
    page.evaluate("() => { __winnow.showHome(); return __winnow.refreshCases(); }")
    page.wait_for_selector("#home:not([hidden]) .home-head")
    btn = page.locator("#home .btn", has_text="Settings")
    assert btn.count() == 1
    btn.click()
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator("#modalTitle").inner_text().lower() == "settings"
    page.keyboard.press("Escape")
    # Put the app back: showHome() hid #app, and openSource() alone does not
    # bring it back — it changes which table is open, not which screen is.
    page.evaluate("() => __winnow.showApp()")
    page.evaluate("(id) => __winnow.openSource(id)", page.evaluate("() => __winnow.S.sources[0].id"))
    page.wait_for_function("() => __winnow.S.view")
    page.wait_for_selector("#btnSession", state="visible")


def test_settings_has_no_case_scoped_controls_left(page):
    """Opened from the home screen there is no case, so anything describing
    'this case' would be describing nothing."""
    page.evaluate("() => __winnow.openSettings()")
    page.wait_for_selector("#modal:not([hidden])")
    page.locator(".settings-section-head", has_text="Timestamps").click()
    body = page.locator(".settings-section-body").filter(
        has=page.locator("text=Every case on this machine"))
    # Asserted structurally: the section has ONE control, the machine-wide
    # one. Checking prose would trip over the pointer text, which quite
    # reasonably says the words "this case".
    assert body.locator("select").count() == 1
    assert "Case settings" in body.inner_text(), "it should say where the per-case option went"
    page.keyboard.press("Escape")


def test_case_settings_is_on_the_session_menu_and_saves(page):
    page.click("#btnSession")
    page.wait_for_selector(".menu")
    page.locator(".menu-item", has_text="Case settings").click()
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator("#modalTitle").inner_text().lower() == "case settings"

    sel = page.locator("#modalBody select").first
    sel.select_option("us")
    page.wait_for_function("() => __winnow.S.caseSettings.ts_format === 'us'")

    # Stored in the CASE, not the browser — so it survives to another
    # analyst opening this file.
    saved = page.evaluate("""() => fetch('/api/case_settings',
      { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())""")
    assert saved["ts_format"] == "us"

    sel.select_option("")   # back to inheriting
    page.wait_for_function("() => !__winnow.S.caseSettings.ts_format")
    page.keyboard.press("Escape")
