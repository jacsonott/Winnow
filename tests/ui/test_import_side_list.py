"""Import a folder in the derive dialog's shape: the profiles listed down
the left, searchable, with pinned ones on top, and the folder, its
settings and the scan's file list on the right.

It was a centred column sized to its content: a profile <select> on top,
then the settings, then every file the scan matched. Picking a profile
rebuilt it, a rescan after each keystroke in a pattern changed its
height, and so the card re-centred and its header slid; a folder with a
dozen matches pushed the Import button off the bottom of the screen. The
test that matters most here asserts that symptom: nothing in the frame
moves while the analyst goes through every profile, both lists and a
pattern that matches nothing."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui

PINS = """() => { try { const all = JSON.parse(localStorage.getItem('winnow.menupins') || '{}');
  return all.import || []; } catch { return []; } }"""


def _rail(page):
    """The list as it reads: [heading, [entry labels]] per section; a
    group's note stands in for its entries as ['(note)']."""
    return page.evaluate("""() => {
      const out = [];
      for (const n of document.querySelectorAll('#modalBody .side-rail-list > *')) {
        if (n.classList.contains('side-rail-group')) out.push([n.textContent, []]);
        else if (n.classList.contains('side-rail-row'))
          out[out.length - 1][1].push(n.querySelector('.side-rail-label').textContent);
        else if (n.classList.contains('side-rail-note')) out[out.length - 1][1].push('(note)');
      }
      return out;
    }""")


def _mine(rail, names):
    """The rail with only this test's profiles left in it — the shared
    server's workspace may hold others'."""
    return [[g, [e for e in entries if e in names or g == "Ad hoc"]] for g, entries in rail]


def _selected(page):
    return page.locator('#modalBody .side-rail-item[aria-selected="true"] .side-rail-label').inner_text()


def _settled(page):
    """The preview shows the answer to the latest change."""
    page.wait_for_selector('#modalBody .side-preview[aria-busy="false"]')


def _pick(page, label):
    page.locator("#modalBody .side-rail-item", has=page.locator(
        ".side-rail-label", has_text=label)).click()
    page.wait_for_selector(f'#modalBody .side-rail-item[aria-selected="true"] >> text="{label}"')


def _pressed(page):
    return page.evaluate("""() => [...document.querySelectorAll('#modalBody .import-types .btn')]
      .filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.dataset.ext)""")


def _files(page):
    return page.evaluate("""() => [...document.querySelectorAll('#modalBody .import-file-path')]
      .map((n) => n.textContent)""")


@pytest.fixture
def folder(tmp_path):
    """A KAPE-shaped tree: EZTools CSVs in subfolders, one of them the
    Amcache list a KAPE profile leaves out, plus a JSON and a note."""
    root = tmp_path / "WS01"
    for rel in ["EventLogs/EvtxECmd_Output.csv", "FileSystem/MFTECmd_Output.csv",
                "ProgramExecution/PECmd_Output.csv", "ProgramExecution/Amcache_AssociatedFileEntries.csv",
                "ProgramExecution/Amcache_UnassociatedFileEntries.csv", "Browser/history.json", "notes.txt"]:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"a": 1}\n' if rel.endswith(".json") else "Host,User\nh1,alice\n", encoding="utf-8")
    return root


@pytest.fixture
def profiles(api):
    made = []

    def _make(name, **kw):
        rec = api("/api/import_profiles", "POST", {"name": name, **kw})
        made.append(rec["id"])
        return rec
    yield _make
    for pid in made:
        api(f"/api/import_profiles/{pid}", "DELETE")


@pytest.fixture
def kape(profiles):
    return profiles("UI side KAPE", extensions=[".csv"], include_patterns=["*Cmd*", "*Amcache*"],
                    exclude_patterns=["*_UnassociatedFileEntries.csv"], recursive=True)


@pytest.fixture
def velo(profiles):
    return profiles("UI side Velociraptor", extensions=[".json"], recursive=False)


def _open(page, root=None, **state):
    page.evaluate("(s) => __winnow.openDirectoryImportModal(s)", {**state, **({"root": str(root)} if root else {})})
    page.wait_for_selector("#modal:not([hidden]) .side-rail-item")
    if root:
        _settled(page)


def test_the_profiles_are_listed_down_the_side(page, folder, kape, velo):
    _open(page, folder)
    names = {kape["name"], velo["name"]}
    assert _mine(_rail(page), names) == [
        ["Ad hoc", ["Custom (not saved)"]],
        ["Profiles", ["UI side KAPE", "UI side Velociraptor"]],
    ], _rail(page)
    # It opens on Custom: every built-in file type on, no patterns, and
    # the caret in the search box.
    assert _selected(page) == "Custom (not saved)"
    assert page.evaluate("() => document.activeElement.classList.contains('side-search')")
    assert ".csv" in _pressed(page) and ".json" in _pressed(page) and "*" not in _pressed(page)
    assert len(_files(page)) == 7
    assert page.locator("#modalBody .row-actions .btn").all_inner_texts() == [
        "Save as profile…", "Import checked (7)", "Cancel"]


def test_picking_a_profile_puts_its_settings_on_screen(page, folder, kape, velo):
    _open(page, folder)
    _pick(page, "UI side KAPE")
    _settled(page)
    assert _pressed(page) == [".csv"]
    assert page.locator("#modalBody .import-pattern textarea").nth(0).input_value() == "*Cmd*\n*Amcache*"
    assert page.locator("#modalBody .import-pattern textarea").nth(1).input_value() == "*_UnassociatedFileEntries.csv"
    assert sorted(_files(page)) == [
        "EventLogs/EvtxECmd_Output.csv", "FileSystem/MFTECmd_Output.csv",
        "ProgramExecution/Amcache_AssociatedFileEntries.csv", "ProgramExecution/PECmd_Output.csv"]
    # A chosen profile can be updated or deleted; Custom can only be saved.
    assert page.locator("#modalBody .row-actions .btn").all_inner_texts() == [
        "Update profile", "Delete profile", "Import checked (4)", "Cancel"]

    # The other list says what was passed over, and why.
    page.locator("#modalBody .import-lists .btn", has_text="Excluded").click()
    rows = page.locator("#modalBody .import-file.excluded")
    assert rows.count() == 3
    assert "excluded by pattern: *_UnassociatedFileEntries.csv" in rows.all_inner_texts()[
        [t.split("\n")[0] for t in rows.all_inner_texts()].index("ProgramExecution/Amcache_UnassociatedFileEntries.csv")]

    _pick(page, "UI side Velociraptor")
    _settled(page)
    assert _pressed(page) == [".json"]
    assert not page.locator("#modalBody .import-subfolders input").is_checked()

    # Back to Custom is back to every file type and no patterns.
    _pick(page, "Custom (not saved)")
    _settled(page)
    assert ".csv" in _pressed(page) and ".plaso" in _pressed(page)
    assert page.locator("#modalBody .import-subfolders input").is_checked()
    assert page.locator("#modalBody .import-pattern textarea").nth(0).input_value() == ""
    assert page.locator("#modalBody .row-actions .btn").all_inner_texts()[0] == "Save as profile…"


def test_nothing_in_the_dialog_moves_as_the_choices_change(page, folder, kape, velo):
    """The complaint, as it was made about the derive dialog and as it
    held here too: the card's size and position changed with what was in
    it. Every profile, both lists, a pattern that matches nothing and one
    that matches everything — the card, the list, the folder row, the
    settings, the preview and the Import button stay exactly where they
    are."""
    _open(page, folder)
    where = """() => {
      const r = (sel) => { const n = document.querySelector(sel); if (!n) return null;
        const b = n.getBoundingClientRect(); return [Math.round(b.left), Math.round(b.top), Math.round(b.width), Math.round(b.height)]; };
      const go = [...document.querySelectorAll('#modalBody .row-actions .btn')].find((b) => b.textContent.startsWith('Import'));
      const g = go.getBoundingClientRect();
      return { card: r('.modal-card'), list: r('#modalBody .side-rail-list'),
               folder: r('#modalBody .import-folder-row'), settings: r('#modalBody .side-settings'),
               preview: r('#modalBody .side-preview'),
               // its right edge and top: the label carries a count, so its width may change
               go: [Math.round(g.right), Math.round(g.top)] };
    }"""
    before = page.evaluate(where)
    assert all(before.values()), before
    for label in ["UI side KAPE", "UI side Velociraptor", "Custom (not saved)", "UI side KAPE"]:
        _pick(page, label)
        _settled(page)
        assert page.evaluate(where) == before, f"the dialog moved on picking {label}"
    page.locator("#modalBody .import-lists .btn", has_text="Excluded").click()
    assert page.evaluate(where) == before, "the dialog moved on showing the excluded files"
    page.locator("#modalBody .import-lists .btn", has_text="Will import").click()
    include = page.locator("#modalBody .import-pattern textarea").nth(0)
    include.fill("*nothing-is-called-this*")
    _settled(page)
    assert page.locator("#modalBody .side-preview-rows").inner_text() == "No files match."
    assert page.evaluate(where) == before, "the dialog moved when nothing matched"
    include.fill("")
    _settled(page)
    assert page.evaluate(where) == before, "the dialog moved when everything matched"


def test_the_search_finds_a_profile_by_its_patterns(page, folder, kape, velo):
    _open(page, folder)
    search = page.locator("#modalBody .side-search")
    page.keyboard.type("velo")
    assert _rail(page) == [["Profiles", ["UI side Velociraptor"]]]

    # A profile is found by what it holds, and the hit says which part.
    search.fill("unassociated")
    assert _rail(page) == [["Profiles", ["UI side KAPE"]]]
    assert page.locator("#modalBody .side-rail-hint").inner_text() == "excludes *_UnassociatedFileEntries.csv"
    search.press("Enter")             # Enter takes the first match
    assert _selected(page) == "UI side KAPE"

    search.fill("qqqq")
    assert page.locator("#modalBody .side-rail-empty").inner_text() == "Nothing matches “qqqq”."
    # Escape clears the search first; only an empty search lets Escape
    # close the dialog.
    search.press("Escape")
    assert search.input_value() == ""
    assert page.locator("#modal").is_visible()


def test_the_arrow_keys_walk_the_list(page, folder, kape, velo):
    _open(page, folder)
    search = page.locator("#modalBody .side-search")
    search.fill("ui side")
    search.press("ArrowDown")
    assert _selected(page) == "UI side KAPE"
    page.keyboard.press("ArrowDown")
    assert _selected(page) == "UI side Velociraptor"
    _settled(page)
    assert _pressed(page) == [".json"], "walking the list picks as it goes"
    page.keyboard.press("ArrowUp")
    page.keyboard.press("ArrowUp")    # from the top of the list, back to the search box
    assert page.evaluate("() => document.activeElement.classList.contains('side-search')")


def test_a_pinned_profile_sits_on_top_and_its_pin_goes_with_it(page, folder, kape, velo, api):
    _open(page, folder)
    row = page.locator("#modalBody .side-rail-row", has_text="UI side Velociraptor")
    row.hover()
    row.locator(".side-rail-pin").click()
    names = {kape["name"], velo["name"]}
    rail = _mine(_rail(page), names)
    assert rail[0] == ["Pinned", ["UI side Velociraptor"]], rail
    assert dict(rail)["Profiles"] == ["UI side KAPE"], "moved, not copied"
    assert page.evaluate(PINS) == [str(velo["id"])]

    # Kept for the next time the dialog opens — on this machine.
    page.evaluate("() => __winnow.closeModal()")
    _open(page, folder)
    assert _rail(page)[0] == ["Pinned", ["UI side Velociraptor"]]

    # Deleting a pinned profile takes its pin too; what it held stays on
    # screen, as Custom, to tweak or save again.
    _pick(page, "UI side Velociraptor")
    _settled(page)
    page.locator("#modalBody .row-actions .btn", has_text="Delete profile").click()
    page.locator(".confirm-card .btn", has_text="Delete").click()
    page.wait_for_function("() => document.querySelector('#modalBody .side-rail-item[aria-selected=\"true\"]')"
                           ".textContent.startsWith('Custom')")
    assert "UI side Velociraptor" not in json.dumps(_rail(page))
    assert page.evaluate(PINS) == []
    assert _pressed(page) == [".json"]
    assert page.locator("#modalBody .row-actions .btn").all_inner_texts()[0] == "Save as profile…"
    assert not any(p["id"] == velo["id"] for p in api("/api/import_profiles"))


def test_saving_a_profile_lists_it_and_chooses_it(page, folder, profiles, api):
    _open(page, folder)
    page.locator('#modalBody .import-types .btn[data-ext=".json"]').click()
    page.locator("#modalBody .import-pattern textarea").nth(1).fill("notes.txt")
    _settled(page)
    page.locator("#modalBody .row-actions .btn", has_text="Save as profile…").click()
    page.locator(".confirm-card input").fill("UI side saved here")
    page.locator(".confirm-card .btn", has_text="OK").click()
    page.wait_for_selector('#modalBody .side-rail-item[aria-selected="true"] >> text="UI side saved here"')
    rec = next(p for p in api("/api/import_profiles") if p["name"] == "UI side saved here")
    try:
        assert ".json" not in rec["extensions"] and rec["exclude_patterns"] == ["notes.txt"]
        assert page.locator("#modalBody .row-actions .btn").all_inner_texts()[:2] == ["Update profile", "Delete profile"]
        # Saved from what was on screen, so nothing on screen changed.
        assert ".json" not in _pressed(page)
    finally:
        api(f"/api/import_profiles/{rec['id']}", "DELETE")


def test_import_waits_for_the_scan_it_would_import_from(page, folder, until):
    """A pattern changes while the list on screen is still the last scan's
    answer, dimmed: Import checked waits for the new answer instead of
    importing files the new pattern has just left out."""
    _open(page, folder)
    imp = page.locator("#modalBody .row-actions .btn", has_text="Import checked")
    assert imp.is_enabled()
    held = []
    page.route("**/api/ingest/dir/scan", lambda r: held.append(r))
    try:
        page.locator("#modalBody .import-pattern textarea").nth(1).fill("*Cmd*")
        until(page, lambda: len(held) == 1, "the rescan held")
        assert page.locator("#modalBody .side-preview").get_attribute("aria-busy") == "true"
        assert imp.is_disabled(), "Import waits while the list is the old answer"
        assert "EventLogs/EvtxECmd_Output.csv" in _files(page), "the old answer stays on screen"
        held.pop().continue_()
        _settled(page)
    finally:
        page.unroute("**/api/ingest/dir/scan")
    assert not any("Cmd" in f for f in _files(page))
    assert imp.is_enabled()
    page.evaluate("() => __winnow.closeModal()")


def test_with_no_profiles_the_list_says_where_they_come_from(page):
    page.route("**/api/import_profiles", lambda r: r.fulfill(
        status=200, content_type="application/json", body="[]") if r.request.method == "GET" else r.continue_())
    try:
        _open(page)
        assert _rail(page) == [["Ad hoc", ["Custom (not saved)"]], ["Profiles", ["(note)"]]]
        assert "Save as profile…" in page.locator("#modalBody .side-rail-note").inner_text()
        assert page.locator("#modalBody .side-preview-foot").inner_text() == "Choose a folder to see what it would import."
    finally:
        page.unroute("**/api/import_profiles")
