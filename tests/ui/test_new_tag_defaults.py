"""A new tag arrives with a colour of its own and the next free key.

Add tag (Edit tags) and + tag (Settings → Default tags for new cases)
both made every tag the same grey-blue with no key, so three added in a
row were three identical chips that no digit would press. Now each takes
the first colour of a nine-colour set that no tag in the list has, and
the first of keys 1–9 that no tag holds.

And a name of its own: every one was "New tag", and sessions match tags
by name, so two unrenamed ones became one tag the first time a session
was loaded back (all of the first one's rows moved onto the second).

The case's tags and the machine's template are shared by the whole UI
session, so every test puts back what it changed.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

OLD_DEFAULT = "#7f9bb5"
KEYS = [str(k) for k in range(1, 10)]


def _close_modal(page):
    page.evaluate("() => { if (!document.getElementById('modal').hidden) __winnow.closeModal(); }")


def _free_names(tags, n):
    """The next n names a new tag can take: "New tag", "New tag 2"…"""
    taken = {t["name"] for t in tags}
    out = []
    for i in range(1, 100):
        name = "New tag" if i == 1 else f"New tag {i}"
        if name not in taken:
            out.append(name)
            if len(out) == n:
                return out


def test_add_tag_twice_gives_two_tags_their_own_colour_and_key(page, api, until):
    before = api("/api/tags")["tags"]
    before_ids = {t["id"] for t in before}
    free_keys = [k for k in KEYS if k not in {t["hotkey"] for t in before}]
    assert len(free_keys) >= 2, "the shared case should leave two keys free for this test"
    names = _free_names(before, 2)

    def added():
        return [t for t in api("/api/tags")["tags"] if t["id"] not in before_ids]

    page.evaluate("() => __winnow.openTagEditor()")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Tags'")
    try:
        for n in (1, 2):
            page.locator("#modalBody button", has_text="Add tag").click()
            until(page, lambda: len(added()) == n, f"tag {n} to be added")
            # Add tag reopens the editor on the new tag; wait for that before
            # the next click lands on the dialog being replaced.
            page.wait_for_function("(rows) => { const a = document.activeElement;"
                                   " return document.querySelectorAll('#modalBody .tag-edit-row').length === rows"
                                   " && a && a.classList.contains('tag-edit-name'); }",
                                   arg=len(before) + n)
        first, second = added()
        # Two tags called "New tag", which a session load merged into one;
        # and the name the caret lands in is the one that was saved.
        assert [first["name"], second["name"]] == names
        assert page.evaluate("() => document.activeElement.value") == names[1]
        old_colours = {t["color"].lower() for t in before}
        # The symptom: both came out the same grey-blue.
        assert first["color"] != second["color"]
        assert OLD_DEFAULT not in (first["color"], second["color"])
        assert first["color"].lower() not in old_colours
        assert second["color"].lower() not in old_colours
        # ... with no key, so pressing a digit did nothing.
        assert [first["hotkey"], second["hotkey"]] == free_keys[:2]
        # The editor shows what was saved.
        rows = page.locator("#modalBody .tag-edit-row")
        last = rows.nth(rows.count() - 1)
        assert last.locator(".tag-edit-key").input_value() == second["hotkey"]
        assert last.locator(".tag-edit-color").input_value() == second["color"]
    finally:
        _close_modal(page)
        for t in added():
            api(f"/api/tags/{t['id']}", "DELETE")
        page.evaluate("() => __winnow.loadTags()")


def test_plus_tag_in_the_template_gives_the_row_its_own_colour_and_key(page, api, until):
    before = api("/api/settings/default_tags")
    try:
        page.keyboard.press("?")
        page.wait_for_selector("#modal:not([hidden])")
        page.click("#modalBody .settings-nav-item:has-text('Default tags for new cases')")
        sec = page.locator(".settings-pane:has(.settings-pane-title:has-text('Default tags for new cases'))")
        sec.locator(".tag-edit-row").first.wait_for()
        sec.locator(".btn", has_text="+ tag").click()
        until(page, lambda: len(api("/api/settings/default_tags")) == len(before) + 1, "the added row to save")
        new = api("/api/settings/default_tags")[-1]
        assert new["name"] == _free_names(before, 1)[0]
        assert new["color"] != OLD_DEFAULT
        assert new["color"].lower() not in {t["color"].lower() for t in before}
        assert new["hotkey"] == next(k for k in KEYS if k not in {t["hotkey"] for t in before})
        last = sec.locator(".tag-edit-row").last
        assert last.locator(".tag-edit-key").input_value() == new["hotkey"]
        assert last.locator(".tag-edit-color").input_value() == new["color"]
        # A second one is not a second "New tag": the template seeds every
        # new case, and Store.upsert_tag's free-name rule never sees it.
        sec.locator(".btn", has_text="+ tag").click()
        until(page, lambda: len(api("/api/settings/default_tags")) == len(before) + 2, "the second row to save")
        assert [t["name"] for t in api("/api/settings/default_tags")[-2:]] == _free_names(before, 2)
    finally:
        _close_modal(page)
        api("/api/settings/default_tags", "POST", {"tags": before})


def test_past_nine_a_new_tag_has_no_key_and_reuses_the_least_used_colour(page):
    """The edges, on the function both editors call: nine colours and nine
    keys, and what the tenth tag gets."""
    got = page.evaluate("""() => {
        const P = __winnow.NEW_TAG_COLORS;
        const make = (n) => P.slice(0, n).map((c, i) => ({ color: c, hotkey: String(i + 1) }));
        const nine = make(9);
        return {
            palette: P,
            fresh: __winnow.newTagDefaults([]),
            fourth: __winnow.newTagDefaults(make(3)),
            tenth: __winnow.newTagDefaults(nine),
            // Every colour once and the first one twice: the second one is
            // now among the least used, earliest in the set.
            eleventh: __winnow.newTagDefaults([...nine, { color: P[0].toUpperCase(), hotkey: null }]),
            // A key freed up in the middle is the one handed out next.
            gap: __winnow.newTagDefaults(nine.filter((t) => t.hotkey !== '5')),
            // Names: the first one free, counting from 2; exact, so a
            // differently-cased "new tag" doesn't take it.
            third: __winnow.newTagDefaults([{ name: 'New tag' }, { name: 'New tag 2' }]).name,
            hole: __winnow.newTagDefaults([{ name: 'New tag' }, { name: 'New tag 3' }]).name,
            cased: __winnow.newTagDefaults([{ name: 'new tag' }]).name,
        };
    }""")
    palette = got["palette"]
    assert len(palette) == 9 and len({c.lower() for c in palette}) == 9
    assert OLD_DEFAULT not in palette
    assert got["fresh"] == {"name": "New tag", "color": palette[0], "hotkey": "1"}
    assert got["fourth"] == {"name": "New tag", "color": palette[3], "hotkey": "4"}
    assert got["tenth"] == {"name": "New tag", "color": palette[0], "hotkey": None}
    assert got["eleventh"]["color"] == palette[1]
    assert got["gap"] == {"name": "New tag", "color": palette[4], "hotkey": "5"}
    assert (got["third"], got["hole"], got["cased"]) == ("New tag 3", "New tag 2", "New tag")


def test_a_case_with_the_old_orange_is_not_handed_the_new_one(page):
    """Suspicious was #d68a2e until the palette darkened it. A case from
    before still has it, and counting it as a color of its own left the new
    orange free: the next tag added came in an orange beside an orange."""
    got = page.evaluate("""() => {
        const P = __winnow.NEW_TAG_COLORS;
        return { palette: P, next: __winnow.newTagDefaults(
            [{ color: P[0] }, { color: '#D68A2E' }, { color: P[2] }]).color };
    }""")
    assert got["next"] == got["palette"][3]
