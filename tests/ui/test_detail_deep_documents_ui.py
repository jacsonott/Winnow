"""A cell too deep or too big to lay out can't break the detail pane.

The JSON and XML layouts recursed once per level with no limit, so ~2,100
nested arrays (a 4 KB cell) or ~3,000 nested elements threw "Maximum call
stack size exceeded" out of showDetail. It threw after the title said the
new line and before the note box was rebound, so the pane read "Line 2"
while a note typed under it was saved on line 1. Evidence decides what a
cell holds. A 200,000-element array didn't throw, but cost about 2 s on
every cursor move onto it.

The rows here are the shared table's, with one cell swapped in the page's
own row cache for the value under test rather than a table imported for
it: what matters is what showDetail does with that value, and a table
would be one more thing every later module in the shard has to look past.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

DEEP_JSON = "[" * 2500 + "]" * 2500
DEEP_XML = "<a>" * 3000 + "</a>" * 3000

RENDER = """(v) => {
  const box = document.createElement('div');
  box.append(__winnow.renderDetailContent(v));
  const hint = box.querySelector('.dfield-hint');
  return {
    pretty: !!box.querySelector('pre.detail-pretty'),
    hint: hint ? hint.textContent : null,
    rest: hint ? box.textContent.slice(hint.textContent.length) : box.textContent,
    nodes: box.querySelectorAll('.struct-node').length,
  };
}"""

NOTE_RID = "() => document.getElementById('noteInput').dataset.rid"


def _render(page, value):
    return page.evaluate(RENDER, value)


def _rid(page, pos):
    return page.evaluate("(p) => __winnow.rowAt(p).rid", pos)


def _set_cell(page, pos, column, value):
    """Puts `value` in one cell of the cached row the pane will read."""
    page.evaluate("""([p, col, v]) => {
      __winnow.rowAt(p).cells[__winnow.S.columns.findIndex((c) => c.name === col)] = v;
    }""", [pos, column, value])


def _open_line_one(page):
    # As an analyst does it: a click on the grid, `d` for the pane.
    page.locator(".row[data-pos='0'] .cell").nth(1).click()
    page.keyboard.press("d")
    page.wait_for_selector("#detail:not([hidden])")
    page.wait_for_function(f"(rid) => ({NOTE_RID})() === String(rid)", arg=_rid(page, 0))


def _arrow_to_line_two(page):
    # The keyboard, not a page.evaluate: a throw in the pane's update is
    # then what it was for the analyst, an error in an event handler that
    # leaves the page as it stood.
    rid = _rid(page, 1)
    page.keyboard.press("ArrowDown")
    page.wait_for_function("() => __winnow.S.cursor === 1")
    # The title is written before anything that can throw, in the same
    # synchronous call as the rest of the pane, so once it says line two
    # the note box has been bound to whatever it is going to be bound to.
    page.wait_for_function("(rid) => document.getElementById('detailTitle').textContent === `Line ${rid}`",
                           arg=rid)
    return rid


def test_a_row_with_a_cell_too_deep_to_lay_out_still_gets_its_own_note(page, api):
    sid = page.evaluate("() => __winnow.S.sourceId")
    first = second = None
    try:
        _open_line_one(page)
        first = _rid(page, 0)
        _set_cell(page, 1, "Payload", DEEP_JSON)
        second = _arrow_to_line_two(page)
        assert page.evaluate(NOTE_RID) == str(second), "the note box is still bound to the line before"
        payload = page.locator('#detailFields dd[data-col="Payload"]')
        assert payload.locator(".dfield-hint").text_content() == \
            "Nested more than 64 levels deep, so it is shown as text."
        assert DEEP_JSON in payload.text_content()
        # Brackets give a line nowhere to break, and 5,000 of them on one
        # line widened the field list until the note box left the pane.
        assert page.evaluate("""() => document.getElementById('noteInput').getBoundingClientRect().right
                                  <= document.getElementById('detail').getBoundingClientRect().right""")
        # The fields before it and after it are all there.
        assert page.locator("#detailFields dt").all_text_contents()[:3] == ["Timestamp", "EventId", "Host"]

        page.keyboard.press("n")
        page.keyboard.type("note meant for line two")
        page.wait_for_function("() => document.getElementById('noteStatus').textContent === 'Saved'")
        view = page.evaluate("() => __winnow.S.view.view_id")
        notes = {r["rid"]: r["note"] for r in api(f"/api/rows?view_id={view}&start=0&count=2")["rows"]}
        assert notes == {first: None, second: "note meant for line two"}
    finally:
        for rid in (first, second):
            if rid:
                api("/api/note", "POST", {"source_id": sid, "rid": rid, "note": ""})


@pytest.mark.parametrize("value, hint", [
    (DEEP_JSON, "Nested more than 64 levels deep, so it is shown as text."),
    (DEEP_XML, "Nested more than 64 levels deep, so it is shown as text."),
    # Would lay out in seconds, on every cursor move onto the row.
    ("[" + ",".join(["1"] * 200_000) + "]", "More than 5,000 items, so it is shown as text."),
    ("<r>" + '<e n="v"/>' * 3000 + "</r>", "More than 5,000 items, so it is shown as text."),
    # Single quotes take the loose layout, which must not be the way round.
    ("[" + ", ".join(["{'k': 1}"] * 3000) + "]", "More than 5,000 items, so it is shown as text."),
], ids=["deep-json", "deep-xml", "long-json-array", "many-xml-attributes", "long-python-list"])
def test_a_document_past_a_limit_is_shown_as_the_text_it_holds(page, value, hint):
    r = _render(page, value)
    assert not r["pretty"], r["hint"]
    assert r["hint"] == hint
    assert r["rest"] == value


@pytest.mark.parametrize("value, nodes", [
    # Exactly 64 levels: the deepest leaf is the 64th.
    ("[" * 63 + "1" + "]" * 63, 1),
    ("<a>" * 64 + "</a>" * 64, 127),
    # A fragment's wrapper is the pane's own, not a level of the document.
    ("<a>" * 64 + "</a>" * 64 + "<b/>", 128),
    # 5,000 items exactly: the array and its 4,999 numbers.
    ("[" + ",".join(["1"] * 4999) + "]", 4999),
], ids=["json-64-deep", "xml-64-deep", "xml-fragment-64-deep", "json-5000-items"])
def test_a_document_at_the_limits_is_still_laid_out(page, value, nodes):
    r = _render(page, value)
    assert r["pretty"] and r["hint"] is None, r["hint"]
    assert r["nodes"] == nodes


def test_an_evtx_record_is_laid_out_with_its_paths(page):
    """Real documents sit far inside both limits: an EVTX record is a few
    levels deep and around a hundred elements and attributes."""
    data = "".join(f'<Data Name="Field{i}">value {i}</Data>' for i in range(40))
    value = ('<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System>'
             '<Provider Name="Microsoft-Windows-Security-Auditing"/><EventID>4624</EventID>'
             f'</System><EventData>{data}</EventData></Event>')
    r = _render(page, value)
    assert r["pretty"] and r["hint"] is None
    paths = page.evaluate("""(v) => [...__winnow.renderDetailContent(v).querySelectorAll('.struct-node')]
                               .map((n) => n.dataset.path)""", value)
    assert "Event/EventData/Data[@Name='Field39']" in paths


def test_a_field_whose_layout_throws_leaves_the_rest_of_the_pane(page, api):
    """Past the limits nothing known throws, so the failure is made: an XML
    parser that refuses to run. The field it was laying out shows its text,
    the fields after it are still there, and the note box is this row's."""
    xml = "<Event><EventID>4624</EventID></Event>"
    try:
        _open_line_one(page)
        _set_cell(page, 1, "CommandLine", xml)
        page.evaluate("""() => {
          window.__realDOMParser = window.DOMParser;
          window.DOMParser = class { parseFromString() { throw new Error('parser unavailable'); } };
        }""")
        second = _arrow_to_line_two(page)
        assert page.evaluate(NOTE_RID) == str(second)
        cmd = page.locator('#detailFields dd[data-col="CommandLine"]')
        assert cmd.locator(".dfield-hint").text_content() == "Could not be laid out, so it is shown as text."
        assert xml in cmd.text_content()
        # Payload comes after CommandLine, and still lays out.
        assert page.locator('#detailFields dd[data-col="Payload"] pre.detail-pretty').count() == 1
    finally:
        page.evaluate("() => { if (window.__realDOMParser) window.DOMParser = window.__realDOMParser; }")
