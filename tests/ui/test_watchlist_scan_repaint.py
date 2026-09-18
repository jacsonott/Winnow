"""A watchlist scan that auto-tags rows of the open table repaints the grid
where the analyst left it — and only once the grid is showing.

The scan runs while the Watchlist tab hides the grid. Painting the grid
then would measure a zero-height viewport and lay the first rows at the
top, and the return (Alt+1 here; tab history and the mouse thumb buttons
take the same path through showGridTab) would restore the real scroll
position over an empty viewport. So the caches are cleared at once and
the repaint waits for the grid. The second test opens a merge: the job
names the member the tags landed on, and the open merge is matched
through its members (invariant #9) or its rows stay untagged until the
analyst rebuilds the view.

Both tests leave the shared case as found: the indicator removed, the
rows it tagged untagged, the merge and the second table dropped.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui

H = {"X-Timeline-Lite-Client": "1"}

# The painted rows that intersect the grid's viewport (below the header).
INTERSECTING_ROWS = """() => {
  const b = document.getElementById('body').getBoundingClientRect();
  const top = b.top + __winnow.headH();
  return [...document.querySelectorAll('#body .row')].filter((r) => {
    const q = r.getBoundingClientRect();
    return q.bottom > top && q.top < b.bottom;
  });
}"""

# The rows in the viewport are real (their page landed) and carry the tag
# stripe exactly where the cache says a tag is — and at least one does: H2
# is every fifth row of ui.csv and a viewport holds a couple of dozen.
PAINTED_WITH_TAGS = f"""() => {{
  const rows = ({INTERSECTING_ROWS})().filter((r) => !r.classList.contains('pending'));
  if (!rows.length) return false;
  const tagged = (r) => (__winnow.rowAt(+r.dataset.pos)?.tags || []).length > 0;
  return rows.some(tagged) && rows.every((r) => !!r.querySelector('.stripe') === tagged(r));
}}"""


def _count_is(value, text):
    return f"""() => {{ const r = [...document.querySelectorAll('.wl-row')]
        .find(x => x.querySelector('.wl-val')?.textContent === {value!r});
      return !!r && r.querySelector('.wl-count').textContent === {text!r}; }}"""


def _get(server, route):
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(server.rstrip("/") + route, headers=H), timeout=10).read())


def _delete(server, route):
    urllib.request.urlopen(urllib.request.Request(
        server.rstrip("/") + route, method="DELETE", headers=H), timeout=10).read()


def _restore(server, server_post, tag):
    """Untag every row an indicator's auto-tag reached, then remove the
    indicators and reset the seen count — the case as the test found it."""
    for ind in _get(server, "/api/watchlist"):
        pairs = [[h["source_id"], h["rid"]] for h in _get(server, f"/api/watchlist/hits?watchlist_id={ind['id']}")["hits"]]
        if pairs:
            server_post("/api/row_tags", {"pairs": pairs, "tag_id": tag, "on": False})
        _delete(server, f"/api/watchlist/{ind['id']}")
    server_post("/api/watchlist/seen", {"count": 0})


def _add_auto_tag_indicator(page, tag, value, count):
    page.locator("#tabWatchlist").click()
    page.wait_for_selector("#watchlistview:not([hidden])")
    page.locator("#wlAutoTag").select_option(str(tag))
    page.locator("#wlValue").fill(value)
    page.locator("#wlAdd").click()
    page.wait_for_function(_count_is(value, count), timeout=15_000)


def test_an_auto_tag_scan_from_the_watchlist_tab_repaints_the_grid_where_it_was(page, server, server_post):
    tag = page.evaluate("() => __winnow.S.tags[0].id")
    _restore(server, server_post, tag)
    # Deep into the table, with the rows there painted and untagged.
    page.wait_for_function(
        "() => { const b = document.getElementById('body'); b.scrollTop = b.scrollHeight; return b.scrollTop > 1000; }")
    deep = page.evaluate("() => document.getElementById('body').scrollTop")
    page.wait_for_function(f"() => ({INTERSECTING_ROWS})().some((r) => !r.classList.contains('pending') && +r.dataset.pos > 150)")
    assert page.locator("#body .row .stripe").count() == 0
    try:
        _add_auto_tag_indicator(page, tag, "H2", "40")
        # The grid was hidden throughout: the repaint is owed, not done.
        assert page.evaluate("() => __winnow.S.gridRepaintPending") is True
        page.keyboard.press("Alt+1")
        page.wait_for_selector("#grid:not([hidden])")
        page.wait_for_function("(deep) => document.getElementById('body').scrollTop === deep", arg=deep)
        # Rows fill the viewport at that position, the H2 ones now striped.
        page.wait_for_function(PAINTED_WITH_TAGS, timeout=15_000)
        assert page.evaluate("() => __winnow.S.gridRepaintPending") is False
        page.wait_for_function("(tag) => __winnow.S.tagCounts[tag] === 40", arg=tag)
    finally:
        page.evaluate("() => { __winnow.closeNoticesOwnedBy('watchlist'); }")
        _restore(server, server_post, tag)


@pytest.fixture
def merge_of_two(page, server, server_post, tmp_path):
    """A merge of the shared table and a small second one with the same
    columns, imported before any indicator exists (the import hook's
    auto-scan then has nothing to do); both dropped again afterwards, the
    merge first since it lives on its members."""
    tag = page.evaluate("() => __winnow.S.tags[0].id")
    _restore(server, server_post, tag)
    first = page.evaluate("() => __winnow.S.sourceId")
    csv2 = tmp_path / "wl_member.csv"
    csv2.write_text("Timestamp,EventId,Host,ExtremelyLongColumnHeaderName,CommandLine\n"
                    "2026-03-15 09:00:00,4624,H2,v,notepad.exe\n"
                    "2026-03-15 09:00:01,4625,H9,v,calc.exe\n", encoding="utf-8")
    status = page.evaluate("""(path) => fetch('/api/ingest/path', { method: 'POST',
      headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }) }).then((r) => r.status)""", str(csv2))
    assert status == 200
    page.evaluate("() => __winnow.loadSources(undefined, { navigate: false })")
    page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'wl_member.csv')", timeout=15_000)
    second = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'wl_member.csv').id")
    merge = server_post("/api/merges", {"name": "wl merge", "source_ids": [first, second]})
    page.evaluate("() => __winnow.loadSources(undefined, { navigate: false })")
    page.wait_for_function("(id) => __winnow.S.sources.some((s) => s.id === id)", arg=merge["id"], timeout=15_000)
    yield merge["id"]
    # The delete route takes the merge's own (positive) id, as tables.js
    # does — the negative one is the tab-strip convention. Left behind, a
    # merge with a dropped member is an errored tab every later page load
    # trips over, so the teardown checks the case really is as found.
    _delete(server, f"/api/merges/{-merge['id']}")
    _delete(server, f"/api/source/{second}")
    assert not [m for m in _get(server, "/api/merges") if m["id"] == merge["id"]]
    assert not [s for s in _get(server, "/api/sources") if s["id"] == second]


def test_an_auto_tag_on_a_member_repaints_the_open_merge(page, server, server_post, merge_of_two):
    tag = page.evaluate("() => __winnow.S.tags[0].id")
    page.evaluate("(id) => __winnow.openSource(id)", merge_of_two)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && !!__winnow.S.view && __winnow.S.view.source_id === id",
                           arg=merge_of_two)
    page.wait_for_function(f"() => ({INTERSECTING_ROWS})().some((r) => !r.classList.contains('pending'))")
    assert page.locator("#body .row .stripe").count() == 0
    try:
        # 40 rows of the shared table and one of the member: the job names
        # both members, never the merge.
        _add_auto_tag_indicator(page, tag, "H2", "41")
        page.keyboard.press("Alt+1")
        page.wait_for_selector("#grid:not([hidden])")
        assert page.evaluate("() => __winnow.S.sourceId") == merge_of_two
        page.wait_for_function(PAINTED_WITH_TAGS, timeout=15_000)
        page.wait_for_function("(tag) => __winnow.S.tagCounts[tag] === 41", arg=tag)
    finally:
        page.evaluate("() => { __winnow.closeNoticesOwnedBy('watchlist'); }")
        _restore(server, server_post, tag)
