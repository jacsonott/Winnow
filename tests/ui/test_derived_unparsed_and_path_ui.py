"""Two items on a derived column's header menu that had never run:
"Show N unparsed rows" (for an extracted field, "Show N rows without this
field") and "Change the field path…".

The first is how "12 failures" becomes "these 12": it asks the server for
a filter fragment (two analyst-named columns quoted into SQL, which the page
never does itself) and opens the table on it. What it owes the analyst is
exactly the rows that failed, and on a merge the rows of every member.

The second edits a JSON or XML field column's path in place: the column
keeps its name and place, and its values change to the new field's. On a
merge that is every member's definition, not only the first one's.
"""

from __future__ import annotations

import json
import time

import pytest

pytestmark = pytest.mark.ui

WHEN = ["2026-03-14 08:00:00", "not a time", "2026-03-14 08:02:00", "n/a", "2026-03-14 08:04:00"]
PAYLOAD = [{"user": "alice", "host": "SRV1"}, {"user": "bob", "host": "WKS2"},
           {"host": "SRV3"}, {"user": "carol", "host": "WKS4"}]


def _csv(path, header, rows):
    def cell(v):
        return '"' + str(v).replace('"', '""') + '"'
    path.write_text("\n".join([",".join(header)] + [",".join(cell(v) for v in r) for r in rows]) + "\n",
                    encoding="utf-8")
    return path


def _ingest(api, path):
    return api("/api/ingest/path", "POST", {"path": str(path), "name": path.name, "build_fts": False})["id"]


def _defs(api, sid):
    return api(f"/api/derived?source_id={sid}")


def _wait_ready(api, sids, timeout=25.0):
    """Polled from Python: wait_for_function does not await a promise."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(d["status"] == "ready" for sid in sids for d in _defs(api, sid)):
            return
        time.sleep(0.2)
    pytest.fail(f"derived columns on {sids} never finished: {[_defs(api, s) for s in sids]}")


def _open(page, sid, col):
    page.evaluate("(id) => __winnow.loadSources().then(() => __winnow.openSource(id))", sid)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.busyCount === 0", arg=sid)
    page.wait_for_selector(f'.hcell[data-col="{col}"]')
    page.wait_for_selector(".row")


def _menu_item(page, col, starts):
    page.locator(f'.hcell[data-col="{col}"]').click(button="right")
    page.wait_for_selector(".menu .menu-item")
    items = page.locator(".menu .menu-item").all_inner_texts()
    match = [i for i in items if i.startswith(starts)]
    assert match, f"no item starting {starts!r} on {col}'s menu: {items}"
    return match[0]


# Every toast shown from now on, in order. A toast can be replaced before
# the next frame, and wait_for_function looks once a frame: "Recomputing"
# is followed 12ms later by the jobs poll's "rows read" for a derive that
# finished at once, so a wait on the toast's current text missed it in CI.
RECORD_TOASTS = """() => {
  window.__toasts = [];
  new MutationObserver((records) => {
    for (const r of records) for (const n of r.addedNodes) window.__toasts.push(n.textContent);
  }).observe(document.getElementById('toast'), { childList: true, subtree: true });
}"""


def _toasted(page, text):
    page.wait_for_function("(t) => window.__toasts.some((s) => s.includes(t))", arg=text)


def _column(page, col):
    """Every value of `col` the view holds, in view order."""
    return page.evaluate("""(col) => {
      const i = __winnow.S.columns.findIndex((c) => c.name === col);
      const n = __winnow.S.view.row_count;
      return Array.from({ length: n }, (_, p) => (__winnow.rowAt(p) || { cells: [] }).cells[i]);
    }""", col)


@pytest.fixture
def home(page, api):
    """Where the page was, put back afterwards with every table this test
    imported gone. Tests append (kind, id) to the list it yields."""
    start = page.evaluate("() => __winnow.S.sourceId")
    made: list[tuple[str, int]] = []
    try:
        yield made
    finally:
        for kind, ident in reversed(made):
            api(f"/api/merges/{-ident}" if kind == "merge" else f"/api/source/{ident}", "DELETE")
        page.evaluate("""(home) => (async () => {
          __winnow.S.sourceId = null;
          await __winnow.loadSources();
          await __winnow.openSource(home);
        })()""", start)
        page.wait_for_selector(".row")


def test_show_unparsed_rows_opens_exactly_the_rows_that_failed(page, api, tmp_path, home):
    sid = _ingest(api, _csv(tmp_path / "du_times.csv", ["When", "Line"],
                            [[w, f"line {i}"] for i, w in enumerate(WHEN, 1)]))
    home.append(("source", sid))
    api("/api/derived", "POST", {"source_id": sid, "name": "WhenParsed", "input_column": "When",
                                 "op_id": "iso8601", "params": {}})
    _wait_ready(api, [sid])
    _open(page, sid, "WhenParsed")

    page.evaluate(RECORD_TOASTS)
    label = _menu_item(page, "WhenParsed", "Show ")
    assert label == "Show 2 unparsed rows"
    page.locator(".menu .menu-item", has_text=label).click()
    page.wait_for_function("() => __winnow.S.view.row_count === 2 && __winnow.busyCount === 0")
    assert sorted(_column(page, "When")) == ["n/a", "not a time"]
    # As a filter the analyst can see, edit and clear: a raw fragment in
    # the filter tree, not a hidden state of the table.
    tree = page.evaluate("() => __winnow.S.filterTree")
    assert [c["type"] for c in tree["children"]] == ["raw"]
    _toasted(page, 'Showing rows where "WhenParsed" could not be parsed')


def test_an_extracted_field_offers_the_rows_without_it(page, api, tmp_path, home):
    """For an extracted field the same count means "no such field in the
    document", and the item says so."""
    sid = _ingest(api, _csv(tmp_path / "du_json.csv", ["Payload"], [[json.dumps(p)] for p in PAYLOAD]))
    home.append(("source", sid))
    api("/api/derived", "POST", {"source_id": sid, "name": "User", "input_column": "Payload",
                                 "op_id": "json_field", "params": {"path": "$.user"}})
    _wait_ready(api, [sid])
    _open(page, sid, "User")

    label = _menu_item(page, "User", "Show ")
    assert label == "Show 1 row without this field"
    page.locator(".menu .menu-item", has_text=label).click()
    page.wait_for_function("() => __winnow.S.view.row_count === 1 && __winnow.busyCount === 0")
    assert json.loads(_column(page, "Payload")[0]) == {"host": "SRV3"}


def test_unparsed_rows_on_a_merge_come_from_every_member(page, api, tmp_path, home):
    """Invariant 9. The menu asks with the first member's definition, and
    the fragment it gets back has to find the second member's failures too."""
    a = _ingest(api, _csv(tmp_path / "du_m0.csv", ["When", "Line"], [[WHEN[0], "a1"], [WHEN[1], "a2"]]))
    b = _ingest(api, _csv(tmp_path / "du_m1.csv", ["When", "Line"],
                          [[WHEN[2], "b1"], [WHEN[3], "b2"], [WHEN[4], "b3"]]))
    home.extend([("source", a), ("source", b)])
    mid = api("/api/merges", "POST", {"name": "du merge", "source_ids": [a, b]})["id"]
    home.append(("merge", mid))
    api("/api/derived", "POST", {"source_id": mid, "name": "WhenParsed", "input_column": "When",
                                 "op_id": "iso8601", "params": {}})
    _wait_ready(api, [a, b])
    _open(page, mid, "WhenParsed")

    label = _menu_item(page, "WhenParsed", "Show ")
    assert label == "Show 2 unparsed rows"
    page.locator(".menu .menu-item", has_text=label).click()
    page.wait_for_function("() => __winnow.S.view.row_count === 2 && __winnow.busyCount === 0")
    assert sorted(_column(page, "Line")) == ["a2", "b2"]


def _change_path(page, col, new_path, *, want_current):
    page.locator(".menu .menu-item", has_text="Change the field path").click()
    box = page.locator(".confirm-input")
    box.wait_for()
    assert box.input_value() == want_current
    box.fill(new_path)
    page.locator(".confirm-card .btn", has_text="Recompute").click()


def test_changing_the_field_path_recomputes_the_column_in_place(page, api, tmp_path, home):
    sid = _ingest(api, _csv(tmp_path / "du_path.csv", ["Payload", "Note"],
                            [[json.dumps(p), f"n{i}"] for i, p in enumerate(PAYLOAD)]))
    home.append(("source", sid))
    api("/api/derived", "POST", {"source_id": sid, "name": "Who", "input_column": "Payload",
                                 "op_id": "json_field", "params": {"path": "$.user"}})
    _wait_ready(api, [sid])
    _open(page, sid, "Who")
    order = page.evaluate("() => __winnow.visibleCols()")

    page.evaluate(RECORD_TOASTS)
    _menu_item(page, "Who", "Change the field path")
    _change_path(page, "Who", "$.host", want_current="$.user")
    _toasted(page, 'Recomputing "Who"')
    _wait_ready(api, [sid])
    [d] = _defs(api, sid)
    assert (d["name"], d["params"]) == ("Who", {"path": "$.host"})
    page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_function("""() => {
      const i = __winnow.S.columns.findIndex((c) => c.name === 'Who');
      return i >= 0 && (__winnow.rowAt(0) || { cells: [] }).cells[i] === 'SRV1';
    }""", timeout=15_000)
    assert _column(page, "Who") == ["SRV1", "WKS2", "SRV3", "WKS4"]
    # Same name, same place.
    assert page.evaluate("() => __winnow.visibleCols()") == order


def test_cancelling_or_keeping_the_path_sends_nothing(page, api, tmp_path, home):
    sid = _ingest(api, _csv(tmp_path / "du_keep.csv", ["Payload"], [[json.dumps(p)] for p in PAYLOAD]))
    home.append(("source", sid))
    api("/api/derived", "POST", {"source_id": sid, "name": "Who", "input_column": "Payload",
                                 "op_id": "json_field", "params": {"path": "$.user"}})
    _wait_ready(api, [sid])
    _open(page, sid, "Who")
    sent = []
    page.on("request", lambda r: sent.append(r.url) if "/rederive" in r.url else None)

    _menu_item(page, "Who", "Change the field path")
    page.locator(".menu .menu-item", has_text="Change the field path").click()
    page.locator(".confirm-input").wait_for()
    page.locator(".confirm-card .btn", has_text="Cancel").click()
    page.wait_for_selector(".confirm-overlay", state="detached")

    _menu_item(page, "Who", "Change the field path")
    _change_path(page, "Who", "  $.user  ", want_current="$.user")
    page.wait_for_selector(".confirm-overlay", state="detached")
    # Nothing to wait for: the check is that no request was made.
    page.wait_for_timeout(300)
    assert sent == []
    assert _defs(api, sid)[0]["params"] == {"path": "$.user"}


def test_changing_the_field_path_on_a_merge_changes_every_member(page, api, tmp_path, home):
    a = _ingest(api, _csv(tmp_path / "du_pm0.csv", ["Payload"], [[json.dumps(p)] for p in PAYLOAD[:2]]))
    b = _ingest(api, _csv(tmp_path / "du_pm1.csv", ["Payload"], [[json.dumps(p)] for p in PAYLOAD[2:]]))
    home.extend([("source", a), ("source", b)])
    mid = api("/api/merges", "POST", {"name": "du path merge", "source_ids": [a, b]})["id"]
    home.append(("merge", mid))
    api("/api/derived", "POST", {"source_id": mid, "name": "Who", "input_column": "Payload",
                                 "op_id": "json_field", "params": {"path": "$.user"}})
    _wait_ready(api, [a, b])
    _open(page, mid, "Who")

    page.evaluate(RECORD_TOASTS)
    _menu_item(page, "Who", "Change the field path")
    _change_path(page, "Who", "$.host", want_current="$.user")
    _toasted(page, 'Recomputing "Who"')
    _wait_ready(api, [a, b])
    for sid in (a, b):
        assert [d["params"] for d in _defs(api, sid)] == [{"path": "$.host"}], sid
    page.evaluate("(id) => __winnow.openSource(id)", mid)
    page.wait_for_function("""() => {
      const i = __winnow.S.columns.findIndex((c) => c.name === 'Who');
      const vals = [0, 1, 2, 3].map((p) => (__winnow.rowAt(p) || { cells: [] }).cells[i]);
      return ['SRV1', 'WKS2', 'SRV3', 'WKS4'].every((h) => vals.includes(h));
    }""", timeout=15_000)
