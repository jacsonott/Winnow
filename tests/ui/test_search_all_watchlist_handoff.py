"""Search-all's "Add to watchlist" hands the scan the sweep it just ran.

The sweep asked every table in its scope exactly the question the scan is
about to — the same two WHERE shapes — so the tables it cleared do not
need reading again. The server decides what the sweep proved
(Store.seed_watchlist_from_search_all, covered in
tests/test_watchlist_from_search_all.py); what this pins is the one part
the client owns: the sweep's job id reaching the scan, and the scan
coming back saying what it did not have to read.

Asserted off the request and the response rather than off the jobs-panel
row, which clears itself a few seconds after a scan lands. The response
is the job's snapshot at START, so the scope (`source_ids`) is the field
to read and not `total` — the worker sets that on its first turn, which
has not happened yet when the POST answers.
"""

from __future__ import annotations

import json
import time
import urllib.request

import pytest

pytestmark = pytest.mark.ui

# Nothing in the fixture table holds these — the sweep comes back clean,
# which is the case the handoff is for.
ABSENT = ["QQZZALPHA", "QQZZBETA"]


def _get(server, route):
    """Read over HTTP from Python, with a timeout. NOT through the page:
    `page.evaluate` on a promise has no timeout of its own, so one fetch
    that never settles hangs the whole job rather than failing a test."""
    req = urllib.request.Request(server.rstrip("/") + route,
                                 headers={"X-Timeline-Lite-Client": "1"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def _clear(server):
    for i in _get(server, "/api/watchlist"):
        urllib.request.urlopen(urllib.request.Request(
            server.rstrip("/") + f"/api/watchlist/{i['id']}", method="DELETE",
            headers={"X-Timeline-Lite-Client": "1"}), timeout=15).read()


@pytest.fixture(autouse=True)
def _clean(page, server):
    _clear(server)
    # Records every scan start: the body sent and the job snapshot back.
    page.evaluate("""() => {
      window.__scans = [];
      const real = window.fetch;
      // The app's own promise is handed straight back and the recording
      // hangs off a DETACHED chain. Awaiting a clone inside the wrapper
      // puts this test's bookkeeping on the app's critical path, and a
      // body that never settles then wedges the caller rather than
      // failing anything.
      window.fetch = (url, opts) => {
        const p = real(url, opts);
        if (String(url).includes('/api/watchlist/scan/start')) {
          p.then((r) => r.clone().json())
            .then((job) => window.__scans.push(
              { body: JSON.parse((opts || {}).body || '{}'), job }))
            .catch(() => {});
        }
        return p;
      };
    }""")
    yield
    page.keyboard.press("Escape")
    page.evaluate("() => { document.getElementById('modal').hidden = true; }")
    _clear(server)


def _sweep_and_add(page, terms, whole_case=True):
    """Sweeps, then adds. `whole_case` picks the scope explicitly, because
    the modal's own default is "This table" — and how many OTHER tables
    the case holds is decided by whichever modules ran before this one in
    the shared fixture case, so a test that assumed one would read as a
    feature failure the first time somebody left a table behind."""
    page.locator("#btnSearchAll").click()
    page.wait_for_selector("#modal:not([hidden])")
    if whole_case:
        page.locator("#modal .vp-seg button", has_text="Every table").click()
    page.locator("#modal .search-all-paste").fill("\n".join(terms))
    page.locator("#modal button", has_text="Search").first.click()
    page.wait_for_function("() => __winnow.S.searchAll && !__winnow.S.searchAll.running && __winnow.S.searchAll.jobId != null",
                           timeout=30_000)
    sweep = page.evaluate("() => __winnow.S.searchAll.jobId")
    page.locator("#modal button", has_text="Add to watchlist").click()
    page.wait_for_function("() => (window.__scans || []).length > 0", timeout=30_000)
    return sweep, page.evaluate("() => window.__scans[0]")


def test_the_scan_is_handed_the_sweeps_job_id(page):
    sweep, scan = _sweep_and_add(page, ABSENT)
    assert sweep is not None
    assert scan["body"]["from_search_all"] == sweep
    assert len(scan["body"]["watchlist_ids"]) == 2


def test_the_tables_the_sweep_cleared_are_not_read_again(page):
    """Every table swept, every table clean for both terms — so the scan is
    left with a scope of no tables at all."""
    _sweep, scan = _sweep_and_add(page, ABSENT)
    tables = scan["job"]["seeded"]["tables"]
    assert tables >= 1
    assert scan["job"]["seeded"]["pairs"] == tables * len(ABSENT)
    assert scan["job"]["source_ids"] == []


def test_a_sweep_of_one_table_clears_only_that_one(page):
    """The modal's default scope. The sweep proves what it read and no
    more, so the table it covered comes out of the scan's scope and the
    rest of the case stays in it."""
    _sweep, scan = _sweep_and_add(page, ABSENT, whole_case=False)
    assert scan["job"]["seeded"]["tables"] == 1
    scope = scan["job"]["source_ids"]
    assert scope is not None, "a scoped sweep should still narrow the scan"
    assert page.evaluate("() => __winnow.S.sourceId") not in scope


def test_a_table_the_sweep_found_something_in_is_still_read(page, server):
    """The other half: a term that matched a table proves nothing about
    it, so that table stays in the scan's scope and its hits get written.
    Only that table is asserted — whether the rest of the case was cleared
    depends on what other modules have left in the shared case."""
    open_id = page.evaluate("() => __winnow.S.sourceId")
    _sweep, scan = _sweep_and_add(page, ["powershell.exe", "QQZZALPHA"])
    scope = scan["job"]["source_ids"]
    assert scope is None or open_id in scope, f"{open_id} was dropped from {scope}"
    # Polled from Python: wait_for_function does not await a promise
    # predicate (tests/test_ui_test_hygiene.py).
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if any(i["value"] == "powershell.exe" and i["hit_count"] > 0
               for i in _get(server, "/api/watchlist")):
            break
        time.sleep(0.25)
    else:
        raise AssertionError("the scan never wrote powershell.exe's hits")


def test_an_add_that_is_not_from_a_sweep_hands_over_nothing(page):
    """The Add row's own scan has no sweep behind it — from_search_all must
    be null there, not a stale id from whenever Search-all last ran."""
    page.locator("#tabWatchlist").click()
    page.wait_for_selector("#watchlistview:not([hidden])")
    page.locator("#wlValue").fill(ABSENT[0])
    page.locator("#wlAdd").click()
    page.wait_for_function("() => (window.__scans || []).length > 0", timeout=30_000)
    scan = page.evaluate("() => window.__scans[0]")
    assert scan["body"]["from_search_all"] is None
    assert scan["job"]["seeded"] == {"tables": 0, "pairs": 0}
    assert scan["job"]["source_ids"] is None
