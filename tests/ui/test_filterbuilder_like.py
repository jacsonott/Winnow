"""The filter builder's "Equivalent SQL (editable):" box and LIKE.

Typed SQL with a LIKE used to become a guided condition that meant
something else: parseWhereFragment never looked at `%` and `_`, so
`"Host" LIKE '%1'` became equals '%1' (no rows; SQLite matches 40),
`LIKE 'h_'` an equals too, `LIKE 'h1'` a case-SENSITIVE equals (no rows,
where LIKE folds case and matches 40), and `LIKE '%powershell%enc%'` a
contains of the literal text `powershell%enc`. Nothing went to raw SQL
mode, which the wiki promises for SQL the builder can't show.

The other direction was off as well: a guided contains on text holding a
`%` or `_` was shown as a LIKE in which they were wildcards.

Both are asserted the way they broke: rows on the grid against what
SQLite says the SQL in the box matches, on the shared table."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _sqlite_count(api, sid, where):
    return api("/api/sql", "POST", {"sql": f"SELECT COUNT(*) FROM src_{sid} WHERE {where}"})["rows"][0][0]


def _reset(page, view_rebuilt):
    with view_rebuilt(page):
        page.evaluate("() => { __winnow.S.filterTree = { type: 'group', op: 'AND', children: [] };"
                      " return __winnow.rebuildView(); }")


@pytest.fixture
def home(page, view_rebuilt):
    sid = page.evaluate("() => __winnow.S.sourceId")
    try:
        yield sid
    finally:
        page.evaluate("() => { if (!document.getElementById('modal').hidden) __winnow.closeModal(); }")
        if page.evaluate("() => __winnow.hasActiveFilterTree()"):
            _reset(page, view_rebuilt)


def _open_builder(page):
    page.evaluate("() => __winnow.openFilterBuilder()")
    page.wait_for_selector("#modal:not([hidden]) .fb-sql")


def _apply(page, view_rebuilt):
    with view_rebuilt(page):
        page.locator("#modal .row-actions .btn.primary").click()
    page.wait_for_selector("#modal[hidden]", state="attached")


# (typed SQL, the guided condition it may become, or None for raw mode)
TYPED = [
    # The reported shapes: none of these has a guided twin.
    ("\"Host\" LIKE '%1'", None),                       # ends with
    ("\"Host\" LIKE 'h_'", None),                       # one-character wildcard
    ("\"Host\" LIKE 'h1'", None),                       # LIKE folds case; equals doesn't
    ("\"CommandLine\" LIKE '%powershell%enc%'", None),  # a wildcard inside a contains
    ("\"CommandLine\" LIKE 'c:%.exe%'", None),          # ... and inside a starts-with
    ("\"Host\" LIKE '%%'", None),                       # every non-NULL value, not "no filter"
    # The two that do mean a guided condition exactly, and stay guided.
    ("\"Host\" LIKE '%h1%'", {"op": "contains", "value": "h1"}),
    ("\"CommandLine\" LIKE 'c:\\windows%'", {"op": "starts", "value": "c:\\windows"}),
    ("\"Payload\" LIKE '%\"u1\"%'", {"op": "contains", "value": '"u1"'}),
    ("\"Host\" LIKE '%h\\_%' ESCAPE '\\'", {"op": "contains", "value": "h_"}),
]


def test_typed_like_filters_the_rows_sqlite_says_it_matches(page, api, home, view_rebuilt):
    sid = home
    for sql, guided in TYPED:
        want = _sqlite_count(api, sid, sql)
        _open_builder(page)
        page.locator("#modal .fb-sql").fill(sql)
        tree = page.evaluate("() => __winnow.S.filterTree")
        raw_banner = page.locator("#modal .fb-raw-banner").count()
        _apply(page, view_rebuilt)
        assert page.evaluate("() => __winnow.S.view.row_count") == want, (sql, tree)
        # And it says which it is: the builder shows what it can and keeps
        # the rest as the SQL that was typed.
        if guided is None:
            assert tree == {"type": "raw", "sql": sql} and raw_banner == 1, (sql, tree)
        else:
            assert tree["type"] == "group" and len(tree["children"]) == 1, (sql, tree)
            cond = tree["children"][0]
            assert {"op": cond["op"], "value": cond["value"]} == guided, sql
        _reset(page, view_rebuilt)


# Guided conditions whose text holds LIKE's own wildcards (or its escape
# character, or a quote). The grid filters them as literal text.
GUIDED = [
    ("CommandLine", "contains", "Windows%System32"),   # was shown as LIKE '%Windows%System32%': 200 rows
    ("Host", "contains", "H_"),                        # was shown as LIKE '%H_%': 200 rows
    ("Host", "starts", "H_"),
    ("CommandLine", "contains", "\\System32\\%"),     # a backslash beside an escaped wildcard
    ("CommandLine", "contains", "System32\\WindowsPowerShell"),  # a backslash alone stays bare
    ("Payload", "contains", "it's"),
    ("Host", "not_contains", "H_"),
]


def test_a_guided_condition_is_shown_as_sql_that_means_the_same(page, api, home, view_rebuilt):
    sid = home
    for column, op, value in GUIDED:
        cond = {"type": "cond", "column": column, "op": op, "value": value}
        with view_rebuilt(page):
            page.evaluate("(c) => { __winnow.S.filterTree = { type: 'group', op: 'AND', children: [c] };"
                          " return __winnow.rebuildView(); }", cond)
        rows = page.evaluate("() => __winnow.S.view.row_count")
        _open_builder(page)
        sql = page.locator("#modal .fb-sql").input_value()
        page.evaluate("() => __winnow.closeModal()")
        assert _sqlite_count(api, sid, sql) == rows, (cond, sql)
        if op != "not_contains":   # its compound shape is raw mode by design
            parsed = page.evaluate("(s) => __winnow.parseWhereFragment(s)", sql)
            assert parsed == {"type": "group", "op": "AND", "children": [cond]}, sql
        _reset(page, view_rebuilt)
