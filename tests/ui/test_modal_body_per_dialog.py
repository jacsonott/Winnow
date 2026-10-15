"""Every dialog gets a body of its own. Dialog builders are often async —
draw a frame, await a fetch, fill it in — and they write into the body
they were handed; while that was the one shared #modalBody, a builder
whose answer came back after the analyst had moved on wrote into whatever
dialog was open by then."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_a_slow_dialog_does_not_fill_the_next_one(page, api):
    """The reported case: the Profile Apply sheet's /plan was slow, the
    analyst gave up on it and opened Settings, and the apply sheet's
    checkboxes and a working Apply button landed inside Settings. /plan is
    held until Settings is up, so the late answer is guaranteed."""
    listed = api("/api/plugin_bundles")
    bundles = listed if isinstance(listed, list) else listed.get("bundles", [])
    assert bundles, "needs at least one profile (the shipped ones are enough)"
    held = []
    page.route("**/api/plugin_bundles/*/plan", lambda route: held.append(route))
    try:
        page.evaluate("""(bd) => {
            __winnow.openProfileApplySheet(bd);
            window.__applyBody = document.getElementById('modalBody');
        }""", bundles[0])
        for _ in range(100):
            if held:
                break
            page.wait_for_timeout(50)
        assert held, "the apply sheet never asked for its plan"
        page.keyboard.press("Escape")
        page.wait_for_selector("#modal[hidden]", state="attached")
        page.evaluate("() => __winnow.openSettings()")
        page.wait_for_function("() => /settings/i.test(document.getElementById('modalTitle').textContent)")
        for r in held:
            r.continue_()
    finally:
        page.unroute("**/api/plugin_bundles/*/plan")
    # The stale builder finishes — into the body it was handed...
    page.wait_for_function("() => window.__applyBody.querySelector('.ap-part')")
    # ...which is not the dialog on screen.
    assert page.locator("#modalTitle").inner_text().lower() == "settings"
    assert page.locator("#modalBody .ap-part").count() == 0
    assert page.locator("#modalBody button", has_text="Apply").count() == 0
    page.keyboard.press("Escape")


def test_a_dialog_does_not_inherit_the_last_one_s_flags(page):
    """Emptying the shared body kept its data-* attributes: the Tables
    manager's kind='tables' stayed on the next dialog, so its 1.5s poll —
    which stops when the open dialog is no longer Tables — never stopped."""
    page.evaluate("() => __winnow.openTablesManager()")
    page.wait_for_selector("#modal:not([hidden])")
    assert page.evaluate("() => document.getElementById('modalBody').dataset.kind") == "tables"
    page.evaluate("() => __winnow.openSettings()")
    page.wait_for_function("() => /settings/i.test(document.getElementById('modalTitle').textContent)")
    assert page.evaluate("() => document.getElementById('modalBody').dataset.kind || ''") == ""
    page.keyboard.press("Escape")
