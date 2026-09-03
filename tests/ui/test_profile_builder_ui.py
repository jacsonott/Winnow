"""The profile builder: build a profile from the bundles menu — plugins,
dashboards and the variables a case of this type must carry — then edit
it, and see its required variable demanded by the New case dialog."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

NAME = "UI Builder Profile"


def _open_builder(page):
    page.keyboard.press("M")
    page.wait_for_selector("#modal:not([hidden])")
    page.locator("#modalBody .btn", has_text="New profile").click()
    page.wait_for_selector("#modalBody .pb-head input")


def _cleanup(api):
    for b in api("/api/plugin_bundles"):
        if not b.get("shipped") and b["name"].lower().startswith("ui builder"):
            api(f"/api/plugin_bundles/{b['id']}", "DELETE")


def test_build_a_profile_with_plugins_dashboards_and_variables(page, api):
    try:
        _open_builder(page)
        assert page.locator("#modalTitle").inner_text().lower() == "new profile"
        page.locator("#modalBody .pb-head input").first.fill(NAME)
        page.locator("#modalBody .pb-head input").nth(1).fill("What the UI test opens with")

        # plugins: start from a known state rather than whatever is enabled
        boxes = page.locator("#modalBody .settings-section", has_text="Plugins").locator(".pb-row input")
        for i in range(boxes.count()):
            if boxes.nth(i).is_checked():
                boxes.nth(i).uncheck()
        boxes.first.check()

        # a required variable, declared here, demanded at case creation
        page.locator("#modalBody .btn", has_text="Add variable").click()
        row = page.locator("#modalBody .pb-var").first
        row.locator("input").nth(0).fill("engagement")
        row.locator("input").nth(1).fill("Engagement name")
        row.locator("input").nth(2).fill("for report titles")
        row.locator(".pb-req input").check()

        page.locator("#modalBody .btn", has_text="Create profile").click()
        page.wait_for_selector("#toast:not([hidden])")
        assert "saved" in page.locator("#toast").inner_text()

        saved = next(b for b in api("/api/plugin_bundles") if b["name"] == NAME)
        assert saved["description"] == "What the UI test opens with"
        assert len(saved["plugins"]) == 1, saved["plugins"]
        assert saved["variables"] == [{"name": "engagement", "label": "Engagement name",
                                       "description": "for report titles", "default": "",
                                       "required": True}]

        # the menu reopens showing what it carries
        page.wait_for_selector(f"#modalBody .session-row:has-text('{NAME}')")
        assert "1 variable (1 required)" in page.locator(
            "#modalBody .session-row", has_text=NAME).inner_text()
    finally:
        page.keyboard.press("Escape")
        _cleanup(api)


def test_a_bad_variable_name_is_refused_before_saving(page, api):
    try:
        _open_builder(page)
        page.locator("#modalBody .pb-head input").first.fill("UI Builder Bad Name")
        page.locator("#modalBody .btn", has_text="Add variable").click()
        page.locator("#modalBody .pb-var input").first.fill("2 bad")
        page.locator("#modalBody .btn", has_text="Create profile").click()
        page.wait_for_selector("#toast:not([hidden])")
        assert "is not a variable name" in page.locator("#toast").inner_text()
        assert not any(b["name"] == "UI Builder Bad Name" for b in api("/api/plugin_bundles"))
    finally:
        page.keyboard.press("Escape")
        _cleanup(api)


def test_editing_an_existing_profile_reopens_it_filled_in(page, api):
    api("/api/plugin_bundles", "POST", {
        "name": NAME, "plugins": [], "description": "before",
        "variables": [{"name": "engagement", "label": "Engagement", "required": True}]})
    try:
        page.keyboard.press("M")
        page.wait_for_selector(f".session-row:has-text('{NAME}')")
        page.locator(".session-row", has_text=NAME).locator(".btn", has_text="✎").click()
        page.wait_for_selector("#modalBody .pb-head input")
        assert page.locator("#modalTitle").inner_text().lower().startswith("edit profile")
        assert page.locator("#modalBody .pb-head input").first.input_value() == NAME
        assert page.locator("#modalBody .pb-var input").first.input_value() == "engagement"
        assert page.locator("#modalBody .pb-req input").first.is_checked()

        page.locator("#modalBody .pb-head input").nth(1).fill("after")
        page.locator("#modalBody .btn", has_text="Save changes").click()
        page.wait_for_selector("#toast:not([hidden])")
        saved = next(b for b in api("/api/plugin_bundles") if b["name"] == NAME)
        assert saved["description"] == "after" and saved["variables"][0]["required"] is True
    finally:
        page.keyboard.press("Escape")
        _cleanup(api)


def test_a_shipped_profile_opens_as_a_copy(page, api):
    try:
        page.keyboard.press("M")
        page.wait_for_selector(".session-row:has-text('KAPE triage')")
        page.locator(".session-row", has_text="KAPE triage").locator(".btn", has_text="Copy").click()
        page.wait_for_selector("#modalBody .pb-head input")
        assert page.locator("#modalTitle").inner_text().lower().startswith("new profile from")
        assert page.locator("#modalBody .pb-head input").first.input_value() == "KAPE triage (copy)"
        # the shipped profile's own boards come along, pre-checked
        assert page.locator("#modalBody .pb-row", has_text="KAPE host overview").count() == 1
    finally:
        page.keyboard.press("Escape")
        _cleanup(api)
