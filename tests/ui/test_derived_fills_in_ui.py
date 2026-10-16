"""A derived column fills in on the table it was added to, without leaving it.

The report: add a derived column and it stays empty, with "(building…)" in
its header, until the analyst navigates to another table and back. The
modal re-opens the table straight after the POST, while the backfill is
still running, so the rows it fetches carry no values yet; when the job
lands, pollJobs refreshed the source list with loadSources(navigate:false),
which deliberately leaves a live grid alone. Nothing re-read the rows or
the column's status (derived.js refreshAfterDerive now does).

A backfill over the shared fixture's 200 rows finishes before the grid can
fetch a page, so that case never reproduces it. This module boots its own
server on a table big enough for the pass to take a while, and sorts it
newest-first so the rows on screen are the last ones the pass reaches.
The first test asserts that the grid really was painted mid-pass before
it asserts the fix, so a pass that outran the page fetch fails loudly
rather than passing having proved nothing.
"""

from __future__ import annotations

import datetime as dt
import time

import pytest

from spawn import running_server

pytestmark = pytest.mark.ui

# Ends in ...0000 on purpose: the re-derive test below reads the last three
# digits, and the newest row's are 000 — nowhere near the top of the order.
N_ROWS = 150_001
EPOCH0 = 1_700_000_000


@pytest.fixture(scope="module")
def big_server(tmp_path_factory):
    d = tmp_path_factory.mktemp("fills")
    csv = d / "big.csv"
    with csv.open("w", encoding="utf-8") as f:
        f.write("Timestamp,Epoch,Host\n")
        for i in range(N_ROWS):
            f.write(f"2026-03-14 08:{i // 60 % 60:02d}:{i % 60:02d},{EPOCH0 + i},H{i % 5}\n")
    with running_server(d, "--case", d / "big.db", "--open", csv, "--no-fts",
                        ready="/api/sources", timeout=90) as srv:
        yield srv.base


@pytest.fixture
def big(own_page, big_server):
    # "load", not "networkidle": a table this size keeps background work
    # (column index builds) going for a while after the first paint.
    with own_page(base=big_server, wait_until="load",
                  ready="#rows .row:not(.pending)", ready_timeout=60_000) as pg:
        yield pg


def _call(page, path, method="GET", body=None):
    return page.evaluate(
        """([path, method, body]) => fetch(path, { method,
              headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
              body: body == null ? undefined : JSON.stringify(body) }).then((r) => r.json())""",
        [path, method, body])


# What the top row shows under a column, and what the column's header says
# about it — read off the DOM, which is what the analyst sees. `value` is
# null until the top row is painted from a fetched page.
_TOP = """(name) => {
  const ci = __winnow.visibleCols().indexOf(name);
  const row = document.querySelector('#rows .row[data-pos="0"]:not(.pending)');
  const cell = row && row.querySelector(`.cell[data-col="${ci}"]`);
  const mark = document.querySelector(`.hcell[data-col="${CSS.escape(name)}"] .hcell-derived`);
  return { value: cell ? cell.textContent : null, header: mark ? mark.title : null };
}"""


def _wait_top(page, name, value, timeout=15_000):
    """Until the top row shows `value` under `name`. Waited for rather than
    read once: a page fetched for the view being replaced can still land
    and paint after the rebuild that replaces it has returned."""
    page.wait_for_function(f"([n, v]) => ({_TOP})(n).value === v", arg=[name, value], timeout=timeout)


def _sort(page, column, direction, top):
    page.evaluate("""([column, dir]) => {
      __winnow.S.sort = [{ column, dir }];
      return __winnow.rebuildView();
    }""", [column, direction])
    _wait_top(page, column, top)


def _status(page, name):
    defs = _call(page, f"/api/derived?source_id={page.evaluate('() => __winnow.S.sourceId')}")
    d = next((x for x in defs if x["name"] == name), None)
    return d and d["status"], d and d["id"]


def _wait_ready(page, name, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status, did = _status(page, name)
        if status == "ready":
            return did
        time.sleep(0.1)
    raise AssertionError(f"{name} never finished building")


def _iso(epoch):
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def test_a_new_column_fills_in_without_leaving_the_table(big):
    page = big
    _sort(page, "Epoch", "desc", str(EPOCH0 + N_ROWS - 1))
    page.evaluate("() => __winnow.openDerivedColumnModal('Epoch')")
    page.wait_for_selector("#modal:not([hidden])")
    page.wait_for_selector(".derived-preview-row")
    name = page.locator(".derived-name").input_value()
    did = None
    try:
        page.click("#modalBody .btn:has-text('Add column')")
        page.wait_for_selector("#modal", state="hidden")
        # The state the report is about: the grid painted the new column
        # while the pass had not reached the rows on screen. If the pass
        # ever outruns the page fetch, this wait is what fails.
        _wait_top(page, name, "")
        assert "building" in page.evaluate(_TOP, name)["header"]
        # Something the analyst did in the meantime, which the landing
        # must not undo: re-opening the table would drop the pick.
        page.locator('#rows .row[data-pos="2"] .rowcheck').click()
        assert page.evaluate("() => __winnow.selCount()") == 1

        did = _wait_ready(page, name)
        # The newest row's value, and a header that has stopped saying
        # "(building…)" — with no navigation in between.
        _wait_top(page, name, _iso(EPOCH0 + N_ROWS - 1))
        assert "building" not in page.evaluate(_TOP, name)["header"]
        assert page.evaluate("() => __winnow.selPositions()") == [2]
    finally:
        if did is None:
            did = _wait_ready(page, name)
        _call(page, f"/api/derived/{did}", "DELETE")


def test_a_view_sorted_by_the_column_is_re_sorted_when_a_re_derive_lands(big):
    """The view on screen was ordered by the column's OLD values, and the
    re-open after a re-derive reuses it (same spec, still alive server-side).
    Refetching its rows would put the new values in the old order; the
    landing has to rebuild it."""
    page = big
    created = _call(page, "/api/derived", "POST", {
        "source_id": page.evaluate("() => __winnow.S.sourceId"), "name": "EpochDigits",
        "input_column": "Epoch", "op_id": "regex_extract", "params": {"pattern": r"(\d+)"}})
    did = created["definition"]["id"]
    try:
        _wait_ready(page, "EpochDigits")
        page.evaluate("() => __winnow.showDerivedColumnsSoon()")
        page.wait_for_function("() => __winnow.visibleCols().includes('EpochDigits')")
        _sort(page, "EpochDigits", "desc", str(EPOCH0 + N_ROWS - 1))

        # Same route the "Change the field path…" and Re-derive… items take.
        _call(page, f"/api/derived/{did}/rederive", "POST",
              {"params": {"pattern": r"(\d{3})$"}, "source_id": page.evaluate("() => __winnow.S.sourceId")})
        page.evaluate("() => __winnow.showDerivedColumnsSoon()")
        _wait_ready(page, "EpochDigits")
        # Highest last-three-digits first. The newest row (…000) led the old
        # order; left in place with its new value, it would read "000".
        _wait_top(page, "EpochDigits", "999")
    finally:
        _call(page, f"/api/derived/{did}", "DELETE")
