"""Leaving a grouping gives the scrollbar back the whole table — and with
it, the tag rail's marks line up with the thumb again.

The grid's scroll height is a spacer (#spacerY), and only the grouped
painter used to size it on every paint. The flat one left that to
whatever changed the row count, and leaving a grouping changes no count:
Ungroup, its hotkey and removing the last pill repaint the flat rows with
no rebuild. So the spacer kept the grouped tree's height — five collapsed
hosts' worth, 120px, under 200 rows — and the scrollbar was sized by the
painted rows block alone: half the track at the top of the table, shrinking
every time a scroll pushed the block further down, until the next filter
change happened to rebuild the view. The rail places a mark at its row's
share of the view, so beside a thumb that wrong it marked somewhere else
than the rows on screen. That is how it was reported: the scrollbar on the
right sometimes not matching the table, which threw the tag marks off.

Each way out is its own case because each is its own gesture; that they
share a code path today is what the fix relies on, not something to
assume of the next one.

The case file is shared by the whole UI session, so the test that tags a
row untags it again.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

TAG = "__winnow.S.tags[0]"

# Three quarters of the way down the fixture's 200 rows: past anything the
# stale scrollbar could reach in one assignment, far from either end.
POS = 150

GEOMETRY = """() => {
  const b = document.getElementById('body'), row = document.querySelector('#rows .row');
  return { scrollHeight: b.scrollHeight, scrollTop: b.scrollTop, clientHeight: b.clientHeight,
           head: document.getElementById('gridHead').offsetHeight,
           rowH: row ? row.offsetHeight : 0, rows: __winnow.S.view.row_count };
}"""


def _leave_by_button(page):
    page.locator(".group-drop-btn").click()


def _leave_by_toggle_hotkey(page):
    page.locator("#body").focus()
    page.keyboard.press("X")


def _leave_by_last_pill(page):
    page.locator(".group-pill-rm").click()


@pytest.fixture(params=[_leave_by_button, _leave_by_toggle_hotkey, _leave_by_last_pill],
                ids=["ungroup-button", "toggle-hotkey", "last-pill"])
def ungrouped(request, page, painted):
    """The page, grouped by Host and then left again by one of the ways
    out, back on the flat rows."""
    page.evaluate("() => __winnow.addGroupLevel('Host')")
    page.wait_for_function("() => __winnow.S.groups.length > 0")
    try:
        painted(page)
        # The precondition that makes the bug possible: five collapsed
        # headers fit on screen, so the grouped spacer is a fraction of
        # the flat table's.
        g = page.evaluate(GEOMETRY)
        assert g["scrollHeight"] <= g["clientHeight"], g
        request.param(page)
        page.wait_for_function("() => !__winnow.S.groupByCols.length"
                               " && !!document.querySelector('#rows .row')")
        painted(page)
        yield page
    finally:
        if page.evaluate("() => __winnow.S.groupByCols.length"):
            page.evaluate("() => __winnow.dropGrouping()")


def test_the_scrollbar_spans_the_whole_table_after_ungrouping(ungrouped, painted):
    page = ungrouped
    g = page.evaluate(GEOMETRY)
    want = g["head"] + g["rows"] * g["rowH"]
    # The whole table, header included — not the rows block that happens
    # to be painted (1,419px of 4,851 on the fixture before the fix).
    assert g["scrollHeight"] == want, g
    # And the same size at the foot as at the head: the stale scrollbar
    # grew with each scroll, which is what made the thumb shrink as it
    # went. One assignment reaches the last row; before, it stopped at the
    # stale extent and the last row was nowhere near the screen.
    page.evaluate("() => { document.getElementById('body').scrollTop = 1e9; }")
    painted(page)
    end = page.evaluate(GEOMETRY)
    assert end["scrollHeight"] == want, end
    assert page.evaluate("() => __winnow.onScreenRows().last") == g["rows"] - 1


def test_a_tag_mark_sits_beside_the_thumb_after_ungrouping(ungrouped, painted):
    """The symptom as it was seen: on the tagged row, the thumb is where
    the rail's mark for that row is. Worked out from the scroll geometry
    rather than read off pixels, since the thumb is the browser's own and
    the suite runs with scrollbars hidden — both are shares of the
    scroller's height, the thumb from scrollTop over scrollHeight and the
    mark from the row's position over the view's row count."""
    page = ungrouped
    page.evaluate(f"() => __winnow.tagRowsAtPositions({TAG}, [{POS}], true)")
    try:
        page.evaluate("() => __winnow.drawRail()")
        page.evaluate(f"() => __winnow.scrollIntoView({POS})")
        painted(page)
        s = page.evaluate("() => __winnow.onScreenRows()")
        assert s["first"] <= POS <= s["last"], s
        g = page.evaluate(GEOMETRY)
        thumb = (g["scrollTop"] / g["scrollHeight"],
                 (g["scrollTop"] + g["clientHeight"]) / g["scrollHeight"])
        mark = POS / g["rows"]
        assert thumb[0] <= mark <= thumb[1], (thumb, mark, g)
        # And the rail really did paint that mark where the arithmetic
        # says, so the comparison above is with what is on screen.
        y = round(mark * page.evaluate("() => document.getElementById('rail').height"))
        lit = page.evaluate("""(y) => {
          const c = document.getElementById('rail');
          const d = c.getContext('2d').getImageData(0, y, c.width, 2).data;
          for (let i = 3; i < d.length; i += 4) if (d[i] > 0) return true;
          return false;
        }""", y)
        assert lit, f"no mark painted at y={y}"
    finally:
        page.evaluate(f"() => __winnow.tagRowsAtPositions({TAG}, [{POS}], false)")
