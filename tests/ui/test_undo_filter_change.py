"""`z` undoes the last filter change, and again for the one before it.

The history is recorded where every filter change ends up — the rebuild —
so these tests reach it the way an analyst does: a header box, the tag
chips, Clear filters. What they pin is what the analyst would see go
wrong: a press that took back only the last letter of a word, one that
undid a sort along with the filter, and one that reached into another
table's filters.
"""
from __future__ import annotations

import urllib.request

import pytest

pytestmark = pytest.mark.ui

ROWS = "() => __winnow.S.view.row_count"
TOAST = "#toast:not([hidden])"


def _filter(page, view_rebuilt, col, text):
    # The box's own debounced rebuild, and no Enter: Enter builds at once
    # but leaves the debounce armed, and a second build landing 220ms
    # later — after a table switch, in the two-table test — is a race
    # these tests would be measuring instead of the undo.
    box = page.locator(f'.fcell input[data-col="{col}"]')
    with view_rebuilt(page):
        box.fill(text)


def _undo(page, view_rebuilt):
    with view_rebuilt(page):
        page.locator("#body").focus()
        page.keyboard.press("z")


def _box(page, col):
    return page.locator(f'.fcell input[data-col="{col}"]').input_value()


def test_z_steps_back_through_filter_changes(page, view_rebuilt):
    assert page.evaluate("() => __winnow.S.keymap.undoFilterChange") == ["z"]
    assert page.evaluate(ROWS) == 200
    _filter(page, view_rebuilt, "EventId", "=4624")
    assert page.evaluate(ROWS) == 50
    _filter(page, view_rebuilt, "Host", "=H0")
    assert page.evaluate(ROWS) == 10

    _undo(page, view_rebuilt)
    assert page.evaluate(ROWS) == 50
    # The box says what is applied, or the analyst can't tell what z did.
    assert _box(page, "Host") == ""
    assert _box(page, "EventId") == "=4624"
    page.wait_for_selector(TOAST)
    assert "Host filter" in page.locator("#toast").inner_text()

    _undo(page, view_rebuilt)
    assert page.evaluate(ROWS) == 200
    assert _box(page, "EventId") == ""

    # Nothing left: says so, and leaves the table as it is.
    assert page.evaluate("() => __winnow.filterUndoDepth()") == 0
    page.evaluate("() => { document.getElementById('toast').hidden = true; }")
    page.keyboard.press("z")
    page.wait_for_selector(TOAST)
    assert page.locator("#toast").inner_text() == "No filter change to undo"
    assert page.evaluate(ROWS) == 200


def test_one_word_typed_with_pauses_is_one_undo(page, view_rebuilt):
    """The box rebuilds on every pause past its 220ms debounce, so a word
    typed at an ordinary pace is several builds. Undo takes the word back,
    not its last letter."""
    box = page.locator('.fcell input[data-col="EventId"]')
    box.focus()
    with view_rebuilt(page):
        box.type("46")
    with view_rebuilt(page):
        box.type("88")
    assert page.evaluate(ROWS) == 50
    page.locator("#body").focus()
    assert page.evaluate("() => __winnow.filterUndoDepth()") == 1

    _undo(page, view_rebuilt)
    assert page.evaluate(ROWS) == 200
    assert _box(page, "EventId") == ""


def test_undo_leaves_the_sort_alone(page, view_rebuilt):
    """Sort is not a filter. Undoing a filter typed before a re-sort must
    not take the sort back too."""
    _filter(page, view_rebuilt, "EventId", "=4625")
    sort = [{"column": "Host", "dir": "desc"}]
    with view_rebuilt(page):
        page.evaluate("(s) => { __winnow.S.sort = s; __winnow.rebuildView(); }", sort)
    # The sort was a rebuild too, and recorded nothing.
    assert page.evaluate("() => __winnow.filterUndoDepth()") == 1

    _undo(page, view_rebuilt)
    assert page.evaluate(ROWS) == 200
    assert page.evaluate("() => __winnow.S.sort") == sort
    with view_rebuilt(page):
        page.evaluate("() => { __winnow.S.sort = []; __winnow.rebuildView(); }")


def test_clear_filters_is_one_step_back(page, view_rebuilt):
    """`c` clears the column filters and the search together; `z` brings
    them all back at once, the way they were."""
    _filter(page, view_rebuilt, "EventId", "=4624")
    _filter(page, view_rebuilt, "Host", "=H0")
    with view_rebuilt(page):
        page.locator("#body").focus()
        page.keyboard.press("c")
    assert page.evaluate(ROWS) == 200

    _undo(page, view_rebuilt)
    assert page.evaluate(ROWS) == 10
    assert (_box(page, "EventId"), _box(page, "Host")) == ("=4624", "=H0")
    # Back out to where the module found the table.
    _undo(page, view_rebuilt)
    _undo(page, view_rebuilt)
    assert page.evaluate(ROWS) == 200


def _delete_source(server, sid):
    req = urllib.request.Request(server.rstrip("/") + f"/api/source/{sid}", method="DELETE",
                                 headers={"X-Timeline-Lite-Client": "1"})
    return urllib.request.urlopen(req, timeout=30).status


def test_each_table_undoes_only_its_own_filters(page, server, server_post, view_rebuilt, tmp_path):
    other = tmp_path / "other.csv"
    other.write_text("Host,User\nh0,u0\nh1,u1\nh1,u2\n", encoding="utf-8")
    server_post("/api/ingest/path", {"path": str(other), "build_fts": False})
    page.evaluate("() => { __winnow.loadSources(undefined, { navigate: false }); }")
    page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'other.csv')", timeout=30_000)
    other_id = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'other.csv').id")
    home_id = page.evaluate("() => __winnow.S.sourceId")
    opened = ("(id) => __winnow.S.sourceId === id && !!__winnow.S.view"
              " && __winnow.S.view.source_id === id && __winnow.busyCount === 0")
    try:
        _filter(page, view_rebuilt, "EventId", "=4624")
        page.evaluate("(id) => { __winnow.openSource(id); }", other_id)
        page.wait_for_function(opened, arg=other_id, timeout=30_000)
        _filter(page, view_rebuilt, "Host", "=h1")
        assert page.evaluate(ROWS) == 2

        # Back on the first table, its own filter is what z takes back —
        # not the one typed into the other table since.
        page.evaluate("(id) => { __winnow.openSource(id); }", home_id)
        page.wait_for_function(opened, arg=home_id, timeout=30_000)
        assert page.evaluate(ROWS) == 50
        _undo(page, view_rebuilt)
        assert page.evaluate(ROWS) == 200

        page.evaluate("(id) => { __winnow.openSource(id); }", other_id)
        page.wait_for_function(opened, arg=other_id, timeout=30_000)
        assert page.evaluate(ROWS) == 2
        _undo(page, view_rebuilt)
        assert page.evaluate(ROWS) == 3
    finally:
        _delete_source(server, other_id)
        page.evaluate("(id) => { __winnow.loadSources(id); }", home_id)
        page.wait_for_function("(id) => __winnow.S.sourceId === id && !!__winnow.S.view", arg=home_id, timeout=30_000)


def test_z_is_kept_off_an_action_that_already_had_it(own_page):
    """`z` was unbound until this action took it, so an analyst may have
    put it on something else. Their binding stays theirs; the new action
    arrives with no key rather than a second chip on the same one."""
    init = ("localStorage.setItem('winnow.keymap', JSON.stringify({ openTables: ['t', 'z'] }));"
            "localStorage.setItem('winnow.keymap.v', '6');")
    with own_page(init=init) as pg:
        assert pg.evaluate("() => __winnow.S.keymap.undoFilterChange") == []
        assert pg.evaluate("() => __winnow.S.keymap.openTables") == ["t", "z"]
