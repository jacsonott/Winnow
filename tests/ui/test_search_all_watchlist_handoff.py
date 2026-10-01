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

import time

import pytest

pytestmark = pytest.mark.ui

# Nothing in the fixture table holds these — the sweep comes back clean,
# which is the case the handoff is for.
ABSENT = ["QQZZALPHA", "QQZZBETA"]


def _clear(page):
    page.evaluate("""async () => {
      const h = { 'X-Timeline-Lite-Client': '1' };
      for (const i of await fetch('/api/watchlist', { headers: h }).then((r) => r.json()))
        await fetch('/api/watchlist/' + i.id, { method: 'DELETE', headers: h });
    }""")


@pytest.fixture(autouse=True)
def _clean(page):
    _clear(page)
    # Records every scan start: the body sent and the job snapshot back.
    page.evaluate("""() => {
      window.__scans = [];
      const real = window.fetch;
      window.fetch = async (url, opts) => {
        const r = await real(url, opts);
        if (String(url).includes('/api/watchlist/scan/start')) {
          const copy = r.clone();
          window.__scans.push({ body: JSON.parse((opts || {}).body || '{}'), job: await copy.json() });
        }
        return r;
      };
    }""")
    yield
    page.keyboard.press("Escape")
    page.evaluate("() => { document.getElementById('modal').hidden = true; }")
    _clear(page)


def _sweep_and_add(page, terms):
    page.locator("#btnSearchAll").click()
    page.wait_for_selector("#modal:not([hidden])")
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


def test_the_table_the_sweep_cleared_is_not_read_again(page):
    _sweep, scan = _sweep_and_add(page, ABSENT)
    # One table in this case, swept clean for both terms — so the scan has
    # nothing left to read at all.
    assert scan["job"]["seeded"]["tables"] == 1
    assert scan["job"]["seeded"]["pairs"] == 2
    # A scope of no tables: everything the scan was for is already answered.
    assert scan["job"]["source_ids"] == []


def test_a_table_the_sweep_found_something_in_is_still_read(page):
    """The other half: a term that matches is not proof of anything, so
    that table is read and the hits get written."""
    _sweep, scan = _sweep_and_add(page, ["powershell.exe", "QQZZALPHA"])
    assert scan["job"]["seeded"] == {"tables": 0, "pairs": 0}
    assert scan["job"]["source_ids"] is None       # nothing narrowed: the whole case
    # Polled from Python: wait_for_function does not await a promise
    # predicate (tests/test_ui_test_hygiene.py).
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        got = page.evaluate("""() => fetch('/api/watchlist', { headers: { 'X-Timeline-Lite-Client': '1' } })
          .then((r) => r.json())""")
        if any(i["value"] == "powershell.exe" and i["hit_count"] > 0 for i in got):
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
