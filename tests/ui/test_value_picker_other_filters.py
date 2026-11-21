"""The value picker counts under every other filter, the column's own picks
excepted — Excel's rule for its header drop-downs.

Reported by analysts: filter column A, open column B's drop-down, and its
values and counts ignored A, so ticking one value A had already ruled out
applied a filter that left no rows. It happened whenever B already had a
value selection: the picker opened such a column on Whole table, which
ignores every filter, so that its own unticked values could be ticked
again. This view now leaves only the column's own picks out, which keeps
both: the values the other filters rule out are gone, and the ones this
column's picks left out are still there, unticked.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.ui

# Host → its users, with counts: WS01 alice 3 + bob 2; WS02 carol 4 +
# dave 1; DC01 alice 1 + admin 2.
ROWS = (
    [["WS01", "alice", "4624"]] * 3 + [["WS01", "bob", "4625"]] * 2
    + [["WS02", "carol", "4624"]] * 4 + [["WS02", "dave", "4688"]]
    + [["DC01", "alice", "4672"]] + [["DC01", "admin", "4624"]] * 2
)


@pytest.fixture
def logons(page, server_post, ui_csv, until):
    p = Path(ui_csv).parent / "logons_vp.csv"
    p.write_text("Host,User,EventID\n" + "".join(",".join(r) + "\n" for r in ROWS))
    server_post("/api/ingest/jobs/path", {"path": str(p), "build_fts": False})
    until(page, lambda: page.evaluate("""() => __winnow.loadSources(undefined, { navigate: false }).then(() =>
            __winnow.S.sources.some((s) => s.name === 'logons_vp.csv' && s.row_count === 13 && s.columns.length))"""),
          "logons_vp.csv to finish importing", timeout=20)
    sid = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'logons_vp.csv').id")
    page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && !!__winnow.S.view", arg=sid)
    yield sid
    page.evaluate("(id) => __winnow.api(`/api/source/${id}`, { method: 'DELETE' }).then(() => __winnow.loadSources())", sid)


def _open(page, col):
    """Opens the column's picker and returns its list once it has loaded:
    {value: (count, ticked)}."""
    page.locator(f'.fcell-pick[data-col="{col}"]').click()
    page.wait_for_selector(".value-picker .vp-row")
    return page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('.value-picker .vp-row')]
      .map((r) => [r.querySelector('.vp-value').textContent,
                   [Number(r.querySelector('.vp-count').textContent.replace(/,/g, '')), r.querySelector('input').checked]]))""")


def _scope(page):
    return page.locator(".value-picker .vp-seg button[aria-pressed='true']").inner_text()


def _pick(page, view_rebuilt, col, values):
    _open(page, col)
    page.locator(".value-picker .vp-actions .btn", has_text="None").click()
    for v in values:
        page.locator(".value-picker .vp-row").filter(has=page.get_by_text(v, exact=True)).locator("input").check()
    with view_rebuilt(page):
        page.locator(".value-picker .vp-actions .btn:not(.ghost)").click()


def _rows(page):
    return page.evaluate("() => __winnow.S.view.row_count")


def _box(page, col):
    return page.locator(f'.fcell input[data-col="{col}"]').input_value()


def test_a_picked_column_lists_only_what_the_other_filters_leave(page, logons, view_rebuilt):
    """The report, step for step: User picked first, then Host. User's list
    used to be the whole table (carol 4, dave 1, admin 2 all offered), and
    ticking carol alone filtered to nothing."""
    _pick(page, view_rebuilt, "User", ["alice", "carol"])
    assert _rows(page) == 8
    _pick(page, view_rebuilt, "Host", ["WS01"])
    assert _rows(page) == 3

    listed = _open(page, "User")
    assert _scope(page) == "This view"
    # WS01's users only, at WS01's counts; bob is offered, unticked, because
    # User's own picks don't narrow User's list.
    assert listed == {"alice": [3, True], "bob": [2, False]}

    page.locator(".value-picker .vp-actions .btn", has_text="None").click()
    page.locator(".value-picker .vp-row").filter(has=page.get_by_text("bob", exact=True)).locator("input").check()
    with view_rebuilt(page):
        page.locator(".value-picker .vp-actions .btn:not(.ghost)").click()
    assert _box(page, "User") == "=bob"
    assert _rows(page) == 2


def test_the_first_column_counts_under_the_second(page, logons, view_rebuilt):
    """The other way round: Host's own pick is left out of Host's list, but
    User's is not — WS02 has no alice, so it isn't offered."""
    _pick(page, view_rebuilt, "Host", ["WS01"])
    _pick(page, view_rebuilt, "User", ["alice"])
    assert _open(page, "Host") == {"WS01": [3, True], "DC01": [1, False]}


def test_ticking_every_value_listed_clears_the_columns_picks(page, logons, view_rebuilt):
    """Every value the other filters leave, ticked, is no filter on this
    column at all — so it is cleared rather than written out, as it is on
    a column that had no picks."""
    _pick(page, view_rebuilt, "Host", ["WS01", "DC01"])
    _pick(page, view_rebuilt, "User", ["alice"])
    assert _rows(page) == 4
    _open(page, "User")
    page.locator(".value-picker .vp-actions .btn", has_text="All").click()
    with view_rebuilt(page):
        page.locator(".value-picker .vp-actions .btn:not(.ghost)").click()
    assert _box(page, "User") == ""
    assert _rows(page) == 8


def test_the_search_box_narrows_it_too(page, logons, view_rebuilt):
    _pick(page, view_rebuilt, "User", ["alice"])
    page.locator("#btnSearchToggle").click()   # the box starts folded away
    try:
        with view_rebuilt(page):
            page.locator("#search").fill("DC01")
        assert _rows(page) == 1
        assert _open(page, "User") == {"alice": [1, True], "admin": [2, False]}
    finally:
        if page.locator(".value-picker").count():
            page.locator(".value-picker .vp-actions .btn", has_text="Cancel").click()
        if page.locator("#search").input_value():
            with view_rebuilt(page):
                page.locator("#search").fill("")


def test_whole_table_is_still_a_click_away(page, logons, view_rebuilt):
    _pick(page, view_rebuilt, "Host", ["WS01"])
    _pick(page, view_rebuilt, "User", ["alice"])
    _open(page, "User")
    page.locator(".value-picker .vp-seg button", has_text="Whole table").click()
    page.wait_for_function("() => document.querySelectorAll('.value-picker .vp-row').length === 5")
    counts = page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('.value-picker .vp-row')]
      .map((r) => [r.querySelector('.vp-value').textContent, r.querySelector('.vp-count').textContent]))""")
    assert counts == {"alice": "4", "carol": "4", "bob": "2", "admin": "2", "dave": "1"}
