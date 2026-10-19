"""The detail pane's note box keeps what is typed in it until it is saved,
and saves it on the row it was typed on.

The box saves 500 ms after the last keystroke, so for that long (and while
the POST is out) it is ahead of the server. Two things used to throw that
lead away:

- every visible page that landed re-showed the cursor row, and the pane
  set the box to the server's copy of the note. A page lands whenever the
  row caches are cleared — a derived column finishing, an import, a tag
  written elsewhere — so the box emptied mid-sentence and the rest of the
  sentence was then saved on its own.
- a cursor move rebound the box to the next row before the pending save
  fired, and the save read its row when it fired: it posted the new row's
  own note back to it, and what was typed on the row being left was lost.
"""
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.ui

NOTE_ROUTE = "**/api/note"
STATUS_SAVED = "() => document.getElementById('noteStatus').textContent === 'Saved'"
# What sixteen places in the app do after a write the server made on this
# client's behalf: drop the cached rows, then repaint, which fetches the
# visible page again — and its landing re-shows the cursor row in the pane.
REFETCH = ("() => { window.__fieldBefore = document.querySelector('#detailFields dt');"
           " __winnow.clearRowCaches(); __winnow.render(); }")
# showDetail rebuilds the field list, so a new first <dt> is the witness that
# the refetched page landed and the pane was painted from it.
PANE_REPAINTED = "() => document.querySelector('#detailFields dt') !== window.__fieldBefore"


def _row_at(page, pos):
    return page.evaluate("(p) => { const r = __winnow.rowAt(p); return { rid: r.rid, sid: r.source_id }; }", pos)


def _open_pane(page, pos):
    page.locator(f".row[data-pos='{pos}'] .cell").first.click()
    page.evaluate("(p) => __winnow.showDetail(p)", pos)
    page.wait_for_selector("#detail:not([hidden])")
    page.wait_for_function("(rid) => document.getElementById('noteInput').dataset.rid === String(rid)",
                           arg=_row_at(page, pos)["rid"])


def _server_note(api, page, pos):
    view = page.evaluate("() => __winnow.S.view.view_id")
    return api(f"/api/rows?view_id={view}&start={pos}&count=1")["rows"][0]["note"] or ""


def _release(page, held):
    # Each held save goes through and is answered before anything else is
    # sent, so a cleanup write can't land ahead of it and be overwritten.
    page.unroute(NOTE_ROUTE)
    while held:
        route = held.pop(0)
        with page.expect_response(lambda r: urlparse(r.url).path == "/api/note"):
            route.continue_()


def test_a_page_landing_while_a_note_is_typed_keeps_the_typing(page, api):
    typed = "suspicious lateral movement from"
    row = None
    # The save is held at the network, so whether its 500 ms has run out or
    # not, the server can't have the text when the page comes back: the page
    # brings the old note, as it does for an analyst mid-sentence.
    held = []
    page.route(NOTE_ROUTE, lambda route: held.append(route))
    try:
        _open_pane(page, 0)
        row = _row_at(page, 0)
        page.click("#noteInput")
        page.keyboard.type(typed)
        page.evaluate(REFETCH)
        page.wait_for_function(PANE_REPAINTED)
        assert page.input_value("#noteInput") == typed, "the box was reset to the server's copy mid-note"
        _release(page, held)
        page.wait_for_function(STATUS_SAVED)
        assert _server_note(api, page, 0) == typed

        # Saved, it is the server's copy again, so a note written elsewhere
        # shows up on the next landing rather than the box holding on forever.
        api("/api/note", "POST", {"source_id": row["sid"], "rid": row["rid"], "note": "written elsewhere"})
        page.evaluate(REFETCH)
        page.wait_for_function(PANE_REPAINTED)
        assert page.input_value("#noteInput") == "written elsewhere"
    finally:
        _release(page, held)
        if row:
            api("/api/note", "POST", {"source_id": row["sid"], "rid": row["rid"], "note": ""})


def test_moving_to_another_row_saves_the_note_on_the_row_it_was_typed_on(page, api):
    typed = "beacon to 10.0.0.5 every 60s"
    _open_pane(page, 0)
    first, second = _row_at(page, 0), _row_at(page, 1)
    try:
        # The move comes before the 500 ms are up — in one script, so that is
        # certain rather than likely. The input event is what typing raises.
        with page.expect_response(lambda r: urlparse(r.url).path == "/api/note") as saved:
            page.evaluate("""(text) => {
              const box = document.getElementById('noteInput');
              box.focus();
              box.value = text;
              box.dispatchEvent(new Event('input', { bubbles: true }));
              __winnow.moveCursor(1);
            }""", typed)
        assert saved.value.ok
        assert _server_note(api, page, 0) == typed, "the note typed on the first row was not saved there"
        assert _server_note(api, page, 1) == ""
        # The box is the second row's now, showing that row's (empty) note.
        assert page.evaluate("() => document.getElementById('noteInput').dataset.rid") == str(second["rid"])
        assert page.input_value("#noteInput") == ""
    finally:
        for row in (first, second):
            api("/api/note", "POST", {"source_id": row["sid"], "rid": row["rid"], "note": ""})
