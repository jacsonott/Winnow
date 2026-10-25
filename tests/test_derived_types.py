"""What type a derived column is, and so how it sorts, compares and groups.

A timestamp parse is a datetime column and a duration is a number column
whatever they hold — the op says so. A text-valued op (regex capture, a
JSON or XML field, a lookup, a decode) can't: the same regex capture is a
host name in one column and a byte count in the next. Those are typed by
their values, by the rule an imported column is typed by (infer_type):
every non-empty value a number makes a number column. Before that they
were all text, so sizes and counts pulled out with a regex sorted 10
before 9 and `> 1000` compared strings.
"""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from winnow.store import IngestCancelled, Store

# The reported case: sizes pulled out of a log line with a regex.
LOG_ROWS = [
    ["Time", "Line"],
    ["2024-01-05 10:00:00", "GET /a.zip bytes=9 status=200"],
    ["2024-01-05 10:01:00", "GET /b.zip bytes=100 status=200"],
    ["2024-01-05 10:02:00", "GET /c.zip bytes=10 status=404"],
    ["2024-01-05 10:03:00", "keepalive"],                         # no capture -> NULL
    ["2024-01-05 10:04:00", "GET /d.zip bytes=2048 status=200"],
    ["2024-01-05 10:05:00", "GET /e.zip bytes=-1.5 status=500"],  # still a number
]
BYTES = {"pattern": r"bytes=(\S+)"}


def _ingest(store, write_csv, rows, name="log.csv"):
    return store.ingest_csv(write_csv(rows, name), name=name, build_fts=False)["id"]


def _derive(store, sid, name, column, op_id, params):
    res = store.add_derived_column(sid, name, column, op_id, params)
    for jid in res.get("job_ids") or [res["job_id"]]:
        assert store.wait_for_ingest_job(jid, timeout=30)["status"] == "done"
    return res["definition"]["id"]


def _col(store, sid, name):
    return next(c for c in store.get_source(sid)["columns"] if c["name"] == name)


def _sorted(store, sid, name, direction="asc", filters=()):
    cols = [c["name"] for c in store.get_source(sid)["columns"]]
    v = store.build_view(sid, {"source_id": sid, "filters": list(filters),
                               "sort": [{"column": name, "dir": direction}]})
    return [r["cells"][cols.index(name)] for r in store.fetch_rows(v["view_id"], 0, 100)["rows"]]


# ------------------------------------------------------------- the report

def test_numbers_captured_with_a_regex_are_a_number_column(store, write_csv):
    sid = _ingest(store, write_csv, LOG_ROWS)
    _derive(store, sid, "Bytes", "Line", "regex_extract", BYTES)
    col = _col(store, sid, "Bytes")
    assert col["type"] == "number"
    assert col["derived_kind"] == "text"   # still an extracted column for the menus
    # Sorted as numbers: 9 before 10. As text it was 10, 100, 2048, 9.
    # (The keepalive row has no capture; NULL sorts at the low end.)
    assert _sorted(store, sid, "Bytes") == [None, "-1.5", "9", "10", "100", "2048"]
    assert _sorted(store, sid, "Bytes", "desc")[:2] == ["2048", "100"]


def test_comparisons_on_it_are_numeric(store, write_csv):
    sid = _ingest(store, write_csv, LOG_ROWS)
    _derive(store, sid, "Bytes", "Line", "regex_extract", BYTES)
    # As text, "9" > "1000" — the 9-byte row came back in a "> 1000" filter.
    big = _sorted(store, sid, "Bytes", filters=[{"column": "Bytes", "op": ">", "value": "50"}])
    assert big == ["100", "2048"]


def test_grouping_and_the_value_picker_order_it_numerically(store, write_csv):
    sid = _ingest(store, write_csv, LOG_ROWS)
    _derive(store, sid, "Bytes", "Line", "regex_extract", BYTES)
    v = store.build_view(sid, {"source_id": sid, "filters": [], "sort": []})
    groups = store.group_summary(v["view_id"], "Bytes", order="value")["groups"]
    assert [g["value"] for g in groups if g["value"] is not None] == ["-1.5", "9", "10", "100", "2048"]


def test_the_api_reports_it_as_a_number(client, store, write_csv):
    """What the table menu's type label and the grid's alignment read."""
    sid = _ingest(store, write_csv, LOG_ROWS)
    _derive(store, sid, "Bytes", "Line", "regex_extract", BYTES)
    listed = next(s for s in client.get("/api/sources").json() if s["id"] == sid)
    assert next(c for c in listed["columns"] if c["name"] == "Bytes")["type"] == "number"


# ------------------------------------------------------- what stays text

def test_one_value_that_is_not_a_number_keeps_it_text(store, write_csv):
    sid = _ingest(store, write_csv, LOG_ROWS)
    _derive(store, sid, "Status", "Line", "regex_extract", {"pattern": r"(bytes=\S+|keepalive)"})
    assert _col(store, sid, "Status")["type"] == "text"


@pytest.mark.parametrize("value", ["1,024", "0x1F", "12 ", "+5", "1.", ".5", "1e3"])
def test_what_an_import_would_not_call_a_number_isnt_one_here(store, write_csv, value):
    """The same rule as infer_type, which is also what _numeric_expr sorts
    by — a looser one would type a column whose values then sort as NULL."""
    rows = [["Line"], ["n=7"], [f"n={value}"]]
    sid = _ingest(store, write_csv, rows, "odd.csv")
    _derive(store, sid, "N", "Line", "regex_extract", {"pattern": r"n=(.*)"})
    assert _col(store, sid, "N")["type"] == "text"


def test_a_column_with_nothing_in_it_is_text(store, write_csv):
    sid = _ingest(store, write_csv, LOG_ROWS)
    _derive(store, sid, "Never", "Line", "regex_extract", {"pattern": r"pid=(\d+)"})
    col = _col(store, sid, "Never")
    assert (col["type"], col["inferred_type"]) == ("text", "empty")


def test_a_host_name_capture_is_still_text(store, write_csv):
    rows = [["Uri"], ["https://c2.evil.example/x"], ["https://10.0.0.5/y"]]
    sid = _ingest(store, write_csv, rows, "uri.csv")
    _derive(store, sid, "Host", "Uri", "regex_extract", {"pattern": r"//([^/]+)"})
    assert _col(store, sid, "Host")["type"] == "text"


# ------------------------------------------------- the other derive types

def test_a_json_field_of_numbers_is_a_number_column(store, write_csv):
    rows = [["Payload"]] + [[json.dumps({"size": n})] for n in (9, 100, 10)]
    sid = _ingest(store, write_csv, rows, "j.csv")
    _derive(store, sid, "Size", "Payload", "json_field", {"path": "$.size"})
    assert _col(store, sid, "Size")["type"] == "number"
    assert _sorted(store, sid, "Size") == ["9", "10", "100"]


def test_an_xml_event_id_is_a_number_column(store, write_csv):
    evt = "<Event><System><EventID>{}</EventID></System></Event>"
    rows = [["Payload"]] + [[evt.format(n)] for n in (4688, 4624, 99)]
    sid = _ingest(store, write_csv, rows, "x.csv")
    _derive(store, sid, "EventID", "Payload", "xml_field", {"path": "Event/System/EventID"})
    assert _sorted(store, sid, "EventID") == ["99", "4624", "4688"]


def test_ops_with_a_type_of_their_own_keep_it(store, write_csv):
    rows = [["Start", "End", "Epoch"],
            ["2024-01-05 10:00:00", "2024-01-05 10:00:09", "1704448800"],
            ["2024-01-05 10:00:00", "2024-01-05 10:01:40", "1704448900"]]
    sid = _ingest(store, write_csv, rows, "t.csv")
    _derive(store, sid, "Took", "End", "duration_delta", {"other_column": "Start"})
    _derive(store, sid, "When", "Epoch", "unix_epoch", {})
    took, when = _col(store, sid, "Took"), _col(store, sid, "When")
    assert (took["type"], took["inferred_type"]) == ("number", None)
    assert (when["type"], when["inferred_type"]) == ("datetime", None)


# ------------------------------------------------------- when it changes

def test_a_rederive_retypes_the_column(store, write_csv):
    sid = _ingest(store, write_csv, LOG_ROWS)
    def_id = _derive(store, sid, "Part", "Line", "regex_extract", BYTES)
    assert _col(store, sid, "Part")["type"] == "number"
    res = store.rederive_column(def_id, {"pattern": r"GET (\S+)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert _col(store, sid, "Part")["type"] == "text"
    res = store.rederive_column(def_id, {"pattern": r"status=(\d+)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert _col(store, sid, "Part")["type"] == "number"


def test_a_cancelled_pass_is_typed_by_what_it_left(store, write_csv):
    """Cancelled, a column holds part of each pass, so its type is looked
    at again rather than kept from the pass before."""
    sid = _ingest(store, write_csv, LOG_ROWS)
    def_id = _derive(store, sid, "Bytes", "Line", "regex_extract", BYTES)
    with pytest.raises(IngestCancelled):
        store.backfill_derived_column(def_id, cancel=lambda: True, drop_on_cancel=False)
    col = _col(store, sid, "Bytes")
    assert (col["derived_status"], col["type"]) == ("partial", "number")


def _older_case(path, write_csv):
    """A closed case as an older Winnow left it: two regex columns, a number
    and a host name, and no inferred_type column at all."""
    s = Store(path)
    sid = _ingest(s, write_csv, LOG_ROWS)
    _derive(s, sid, "Bytes", "Line", "regex_extract", BYTES)
    _derive(s, sid, "Host", "Line", "regex_extract", {"pattern": r"GET (\S+)"})
    s.close()
    with sqlite3.connect(path) as raw:
        raw.execute("ALTER TABLE derived_columns DROP COLUMN inferred_type")
    return sid


def _held_typing(monkeypatch):
    """Typing that doesn't finish until it is let go — a scan the size of a
    real case's (seconds to minutes), as a Store() that waits for it never
    coming back rather than coming back a little late."""
    release = threading.Event()
    real = Store._infer_derived_type

    def held(self, d, conn=None):
        release.wait(30)
        return real(self, d, conn)

    monkeypatch.setattr(Store, "_infer_derived_type", held)
    return release


def test_a_case_from_before_this_is_typed_once_it_opens(case_path, write_csv):
    sid = _older_case(case_path, write_csv)
    s = Store(case_path)
    try:
        s.wait_for_derived_types(10)
        assert _col(s, sid, "Bytes")["type"] == "number"
        assert _col(s, sid, "Host")["type"] == "text"
        assert _sorted(s, sid, "Bytes")[1:4] == ["-1.5", "9", "10"]
    finally:
        s.close()


def test_opening_it_does_not_wait_for_the_typing(case_path, write_csv, monkeypatch):
    # The regression: typing ran inside Store(), so a case with a few
    # million rows in its derived columns took seconds to minutes to open —
    # the home screen's Open did nothing visible for that long.
    sid = _older_case(case_path, write_csv)
    release = _held_typing(monkeypatch)
    opened: list[Store] = []
    t = threading.Thread(target=lambda: opened.append(Store(case_path)), daemon=True)
    t.start()
    t.join(10)
    try:
        assert opened, "opening the case waited for its derived columns to be typed"
        s = opened[0]
        # Until the answer lands the column reads as the op's own type, the
        # way every derived column read before they were typed at all, and
        # the table pages and sorts meanwhile.
        assert _col(s, sid, "Bytes")["type"] == "text"
        assert len(_sorted(s, sid, "Bytes")) == len(LOG_ROWS) - 1
        release.set()
        s.wait_for_derived_types(10)
        assert _col(s, sid, "Bytes")["type"] == "number"
    finally:
        release.set()
        t.join(10)
        for s in opened:
            s.close()


def test_the_home_screens_open_does_not_wait_for_it(client, tmp_path, write_csv, monkeypatch):
    # What Open on a home-screen case card posts. It runs Store() under the
    # case-switch lock, so a wait there held every other open behind it too.
    path = str(tmp_path / "older.db-winnow")
    sid = _older_case(path, write_csv)
    release = _held_typing(monkeypatch)
    answers = []
    t = threading.Thread(target=lambda: answers.append(
        client.post("/api/case/open", json={"path": path})), daemon=True)
    t.start()
    t.join(10)
    try:
        assert answers, "/api/case/open waited for the case's derived columns to be typed"
        assert answers[0].status_code == 200, answers[0].text
        assert [x["id"] for x in answers[0].json()["sources"]] == [sid]
    finally:
        release.set()
        t.join(10)


def test_closing_the_case_stops_the_scan(case_path, write_csv, monkeypatch):
    sid = _older_case(case_path, write_csv)
    # Enough values that the scan runs well past a progress tick.
    with sqlite3.connect(case_path) as raw:
        raw.execute(
            f"WITH RECURSIVE n(x) AS (SELECT 1000 UNION ALL SELECT x + 1 FROM n WHERE x < 300000) "
            f"INSERT INTO drv_{sid}(rid, \"Bytes\") SELECT x, '5' FROM n")
    raised = []
    real = Store._infer_derived_type

    def closing_meanwhile(self, d, conn=None):
        self._closing = True   # the first thing close() does
        try:
            return real(self, d, conn)
        except sqlite3.Error as e:
            raised.append(e)
            raise

    monkeypatch.setattr(Store, "_infer_derived_type", closing_meanwhile)
    s = Store(case_path)
    s.wait_for_derived_types(10)
    s.close()
    # Interrupted rather than waited out: close() joins this thread, and a
    # full scan of a big column is seconds.
    assert raised and "interrupt" in str(raised[0])
    with sqlite3.connect(case_path) as raw:
        assert raw.execute("SELECT inferred_type FROM derived_columns").fetchall() == [(None,), (None,)]
    # ...and left for the next open.
    monkeypatch.undo()
    s = Store(case_path)
    try:
        s.wait_for_derived_types(10)
        assert _col(s, sid, "Bytes")["type"] == "number"
    finally:
        s.close()


def test_save_as_table_keeps_it_a_number(store, write_csv):
    sid = _ingest(store, write_csv, LOG_ROWS)
    _derive(store, sid, "Bytes", "Line", "regex_extract", BYTES)
    v = store.build_view(sid, {"source_id": sid, "filters": [], "sort": []})
    new = store.save_view_as_source(v["view_id"], "copy")["source"]["id"]
    assert _col(store, new, "Bytes")["type"] == "number"


def test_copying_the_table_to_another_case_keeps_it_a_number(store, write_csv, tmp_path):
    sid = _ingest(store, write_csv, LOG_ROWS)
    _derive(store, sid, "Bytes", "Line", "regex_extract", BYTES)
    other = str(tmp_path / "other.db")
    Store(other).close()
    (copied,) = store.copy_sources_to(other, [sid])["copied"]
    t = Store(other)
    try:
        assert _col(t, copied["id"], "Bytes")["type"] == "number"
    finally:
        t.close()


# ------------------------------------------------------------------ merges

A_ROWS = [["Line"], ["bytes=9"], ["bytes=100"]]
B_ROWS = [["Line"], ["bytes=10"], ["bytes=2048"]]
QUIET_ROWS = [["Line"], ["keepalive"]]
TEXT_ROWS = [["Line"], ["bytes=lots"]]


def _merge(store, write_csv, *row_sets):
    ids = [_ingest(store, write_csv, rows, f"m{i}.csv") for i, rows in enumerate(row_sets)]
    return store.create_merge("merged", ids)["id"]


def test_a_merge_of_number_columns_sorts_as_numbers(store, write_csv):
    mid = _merge(store, write_csv, A_ROWS, B_ROWS)
    _derive(store, mid, "Bytes", "Line", "regex_extract", BYTES)
    assert _col(store, mid, "Bytes")["type"] == "number"
    assert _sorted(store, mid, "Bytes") == ["9", "10", "100", "2048"]


def test_a_member_with_nothing_in_the_column_doesnt_make_the_merge_text(store, write_csv):
    """A host whose log never has the field: its copy of the column is
    empty, which says nothing about what type the merge's column is."""
    mid = _merge(store, write_csv, QUIET_ROWS, A_ROWS)
    _derive(store, mid, "Bytes", "Line", "regex_extract", BYTES)
    assert _col(store, mid, "Bytes")["type"] == "number"
    assert _sorted(store, mid, "Bytes")[-2:] == ["9", "100"]   # NULL sorts first


def test_a_member_holding_text_makes_the_merge_text(store, write_csv):
    mid = _merge(store, write_csv, A_ROWS, TEXT_ROWS)
    _derive(store, mid, "Bytes", "Line", "regex_extract", BYTES)
    assert _col(store, mid, "Bytes")["type"] == "text"
