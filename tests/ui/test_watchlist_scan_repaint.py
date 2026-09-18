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

Two more things the owed repaint has to get right. It belongs to the
table it was owed to: openSource comes through showGridTab before it has
swapped S.view/S.sourceId, so paying it there would page the table being
left. And under a grouping BY TAG the rows the scan tagged changed
bucket, and the tree is server-side — so coming back regroups rather
than repainting the old counts.

Every test leaves the shared case as found: the indicator removed, the
rows it tagged untagged, the grouping dropped, the merge and the second
table dropped.
"""

from __future__ import annotations

import json
import re
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

# The precondition each test needs is its own: no row on screen already
# wears the tag the scan is about to apply, so every stripe that appears
# is the scan's. A stripe from some other tag is somebody else's business
# — PAINTED_WITH_TAGS reads each row against the cache, not against zero.
NONE_CARRY_TAG = f"""(tag) => ({INTERSECTING_ROWS})()
  .every((r) => !(__winnow.rowAt(+r.dataset.pos)?.tags || []).includes(tag))"""


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
    assert page.evaluate(NONE_CARRY_TAG, tag)
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
def plain_second_table(page, server, tmp_path):
    """A second small table in the shared case, holding nothing the H2
    indicator matches (so the hit count the tests wait on is the same with
    it here), dropped again afterwards."""
    csv2 = tmp_path / "wl_other.csv"
    csv2.write_text("Timestamp,EventId,Host,ExtremelyLongColumnHeaderName,CommandLine\n"
                    "2026-03-16 10:00:00,4624,HX,v,notepad.exe\n"
                    "2026-03-16 10:00:01,4625,HY,v,calc.exe\n", encoding="utf-8")
    status = page.evaluate("""(path) => fetch('/api/ingest/path', { method: 'POST',
      headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }) }).then((r) => r.status)""", str(csv2))
    assert status == 200
    page.evaluate("() => __winnow.loadSources(undefined, { navigate: false })")
    page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'wl_other.csv')", timeout=15_000)
    sid = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'wl_other.csv').id")
    yield sid
    _delete(server, f"/api/source/{sid}")
    assert not [x for x in _get(server, "/api/sources") if x["id"] == sid]


def test_an_owed_repaint_is_never_spent_on_the_table_being_left(page, server, server_post, plain_second_table):
    """Opening another table while the repaint is owed must not pay it:
    openSource comes through showGridTab BEFORE it swaps S.view/S.sourceId,
    so a paint there fetches a page of the table being left (and, on a case
    switch, asks the new Store about the old case's view). It passes
    repaint:false and paints the new table itself — the owed repaint goes
    with the table it was owed to, which still has it when the analyst
    comes back."""
    tag = page.evaluate("() => __winnow.S.tags[0].id")
    _restore(server, server_post, tag)
    first = page.evaluate("() => __winnow.S.sourceId")
    page.wait_for_function(f"() => ({INTERSECTING_ROWS})().some((r) => !r.classList.contains('pending'))")
    assert page.evaluate(NONE_CARRY_TAG, tag)
    urls = []
    page.on("request", lambda r: urls.append(r.url))
    try:
        _add_auto_tag_indicator(page, tag, "H2", "40")
        assert page.evaluate("() => __winnow.S.gridRepaintPending") is True
        left_behind = page.evaluate("() => __winnow.S.view.view_id")
        mark = len(urls)
        page.evaluate("(id) => __winnow.openSource(id)", plain_second_table)
        page.wait_for_function("(id) => __winnow.S.sourceId === id && !!__winnow.S.view"
                               " && __winnow.S.view.source_id === id && __winnow.busyCount === 0",
                               arg=plain_second_table, timeout=15_000)
        assert not [u for u in urls[mark:] if re.search(rf"view_id={left_behind}(?![0-9])", u)]
        assert page.evaluate("() => __winnow.S.gridRepaintPending") is False
        # Back to the table the scan tagged: its rows wear the tags,
        # whoever did the painting.
        page.evaluate("(id) => __winnow.openSource(id)", first)
        page.wait_for_function("(id) => __winnow.S.sourceId === id && !!__winnow.S.view"
                               " && __winnow.S.view.source_id === id", arg=first, timeout=15_000)
        page.wait_for_function(PAINTED_WITH_TAGS, timeout=15_000)
    finally:
        page.evaluate("() => { __winnow.closeNoticesOwnedBy('watchlist'); }")
        if page.evaluate("() => __winnow.S.sourceId") != first:
            page.evaluate("(id) => __winnow.openSource(id)", first)
            page.wait_for_function("(id) => __winnow.S.sourceId === id", arg=first)
        _restore(server, server_post, tag)


def test_an_auto_tag_scan_regroups_a_grouping_by_tag_on_the_way_back(page, server, server_post):
    """A grouping BY TAG buckets rows by the tags on them, so an auto-tag
    moves rows between buckets — the tree the analyst comes back to must
    not still say every row is untagged. The tree is server-side (the
    counts and each expanded group's sub-view), so there is nothing here
    to patch it with: the repaint owed from the hidden grid regroups."""
    tag = page.evaluate("() => __winnow.S.tags[0].id")
    _restore(server, server_post, tag)
    page.evaluate("() => __winnow.addGroupLevel(__winnow.TAG_GROUP_COLUMN)")
    page.wait_for_function("() => __winnow.S.groups.length > 0")
    assert page.evaluate("(t) => !__winnow.S.groups.some((g) => g.value === t)", tag)
    try:
        _add_auto_tag_indicator(page, tag, "H2", "40")
        assert page.evaluate("() => __winnow.S.gridRepaintPending") is True
        page.keyboard.press("Alt+1")
        page.wait_for_selector("#grid:not([hidden])")
        # The 40 rows the scan tagged are in the tag's own group now, and
        # the group the tree had for them is that much shorter.
        page.wait_for_function("(t) => __winnow.S.groups.some((g) => g.value === t && g.count === 40)",
                               arg=tag, timeout=15_000)
        assert page.evaluate("() => __winnow.S.groups.some((g) => g.value === null && g.count === 160)")
    finally:
        page.evaluate("() => { __winnow.closeNoticesOwnedBy('watchlist'); }")
        page.evaluate("() => __winnow.dropGrouping()")
        page.wait_for_function("() => !__winnow.S.groupByCols.length")
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
    assert page.evaluate(NONE_CARRY_TAG, tag)
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
