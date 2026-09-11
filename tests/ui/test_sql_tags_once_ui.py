"""The SQL pane's Tags column, once.

A pane view already carries a Tags column (the names, comma-joined), and
the renderer joined a second one (the chips) whenever rid resolved — so
`SELECT * FROM src_1` showed two Tags columns saying the same thing. The
chips now land in the view's column when it's in the result."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open_sql(page):
    page.click("#tabSql")
    page.wait_for_selector("#sqlview:not([hidden])")
    page.wait_for_function(
        "() => __winnow.S.sqlTabs.length > 0 && !document.getElementById('sqlText').disabled")


def _run(page, sql):
    page.locator("#sqlText").fill(sql)
    page.click("#btnRunSql")
    page.wait_for_function("() => /tags joined via rid/.test(document.querySelector('#sqlResult').textContent)")


def _headers(page):
    heads = page.locator("#sqlResult th")
    return [heads.nth(i).inner_text() for i in range(heads.count())]


def test_the_views_tags_column_hosts_the_chips_instead_of_a_second_column(page):
    page.locator(".row").nth(0).click()
    page.keyboard.press("1")
    page.wait_for_function("() => __winnow.rowAt(0).tags.length === 1")
    rid = page.evaluate("() => __winnow.rowAt(0).rid")
    try:
        _open_sql(page)
        _run(page, f"SELECT rid, EventId, Tags FROM src_1 WHERE rid = {rid}")
        assert _headers(page) == ["rid", "EventId", "Tags"]
        assert page.locator(".sql-tag-chip").count() == 1
        # ...and the chip sits in the Tags column, not off the end.
        assert page.locator("#sqlResult tr").nth(1).locator("td").nth(2).locator(".sql-tag-chip").count() == 1

        # SELECT * carries every view column; still exactly one Tags header.
        _run(page, f"SELECT * FROM src_1 WHERE rid = {rid}")
        heads = _headers(page)
        assert heads.count("Tags") == 1, heads
        assert page.locator(".sql-tag-chip").count() == 1

        # Without the view's column in the result the chips still get their own.
        _run(page, f"SELECT rid, EventId FROM src_1 WHERE rid = {rid}")
        assert _headers(page) == ["rid", "EventId", "Tags"]
        assert page.locator(".sql-tag-chip").count() == 1
    finally:
        page.evaluate("() => __winnow.showGridTab()")
        page.locator(".row").nth(0).click()
        page.keyboard.press("1")
        page.wait_for_function("() => __winnow.rowAt(0).tags.length === 0")


def test_a_files_own_tags_column_keeps_its_text_on_a_table_and_on_a_merge(page, server_post, tmp_path):
    """A file whose header has a Tags column: the view names Winnow's
    column 'Winnow Tags', and the file's text must stay text — on the
    single table and on a merge of two such files, where the result
    carries source_id and the chips resolve per row."""
    a = tmp_path / "a_tags.csv"
    b = tmp_path / "b_tags.csv"
    a.write_text("When,Tags,Host\n2026-01-01 00:00:00,from-file-a,H1\n")
    b.write_text("When,Tags,Host\n2026-01-02 00:00:00,from-file-b,H2\n")
    before = page.evaluate("() => __winnow.S.sources.length")
    server_post("/api/ingest/jobs/path", {"path": str(a)})
    server_post("/api/ingest/jobs/path", {"path": str(b)})
    page.wait_for_function("(n) => { __winnow.loadSources(); return __winnow.S.sources.filter((s) => !s.is_merge).length >= n + 2; }",
                           arg=before, timeout=15_000)
    ids = page.evaluate("() => Object.fromEntries(__winnow.S.sources.filter((s) => s.name.endsWith('_tags.csv')).map((s) => [s.name, s.id]))")
    merge = server_post("/api/merges", {"name": "tags-merge", "source_ids": [ids["a_tags.csv"], ids["b_tags.csv"]]})
    try:
        page.evaluate("() => __winnow.loadSources()")
        page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'tags-merge')")
        _open_sql(page)
        _run(page, f"SELECT rid, Tags, Host FROM src_{ids['a_tags.csv']}")
        assert _headers(page) == ["rid", "Tags", "Host", "Tags"], "the file's Tags is text; chips get their own column"
        assert page.locator("#sqlResult tr").nth(1).locator("td").nth(1).inner_text() == "from-file-a"

        mid = abs(int(merge["id"]))
        _run(page, f"SELECT source_id, rid, Tags FROM merge_{mid} ORDER BY source_id")
        heads = _headers(page)
        assert heads[:3] == ["source_id", "rid", "Tags"] and heads.count("Tags") == 2, heads
        cells = [page.locator("#sqlResult tr").nth(i).locator("td").nth(2).inner_text() for i in (1, 2)]
        assert sorted(cells) == ["from-file-a", "from-file-b"], cells
    finally:
        page.evaluate("() => __winnow.showGridTab()")
        for route in (f"/api/merges/{abs(int(merge['id']))}", f"/api/source/{ids['a_tags.csv']}", f"/api/source/{ids['b_tags.csv']}"):
            page.evaluate("(r) => fetch(r, { method: 'DELETE', headers: { 'X-Timeline-Lite-Client': '1' } })", route)
        page.wait_for_timeout(300)
        page.evaluate("() => __winnow.loadSources()")
