"""Case variables in the UI: the Variables section of Case settings, and a
profile's required variables gating the New case dialog."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _clear_variables(page, api):
    for v in api("/api/case/variables"):
        api(f"/api/case/variables/{v['name']}", "DELETE")
    page.evaluate("() => __winnow.loadCaseVariables()")


def _open_case_settings(page):
    page.click("#btnCase")
    page.wait_for_selector(".menu")
    page.locator(".menu-item", has_text="Case settings").click()
    page.wait_for_selector("#modal:not([hidden]) .case-settings")
    page.locator("#modalBody .side-rail-item[data-section=variables]").click()
    page.wait_for_selector("#modalBody .cs-vars-box")


def test_case_settings_lists_adds_edits_and_removes_variables(page, api):
    try:
        _clear_variables(page, api)
        _open_case_settings(page)
        box = page.locator("#modalBody .cs-vars-box")
        assert "No variables yet" in box.inner_text()
        assert page.locator("#modalBody .side-rail-item[data-section=variables]").inner_text().endswith("None yet")

        # add, from the list's last line
        box.locator(".cs-vars-add input[aria-label='New variable name']").fill("engagement")
        box.locator(".cs-vars-add input[aria-label='New variable value']").fill("ACME-2026-09")
        box.locator(".cs-vars-add .btn", has_text="Add").click()
        page.wait_for_selector("#modalBody .cs-var-row[data-id=engagement]")
        assert api("/api/case/variables") == [
            {"name": "engagement", "value": "ACME-2026-09", "description": "", "required": False}]
        # the side list says what the case has now
        page.wait_for_function("() => document.querySelector('#modalBody .side-rail-item[data-section=variables]')"
                               ".textContent.includes('1 set')")

        # edit in place saves on change
        row = box.locator(".cs-var-row[data-id=engagement]")
        row.locator("[data-role=value]").fill("ACME-2026-10")
        row.locator("[data-role=value]").press("Tab")
        page.wait_for_function(
            "() => (__winnow.S.caseVariables.find((v) => v.name === 'engagement') || {}).value === 'ACME-2026-10'")

        # a required-but-empty one is flagged
        api("/api/case/variables", "POST", {"name": "report_api", "required": True, "description": "where reports go"})
        page.keyboard.press("Escape")
        _open_case_settings(page)   # reopening re-fetches, so an out-of-band change shows
        page.wait_for_selector("#modalBody .cs-var-row[data-id=report_api]")
        flagged = page.locator("#modalBody .cs-var-row.missing")
        assert flagged.count() == 1 and "report_api" in flagged.inner_text()
        assert "required" in flagged.locator(".tables-sub").inner_text()
        assert "1 missing" in page.locator("#modalBody .side-rail-item[data-section=variables]").inner_text()

        # remove, from the row's ⋯
        flagged.locator("[data-role=more]").click()
        page.locator(".menu .menu-item", has_text="Remove").click()
        page.wait_for_function("() => !__winnow.S.caseVariables.some((v) => v.name === 'report_api')")
        page.wait_for_selector("#modalBody .cs-var-row.missing", state="detached")
        page.keyboard.press("Escape")
    finally:
        page.keyboard.press("Escape")
        _clear_variables(page, api)


def test_a_bad_variable_name_is_refused_with_a_toast(page, api):
    try:
        _open_case_settings(page)
        box = page.locator("#modalBody .cs-vars-box")
        box.locator(".cs-vars-add input[aria-label='New variable name']").fill("1 bad name")
        box.locator(".cs-vars-add .btn", has_text="Add").click()
        page.wait_for_selector("#toast:not([hidden])")
        assert "Could not add" in page.locator("#toast").inner_text()
        assert not any(v["name"] == "1 bad name" for v in api("/api/case/variables"))
    finally:
        page.keyboard.press("Escape")
        _clear_variables(page, api)


def test_new_case_dialog_asks_for_a_profiles_required_variables(page, api):
    """A saved profile with a required variable: the New case dialog shows
    the input once that profile is chosen and refuses to create the case
    until it is filled."""
    rec = api("/api/plugin_bundles", "POST", {
        "name": "UI Vars Profile", "plugins": [],
        "variables": [{"name": "engagement", "label": "Engagement", "required": True,
                       "description": "for report titles"},
                      {"name": "doc_link", "label": "Scoping doc", "default": "https://docs.local/x"}]})
    try:
        page.evaluate("() => { __winnow.showHome(); return __winnow.refreshCases(); }")
        page.wait_for_selector("#home:not([hidden]) .home-head")
        page.locator("#home .btn", has_text="New case").click()
        page.wait_for_selector("#modal:not([hidden])")
        item = page.locator(f"#modalBody .side-rail-item[data-section='{rec['id']}']")
        assert "UI Vars Profile" in item.inner_text()
        # None is chosen until a profile is, and asks for nothing
        assert page.locator("#modalBody .nc-vars").count() == 0
        item.click()
        page.wait_for_selector("#modalBody .nc-vars")
        vars_box = page.locator("#modalBody .nc-vars")
        assert "Engagement *" in vars_box.inner_text()
        assert vars_box.locator("input[data-var=doc_link]").input_value() == "https://docs.local/x"
        assert "for report titles" in vars_box.inner_text()

        # required and empty → Create refuses, case not made
        before = page.evaluate("() => fetch('/api/cases').then((r) => r.json()).then((c) => c.length)")
        page.locator("#modalBody input").first.fill("Vars gate case")
        assert "needs: Engagement" in page.locator("#modal .nc-status").inner_text()
        page.locator("#modal .nc-go", has_text="Create").click()
        page.wait_for_selector("#toast:not([hidden])")
        assert "needs: Engagement" in page.locator("#toast").inner_text()
        assert page.locator("#modal:not([hidden])").count() == 1
        assert "invalid" in vars_box.locator("input[data-var=engagement]").get_attribute("class")
        after = page.evaluate("() => fetch('/api/cases').then((r) => r.json()).then((c) => c.length)")
        assert after == before
    finally:
        page.keyboard.press("Escape")
        api(f"/api/plugin_bundles/{rec['id']}", "DELETE")
        page.evaluate("() => __winnow.showApp()")
        page.evaluate("(id) => __winnow.openSource(id)", page.evaluate("() => __winnow.S.sources[0].id"))
        page.wait_for_function("() => __winnow.S.view")


def test_applying_a_profile_prompts_for_required_variables_it_seeds(page, api, restore_plugin_overrides):
    """Applying to an existing case seeds the definitions and asks for the
    required ones in one dialog; Later leaves them flagged in Case settings."""
    rec = api("/api/plugin_bundles", "POST", {
        "name": "UI Vars Apply", "plugins": [],
        "variables": [{"name": "engagement", "label": "Engagement", "required": True}]})
    try:
        _clear_variables(page, api)
        page.keyboard.press("p")
        page.wait_for_selector(".pm-item:has-text('UI Vars Apply')")
        page.locator(".pm-item", has_text="UI Vars Apply").click()
        # Apply goes through the sheet now — the variables part is the only
        # one this profile has, and it says so before anything happens.
        page.locator(".pm-foot .btn", has_text="Apply to this case").click()
        page.wait_for_selector('.ap-part[data-part="variables"]', timeout=15_000)
        assert "Engagement" in page.locator('.ap-part[data-part="variables"]').inner_text()
        page.locator(".row-actions .btn", has_text="Apply").click()
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
        vs = {v["name"]: v for v in api("/api/case/variables")}
        assert vs["engagement"]["required"] is True and vs["engagement"]["value"] == "ACME"
    finally:
        page.keyboard.press("Escape")
        api(f"/api/plugin_bundles/{rec['id']}", "DELETE")
        _clear_variables(page, api)
