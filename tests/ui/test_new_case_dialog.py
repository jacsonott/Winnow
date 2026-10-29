"""+ New case on the side-list frame: the profiles down the side, the case
and the chosen profile's values on the right.

The dialog was a column of captioned boxes with the profile a <select> of
names. These pin what the frame has to keep true: each profile says what
it carries in the side list, the pane holds the fields and nothing to
read past them, choosing another profile loses nothing that was typed,
and the folder browser's round trip comes back to the same choice.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

PROFILE = {
    "name": "UI New Case Profile", "description": "What the new-case test chooses",
    "plugins": ["lateral_movement"],
    "dashboards": [{"name": "UI board", "widgets": [{"title": "Logons", "render": "stat", "sql": "select 1"}]}],
    "variables": [{"name": "engagement", "label": "Engagement", "required": True,
                   "description": "for report titles"},
                  {"name": "ticket", "label": "Ticket"}],
}


@pytest.fixture
def profile(api):
    rec = api("/api/plugin_bundles", "POST", PROFILE)
    try:
        yield rec
    finally:
        api(f"/api/plugin_bundles/{rec['id']}", "DELETE")


def _open(page, state="{}"):
    page.evaluate(f"() => __winnow.openNewCaseModal({state})")
    page.wait_for_selector("#modal:not([hidden]) .nc-case")


def _close(page):
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")


def _rail(page):
    return page.evaluate("""() => [...document.querySelectorAll('#modalBody .side-rail-list > *')].map((n) =>
      n.classList.contains('side-rail-group') ? '# ' + n.textContent
        : n.querySelector('.side-rail-label').textContent + ' | ' + n.querySelector('.side-rail-hint').textContent)""")


def test_the_profiles_are_down_the_side_with_what_each_carries(page, profile):
    _open(page)
    try:
        rail = _rail(page)
        # None first and chosen; the shipped profiles under the same head;
        # this machine's own under theirs, each with its counts.
        assert rail[:2] == ["# Profile", "None | This machine’s plugins"], rail
        assert any(r.startswith("KAPE triage | 1 plugin") for r in rail), rail
        assert rail.index("# Your profiles") > max(i for i, r in enumerate(rail) if r.startswith("KAPE triage")), rail
        assert "UI New Case Profile | 1 plugin · 1 widget" in rail, rail
        assert page.locator("#modalBody .side-rail-item[data-section=none]").get_attribute("aria-selected") == "true"
        assert page.locator("#modal .nc-go").text_content() == "Create case"
    finally:
        _close(page)


def test_the_pane_is_the_fields_and_nothing_to_read_past_them(page, profile):
    """"Too chatty" was the complaint about the first cut: a stats row, a
    sentence per thing the profile did, a heading and a help line over its
    values, a status line beside the button. None of that comes back: the
    side list already says what a profile carries."""
    _open(page)
    try:
        page.locator("#modalBody .nc-name").fill("Acme intrusion")
        assert page.locator("#modalBody .nc-profile").is_hidden(), "None has nothing to add under the case"
        page.locator(f"#modalBody .side-rail-item[data-section='{profile['id']}']").click()
        page.wait_for_selector("#modalBody input[data-var=engagement]")
        assert page.locator("#modalBody .nc-about").inner_text() == "What the new-case test chooses"
        text = page.locator("#modalBody .nc-pane").inner_text()
        for gone in ("PLUGIN ON", "Turns on", "Opens with", "asks for", "Not for tokens"):
            assert gone not in text, (gone, text)
        assert page.locator("#modal .table-menu-foot").inner_text().split() == ["Cancel", "Create", "case"]
    finally:
        _close(page)


def test_choosing_another_profile_keeps_what_was_typed(page, profile):
    _open(page)
    try:
        page.locator("#modalBody .nc-name").fill("Keep me")
        page.locator("#modalBody .nc-case input[list]").fill("ACME IR")
        pid = profile["id"]
        page.locator(f"#modalBody .side-rail-item[data-section='{pid}']").click()
        page.locator("#modalBody input[data-var=engagement]").fill("ACME-26")
        page.locator("#modalBody .side-rail-item[data-section=none]").click()
        page.wait_for_selector("#modalBody input[data-var=engagement]", state="detached")
        page.locator(f"#modalBody .side-rail-item[data-section='{pid}']").click()
        assert page.locator("#modalBody input[data-var=engagement]").input_value() == "ACME-26"
        assert page.locator("#modalBody .nc-name").input_value() == "Keep me"
        assert page.locator("#modalBody .nc-case input[list]").input_value() == "ACME IR"
        assert page.locator("#modalBody .nc-path").input_value().endswith("keep-me.db-winnow")
    finally:
        _close(page)


def test_enter_asks_for_what_is_missing_instead_of_creating(page, profile, api):
    """Enter in the Name box is the main button: with a required value
    empty it refuses, says which, and marks the box."""
    before = len(api("/api/cases"))
    _open(page)
    try:
        page.locator(f"#modalBody .side-rail-item[data-section='{profile['id']}']").click()
        name = page.locator("#modalBody .nc-name")
        name.fill("Enter refused")
        name.press("Enter")
        page.wait_for_selector("#toast:not([hidden])")
        assert "needs: Engagement" in page.locator("#toast").inner_text()
        box = page.locator("#modalBody input[data-var=engagement]")
        assert "invalid" in box.get_attribute("class")
        assert page.evaluate("() => document.activeElement.dataset.var") == "engagement"
        box.fill("x")
        assert "invalid" not in box.get_attribute("class")
    finally:
        _close(page)
    assert len(api("/api/cases")) == before


def test_browse_comes_back_to_the_same_profile_and_values(page, profile):
    """Browse… swaps the dialog for the folder picker, so everything is
    carried across in a snapshot — the chosen profile and what was typed
    into its values included."""
    _open(page)
    try:
        pid = str(profile["id"])
        page.locator("#modalBody .nc-name").fill("Round trip")
        page.locator(f"#modalBody .side-rail-item[data-section='{pid}']").click()
        page.locator("#modalBody input[data-var=ticket]").fill("IR-1234")
        page.locator("#modalBody .btn", has_text="Browse…").click()
        page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Choose a folder'")
        page.locator("#modal .table-menu-foot .btn", has_text="Cancel").click()
        page.wait_for_selector("#modal:not([hidden]) .nc-case")
        assert page.locator(f"#modalBody .side-rail-item[data-section='{pid}']").get_attribute("aria-selected") == "true"
        assert page.locator("#modalBody input[data-var=ticket]").input_value() == "IR-1234"
        assert page.locator("#modalBody .nc-name").input_value() == "Round trip"
    finally:
        _close(page)
