"""First non-empty value over timestamp columns that store their times in
different shapes.

The report: two tables merged, a coalesce of a timestamp column from each,
and both columns showed in the grid in the same format, but the coalesced
column came out as text holding each table's times as stored, two shapes
side by side. A datetime column shows every value in the case's format and
sorts them through TS_NORMALIZE, so its stored shape never shows. A derived
column copied that shape, and a derived column is only a datetime column
when every value is already canonical (_infer_derived_type). So the
coalesce was a datetime column only when every table happened to store
"YYYY-MM-DD HH:MM:SS".

When every column it reads is a datetime column, coalesce now writes each
time canonical (timeparse.canonical_timestamp), so the column is a
datetime column whatever the shapes. A plain merge of same-named timestamp
columns in two shapes was already a datetime column, and is pinned here
too."""

from __future__ import annotations

import pytest

from winnow import timeparse
from winnow.store import _sql_is_canonical_ts

EVTX = [["TimeCreated", "Computer"],
        ["2024-01-01 10:00:00.1234567", "SRV1"],
        ["2024-01-03 08:00:00.0000000", "SRV1"]]
# ISO with a T and a Z, the way Velociraptor and Chainsaw write it.
ISO = [["Timestamp", "Message"],
       ["2024-01-02T09:00:00Z", "a"],
       ["2024-01-04T12:30:00.5Z", "b"]]
# Month first with slashes and a 12-hour clock, as Timeline Explorer
# exports it: as text, 12/31/2023 sorts after every 2024 ISO stamp.
US = [["Timestamp", "Message"],
      ["12/31/2023 11:15:00 PM", "c"],
      ["1/5/2024 1:00:00 PM", "d"]]
CO = {"extra_columns": ["Timestamp"]}


def _wait(store, res):
    for jid in res.get("job_ids") or [res["job_id"]]:
        store.wait_for_ingest_job(jid, timeout=30)


def _col(store, sid, name):
    return next(c for c in store.get_source(sid)["columns"] if c["name"] == name)


def _cells(store, sid, column, **spec):
    spec.setdefault("filters", [])
    spec.setdefault("sort", [])
    v = store.build_view(sid, {"source_id": sid, **spec})
    names = [c["name"] for c in store.get_source(sid)["columns"]]
    return [r["cells"][names.index(column)] for r in store.fetch_rows(v["view_id"], 0, 100)["rows"]]


@pytest.fixture
def tables(store, write_csv):
    e = store.ingest_csv(write_csv(EVTX, "evtx.csv"), name="evtx", build_fts=False)["id"]
    i = store.ingest_csv(write_csv(ISO, "iso.csv"), name="iso", build_fts=False)["id"]
    u = store.ingest_csv(write_csv(US, "us.csv"), name="us", build_fts=False)["id"]
    for sid in (e, i, u):
        assert next(c for c in store.get_source(sid)["columns"] if c["name"] in ("TimeCreated", "Timestamp"))[
            "type"] == "datetime"
    return e, i, u


# ------------------------------------------------------------- the report

def test_coalesce_on_a_merge_of_three_timestamp_shapes_is_a_datetime_column(store, tables):
    e, i, u = tables
    mid = store.create_merge("all three", [e, i, u])["id"]
    # The preview shows what the column will hold: one shape, each
    # table's time read the way the grid shows it.
    pre = store.preview_derived(mid, "TimeCreated", "coalesce", CO)
    assert sorted(p["output"] for p in pre["preview"]) == [
        "2023-12-31 23:15:00", "2024-01-01 10:00:00.1234567", "2024-01-02 09:00:00",
        "2024-01-03 08:00:00.0000000", "2024-01-04 12:30:00.5", "2024-01-05 13:00:00"]
    assert pre["failures"] == 0

    res = store.add_derived_column(mid, "When", "TimeCreated", "coalesce", CO)
    _wait(store, res)
    when = _col(store, mid, "When")
    assert (when["type"], when["derived_status"], when["parse_failures"]) == ("datetime", "ready", 0)
    for sid in (e, i, u):
        assert _col(store, sid, "When")["type"] == "datetime"

    # Sorted by time across all three tables: as text, the US stamp from
    # 2023 would come last.
    assert _cells(store, mid, "When", sort=[{"column": "When", "dir": "asc"}]) == [
        "2023-12-31 23:15:00", "2024-01-01 10:00:00.1234567", "2024-01-02 09:00:00",
        "2024-01-03 08:00:00.0000000", "2024-01-04 12:30:00.5", "2024-01-05 13:00:00"]
    tr = {"enabled": True, "column": "When", "start": "2024-01-02 00:00:00", "end": "2024-01-05 12:00:00"}
    assert sorted(_cells(store, mid, "When", time_range=tr)) == [
        "2024-01-02 09:00:00", "2024-01-03 08:00:00.0000000", "2024-01-04 12:30:00.5"]
    v = store.build_view(mid, {"source_id": mid, "filters": [], "sort": []})
    assert sum(n for _, n in store.time_histogram(v["view_id"], "When")["buckets"]) == 6


def test_merge_preview_reads_every_table_with_its_own_input_types(store, tables):
    """The preview judges each table on the columns it has, as its
    backfill does: a table with only TimeCreated and one with only
    Timestamp are both all-timestamp, so both answer canonical."""
    e, i, _ = tables
    mid = store.create_merge("evtx+iso", [e, i])["id"]
    pre = store.preview_derived(mid, "TimeCreated", "coalesce", CO)
    assert [p["output"] for p in pre["preview"]] == [
        "2024-01-01 10:00:00.1234567", "2024-01-03 08:00:00.0000000",
        "2024-01-02 09:00:00", "2024-01-04 12:30:00.5"]


def test_one_table_two_timestamp_columns_in_different_shapes(store, write_csv):
    sid = store.ingest_csv(write_csv([["A", "B"], ["2024-01-02T09:00:00+02:00", ""],
                                      ["", "01/05/2024 1:00:00 PM"], ["2024-01-03 10:00:00", "1/1/2024 9:00:00"]],
                                     "two.csv"), name="two", build_fts=False)["id"]
    assert {c["name"]: c["type"] for c in store.get_source(sid)["columns"]} == {"A": "datetime", "B": "datetime"}
    pre = store.preview_derived(sid, "A", "coalesce", {"extra_columns": ["B"]})
    # An offset is applied, as Parse a timestamp applies one.
    assert [p["output"] for p in pre["preview"]] == [
        "2024-01-02 07:00:00", "2024-01-05 13:00:00", "2024-01-03 10:00:00"]
    _wait(store, store.add_derived_column(sid, "AB", "A", "coalesce", {"extra_columns": ["B"]}))
    assert _col(store, sid, "AB")["type"] == "datetime"
    assert _cells(store, sid, "AB") == ["2024-01-02 07:00:00", "2024-01-05 13:00:00", "2024-01-03 10:00:00"]


def test_a_value_that_is_not_a_time_is_passed_over_and_counted(store, write_csv):
    """A column is typed datetime when 80% of its values look like times,
    so the rest may not be. Passed over like a blank: the next column's
    time wins, and a row with no time anywhere is unparsed, as it would be
    under Parse a timestamp, rather than putting text in a datetime column."""
    rows = [["A", "B"]] + [[f"2024-01-0{n} 10:00:00", ""] for n in range(1, 9)] + [
        ["-", "2024-02-01T00:00:00Z"], ["not a time", ""]]
    sid = store.ingest_csv(write_csv(rows, "gaps.csv"), name="gaps", build_fts=False)["id"]
    assert {c["name"]: c["type"] for c in store.get_source(sid)["columns"]} == {"A": "datetime", "B": "datetime"}
    _wait(store, store.add_derived_column(sid, "AB", "A", "coalesce", {"extra_columns": ["B"]}))
    ab = _col(store, sid, "AB")
    assert (ab["type"], ab["parse_failures"]) == ("datetime", 1)
    assert _cells(store, sid, "AB")[-2:] == ["2024-02-01 00:00:00", None]


def test_a_text_column_among_them_still_copies(store, write_csv):
    """Only when every column is a timestamp column: a coalesce of a time
    and a host name is a copy of each, as it always was."""
    sid = store.ingest_csv(write_csv([["When", "Host"], ["2024-01-02T09:00:00Z", "WS01"], ["", "WS02"]],
                                     "mixed.csv"), name="mixed", build_fts=False)["id"]
    _wait(store, store.add_derived_column(sid, "Either", "When", "coalesce", {"extra_columns": ["Host"]}))
    assert _cells(store, sid, "Either") == ["2024-01-02T09:00:00Z", "WS02"]
    assert _col(store, sid, "Either")["type"] == "text"


def test_a_derived_timestamp_column_counts_as_one(store, write_csv):
    """A column made by Parse a timestamp is a datetime column like an
    imported one, so it combines with one."""
    sid = store.ingest_csv(write_csv([["Epoch", "Seen"], ["1704099600", ""], ["", "1/5/2024 1:00:00 PM"]],
                                     "epoch.csv"), name="epoch", build_fts=False)["id"]
    _wait(store, store.add_derived_column(sid, "EpochTime", "Epoch", "unix_epoch", {}))
    _wait(store, store.add_derived_column(sid, "When", "EpochTime", "coalesce", {"extra_columns": ["Seen"]}))
    assert _col(store, sid, "When")["type"] == "datetime"
    assert _cells(store, sid, "When") == ["2024-01-01 09:00:00", "2024-01-05 13:00:00"]


def test_rederive_turns_an_old_text_coalesce_into_a_datetime_one(store, tables):
    """A column made before this holds each table's text as stored. A
    re-derive writes it again, canonical."""
    e, i, _ = tables
    mid = store.create_merge("evtx+iso", [e, i])["id"]
    _wait(store, store.add_derived_column(mid, "When", "TimeCreated", "coalesce", CO))
    defs = [d for d in store.list_derived_columns(e) + store.list_derived_columns(i) if d["name"] == "When"]
    # What the column held before: the stored text, typed text.
    with store.lock, store.db:
        for d in defs:
            drv = store._derived_table(d["source_id"])
            store.db.execute(f'UPDATE "{drv}" SET "When" = \'2024-01-02T09:00:00Z\' WHERE "When" LIKE \'2024-01-02%\'')
            store.db.execute("UPDATE derived_columns SET inferred_type='text' WHERE id=?", (d["id"],))
    assert _col(store, mid, "When")["type"] == "text"
    _wait(store, store.rederive_column(defs[0]["id"], CO, source_id=mid))
    assert _col(store, mid, "When")["type"] == "datetime"
    assert "2024-01-02 09:00:00" in _cells(store, mid, "When")


def test_plain_merge_of_same_named_timestamp_columns_in_two_shapes(store, tables):
    """No derived column at all: the merge's Timestamp is each table's
    own, already a datetime column on both, so it stays one and sorts by
    time; the grid shows both shapes in the case's format."""
    _, i, u = tables
    mid = store.create_merge("iso+us", [i, u])["id"]
    assert _col(store, mid, "Timestamp")["type"] == "datetime"
    assert _cells(store, mid, "Timestamp", sort=[{"column": "Timestamp", "dir": "asc"}]) == [
        "12/31/2023 11:15:00 PM", "2024-01-02T09:00:00Z", "2024-01-04T12:30:00.5Z", "1/5/2024 1:00:00 PM"]


# --------------------------------------------------- canonical_timestamp

@pytest.mark.parametrize("raw, want", [
    # Already canonical: unchanged, the fraction as written.
    ("2024-01-05 13:25:21.1234567", "2024-01-05 13:25:21.1234567"),
    ("2024-01-05 13:25:21", "2024-01-05 13:25:21"),
    # ISO in its other writings: a T, Z, a comma fraction, a one-digit
    # hour, no seconds, no time at all.
    ("2024-01-05T13:25:21Z", "2024-01-05 13:25:21"),
    ("2024-01-05T13:25:21,5", "2024-01-05 13:25:21.5"),
    ("2024-01-05 1:02:03", "2024-01-05 01:02:03"),
    ("2024-01-05 13:25", "2024-01-05 13:25:00"),
    ("2024-01-05", "2024-01-05 00:00:00"),
    ("  2024-01-05 13:25:21  ", "2024-01-05 13:25:21"),
    # Offsets are applied; the fraction is unchanged by a whole-minute one.
    ("2024-01-05T13:25:21.1234567+02:00", "2024-01-05 11:25:21.1234567"),
    ("2024-01-05T00:30:00-05:30", "2024-01-05 06:00:00"),
    ("2024-01-05 13:25:21 UTC", "2024-01-05 13:25:21"),
    # US, day-first and month names, as the grid reads them.
    ("01/05/2024 1:25:21 PM", "2024-01-05 13:25:21"),
    ("1/5/2024 12:00:00.250 a.m.", "2024-01-05 00:00:00.250"),
    ("1/5/2024", "2024-01-05 00:00:00"),
    ("23-06-2026 13:05:01", "2026-06-23 13:05:01"),
    ("5-1-2026 1:05 PM", "2026-01-05 13:05:00"),
    ("Jun 23 2026 00:11:00", "2026-06-23 00:11:00"),
    ("June 23rd, 2026 5:11 PM", "2026-06-23 17:11:00"),
    ("23 Sept 2026", "2026-09-23 00:00:00"),
])
def test_canonical_timestamp_reads_what_the_grid_shows(raw, want):
    assert timeparse.canonical_timestamp(raw) == want


@pytest.mark.parametrize("raw", [
    None, "", "   ", "-", "N/A", "1704099600", "WS01",
    "2024-02-30 10:00:00",            # a day February doesn't have
    "2024-01-05 24:00:00",
    "2024-01-05 13:25:21 EST",        # a zone with no fixed offset
    "2024-01-05T13:25:21 Europe/Paris",
    "0001-01-01T00:00:00+05:00",      # falls off the start in UTC
])
def test_canonical_timestamp_refuses_what_is_not_a_time(raw):
    assert timeparse.canonical_timestamp(raw) is None


def test_canonical_output_is_what_a_derived_datetime_column_holds(store):
    """Every answer is in the shape _infer_derived_type types datetime."""
    vals = ["2024-01-05T13:25:21.1234567+02:00", "1/5/2024 1:00:00 PM", "Jun 23 2026", "23-06-2026 13:05"]
    with store._reader() as ro:
        for raw in vals:
            out = timeparse.canonical_timestamp(raw)
            assert ro.execute(f"SELECT {_sql_is_canonical_ts('v')} FROM (SELECT ? AS v)",
                              (out,)).fetchone()[0] == 1, out
