"""A table the Search-all sweep could not count is named, not counted as
zero.

The sweep recorded a count that raised as no match, so a table nobody
could read — one wide enough that its search expression was refused —
came back as "No matches.", the one answer an analyst moves on from. The
server now reports such a table in the job's `failed` list; this pins
that the dialog and the finished-in-the-background toast both say so.

The sweep is stubbed (page.route), the idiom test_search_all_landing.py
uses: what is under test is what the page says about a failure, and the
shared case has no table the real sweep would fail on.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui


def _stub_sweep(page, failed):
    scope = {"requested": None, "source_ids": None, "merges": []}
    base = {"job_id": 12, "scope": scope, "error": None, "cancelled": False, "hits": []}

    page.route("**/api/search_all/start", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({**base, "scanned": 0, "total": 1, "failed": [], "done": False})))
    page.route("**/api/search_all/job*", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({**base, "scanned": 1, "total": 1, "failed": failed, "done": True})))


def _unstub(page):
    page.unroute("**/api/search_all/start")
    page.unroute("**/api/search_all/job*")
    page.evaluate("() => { __winnow.S.searchAll = null; __winnow.updateSearchAllButton(); }")


def test_the_dialog_names_a_table_it_could_not_search(page):
    source_id = page.evaluate("() => __winnow.S.sourceId")
    _stub_sweep(page, [{"source_id": source_id, "name": "ui.csv",
                        "error": "Expression tree is too large (maximum depth 1000)"}])
    try:
        page.evaluate("() => __winnow.openSearchAllModal()")
        page.wait_for_selector("#modal:not([hidden])")
        page.locator("#modal .search-all-paste").fill("4624")
        page.locator("#modalBody .row-actions button").first.click()
        page.wait_for_selector("#modalBody .search-all-failed", timeout=15_000)
        said = page.locator("#modalBody").inner_text()
        assert "Could not search" in said and "Expression tree is too large" in said, said
        page.wait_for_function("() => !__winnow.S.searchAll.running", timeout=15_000)
        said = page.locator("#modalBody").inner_text()
        assert "No matches.\n" not in said + "\n", said
        assert "No matches in the tables that could be searched." in said, said
    finally:
        page.keyboard.press("Escape")
        _unstub(page)


def test_the_background_toast_does_not_say_no_matches(page):
    source_id = page.evaluate("() => __winnow.S.sourceId")
    _stub_sweep(page, [{"source_id": source_id, "name": "ui.csv", "error": "disk I/O error"}])
    try:
        page.evaluate("() => __winnow.openSearchAllModal()")
        page.wait_for_selector("#modal:not([hidden])")
        page.locator("#modal .search-all-paste").fill("4624")
        # Start, then close the dialog before the first poll lands: the
        # result arrives as a toast.
        page.evaluate("""() => { __winnow.startSearchAll();
                                 document.getElementById('modal').hidden = true; }""")
        page.wait_for_function(
            "() => { const t = document.getElementById('toast');"
            " return !t.hidden && t.textContent.includes('finished'); }", timeout=15_000)
        said = page.locator("#toast").inner_text()
        assert "could not be searched" in said, said
    finally:
        _unstub(page)
