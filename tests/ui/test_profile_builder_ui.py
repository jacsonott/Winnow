"""The profile builder: build a profile from the bundles menu — plugins,
dashboards and the variables a case of this type must carry — then edit
it, and see its required variable demanded by the New case dialog."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

NAME = "UI Builder Profile"


def _open_builder(page):
    page.keyboard.press("p")
    page.wait_for_selector("#modal:not([hidden])")
    page.wait_for_selector(".pm-item", timeout=15_000)   # the manager's list loads async
    page.locator("#modalBody .btn", has_text="New profile").click()
    _wait_for_builder(page)


def _wait_for_builder(page):
    """The name field is drawn before the plugin and dashboard lists are
    fetched, so waiting on it alone races their awaits — wait for both
    sections to have finished painting (rows, or the note that says why
    there are none)."""
    page.wait_for_selector("#modalBody .pb-head input")
    for section in ("plugins", "dashboards"):
        page.wait_for_function(
            """(s) => !!document.querySelector(
                 `#modalBody .pb-sec[data-sec="${s}"] :is(.pb-row, .note-status)`)""",
            arg=section, timeout=15_000)


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
        boxes = page.locator('#modalBody .pb-sec[data-sec="plugins"] .pb-row input')
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

        # the manager reopens with the new profile selected, showing what
        # it carries rather than one ellipsised line of it
        page.wait_for_selector(f"#modalBody .pm-item:has-text('{NAME}')")
        assert page.locator(".pm-detail h3").inner_text() == NAME
        assert "1 · 1 required" in page.locator('.pm-sec[data-sec="variables"]').inner_text()
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
        page.keyboard.press("p")
        page.wait_for_selector(f".pm-item:has-text('{NAME}')")
        page.locator(".pm-item", has_text=NAME).click()
        page.locator(".pm-head-acts .btn", has_text="Edit…").click()
        _wait_for_builder(page)
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
        page.keyboard.press("p")
        page.wait_for_selector(".pm-item:has-text('KAPE triage')")
        page.locator(".pm-item", has_text="KAPE triage").click()
        page.locator(".pm-head-acts .btn", has_text="Copy to edit").click()
        _wait_for_builder(page)
        assert page.locator("#modalTitle").inner_text().lower().startswith("new profile from")
        assert page.locator("#modalBody .pb-head input").first.input_value() == "KAPE triage (copy)"
        # the shipped profile's own board comes along, pre-checked
        page.wait_for_selector("#modalBody .pb-row:has-text('the profile\u2019s own board')")
        assert page.locator("#modalBody .pb-row", has_text="KAPE triage").count() == 1

        # Saving it records the lineage and carries the parts the builder
        # has no editor for: a copy that lost the shipped watchlist looked
        # like a bug from every angle except the builder's.
        page.locator("#modalBody .pb-head input").first.fill("UI Builder Copy")
        page.locator("#modalBody .btn", has_text="Create profile").click()
        page.wait_for_selector("#toast:not([hidden])")
        shipped = next(b for b in api("/api/plugin_bundles") if b["name"] == "KAPE triage")
        saved = next(b for b in api("/api/plugin_bundles") if b["name"] == "UI Builder Copy")
        assert saved["from_profile"] == "KAPE triage"
        assert saved["from_version"] == shipped["version"]
        assert saved["update_available"] is None, "a copy of today's profile has nothing to take"
        assert [i["value"] for i in saved["watchlist"]] == [i["value"] for i in shipped["watchlist"]]
    finally:
        page.keyboard.press("Escape")
        _cleanup(api)


# ---------------------------------------------------------------------------
# The two-pane builder. It was one long column: a three-line intro, then
# Plugins, Dashboards and Variables as collapsible Settings sections, each
# variable a row of five boxes whose only captions were their placeholders
# (gone the moment anything was typed), and nothing anywhere that said what
# applying the profile would do. The tests below pin what replaced that, in
# the terms an analyst would notice it missing.

def _box(page, selector):
    return page.locator(selector).first.bounding_box()


def _summary(page):
    return page.locator("#modalBody .pb-summary").inner_text()


def _add_variable(page, name, label="", desc="", default="", required=False):
    cards = page.locator("#modalBody .pb-var")
    n = cards.count()
    page.locator("#modalBody .btn", has_text="Add variable").click()
    card = cards.nth(n)      # this card, not whichever is last later on
    card.locator("input").nth(0).fill(name)
    card.locator("input").nth(1).fill(label)
    card.locator("input").nth(2).fill(desc)
    card.locator("input").nth(3).fill(default)
    if required:
        card.locator(".pb-req input").check()
    return card


def test_what_a_case_gets_sits_beside_what_the_profile_turns_on(page):
    """Two panes side by side in an ordinary window: the variables are not
    at the bottom of a column under every plugin and board."""
    try:
        _open_builder(page)
        heads = page.locator("#modalBody .pb-pane-head").all_inner_texts()
        assert heads == ["What the profile turns on", "What a case gets"], heads
        plugins = _box(page, '#modalBody .pb-sec[data-sec="plugins"]')
        variables = _box(page, '#modalBody .pb-sec[data-sec="variables"]')
        assert variables["x"] >= plugins["x"] + plugins["width"], (plugins, variables)
        # ...and level with it, not pushed below it
        assert variables["y"] < plugins["y"] + plugins["height"], (plugins, variables)
        # the profile's own fields are labelled, not left to a placeholder
        labels = page.locator("#modalBody .pb-head .dlg-field > label").all_inner_texts()
        assert labels == ["Profile name", "Description"], labels
    finally:
        page.keyboard.press("Escape")


def test_every_box_of_a_variable_says_what_it_is_once_it_is_filled_in(page):
    """The old row's captions were placeholders, which vanish as soon as
    the box has a value — a filled-in variable was five unlabelled boxes.
    Every box in a card is named by a visible label of its own."""
    try:
        _open_builder(page)
        _add_variable(page, "engagement", "Engagement name", "for report titles", "ACME")
        named = page.evaluate("""() => [...document.querySelectorAll('#modalBody .pb-var input')].map((i) => {
            const l = i.type === 'checkbox' ? i.closest('label')
              : document.querySelector(`label[for="${i.id}"]`);
            return [i.type, !!l && l.getBoundingClientRect().height > 0 ? l.textContent.trim() : null];
          })""")
        assert named == [["text", "Name"], ["text", "Label"], ["text", "What it’s for"],
                         ["text", "Default"], ["checkbox", "Required at case creation"]], named
    finally:
        page.keyboard.press("Escape")


def test_the_summary_says_what_applying_it_does_as_the_picks_change(page):
    try:
        _open_builder(page)
        boxes = page.locator('#modalBody .pb-sec[data-sec="plugins"] .pb-row input')
        assert boxes.count() >= 2, "needs two plugins (the bundled examples are enough)"
        for i in range(boxes.count()):
            if boxes.nth(i).is_checked():
                boxes.nth(i).uncheck()
        assert _summary(page).startswith("Turns every plugin off")

        boxes.nth(0).check()
        assert _summary(page).startswith("Turns on 1 plugin ·")
        boxes.nth(1).check()
        assert _summary(page).startswith("Turns on 2 plugins ·")

        boards = page.locator('#modalBody .pb-sec[data-sec="dashboards"] .pb-row input:checked').count()
        assert (f"opens {boards} dashboard" if boards else "opens no dashboards") in _summary(page)

        assert _summary(page).endswith("asks for nothing")
        card = _add_variable(page, "engagement", "Engagement name", required=True)
        assert _summary(page).endswith("asks for 1 value (1 required)")
        _add_variable(page, "ticket")
        assert _summary(page).endswith("asks for 2 values (1 required)")
        card.locator(".pb-req input").uncheck()
        assert _summary(page).endswith("asks for 2 values")
        card.locator(".pb-var-del").click()
        assert _summary(page).endswith("asks for 1 value")
    finally:
        page.keyboard.press("Escape")


def test_the_preview_is_the_values_prompt_and_takes_no_typing(page):
    """What the builder previews is what the values prompt shows for the
    same variables — label, star, what it's for, the default in the box —
    because both are drawn by the prompt's own code. And it is a picture:
    clicking into it doesn't leave a caret in a box nobody reads."""
    try:
        _open_builder(page)
        assert "Nothing" in page.locator("#modalBody .pb-preview").inner_text()
        _add_variable(page, "engagement", "Engagement name", "for report titles", required=True)
        _add_variable(page, "api_base", "", "where the backend lives", "http://example")

        read = """(root) => [...root.querySelectorAll('.dlg-field')].map((f) => ({
            label: f.querySelector('label').textContent,
            help: (f.querySelector('small') || {}).textContent || null,
            value: f.querySelector('input').value,
            placeholder: f.querySelector('input').placeholder }))"""
        preview = page.locator("#modalBody .pb-preview").evaluate(read)
        assert [p["label"] for p in preview] == ["Engagement name *", "api_base"], preview

        page.locator("#modalBody .pb-preview input").first.click(force=True)
        assert not page.evaluate(
            "() => !!document.activeElement.closest('.pb-preview')"), "the preview took focus"

        defs = page.evaluate("""() => [...document.querySelectorAll('#modalBody .pb-var')].map((c) => c.read())""")
        page.evaluate("(defs) => { __winnow.promptForVariables(defs); }", defs)
        page.wait_for_selector("#modalBody .case-var-prompt")
        prompt = page.locator("#modalBody").evaluate(read)
        assert preview == prompt
    finally:
        page.keyboard.press("Escape")


def test_a_bad_variable_name_is_flagged_under_the_box_before_saving(page):
    try:
        _open_builder(page)
        card = _add_variable(page, "2 bad")
        assert "Not a variable name" in card.locator(".pb-hint").inner_text()
        card.locator("input").first.fill("engagement")
        assert not card.locator(".pb-hint").is_visible()
        other = _add_variable(page, "Engagement")
        assert "Another variable has this name" in other.locator(".pb-hint").inner_text()
        assert "Another variable has this name" in card.locator(".pb-hint").inner_text()
    finally:
        page.keyboard.press("Escape")


def test_a_name_that_would_overwrite_a_profile_says_so_before_save(page, api):
    """Profiles are saved by name: a taken name replaces that profile, and
    renaming one while editing saves a second profile beside it. Neither
    was said anywhere before Save."""
    api("/api/plugin_bundles", "POST", {"name": "UI Builder Taken", "plugins": []})
    try:
        _open_builder(page)
        page.locator("#modalBody .pb-head input").first.fill("ui builder taken")
        hint = page.locator("#modalBody .pb-head .pb-hint")
        assert "replaces your profile “UI Builder Taken”" in hint.inner_text()
        page.keyboard.press("Escape")

        page.evaluate("() => __winnow.openPluginBundlesModal()")
        page.wait_for_selector(".pm-item:has-text('UI Builder Taken')")
        page.locator(".pm-item", has_text="UI Builder Taken").click()
        page.locator(".pm-head-acts .btn", has_text="Edit…").click()
        _wait_for_builder(page)
        assert not hint.is_visible(), "its own name is not taken"
        page.locator("#modalBody .pb-head input").first.fill("UI Builder Renamed")
        assert "“UI Builder Taken” stays as it is" in hint.inner_text()
    finally:
        page.keyboard.press("Escape")
        _cleanup(api)


def test_save_waits_for_the_plugin_list(page, api):
    """The panes are drawn before the plugin and dashboard lists arrive.
    A Save in that gap would have written a profile with no plugins and
    no boards, so it stays off until they land."""
    held = []
    page.route("**/api/plugins", lambda route: held.append(route))
    try:
        page.keyboard.press("p")
        page.wait_for_selector(".pm-item", timeout=15_000)
        page.locator("#modalBody .btn", has_text="New profile").click()
        page.wait_for_selector("#modalBody .pb-head input")
        page.locator("#modalBody .pb-head input").first.fill("UI Builder Early")
        create = page.locator("#modalBody .btn", has_text="Create profile")
        for _ in range(100):
            if held:
                break
            page.wait_for_timeout(50)
        assert held, "the builder never asked for the plugin list"
        assert create.is_disabled()
        assert "Reading" in _summary(page)
        create.click(force=True)
        page.wait_for_timeout(300)
        assert not any(b["name"] == "UI Builder Early" for b in api("/api/plugin_bundles"))

        for r in held:
            r.continue_()
        page.unroute("**/api/plugins")
        _wait_for_builder(page)
        assert create.is_enabled()
        assert not _summary(page).startswith("Reading")
    finally:
        page.keyboard.press("Escape")
        _cleanup(api)


def test_the_panes_stack_in_a_narrow_window(page):
    """Below the merge builder's breakpoint the two panes stack rather than
    squeezing the cards into slivers, nothing scrolls sideways, and the
    dialog's Save stays on screen as it scrolls."""
    try:
        page.set_viewport_size({"width": 760, "height": 760})
        _open_builder(page)
        _add_variable(page, "engagement", "Engagement name")
        left = _box(page, "#modalBody .pb-pane")
        right = _box(page, "#modalBody .pb-pane.pb-result")
        assert right["y"] >= left["y"] + left["height"] - 1, (left, right)
        assert abs(right["x"] - left["x"]) <= 1 and abs(right["width"] - left["width"]) <= 1
        assert page.locator("#modalBody").evaluate("(b) => b.scrollWidth <= b.clientWidth")
        page.locator("#modalBody").evaluate("(b) => { b.scrollTop = 0; }")
        create = _box(page, "#modalBody .pb-actions .btn.primary")
        assert create["y"] + create["height"] <= 760, create
    finally:
        page.keyboard.press("Escape")
        page.set_viewport_size({"width": 1500, "height": 900})
