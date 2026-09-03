"""Case variables in the UI: the Variables section of Case settings, and a
profile's required variables gating the New case dialog."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

HDR = "{ 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' }"


def _api(page, path, method="GET", body=None):
    return page.evaluate(
        """([path, method, body]) => fetch(path, { method, headers: %s,
              body: body == null ? undefined : JSON.stringify(body) }).then((r) => r.json())""" % HDR,
        [path, method, body])


def _clear_variables(page):
    for v in _api(page, "/api/case/variables"):
        _api(page, f"/api/case/variables/{v['name']}", "DELETE")
    page.evaluate("() => __winnow.loadCaseVariables()")


def _open_case_settings(page):
    page.click("#btnCase")
    page.wait_for_selector(".menu")
    page.locator(".menu-item", has_text="Case settings").click()
    page.wait_for_selector("#modal:not([hidden]) .case-vars")


def test_case_settings_lists_adds_edits_and_removes_variables(page):
    try:
        _clear_variables(page)
        _open_case_settings(page)
        box = page.locator("#modalBody .case-vars")
        assert "No variables yet" in box.inner_text()

        # add
        box.locator(".case-var-name-in").fill("engagement")
        box.locator(".case-var-add .case-var-value").fill("ACME-2026-09")
        box.locator(".case-var-add .btn", has_text="Add").click()
        page.wait_for_selector("#modalBody .case-var-row .case-var-name:has-text('engagement')")
        assert _api(page, "/api/case/variables") == [
            {"name": "engagement", "value": "ACME-2026-09", "description": "", "required": False}]

        # edit in place saves on change
        row = box.locator(".case-var-row", has_text="engagement")
        row.locator(".case-var-value").fill("ACME-2026-10")
        row.locator(".case-var-value").press("Tab")
        page.wait_for_function(
            "() => (__winnow.S.caseVariables.find((v) => v.name === 'engagement') || {}).value === 'ACME-2026-10'")

        # a required-but-empty one is flagged
        _api(page, "/api/case/variables", "POST", {"name": "report_api", "required": True, "description": "where reports go"})
        page.keyboard.press("Escape")
        _open_case_settings(page)   # reopening re-fetches, so an out-of-band change shows
        page.wait_for_selector("#modalBody .case-var-row:has-text('report_api')")
        flagged = page.locator("#modalBody .case-var-row.missing")
        assert flagged.count() == 1 and "report_api" in flagged.inner_text()
        assert "required" in flagged.locator(".case-var-desc").inner_text()

        # remove
        flagged.locator(".case-var-del").click()
        page.wait_for_function("() => !__winnow.S.caseVariables.some((v) => v.name === 'report_api')")
        assert page.locator("#modalBody .case-var-row.missing").count() == 0
        page.keyboard.press("Escape")
    finally:
        page.keyboard.press("Escape")
        _clear_variables(page)


def test_a_bad_variable_name_is_refused_with_a_toast(page):
    try:
        _open_case_settings(page)
        box = page.locator("#modalBody .case-vars")
        box.locator(".case-var-name-in").fill("1 bad name")
        box.locator(".case-var-add .btn", has_text="Add").click()
        page.wait_for_selector("#toast:not([hidden])")
        assert "Could not add" in page.locator("#toast").inner_text()
        assert not any(v["name"] == "1 bad name" for v in _api(page, "/api/case/variables"))
    finally:
        page.keyboard.press("Escape")
        _clear_variables(page)


def test_new_case_dialog_asks_for_a_profiles_required_variables(page):
    """A saved profile with a required variable: the New case dialog shows
    the input once that profile is chosen and refuses to create the case
    until it is filled."""
    rec = _api(page, "/api/plugin_bundles", "POST", {
        "name": "UI Vars Profile", "plugins": [],
        "variables": [{"name": "engagement", "label": "Engagement", "required": True,
                       "description": "for report titles"},
                      {"name": "doc_link", "label": "Scoping doc", "default": "https://docs.local/x"}]})
    try:
        page.evaluate("() => { __winnow.showHome(); return __winnow.refreshCases(); }")
        page.wait_for_selector("#home:not([hidden]) .home-head")
        page.locator("#home .btn", has_text="New case").click()
        page.wait_for_selector("#modal:not([hidden])")
        sel = page.locator("#modalBody select").first
        page.wait_for_function("() => [...document.querySelectorAll('#modalBody select option')].some((o) => o.textContent.startsWith('UI Vars Profile'))")
        assert page.locator("#modalBody .new-case-vars").is_hidden()
        sel.select_option(str(rec["id"]))
        page.wait_for_selector("#modalBody .new-case-vars:not([hidden])")
        vars_box = page.locator("#modalBody .new-case-vars")
        assert "Engagement *" in vars_box.inner_text()
        assert vars_box.locator("input[data-var=doc_link]").input_value() == "https://docs.local/x"
        assert "for report titles" in vars_box.inner_text()

        # required and empty → Create refuses, case not made
        before = page.evaluate("() => fetch('/api/cases').then((r) => r.json()).then((c) => c.length)")
        page.locator("#modalBody input").first.fill("Vars gate case")
        page.locator("#modalBody .btn", has_text="Create").click()
        page.wait_for_selector("#toast:not([hidden])")
        assert "needs: Engagement" in page.locator("#toast").inner_text()
        assert page.locator("#modal:not([hidden])").count() == 1
        after = page.evaluate("() => fetch('/api/cases').then((r) => r.json()).then((c) => c.length)")
        assert after == before
    finally:
        page.keyboard.press("Escape")
        _api(page, f"/api/plugin_bundles/{rec['id']}", "DELETE")
        page.evaluate("() => __winnow.showApp()")
        page.evaluate("(id) => __winnow.openSource(id)", page.evaluate("() => __winnow.S.sources[0].id"))
        page.wait_for_function("() => __winnow.S.view")


def test_applying_a_profile_prompts_for_required_variables_it_seeds(page):
    """Applying to an existing case seeds the definitions and asks for the
    required ones in one dialog; Later leaves them flagged in Case settings."""
    rec = _api(page, "/api/plugin_bundles", "POST", {
        "name": "UI Vars Apply", "plugins": [],
        "variables": [{"name": "engagement", "label": "Engagement", "required": True}]})
    try:
        _clear_variables(page)
        page.keyboard.press("M")
        page.wait_for_selector(".session-row:has-text('UI Vars Apply')")
        page.locator(".session-row", has_text="UI Vars Apply").locator(".btn", has_text="Apply to this case").click()
        page.wait_for_selector("#modal:not([hidden]) .case-var-prompt")
        assert "needs a few values" in page.locator("#modalTitle").inner_text().lower()
        page.locator("#modalBody .btn", has_text="Save").click()       # empty → refused
        page.wait_for_selector("#toast:not([hidden])")
        assert "Fill in: Engagement" in page.locator("#toast").inner_text()
        page.locator("#modalBody .case-var-prompt").fill("ACME")
        page.locator("#modalBody .btn", has_text="Save").click()
        page.wait_for_selector("#modal[hidden]", state="attached")
        page.wait_for_function(
            "() => (__winnow.S.caseVariables.find((v) => v.name === 'engagement') || {}).value === 'ACME'")
        vs = {v["name"]: v for v in _api(page, "/api/case/variables")}
        assert vs["engagement"]["required"] is True and vs["engagement"]["value"] == "ACME"
    finally:
        page.keyboard.press("Escape")
        _api(page, f"/api/plugin_bundles/{rec['id']}", "DELETE")
        _clear_variables(page)
