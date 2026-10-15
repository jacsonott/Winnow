"""Both tag list editors save as they change, with no Save button to forget.

Edit tags had a Save on every row and Settings → Default tags for new
cases one Save template for the whole list; a rename typed and closed
without it was gone, with nothing to say so. Now the colour and the key
save at once and the name once typing pauses or the box is left — and a
name emptied while it is being retyped is never sent, because the
template's server side drops a nameless entry: a pause mid-retype would
have deleted the row being edited.

The case's tags and the machine's template are shared by the whole UI
session, so every test puts back what it changed.
"""

from __future__ import annotations

import json
import re
import time

import pytest

pytestmark = pytest.mark.ui


def _until(probe, what, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe():
            return
        time.sleep(0.1)
    pytest.fail(f"timed out waiting for {what}")


def _server_tag(api, tag_id):
    return next((t for t in api("/api/tags")["tags"] if t["id"] == tag_id), None)


@pytest.fixture
def probe_tag(page, api):
    tag = api("/api/tags", "POST", {"name": "autosave probe", "color": "#123456", "hotkey": None})
    page.evaluate("() => __winnow.loadTags()")
    page.wait_for_function("(id) => __winnow.S.tags.some((t) => t.id === id)", arg=tag["id"])
    yield tag
    page.evaluate("() => { if (!document.getElementById('modal').hidden) __winnow.closeModal(); }")
    if _server_tag(api, tag["id"]):
        api(f"/api/tags/{tag['id']}", "DELETE")
    page.evaluate("() => __winnow.loadTags()")


def _open_editor_row(page, name):
    page.evaluate("() => __winnow.openTagEditor()")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Tags'")
    rows = page.locator("#modalBody .tag-edit-row")
    for i in range(rows.count()):
        if rows.nth(i).locator(".tag-edit-name").input_value() == name:
            return rows.nth(i)
    pytest.fail(f"no row for {name!r} in Edit tags")


def test_edit_tags_saves_a_rename_a_key_and_a_colour_with_nothing_pressed(page, api, probe_tag):
    row = _open_editor_row(page, "autosave probe")
    # The symptom: a Save per row that the analyst had to remember.
    assert page.locator("#modalBody button", has_text=re.compile(r"^\s*Save\s*$")).count() == 0

    row.locator(".tag-edit-name").fill("autosave renamed")
    _until(lambda: _server_tag(api, probe_tag["id"])["name"] == "autosave renamed", "the rename to save")
    page.wait_for_function("(id) => __winnow.S.tags.find((t) => t.id === id).name === 'autosave renamed'",
                           arg=probe_tag["id"])
    state = row.locator(".tag-edit-state")
    state.evaluate("(n) => new Promise((ok) => { const t = setInterval(() => {"
                   " if (n.textContent !== 'Saving…') { clearInterval(t); ok(); } }, 50); })")
    assert state.inner_text() in ("Saved", ""), state.inner_text()

    row.locator(".tag-edit-key").fill("8")
    _until(lambda: _server_tag(api, probe_tag["id"])["hotkey"] == "8", "the key to save")
    row.locator(".tag-edit-color").fill("#ff0000")
    _until(lambda: _server_tag(api, probe_tag["id"])["color"] == "#ff0000", "the colour to save")
    # The case's own list follows, so the key works without reopening
    # anything — the ribbon and the hotkeys read S.tags.
    page.wait_for_function(
        "(id) => { const t = __winnow.S.tags.find((x) => x.id === id); return t.hotkey === '8' && t.color === '#ff0000'; }",
        arg=probe_tag["id"])


def test_add_tag_puts_the_caret_in_the_new_name(page, api):
    """Add tag makes "New tag"; the name is what the analyst does next,
    so it arrives selected and is typed straight over."""
    before = {t["id"] for t in api("/api/tags")["tags"]}
    page.evaluate("() => __winnow.openTagEditor()")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Tags'")
    try:
        page.locator("#modalBody button", has_text="Add tag").click()
        page.wait_for_function("() => { const a = document.activeElement;"
                               " return a && a.classList.contains('tag-edit-name') && a.value === 'New tag'"
                               " && a.selectionStart === 0 && a.selectionEnd === a.value.length; }")
        page.keyboard.type("Typed over")
        _until(lambda: any(t["name"] == "Typed over" for t in api("/api/tags")["tags"] if t["id"] not in before),
               "the new tag's name to save")
    finally:
        page.evaluate("() => { if (!document.getElementById('modal').hidden) __winnow.closeModal(); }")
        for t in api("/api/tags")["tags"]:
            if t["id"] not in before:
                api(f"/api/tags/{t['id']}", "DELETE")
        page.evaluate("() => __winnow.loadTags()")


def test_a_name_typed_and_closed_straight_away_is_kept(page, api, probe_tag):
    """Closing inside the pause used to be the case where work was lost
    (with a Save button it was lost however long you waited)."""
    row = _open_editor_row(page, "autosave probe")
    row.locator(".tag-edit-name").fill("closed at once")
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")
    _until(lambda: _server_tag(api, probe_tag["id"])["name"] == "closed at once", "the name to save after closing")


def test_an_emptied_name_is_not_saved_and_comes_back(page, api, probe_tag):
    row = _open_editor_row(page, "autosave probe")
    name = row.locator(".tag-edit-name")
    name.fill("")
    # Past the pause: a save would have gone by now.
    page.wait_for_timeout(1200)
    assert _server_tag(api, probe_tag["id"])["name"] == "autosave probe"
    assert row.locator(".tag-edit-state").inner_text() == "Needs a name"
    # Leaving the box puts the saved name back rather than keeping a blank
    # that looks saved.
    name.press("Tab")
    assert name.input_value() == "autosave probe"
    assert row.locator(".tag-edit-state").inner_text() == ""
    assert _server_tag(api, probe_tag["id"])["name"] == "autosave probe"


def test_a_refused_save_says_why_and_does_not_claim_saved(page, api, probe_tag):
    detail = "Tag no longer exists in this case"

    def refuse(route):
        if route.request.method == "POST":
            route.fulfill(status=409, content_type="application/json", body=json.dumps({"detail": detail}))
        else:
            route.continue_()
    page.route("**/api/tags", refuse)
    try:
        row = _open_editor_row(page, "autosave probe")
        row.locator(".tag-edit-name").fill("never lands")
        page.wait_for_function(
            "(t) => { const n = document.getElementById('toast'); return !n.hidden && n.textContent.includes(t); }",
            arg=detail, timeout=5000)
        assert row.locator(".tag-edit-state").inner_text() == "Not saved"
    finally:
        page.unroute("**/api/tags", refuse)
    assert _server_tag(api, probe_tag["id"])["name"] == "autosave probe"


def test_the_default_tag_template_saves_as_it_changes(page, api):
    before = api("/api/settings/default_tags")
    try:
        page.keyboard.press("?")
        page.wait_for_selector("#modal:not([hidden])")
        page.click(".settings-section-head:has-text('Default tags for new cases')")
        sec = page.locator(".settings-section:has(.settings-section-head:has-text('Default tags for new cases'))")
        sec.locator(".tag-edit-row").first.wait_for()
        assert sec.locator("button", has_text="Save template").count() == 0

        sec.locator(".btn", has_text="+ tag").click()
        _until(lambda: len(api("/api/settings/default_tags")) == len(before) + 1, "the added row to save")
        # The new row's name has the caret with its text selected, so the
        # name is typed straight over "New tag".
        page.wait_for_function("() => { const a = document.activeElement;"
                               " return a && a.classList.contains('tag-edit-name') && a.value === 'New tag'"
                               " && a.selectionStart === 0 && a.selectionEnd === a.value.length; }")
        page.keyboard.type("Lateral")
        _until(lambda: api("/api/settings/default_tags")[-1]["name"] == "Lateral", "the new name to save")

        last = sec.locator(".tag-edit-row").last
        last.locator(".tag-edit-key").fill("7")
        _until(lambda: api("/api/settings/default_tags")[-1]["hotkey"] == "7", "the key to save")

        # Emptying a template name must not drop the row server-side.
        last.locator(".tag-edit-name").fill("")
        page.wait_for_timeout(1200)
        assert [t["name"] for t in api("/api/settings/default_tags")][-1] == "Lateral"

        last.locator(".tag-edit-remove").click()
        _until(lambda: api("/api/settings/default_tags") == before, "the removal to save")
        assert sec.locator(".tag-edit-row").count() == len(before)
    finally:
        page.evaluate("() => { if (!document.getElementById('modal').hidden) __winnow.closeModal(); }")
        api("/api/settings/default_tags", "POST", {"tags": before})
