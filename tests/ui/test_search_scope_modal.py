"""The search dialog's scope row: every table, this table, or a pick.

What makes this worth a browser test rather than a backend one is the
second half — the results pane has to keep describing the scope the
results CAME from, not the one the row above has since been switched to.
The same trap `st.terms` already exists to avoid: a pane that re-labels
itself when you change your mind is a pane that lies about numbers nobody
re-ran.

The job itself is stubbed with page.route: the shared session case has one
table, and the assertions here are about what the dialog sends and what it
says afterwards, not about counting rows.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui


def _stub_sweep(page, hits):
    """Answer /api/search_all/start and its poll from here. Returns the
    list the request bodies land in, newest last."""
    sent: list[dict] = []
    state: dict = {}

    def _record(route):
        body = json.loads(route.request.post_data)
        sent.append(body)
        state["scope"] = {"requested": body.get("source_ids"),
                          "source_ids": body.get("source_ids"),
                          "merges": []}
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "job_id": 7, "scope": state["scope"], "scanned": 0, "total": 1,
            "hits": [], "done": False, "error": None, "cancelled": False,
        }))

    def _poll(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "job_id": 7, "scope": state["scope"], "scanned": 1, "total": 1,
            "hits": hits, "done": True, "error": None, "cancelled": False,
        }))

    page.route("**/api/search_all/start", _record)
    page.route("**/api/search_all/job*", _poll)
    return sent


def _pressed_scope(page):
    return page.locator('#modalBody .search-all-scope .vp-seg button[aria-pressed="true"]').inner_text()


def _run(page, term, expect):
    """Type the term, press Search, wait for the stubbed job to finish and
    the pane to say `expect`. Both halves matter: `running` is false before
    the run starts as well as after it ends, and the scope line is painted
    from the pending scope before the first poll answers."""
    page.locator("#modal .search-all-paste").fill(term)
    page.locator("#modalBody .row-actions button").first.click()
    page.wait_for_function(
        "(t) => { const st = __winnow.S.searchAll;"
        " const n = document.querySelector('#modalBody .search-all-results');"
        " return !!st && !st.running && st.jobId === 7 && !!n && n.textContent.includes(t); }",
        arg=expect, timeout=15_000)


def test_scope_defaults_to_the_open_table_and_travels_with_the_results(page):
    source_id = page.evaluate("() => __winnow.S.sourceId")
    hits = [{"source_id": source_id, "name": "ui.csv", "match_count": 1000,
             "capped": True, "terms": []}]
    sent = _stub_sweep(page, hits)
    try:
        page.evaluate("() => __winnow.openSearchAllModal()")
        page.wait_for_selector("#modal:not([hidden])")
        # A table is open, so the sweep is offered scoped to it — the
        # whole-case sweep is one click away, the reverse is minutes.
        assert _pressed_scope(page) == "This table"

        _run(page, "powershell", "Searched ui.csv.")
        assert sent[-1]["source_ids"] == [source_id]
        pane = page.locator("#modalBody .search-all-results").inner_text()
        # The cap is the reason the count says "1,000+", so the pane says
        # where the exact number lives instead of leaving "+" to explain it.
        assert "Counts stop at 1,000 per table" in pane

        # Re-scoping without re-running must NOT re-label the results: they
        # are still an answer about the table that was searched.
        page.locator("#modalBody .search-all-scope .vp-seg button", has_text="Every table").click()
        assert _pressed_scope(page) == "Every table"
        assert "Searched ui.csv." in page.locator("#modalBody .search-all-results").inner_text()

        # ...and running it whole-case says so, and sends no scope at all.
        _run(page, "powershell", "Searched every table in this case.")
        assert sent[-1]["source_ids"] is None
    finally:
        page.keyboard.press("Escape")
        page.evaluate("() => { __winnow.S.searchAll = null; }")


def test_choosing_tables_sends_exactly_the_ticked_ones(page):
    source_id = page.evaluate("() => __winnow.S.sourceId")
    sent = _stub_sweep(page, [])
    try:
        page.evaluate("() => __winnow.openSearchAllModal()")
        page.wait_for_selector("#modal:not([hidden])")
        chips = page.locator("#modalBody .search-all-picks button")
        assert chips.count() >= 1
        # Untick the only table there is: an empty scope is not a sweep of
        # everything, so the dialog refuses to start rather than quietly
        # searching the whole case.
        chips.first.click()
        assert _pressed_scope(page) == "Choose…"
        page.locator("#modal .search-all-paste").fill("powershell")
        page.locator("#modalBody .row-actions button").first.click()
        page.wait_for_selector(".toast", timeout=5_000)
        assert "at least one table" in page.locator(".toast").inner_text()
        assert sent == []

        # Tick it again and the pick goes out as an explicit id list.
        chips.first.click()
        _run(page, "powershell", "Searched ui.csv.")
        assert sent[-1]["source_ids"] == [source_id]
    finally:
        page.keyboard.press("Escape")
        page.evaluate("() => { __winnow.S.searchAll = null; }")


def test_a_scoped_start_says_it_will_stop_the_running_sweep(page):
    """One search job per case, so a scoped start takes the slot from a
    whole-case sweep that may be minutes in. That is a real loss, so it is
    asked for rather than done quietly."""
    sent: list[dict] = []
    # A sweep that never finishes, so there is always one to supersede. The
    # poll answers with the scope of the most recent start, the way the
    # server does — a poll that kept naming the old scope would overwrite
    # the new run's with it.
    state: dict = {"scope": {"requested": None, "source_ids": None, "merges": []}}

    def _start(route):
        body = json.loads(route.request.post_data)
        sent.append(body)
        state["scope"] = {"requested": body.get("source_ids"),
                          "source_ids": body.get("source_ids"), "merges": []}
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "job_id": 7, "scope": state["scope"],
            "scanned": 1, "total": 12, "hits": [], "done": False, "error": None, "cancelled": False,
        }))

    def _poll(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "job_id": 7, "scope": state["scope"],
            "scanned": 3, "total": 12, "hits": [], "done": False, "error": None, "cancelled": False,
        }))

    page.route("**/api/search_all/start", _start)
    page.route("**/api/search_all/job*", _poll)
    try:
        page.evaluate("() => __winnow.openSearchAllModal()")
        page.wait_for_selector("#modal:not([hidden])")
        page.locator("#modalBody .search-all-scope .vp-seg button", has_text="Every table").click()
        page.locator("#modal .search-all-paste").fill("powershell")
        page.locator("#modalBody .row-actions button").first.click()
        page.wait_for_function(
            "() => __winnow.S.searchAll && __winnow.S.searchAll.running && __winnow.S.searchAll.total === 12",
            timeout=15_000)

        # Now scope it down and search again: the sweep is still running.
        page.locator("#modalBody .search-all-scope .vp-seg button", has_text="This table").click()
        page.locator("#modalBody .row-actions button").first.click()
        page.wait_for_selector(".confirm-overlay .confirm-message", timeout=5_000)
        msg = page.locator(".confirm-overlay .confirm-message").inner_text()
        assert "still running" in msg and "3 of 12 tables" in msg
        # Backing out leaves the sweep alone — no second job was started.
        page.locator(".confirm-overlay .confirm-actions button", has_text="Cancel").click()
        page.wait_for_selector(".confirm-overlay", state="detached", timeout=5_000)
        assert len(sent) == 1
        assert page.evaluate("() => __winnow.S.searchAll.running") is True

        page.locator("#modalBody .row-actions button").first.click()
        page.locator(".confirm-overlay .confirm-actions button", has_text="Stop it and search").click()
        page.wait_for_function("() => __winnow.S.searchAll.ranScope.source_ids !== null", timeout=15_000)
        assert len(sent) == 2 and sent[-1]["source_ids"] is not None
    finally:
        page.keyboard.press("Escape")
        page.evaluate("() => { __winnow.S.searchAll = null; }")
