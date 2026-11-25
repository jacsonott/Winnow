"""How much of a second a converted timestamp carries.

The report: converting an epoch to a timestamp lost the resolution past
the second. The stored value never did ("2025-11-25 19:46:40.087000" for
1764100000087), but the grid shows a datetime column in the case's
format, which is "YYYY-MM-DD HH:MM:SS" unless someone changes it, so a
millisecond epoch was shown as "…:40", the same as every other row in
that second. A derived timestamp column now records how much of a second
its values carry (`subsecond`: 0, 3 or 6), and the client widens that
default format to it (tsformat.js's withFraction;
tests/ui/test_epoch_subsecond_ui.py holds the grid).

Asked of the values as stored, never the op's unit: "auto" picks a unit
per value, a seconds column may carry a fraction on some rows only, and
FILETIME writes .000000 for a whole second.

The SQLite import's Chrome-time conversion is the other place a number
becomes a timestamp: it cut WebKit's microseconds to milliseconds, and its
columns record `subsecond` the same way."""

from __future__ import annotations

import datetime
import sqlite3

import pytest

from winnow.store import Store, _subsecond_digits, _webkit_to_iso, WEBKIT_EPOCH_OFFSET_US

# 2025-11-25 19:46:40 UTC.
T0 = 1_764_100_000

ROWS = [
    ["S", "SFrac", "Ms", "Us", "Ns", "Ft", "Msg"],
    # FILETIME of a whole second (.000000) on every row.
    [str(T0), str(T0), f"{T0}000", f"{T0}000000", f"{T0}000000000", "134083736000000000", "a"],
    [str(T0 + 1), f"{T0}.5", f"{T0}087", f"{T0}087123", f"{T0}087123456", "134083736010000000", "b"],
    [str(T0 + 2), f"{T0}.25", f"{T0}412", f"{T0}412000", f"{T0}412000999", "134083736020000000", "c"],
    ["", "", "", "", "", "", "d"],
]


def _ingest(store, write_csv, rows=ROWS, name="epochs.csv"):
    return store.ingest_csv(write_csv(rows, name=name), build_fts=False)["id"]


def _add(store, sid, name, column, op_id="unix_epoch", params=None):
    res = store.add_derived_column(sid, name, column, op_id, params or {})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    return res["definition"]["id"]


def _col(store, sid, name):
    return next(c for c in store.get_source(sid)["columns"] if c["name"] == name)


def _values(store, sid, name, spec=None):
    view = store.build_view(sid, spec or {})
    cols = [c["name"] for c in store.get_source(sid)["columns"]]
    rows = store.fetch_rows(view["view_id"], 0, 100)["rows"]
    return [dict(zip(cols, r["cells"]))[name] for r in rows]


@pytest.mark.parametrize("column,digits", [
    ("S", 0),       # whole seconds: nothing to show
    ("SFrac", 3),   # 1764100000.5 and .25 are .500000 and .250000
    ("Ms", 3),
    ("Us", 6),
    ("Ns", 6),      # kept to the microsecond, which is what the shape holds
])
def test_a_unix_epoch_column_records_the_fraction_it_carries(store, write_csv, column, digits):
    sid = _ingest(store, write_csv)
    def_id = _add(store, sid, "T", column)
    assert store.get_derived_column(def_id)["subsecond"] == digits
    assert _col(store, sid, "T")["subsecond"] == digits


def test_the_stored_value_keeps_the_fraction(store, write_csv):
    """What the fix rests on: every digit the epoch had, to the
    microsecond, is in the stored value; nothing was rounded or cut."""
    sid = _ingest(store, write_csv)
    for c in ("Ms", "Us", "Ns"):
        _add(store, sid, f"T{c}", c)
    assert _values(store, sid, "TMs")[:3] == [
        "2025-11-25 19:46:40.000000", "2025-11-25 19:46:40.087000", "2025-11-25 19:46:40.412000"]
    assert _values(store, sid, "TUs")[1] == "2025-11-25 19:46:40.087123"
    assert _values(store, sid, "TNs")[1] == "2025-11-25 19:46:40.087123"


def test_rows_in_one_second_sort_by_their_fraction(store, write_csv):
    """Sorting on the converted column orders the rows that share a second
    by their milliseconds, both ways, whatever the file order."""
    rows = [ROWS[0], *reversed(ROWS[1:4])]
    sid = _ingest(store, write_csv, rows)
    _add(store, sid, "When", "Ms")
    want = ["2025-11-25 19:46:40.000000", "2025-11-25 19:46:40.087000", "2025-11-25 19:46:40.412000"]
    assert _values(store, sid, "When", {"sort": [{"column": "When", "dir": "asc"}]}) == want
    assert _values(store, sid, "When", {"sort": [{"column": "When", "dir": "desc"}]}) == want[::-1]


def test_a_filetime_column_of_whole_seconds_carries_none(store, write_csv):
    """FILETIME writes .000000 for a whole second: that is no fraction."""
    sid = _ingest(store, write_csv)
    def_id = _add(store, sid, "Ft time", "Ft", "windows_filetime")
    assert _values(store, sid, "Ft time")[0].endswith(".000000")
    assert store.get_derived_column(def_id)["subsecond"] == 0


def test_a_text_op_records_nothing(store, write_csv):
    sid = _ingest(store, write_csv)
    def_id = _add(store, sid, "Digits", "Msg", "regex_extract", {"pattern": "(.)"})
    assert store.get_derived_column(def_id)["subsecond"] is None
    assert _col(store, sid, "Digits")["subsecond"] is None


def test_a_rederive_records_the_new_values(store, write_csv):
    """Read off what the pass wrote, so a re-derive under other params says
    what the column holds now."""
    sid = _ingest(store, write_csv)
    def_id = _add(store, sid, "T", "Us", params={"unit": "us"})
    assert store.get_derived_column(def_id)["subsecond"] == 6
    res = store.rederive_column(def_id, {"unit": "ms"})
    for job in res.get("job_ids") or [res["job_id"]]:
        store.wait_for_ingest_job(job, timeout=30)
    # Microsecond values read as milliseconds land in no plausible year:
    # every value fails, so there is no fraction left to show.
    assert store.get_derived_column(def_id)["subsecond"] == 0


def test_a_merge_shows_as_much_as_any_member_carries(store, write_csv):
    """One member's times are whole seconds, the other's milliseconds: the
    merge's column is not cut to the second because the first member's
    happen to be."""
    a = _ingest(store, write_csv, [["Epoch", "Msg"], [str(T0), "a"]], name="a.csv")
    b = _ingest(store, write_csv, [["Epoch", "Msg"], [f"{T0}087", "b"]], name="b.csv")
    mid = store.create_merge("both", [a, b])["id"]   # negative, as a merge's source id is
    res = store.add_derived_column(mid, "When", "Epoch", "unix_epoch", {})
    for job in res.get("job_ids") or [res["job_id"]]:
        store.wait_for_ingest_job(job, timeout=30)
    assert _col(store, a, "When")["subsecond"] == 0
    assert _col(store, b, "When")["subsecond"] == 3
    assert _col(store, mid, "When")["subsecond"] == 3


def test_a_case_from_before_is_looked_at_when_it_opens(case_path, write_csv):
    """A column made before this was recorded has subsecond NULL; the
    open-time pass reads it off the values, as it types text columns."""
    s = Store(case_path)
    try:
        sid = _ingest(s, write_csv)
        def_id = _add(s, sid, "T", "Ms")
        _add(s, sid, "Grab", "Msg", "regex_extract", {"pattern": "(.)"})
        with s.lock, s.db:
            s.db.execute("UPDATE derived_columns SET subsecond=NULL")
    finally:
        s.close()
    s = Store(case_path)
    try:
        s.wait_for_derived_types(timeout=30)
        assert s.get_derived_column(def_id)["subsecond"] == 3
        assert _col(s, sid, "Grab")["subsecond"] is None
    finally:
        s.close()


def test_a_case_from_before_the_column_existed_opens_and_is_looked_at(case_path, write_csv):
    """The migration: a derived_columns table without the column gets it,
    and its timestamp columns are looked at."""
    s = Store(case_path)
    try:
        sid = _ingest(s, write_csv)
        def_id = _add(s, sid, "T", "Us")
    finally:
        s.close()
    # The table as such a case has it, comment and all: rebuilt rather
    # than DROP COLUMN, which some SQLite builds refuse on this schema's
    # trailing comment ("incomplete input"), and which isn't how a case
    # from before came to lack the column anyway.
    db = sqlite3.connect(case_path)
    try:
        db.executescript("""
            CREATE TABLE derived_columns_old (
                id             INTEGER PRIMARY KEY,
                source_id      INTEGER NOT NULL,
                name           TEXT NOT NULL,
                input_column   TEXT NOT NULL,
                op_id          TEXT NOT NULL,
                params         TEXT NOT NULL DEFAULT '{}',
                status         TEXT NOT NULL DEFAULT 'building',
                parse_failures INTEGER,
                created_at     TEXT,
                inferred_type  TEXT                -- number | datetime | text | empty
            );
            INSERT INTO derived_columns_old
                SELECT id, source_id, name, input_column, op_id, params, status,
                       parse_failures, created_at, inferred_type FROM derived_columns;
            DROP TABLE derived_columns;
            ALTER TABLE derived_columns_old RENAME TO derived_columns;
        """)
    finally:
        db.close()
    s = Store(case_path)
    try:
        s.wait_for_derived_types(timeout=30)
        assert s.get_derived_column(def_id)["subsecond"] == 6
    finally:
        s.close()


def test_a_copy_to_another_case_keeps_it(store, write_csv, tmp_path):
    sid = _ingest(store, write_csv)
    _add(store, sid, "T", "Ms")
    target = str(tmp_path / "other.db-winnow")
    Store(target).close()   # an existing case is what a copy goes into
    out = store.copy_sources_to(target, [sid])
    new_id = out["copied"][0]["id"]
    t = Store(target)
    try:
        assert _col(t, new_id, "T")["subsecond"] == 3
    finally:
        t.close()


# ------------------------------------------------------------ Chrome time

def _webkit(dt: datetime.datetime, extra_us: int = 0) -> int:
    epoch_1601 = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)
    return (dt - epoch_1601) // datetime.timedelta(microseconds=1) + extra_us


def test_chrome_time_keeps_its_microseconds():
    """Cut to the millisecond before: "…00.123Z" for .123457."""
    assert _webkit_to_iso(13348668000123457) == "2024-01-02T11:20:00.123457Z"
    assert _webkit_to_iso(13348668000000000) == "2024-01-02T11:20:00.000000Z"
    # Integer arithmetic: the last digit of a 17-digit value survives.
    assert _webkit_to_iso(13348668000999999) == "2024-01-02T11:20:00.999999Z"
    assert _webkit_to_iso(WEBKIT_EPOCH_OFFSET_US + 1) is None   # 1970: not plausible


@pytest.mark.parametrize("iso,digits", [
    ("2024-01-02T11:20:00.123457Z", 6), ("2024-01-02T11:20:00.123000Z", 3),
    ("2024-01-02T11:20:00.000000Z", 0), ("2024-01-02 11:20:00", 0),
    ("2024-01-02 11:20:00.5", 3), ("2024-01-02 11:20:00.0000001", 6),
])
def test_subsecond_digits(iso, digits):
    assert _subsecond_digits(iso) == digits


@pytest.mark.parametrize("extra,digits", [(0, 0), (87_000, 3), (87_123, 6)])
def test_a_converted_chrome_column_records_its_fraction(store, tmp_path, extra, digits):
    path = str(tmp_path / "History")
    db = sqlite3.connect(path)
    base = datetime.datetime(2025, 11, 25, 19, 46, 40, tzinfo=datetime.timezone.utc)
    db.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, visit_time INTEGER)")
    db.executemany("INSERT INTO visits VALUES (?, ?)",
                   [(1, _webkit(base)), (2, _webkit(base, extra)), (3, _webkit(base, 1_000_000))])
    db.commit()
    db.close()
    rec = store.ingest_sqlite_table(path, "visits", build_fts=False, timestamp_columns=["visit_time"])
    col = next(c for c in rec["columns"] if c["name"] == "visit_time")
    assert col["type"] == "datetime"
    assert col.get("subsecond", 0) == digits
    view = store.build_view(rec["id"], {})
    rows = store.fetch_rows(view["view_id"], 0, 10)["rows"]
    stamps = [r["cells"][1] for r in rows]
    assert stamps[1] == f"2025-11-25T19:46:40.{extra:06d}Z"
    # A column the analyst did not convert records nothing.
    assert "subsecond" not in next(c for c in rec["columns"] if c["name"] == "id")
