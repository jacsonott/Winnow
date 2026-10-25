"""The Watchlist's Export… file goes back in through its own Import….

The export is a CSV under a header row; the import read it as a plain
one-per-line list, so the dialog counted the header as an indicator, the
import added one called `value`, every kind came back as the rest of its
line ("filename,,0") and a value holding a comma was cut at it. This
drives the real button into the real dialog: export, clear the watchlist,
import the downloaded file, and the same indicators come back with their
types and notes.

Every value is chosen not to appear in the fixture table, so the scan the
import starts flags nothing in the case these modules share.
"""

from __future__ import annotations

import json
import time
import urllib.request

import pytest

pytestmark = pytest.mark.ui

INDICATORS = [
    {"value": "QXRT-roundtrip.exe", "kind": "filename", "note": None},
    {"value": "qxrt,bad.example", "kind": "domain", "note": "seen in proxy, dns logs"},
    {"value": "10.99.88.77", "kind": "ip", "note": 'line one\nsaid "hi"'},
]


def _req(server, route, method="GET", body=None):
    """Over HTTP from Python, with a timeout — page.evaluate on a fetch has
    none (see test_watchlist_import_options.py)."""
    req = urllib.request.Request(
        server.rstrip("/") + route, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"X-Timeline-Lite-Client": "1", "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read() or "null")


def _clear(server):
    for i in _req(server, "/api/watchlist"):
        _req(server, f"/api/watchlist/{i['id']}", method="DELETE")


def _as_added(items):
    return sorted((i["value"], i["kind"], i["note"]) for i in items)


@pytest.fixture(autouse=True)
def _clean(page, server):
    _clear(server)
    yield
    page.keyboard.press("Escape")
    page.evaluate("() => { document.getElementById('modal').hidden = true; }")
    _clear(server)


def test_an_export_imports_back_as_the_same_indicators(page, server, tmp_path):
    for ind in INDICATORS:
        _req(server, "/api/watchlist", method="POST", body=ind)
    page.locator("#tabWatchlist").click()
    page.wait_for_selector("#watchlistview:not([hidden])")
    page.wait_for_function("(n) => document.querySelectorAll('#wlList .wl-row').length === n",
                           arg=len(INDICATORS), timeout=10_000)

    with page.expect_download(timeout=15_000) as dl:
        page.locator("#wlExport").click()
    exported = tmp_path / "watchlist.csv"
    dl.value.save_as(str(exported))

    # A fresh watchlist: nothing on it for the import to skip as a duplicate.
    _clear(server)
    page.locator("#wlImportFile").set_input_files(str(exported))
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Import indicators'")
    d = page.evaluate("""() => {
      const b = document.getElementById('modalBody');
      return {
        head: b.querySelector('.note-status').textContent,
        preview: [...b.querySelectorAll('.wl-import-preview > div')].map((x) => x.textContent),
        button: b.querySelector('.row-actions button').textContent,
      };
    }""")
    # The dialog's count is the part an analyst checks — it said 4, and
    # previewed "value" as the first indicator.
    assert d["head"].startswith("3 indicators from watchlist.csv"), d["head"]
    assert d["preview"] == [i["value"] for i in INDICATORS]
    assert d["button"] == "Import 3"

    page.locator("#modalBody .row-actions button", has_text="Import 3").click()
    page.wait_for_selector("#modal[hidden]", state="attached", timeout=10_000)
    deadline = time.monotonic() + 20
    got = []
    while time.monotonic() < deadline:
        got = _req(server, "/api/watchlist")
        if len(got) >= len(INDICATORS):
            break
        time.sleep(0.2)
    assert "value" not in [i["value"] for i in got]
    assert _as_added(got) == _as_added(INDICATORS)
