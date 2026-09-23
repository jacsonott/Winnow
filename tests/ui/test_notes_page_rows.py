"""The Notes page in the browser: an empty case that says it is empty, and
the row notes listed on the page that is named after them.

Two symptoms, both only visible in a rendered page. On an empty case the
editor's placeholder was a worked sample note — a heading, bullets,
timestamped lines, in the editor's own face and weight — beside a preview
pane that was simply blank, which reads as a narrative somebody wrote and a
preview that failed to render it. And the notes an analyst writes on
evidence rows lived in the detail pane alone, so the page called Notes
accounted for one of the two things that word means.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui


def _open_notes(page):
    page.locator("#tabNotes").click()
    page.wait_for_selector("#notesview:not([hidden])")


def _leave_clean(page):
    """Empty the narrative through the editor's own input event, so the
    autosave that follows is the one that clears it — the case is shared by
    every module in this suite (see tests/ui/conftest.py)."""
    page.evaluate("() => { const ed = document.getElementById('notesEditor'); ed.value = ''; "
                  "ed.dispatchEvent(new Event('input')); }")
    page.wait_for_function(
        "() => document.getElementById('notesSaved').textContent === 'Saved'", timeout=6000)


@pytest.fixture
def no_row_notes(server, server_post):
    """The case carries no row notes on either side of the test. They are
    cleared the way the detail pane clears one — an empty note is a delete —
    rather than by touching the table underneath."""
    def clear():
        req = urllib.request.Request(server.rstrip("/") + "/api/row_notes",
                                     headers={"X-Timeline-Lite-Client": "1"})
        for n in json.loads(urllib.request.urlopen(req).read())["notes"]:
            server_post("/api/note", {"source_id": n["source_id"], "rid": n["rid"], "note": ""})

    clear()
    yield
    clear()


def test_an_empty_case_reads_as_empty_on_both_sides_of_the_split(page, no_row_notes):
    _open_notes(page)
    try:
        _leave_clean(page)
        # The preview pane says so rather than sitting blank beside text.
        empty = page.locator("#notesPreviewBody .notes-preview-empty")
        empty.wait_for(state="visible", timeout=4000)
        assert "Nothing written yet" in empty.inner_text()

        placeholder = page.locator("#notesEditor").get_attribute("placeholder")
        # Prose, not a sample note: it is the SHAPE — headings and bullet
        # lines — that reads as content however far the colour is dimmed.
        assert not [ln for ln in placeholder.splitlines()
                    if ln.startswith("#") or ln.startswith("- ")], placeholder

        # And quieter than real text: measured as contrast against the
        # editor's own background, not as "some other colour", because a
        # placeholder that merely differs can still shout.
        quiet = page.evaluate("""() => {
          const ed = document.getElementById('notesEditor');
          const lum = (c) => {
            const [r, g, b] = c.match(/[\\d.]+/g).slice(0, 3).map((v) => {
              const s = v / 255;
              return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
            });
            return 0.2126 * r + 0.7152 * g + 0.0722 * b;
          };
          const ratio = (a, b) => {
            const [hi, lo] = lum(a) > lum(b) ? [lum(a), lum(b)] : [lum(b), lum(a)];
            return (hi + 0.05) / (lo + 0.05);
          };
          const own = getComputedStyle(ed), ph = getComputedStyle(ed, '::placeholder');
          return { text: ratio(own.color, own.backgroundColor),
                   placeholder: ratio(ph.color, own.backgroundColor),
                   style: ph.fontStyle };
        }""")
        assert quiet["placeholder"] < quiet["text"], quiet
        assert quiet["style"] == "italic", quiet

        # Typing takes the empty line away — it is the pane's empty state,
        # not a header it now sits above.
        page.locator("#notesEditor").fill("# Real narrative")
        page.wait_for_function(
            "() => document.getElementById('notesPreviewBody').innerHTML.includes('<h1>Real narrative</h1>')",
            timeout=3000)
        assert page.locator("#notesPreviewBody .notes-preview-empty").count() == 0
    finally:
        _leave_clean(page)


def test_the_row_notes_of_the_case_are_listed_and_open_their_row(page, server_post, no_row_notes):
    # Written the way the detail pane writes them, out of order on purpose.
    server_post("/api/note", {"source_id": 1, "rid": 7, "note": "staged here"})
    server_post("/api/note", {"source_id": 1, "rid": 3, "note": "first success\nsecond line"})
    _open_notes(page)
    try:
        page.wait_for_selector("#notesRowsBody .notes-row")
        rows = page.locator("#notesRowsBody .notes-row")
        assert rows.count() == 2
        # The heading counts them; each entry names its table and line, so
        # the list is readable without opening anything.
        # text_content, not inner_text: the heading is uppercased in CSS.
        assert page.locator("#notesRowsCount").text_content() == "Row notes (2)"
        assert rows.nth(0).locator(".notes-row-where").inner_text() == "ui.csv · Line 3"
        assert rows.nth(1).locator(".notes-row-where").inner_text() == "ui.csv · Line 7"
        # One line in the list, whole in the tooltip: a note is free text.
        assert rows.nth(0).locator(".notes-row-note").inner_text() == "first success second line"
        assert "first success\nsecond line" in rows.nth(0).get_attribute("title")

        rows.nth(0).click()
        page.wait_for_function("() => __winnow.S.activeTab === 'grid' && __winnow.S.sourceId === 1 && !!__winnow.S.view")
        # Landed ON the row, not merely in its table (an unfiltered view's
        # position is rid - 1) — same landing as a watchlist hit.
        page.wait_for_function("() => __winnow.S.cursor === 2", timeout=10_000)
    finally:
        _leave_clean(page)


def test_with_no_row_notes_the_strip_says_where_they_come_from(page, no_row_notes):
    _open_notes(page)
    try:
        empty = page.locator("#notesRowsBody .notes-rows-empty")
        empty.wait_for(state="visible", timeout=4000)
        # An analyst who has never used the detail pane's note box has no
        # other way to learn that this section is waiting for it.
        assert "detail pane" in empty.inner_text()
        assert page.locator("#notesRowsCount").text_content() == "Row notes"
    finally:
        _leave_clean(page)


def test_closing_the_strip_holds_across_visits(page, no_row_notes):
    _open_notes(page)
    try:
        page.wait_for_selector("#notesRowsBody .notes-rows-empty")
        page.locator("#btnNotesRows").click()
        assert not page.locator("#notesRowsBody").is_visible()
        assert page.locator("#btnNotesRows").get_attribute("aria-expanded") == "false"
        # Beside the divider ratio, in the same per-browser record.
        assert page.evaluate("() => JSON.parse(localStorage.getItem('winnow.notes')).rows") is False

        # Leaving and coming back re-applies it — unlike the Edit/Split/
        # Preview mode, which deliberately resets to Split every visit.
        page.evaluate("() => __winnow.showGridTab()")
        page.wait_for_selector("#notesview", state="hidden")
        _open_notes(page)
        assert not page.locator("#notesRowsBody").is_visible()

        page.locator("#btnNotesRows").click()
        page.wait_for_selector("#notesRowsBody .notes-rows-empty")
    finally:
        _leave_clean(page)


def test_a_case_switch_empties_the_row_note_strip(page, server_post, no_row_notes):
    """The entries are (source_id, rid) pairs, and both restart at 1 in a new
    case: a button left over from the previous one opens a real row of a real
    table and presents it as a row somebody annotated. Nothing errors, which
    is what makes it worth a test.

    Driven through resetNotes() rather than through a second case, because
    the UI suite shares one server and one case file (tests/ui/conftest.py) —
    opening another would pull the case out from under every later module.
    resetNotes is the seam the switch goes through: openCase calls it, for
    this reason, beside the clearing of every other per-case id it holds."""
    server_post("/api/note", {"source_id": 1, "rid": 11, "note": "case A row"})
    _open_notes(page)
    try:
        page.wait_for_selector("#notesRowsBody .notes-row")
        page.evaluate("() => __winnow.resetNotes()")
        # No button survives: not hidden, not disabled — gone from the DOM,
        # so a click during the window before the next fetch lands has
        # nothing to hit.
        assert page.locator("#notesRowsBody .notes-row").count() == 0
        # And the count goes with them. "Row notes (1)" over an empty strip
        # is the same claim about the wrong case, made in the heading.
        assert page.locator("#notesRowsCount").text_content() == "Row notes"

        # Re-opening the page refetches, so the strip is only empty for as
        # long as the answer takes.
        page.evaluate("() => __winnow.showGridTab()")
        page.wait_for_selector("#notesview", state="hidden")
        _open_notes(page)
        page.wait_for_selector("#notesRowsBody .notes-row")
        assert page.locator("#notesRowsCount").text_content() == "Row notes (1)"
    finally:
        _leave_clean(page)


def test_a_fetch_that_fails_says_so_rather_than_leaving_the_list_up(page, server_post, no_row_notes):
    """The failure path is the one that makes a stale list permanent: there
    is no second refetch to correct it, so a list left standing after an
    unanswered request is a list nobody can tell is current."""
    server_post("/api/note", {"source_id": 1, "rid": 5, "note": "before the outage"})
    _open_notes(page)
    try:
        page.wait_for_selector("#notesRowsBody .notes-row")
        page.route("**/api/row_notes", lambda route: route.abort())
        page.evaluate("() => __winnow.loadRowNotes()")
        line = page.locator("#notesRowsBody .notes-rows-empty")
        line.wait_for(state="visible", timeout=4000)
        assert "Could not load the row notes" in line.inner_text()
        assert page.locator("#notesRowsBody .notes-row").count() == 0
        # The narrative is the page's job and must survive the listing's
        # failure — the editor is still editable and still saving.
        page.locator("#notesEditor").fill("still writing")
        page.wait_for_function(
            "() => document.getElementById('notesSaved').textContent === 'Saved'", timeout=6000)
        page.unroute("**/api/row_notes")
    finally:
        _leave_clean(page)
