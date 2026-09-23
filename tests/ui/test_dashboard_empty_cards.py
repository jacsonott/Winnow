"""A card that resolved to nothing is a line, not a panel.

Walked against a real KAPE collection, the first screen of the shipped
triage board was two full-width cards reading "(no host facts in this
RECmd output)" and "(no Defender alert events in the logs)". Nothing was
wrong — those artefacts were not in the collection — but the board spent
its best space saying so, and never said what would change it.

So the cards fold into one dashed strip at the top, a line each. What is
asserted here is what only a browser can show: that the folded cards take
no room at all (two of them together are shorter than one card that has
something to say), that the line names WHICH kind of empty it is — an
artefact nobody collected, which importing a file fixes, versus a table
that is in the case whose query matched nothing, which is a finding — and
that a zero is left alone, because 0 is an answer.

And that a card stops being folded in the right order. A folded card is
display:none, so it has no size, and a bar or histogram measures its
canvas one frame after it is appended — unfold on the strip's frame
instead of before the paint and the card that finally got its rows comes
back carrying a chart drawn at 300×150 and stretched, whose hit boxes are
in that same phantom space. Only a browser can tell either half of that.

The shared fixture table is 200 rows over four EventIds, 50 each, with no
row anywhere carrying EventId 9999.
"""

from __future__ import annotations

import json
import re

import pytest

pytestmark = pytest.mark.ui

PREVIEW = re.compile(r".*/api/dashboard/widget/preview$")

H = "{ 'Content-Type': 'application/json', 'X-Timeline-Lite-Client': '1' }"


def _src(page):
    return page.evaluate("() => __winnow.S.sourceId")


def _board(page, name, widgets):
    return page.evaluate(f"""async ([name, widgets]) => {{
      const d = await fetch('/api/dashboards', {{ method: 'POST', headers: {H},
        body: JSON.stringify({{ name, widgets }}) }}).then(r => r.json());
      await __winnow.loadDashboards();
      __winnow.renderSidebar();
      return d.id;
    }}""", [name, widgets])


def _drop(page, did):
    page.evaluate(f"""async (id) => {{
      await fetch('/api/dashboards/' + id, {{ method: 'DELETE', headers: {H} }});
      await __winnow.loadDashboards();
      __winnow.renderSidebar();
    }}""", did)


def _show(page, did):
    page.evaluate("(id) => __winnow.showDashboard(id)", did)
    page.wait_for_selector("#dashboardview:not([hidden])", timeout=15_000)


def _cards(page):
    return page.locator("#dashGrid .dash-card:not(.dash-add)")


def _rows(page):
    return page.locator("#dashGrid .dash-empties-row")


def _mixed(src):
    """Four cards: one whose artefact is not in this case, one whose table
    is and whose query matched nothing, one zero, one with rows."""
    t = f"src_{src}"
    return [
        # {{registry}} binds to a RECmd batch, which the fixture case has
        # no table shaped like. This is the "(no host facts…)" card.
        {"title": "Host", "source": "sql", "render": "kv", "span": 2,
         "query": {"sql": "SELECT 'Computer name', ValueData FROM {{registry}}"}},
        # The table IS here; nothing in it is a 9999. This is the
        # "(no Defender alert events…)" card.
        {"title": "Recent alerts", "source": "sql", "render": "list", "span": 2,
         "query": {"sql": f"SELECT Host, COUNT(*) FROM {t} WHERE EventId = '9999' GROUP BY Host"}},
        # Zero rows AND rendered as a number: the card reads 0, and 0 is an
        # answer — folding it away would hide the reassuring half of a
        # triage board.
        {"title": "Alert count", "source": "sql", "render": "stat", "span": 2,
         "query": {"sql": f"SELECT EventId FROM {t} WHERE EventId = '9999'"}},
        {"title": "Top hosts", "source": "sql", "render": "list", "span": 2,
         "query": {"sql": f"SELECT Host, COUNT(*) AS n FROM {t} GROUP BY Host ORDER BY n DESC"}},
    ]


def _wait_folded(page, n):
    page.wait_for_function(
        "(n) => document.querySelectorAll('#dashGrid .dash-empties-row').length === n",
        arg=n, timeout=15_000)


def test_empty_cards_fold_into_a_strip_and_take_no_card_space(page):
    did = _board(page, "Empty cards", _mixed(_src(page)))
    try:
        _show(page, did)
        _wait_folded(page, 2)
        cards = _cards(page)
        assert cards.count() == 4
        # The two that resolved to nothing are off the layout entirely —
        # not a short card, not an empty one: no box at all.
        assert not cards.nth(0).is_visible() and not cards.nth(1).is_visible()
        # And the two that have something to say are untouched, the zero
        # included.
        assert cards.nth(2).is_visible() and cards.nth(3).is_visible()
        assert page.locator("#dashGrid .dash-stat").inner_text().strip() == "0"

        strip = page.locator("#dashGrid .dash-empties")
        assert strip.is_visible()
        # The measurement the finding was written from: what two empty
        # cards cost is now less than what ONE card costs.
        assert strip.bounding_box()["height"] < cards.nth(3).bounding_box()["height"]
        # At the top of the board, above every card still on it.
        assert strip.bounding_box()["y"] < cards.nth(2).bounding_box()["y"]
    finally:
        _drop(page, did)


def test_the_strip_says_which_kind_of_empty_each_card_is(page):
    """An analyst about to write "not present" in a report needs to know
    whether anything looked. A card whose artefact was never collected and
    a card whose query ran and matched nothing paint identically, and are
    not the same answer."""
    did = _board(page, "Empty kinds", _mixed(_src(page)))
    try:
        _show(page, did)
        _wait_folded(page, 2)
        rows = _rows(page)
        # In board order, each naming the card it stands for.
        assert [t.strip() for t in rows.locator(".t").all_text_contents()] \
            == ["Host", "Recent alerts"]
        why = [r.strip() for r in rows.locator(".r").all_text_contents()]
        assert why[0] != why[1]
        # The artefact nobody collected is named, so the line says what to
        # go and get.
        assert "Registry (RECmd batch)" in why[0]
        # The other one says the opposite: the table is here, and this is
        # the finding.
        assert "matched nothing" in why[1]
        # Only the first has anything to import — an import cannot make a
        # query that ran match rows it did not match.
        assert rows.nth(0).locator("button", has_text="Import").count() == 1
        assert rows.nth(1).locator("button", has_text="Import").count() == 0
    finally:
        _drop(page, did)


def test_the_import_action_opens_the_import_dialog(page):
    """The line offers a fix, so the fix has to be one click. A label that
    looked like an action and did nothing would pass every assertion
    above."""
    did = _board(page, "Empty import", _mixed(_src(page)))
    try:
        _show(page, did)
        _wait_folded(page, 2)
        _rows(page).nth(0).locator("button", has_text="Import").click()
        page.wait_for_selector("#modal:not([hidden])", timeout=15_000)
        assert page.locator("#modalTitle").inner_text().strip().lower() == "import"
    finally:
        page.keyboard.press("Escape")
        page.wait_for_function("() => document.getElementById('modal').hidden", timeout=15_000)
        _drop(page, did)


def test_a_folded_card_can_be_put_back_and_says_why_it_is_empty(page):
    """Folding must not make a card unreachable — it still has an editor,
    a query and a Remove. And the card put back cannot be blank: the
    shipped queries no longer UNION a sentence of their own in, which is
    what used to hold the full-width card."""
    did = _board(page, "Empty unfold", _mixed(_src(page)))
    try:
        _show(page, did)
        _wait_folded(page, 2)
        cards = _cards(page)
        page.locator("#dashGrid .dash-empties-head button").click()
        page.wait_for_function(
            "() => document.querySelectorAll('#dashGrid .dash-card.is-empty').length === 0",
            timeout=15_000)
        assert cards.nth(0).is_visible() and cards.nth(1).is_visible()
        assert "Registry (RECmd batch)" in cards.nth(0).locator(".dash-empty-note").inner_text()
        assert "matched nothing" in cards.nth(1).locator(".dash-empty-note").inner_text()
        # And back: the strip is the default, not a one-way trip.
        page.locator("#dashGrid .dash-empties-head button").click()
        page.wait_for_function(
            "() => document.querySelectorAll('#dashGrid .dash-card.is-empty').length === 2",
            timeout=15_000)
    finally:
        _drop(page, did)


def test_a_folded_card_is_still_editable_from_its_line(page):
    """Two clicks to reach the editor of a card that is one line is one
    too many when the reason you are reading the line is to delete the
    card."""
    did = _board(page, "Empty edit", _mixed(_src(page)))
    try:
        _show(page, did)
        _wait_folded(page, 2)
        _rows(page).nth(1).locator(".dash-empties-edit").click()
        page.wait_for_selector("#modal:not([hidden]) .dash-form", timeout=15_000)
        assert page.locator("#modal .dash-form input.confirm-input").first.input_value() \
            == "Recent alerts"
    finally:
        page.keyboard.press("Escape")
        page.wait_for_function("() => document.getElementById('modal').hidden", timeout=15_000)
        _drop(page, did)


def test_a_board_with_nothing_empty_has_no_strip(page):
    """The strip is chrome a full board must not pay for."""
    did = _board(page, "Nothing empty", [_mixed(_src(page))[3]])
    try:
        _show(page, did)
        page.wait_for_function(
            "() => document.querySelectorAll('#dashGrid .dash-list-row').length > 0", timeout=15_000)
        assert not page.locator("#dashGrid .dash-empties").is_visible()
        assert _cards(page).nth(0).is_visible()
    finally:
        _drop(page, did)


# ------------------------------------------- the card that stops being empty

class _Filled:
    """The two answers one widget gives across an import: nothing, then
    rows. Stubbed rather than driven by a real import, because what is
    being measured is the pixel geometry of the card the second answer
    lands on, and that needs a row count and a card width the test knows.
    """

    def __init__(self, page, rows):
        self.rows = rows
        self.n = 0
        page.route(PREVIEW, self._on)

    def _on(self, route):
        self.n += 1
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"columns": ["Host", "n"],
                                       "rows": [] if self.n == 1 else self.rows,
                                       "elapsed_ms": 1}))


def _bar_board(page, name):
    """One folded-then-filled card, wide enough that a chart drawn at
    fit()'s 300×150 fallback cannot pass for one drawn at the card's own
    size."""
    src = _src(page)
    return _board(page, name, [{
        "title": "Registry entries by category", "source": "sql", "render": "bar", "span": 2,
        "query": {"sql": f"SELECT Host, COUNT(*) AS n FROM src_{src} GROUP BY Host"},
        "drill": {"table": f"src_{src}", "column": "Host"}}])


def _fill(page):
    """↻ Refresh all, the way an analyst reaches for it after importing
    the artefact a folded card was waiting on. It runs quiet — no
    render() — which is the whole reason the unfold has to be its own
    step."""
    page.evaluate("() => __winnow.refreshBoard()")
    page.wait_for_function(
        "() => document.querySelectorAll('#dashGrid .dash-empties-row').length === 0",
        timeout=15_000)
    page.wait_for_selector("#dashGrid canvas", timeout=15_000)
    page.wait_for_timeout(300)   # the bars draw on the next frame


def test_a_card_that_fills_in_draws_its_chart_at_the_cards_own_size(page):
    """A folded card is display:none, so a canvas measured while it is
    still folded has no size at all: fit() falls back to 300×150 and the
    board then stretches that bitmap across a card twice as wide. The
    fold has to be lifted before the paint, not a frame after it."""
    _Filled(page, [[f"H{i}", 8 - i] for i in range(8)])
    did = _bar_board(page, "Filled bar")
    try:
        _show(page, did)
        _wait_folded(page, 1)
        _fill(page)
        box = page.evaluate("""() => {
          const c = document.querySelector('#dashGrid canvas');
          const dpr = Math.min(window.devicePixelRatio || 1, 2);
          return { w: c.clientWidth, h: c.clientHeight,
                   bw: Math.round(c.width / dpr), bh: Math.round(c.height / dpr) };
        }""")
        assert box["w"] > 340 and abs(box["h"] - 150) > 4, box   # not 300×150 by accident
        assert (box["bw"], box["bh"]) == (box["w"], box["h"]), box
    finally:
        _drop(page, did)


def test_a_bar_that_fills_in_drills_on_the_row_that_was_clicked(page):
    """The expensive half of the same bug, and the silent one. drawBars
    hands back hit boxes in the coordinate space it drew in, and pickBar
    tests them against the click's real offsets — so a chart sized 300×150
    inside a ~600×160 card swallows every click past its left half, and
    the ones that do land are a row out, because the row height is the
    canvas height divided by the row count."""
    _Filled(page, [[f"H{i}", 8 - i] for i in range(8)])
    did = _bar_board(page, "Filled bar drill")
    try:
        _show(page, did)
        _wait_folded(page, 1)
        _fill(page)
        canvas = page.locator("#dashGrid canvas.drillable").first
        # The chart's own hit map, not arithmetic over the canvas height:
        # the bands have padding between them, so a row height derived
        # from height/rows lands in a gap. The point taken is the middle
        # of the third bar at the right-hand end of the card — at the
        # 300x150 fallback that x is past the end of every box, which is
        # the failure this test is here for.
        page.wait_for_function(
            "() => { const c = document.querySelector('#dashGrid canvas.drillable');"
            " return c && c.bars && c.bars.length >= 3; }", timeout=15_000)
        spot = page.evaluate(
            "() => { const c = document.querySelector('#dashGrid canvas.drillable');"
            " const b = c.bars[2];"
            " return { x: Math.round(c.clientWidth - 8), y: Math.round(b.y + b.h / 2),"
            "          label: b.row.label }; }")
        assert spot["label"] == "H2", spot
        canvas.click(position={"x": spot["x"], "y": spot["y"]})
        page.wait_for_selector("#dashboardview", state="hidden", timeout=15_000)
        # The filter is applied after the table has opened, so the grid
        # settling is what says the drill finished — reading the tree the
        # moment the board goes away catches drillInto mid-await.
        page.wait_for_function(
            "() => __winnow.S.view && __winnow.S.view.row_count === 40", timeout=15_000)
        assert page.evaluate("() => __winnow.S.filterTree")["children"] == [
            {"type": "cond", "column": "Host", "op": "equals", "value": "H2"}]
    finally:
        page.evaluate("""async () => {
          __winnow.S.filterTree = { type: 'group', op: 'AND', children: [] };
          __winnow.updateFiltersButton();
          await __winnow.rebuildView({ keepScroll: false, keepRow: false });
        }""")
        _drop(page, did)
