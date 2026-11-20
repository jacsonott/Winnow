"""Custom timestamp patterns: the `custom_pattern` derived-column operation
(timeparse.compile_pattern and friends), the analyst's saved formats
(workspace.TimestampFormats, /api/timestamp_formats) and detection trying
them (timeparse.detect's custom_formats).

The reading rules are the part most worth pinning, because each one is a
choice about what an analyst means: "dd-mm-yyyy hh:mm:ss" is day, MONTH,
year, 24-hour time, however .NET would read it; a value that goes on past
what the pattern covers (a time after a date-only pattern, an AM/PM the
pattern doesn't read, seconds after a pattern that stops at minutes) does
not parse rather than parsing to a silently wrong moment; and a saved
format beats a built-in one on a tie."""

from __future__ import annotations

import json

import pytest

from winnow import timeparse
from winnow import workspace as WS

OP = timeparse.OPERATIONS["custom_pattern"]


def read(pattern, value, **params):
    return timeparse.parse_or_none(OP, value, {"pattern": pattern, **params}, {})


# ------------------------------------------------------------- reading

@pytest.mark.parametrize("pattern,value,want", [
    # The way people write it: mm between dashes is the month, beside an
    # hour or a second the minute; hh with no AM/PM is the 24-hour clock.
    ("dd-mm-yyyy hh:mm:ss", "14-03-2026 13:22:01", "2026-03-14 13:22:01"),
    ("dd-mm-yyyy hh:mm:ss", "5-1-2026 3:02:01", "2026-01-05 03:02:01"),
    # .NET's letters read the same.
    ("dd-MM-yyyy HH:mm:ss", "14-03-2026 13:22:01", "2026-03-14 13:22:01"),
    # strftime.
    ("%d-%m-%Y %H:%M:%S", "14-03-2026 13:22:01", "2026-03-14 13:22:01"),
    ("%d/%b/%Y:%H:%M:%S %z", "14/Mar/2026:13:22:01 +0100", "2026-03-14 12:22:01"),
    ("%Y-%m-%dT%T", "2026-03-14T13:22:01", "2026-03-14 13:22:01"),
    # Winnow's own display labels, uppercase and all.
    ("YYYY-MM-DD HH:MM:SS", "2026-03-14 13:22:01", "2026-03-14 13:22:01"),
    # Run together: every number its full width.
    ("yyyyMMddHHmmss", "20260314132201", "2026-03-14 13:22:01"),
    ("HHmmss ddMMyyyy", "132201 14032026", "2026-03-14 13:22:01"),
    # A fraction: any number of digits, . or , before it, and it and its
    # separator may both be missing.
    ("dd.mm.yyyy hh:mm:ss,fff", "14.03.2026 13:22:01,482", "2026-03-14 13:22:01.482000"),
    ("dd.mm.yyyy hh:mm:ss.fff", "14.03.2026 13:22:01.4823919", "2026-03-14 13:22:01.482391"),
    ("dd.mm.yyyy hh:mm:ss.fff", "14.03.2026 13:22:01", "2026-03-14 13:22:01"),
    ("HH:mm:ss.SSS dd/MM/yyyy", "13:22:01.5 14/03/2026", "2026-03-14 13:22:01.500000"),
    # A 12-hour clock, in the ways exporters spell the marker.
    ("MM/dd/yyyy hh:mm:ss tt", "03/14/2026 01:22:01 PM", "2026-03-14 13:22:01"),
    ("MM/dd/yyyy hh:mm:ss tt", "03/14/2026 12:22:01am", "2026-03-14 00:22:01"),
    ("dd-MMM-yy hh.mm.ss.ffffff tt", "14-MAR-26 01.22.01.482391 PM", "2026-03-14 13:22:01.482391"),
    # Names: months abbreviated or in full, weekdays read and skipped.
    ("ddd, dd MMM yyyy HH:mm:ss", "Sat, 14 Mar 2026 13:22:01", "2026-03-14 13:22:01"),
    ("dddd d MMMM yyyy", "Saturday 14 March 2026", "2026-03-14 00:00:00"),
    ("MMM d yyyy", "Sept 14 2026", "2026-09-14 00:00:00"),
    # Quoted and escaped text, and a bare T.
    ("dd MMM yyyy 'at' HH:mm", "14 Mar 2026 at 13:22", "2026-03-14 13:22:00"),
    (r"dd\h MM yyyy", "14h 03 2026", "2026-03-14 00:00:00"),
    ("yyyy-MM-ddTHH:mm:ss", "2026-03-14T13:22:01", "2026-03-14 13:22:01"),
    # Dotted times.
    ("dd.mm.yyyy HH.mm", "14.03.2026 13.22", "2026-03-14 13:22:00"),
    # Two-digit years as RFC 2822 widens them.
    ("dd/MM/yy HH:mm", "14/03/26 13:22", "2026-03-14 13:22:00"),
    ("dd/MM/yy HH:mm", "14/03/69 13:22", "1969-03-14 13:22:00"),
    # A zone the pattern names is converted; Z is UTC.
    ("[dd/MMM/yyyy:HH:mm:ss zzz]", "[14/Mar/2026:13:22:01 +0100]", "2026-03-14 12:22:01"),
    ("yyyy-MM-ddTHH:mm:ss.fffZ", "2026-03-14T13:22:01.5Z", "2026-03-14 13:22:01.500000"),
    ("yyyy-MM-ddTHH:mm:ssK", "2026-03-14T13:22:01+05:30", "2026-03-14 07:52:01"),
    ("dd-MM-yyyy HH:mm:ss zzz", "14-03-2026 13:22:01 GMT+2", "2026-03-14 11:22:01"),
    # A zone after the match is read as every text format reads one...
    ("dd-mm-yyyy hh:mm:ss", "14-03-2026 13:22:01 +02:00", "2026-03-14 11:22:01"),
    ("dd-mm-yyyy hh:mm:ss", "14-03-2026 13:22:01 UTC", "2026-03-14 13:22:01"),
    # ...and any other trailing text (the rest of a log line) left alone.
    ("dd-mm-yyyy hh:mm:ss", "14-03-2026 13:22:01 fw01 deny tcp", "2026-03-14 13:22:01"),
])
def test_reads(pattern, value, want):
    assert read(pattern, value) == want


@pytest.mark.parametrize("pattern,value", [
    # A time of day after a date-only pattern: midnight would be a lie.
    ("dd-mm-yyyy", "14-03-2026 13:22:01"),
    ("dd-mm-yyyy", "14-03-2026T13:22"),
    # An AM/PM the pattern doesn't read: 1:22 PM would come out 01:22.
    ("dd-mm-yyyy hh:mm:ss", "14-03-2026 1:22:01 PM"),
    # Digits past the end of the pattern: the seconds or the fraction
    # would be dropped unseen.
    ("dd-mm-yyyy hh:mm", "14-03-2026 13:22:01"),
    ("dd-mm-yyyy hh:mm:ss", "14-03-2026 13:22:01.5"),
    ("yyyyMMddHHmmss", "202603141322011"),
    # A zone that can't be converted, after the match or in it.
    ("dd-mm-yyyy hh:mm:ss", "14-03-2026 13:22:01 EST"),
    ("dd-MM-yyyy HH:mm:ss zzz", "14-03-2026 13:22:01 +15:00"),
    # Not a date, or not this layout.
    ("dd-MM-yyyy HH:mm:ss", "31-02-2026 13:22:01"),
    ("dd-MM-yyyy HH:mm:ss", "14-13-2026 13:22:01"),
    ("dd-MM-yyyy HH:mm:ss", "2026-03-14 13:22:01"),
    ("MMM d yyyy", "Mon 14 2026"),
    ("dd-MM-yyyy", ""),
])
def test_does_not_read(pattern, value):
    assert read(pattern, value) is None


def test_source_utc_offset_shifts_a_value_with_no_zone_only():
    assert read("dd-mm-yyyy hh:mm:ss", "14-03-2026 13:22:01", utc_offset="-05:00") == "2026-03-14 18:22:01"
    # A zone in the value wins over the setting, as it does for ISO 8601.
    assert read("dd-mm-yyyy hh:mm:ss", "14-03-2026 13:22:01 +00:00", utc_offset="-05:00") == "2026-03-14 13:22:01"


@pytest.mark.parametrize("pattern,example", [
    # The day above 12, so "dd-mm" is visibly the 14th of March.
    ("dd-mm-yyyy hh:mm:ss", "14-03-2026 13:22:01"),
    ("d/M/yy H:m", "14/3/26 13:22"),
    ("MM/dd/yyyy hh:mm:ss tt", "03/14/2026 01:22:01 PM"),
    ("ddd, dd MMMM yyyy HH:mm:ss.fff zzz", "Sat, 14 March 2026 13:22:01.482 +00:00"),
    ("yyyy-MM-ddTHH:mm:ssZ", "2026-03-14T13:22:01Z"),
    ("%d.%m.%Y %H:%M:%S,%f", "14.03.2026 13:22:01,482391"),
    ("dd MMM yyyy 'at' HH:mm", "14 Mar 2026 at 13:22"),
])
def test_example_shows_what_the_pattern_means(pattern, example):
    assert timeparse.compile_pattern(pattern).example() == example
    # ...and the pattern reads its own example back.
    assert read(pattern, example) is not None


@pytest.mark.parametrize("pattern,message", [
    ("", "Write a pattern"),
    ("   ", "Write a pattern"),
    ("x" * 201, "at most 200"),
    ("HH:mm:ss", "no year, month or day"),
    ("dd-MM HH:mm", "no year"),
    ("dd-MM-yyyy MM", "two months"),
    ("yyyy yyyy MM dd", "the year twice"),
    ("dd-mm-yyyy hh:mm:ss UTC", "'UTC'"),
    ("dd-MM-yyyy 'at HH:mm", "never closed"),
    ("dd-MM-yyyy\\", "backslash"),
    ("%j %Y", "%j isn't one Winnow reads"),
    ("MM/dd/yyyy mm", "minute needs an hour"),
    ("dd-MM-yyyy ss", "second needs a minute"),
    ("dd-MM-yyyy fff", "fraction of a second needs a second"),
    ("dd-MM-yyyy tt", "AM/PM needs an hour"),
    ("dd-MM-yyyyy", "isn't part of a date"),
])
def test_a_bad_pattern_says_what_is_wrong(pattern, message):
    with pytest.raises(timeparse.PatternError, match=message.replace("(", r"\(")):
        timeparse.compile_pattern(pattern)


def test_two_m_runs_context_cannot_settle_fall_back_on_case():
    # Neither is beside an hour or a second, so .NET's reading decides:
    # MM the month, mm the minute.
    assert read("yyyy-MM-dd HH mm", "2026-03-14 13 22") == "2026-03-14 13:22:00"


def test_validate_params_refuses_a_bad_pattern_and_strips_a_good_one():
    with pytest.raises(ValueError, match="two months"):
        timeparse.validate_params("custom_pattern", {"pattern": "dd-MM-yyyy MM"})
    with pytest.raises(ValueError, match="Pattern"):
        timeparse.validate_params("custom_pattern", {"pattern": ""})
    assert timeparse.validate_params("custom_pattern", {"pattern": "  dd-MM-yyyy  "}) == {"pattern": "dd-MM-yyyy"}


def test_the_operation_lists_as_a_timestamp_format():
    op = next(o for o in timeparse.list_ops() if o["id"] == "custom_pattern")
    assert (op["family"], op["derived_kind"], op["value_type"]) == ("datetime", "datetime", "datetime")
    assert [p["name"] for p in op["params"]] == ["pattern", "utc_offset"]
    assert op["params"][0]["placeholder"] == "dd-MM-yyyy HH:mm:ss"


# ------------------------------------------------------------- detection

EU = ["14.03.2026 13:22:01,482", "14.03.2026 13:24:11,001", "15.03.2026 08:00:00,000"]


def test_plain_detection_never_offers_the_empty_pattern():
    assert all(r["op_id"] != "custom_pattern" for r in timeparse.detect(EU))


def test_a_saved_format_is_detected_with_its_id():
    ranked = timeparse.detect(EU, [{"id": 7, "name": "Firewall (EU)", "pattern": "dd.mm.yyyy hh:mm:ss,fff"}])
    assert ranked[0] == {"op_id": "custom_pattern", "label": "Firewall (EU)", "confidence": 1.0,
                         "params": {"pattern": "dd.mm.yyyy hh:mm:ss,fff"}, "format_id": 7}


def test_a_saved_format_wins_a_tie_with_a_built_in_one():
    # Every day here is 12 or under, so US month-first reads all of it too.
    vals = ["05/01/2026 13:22:01", "06/01/2026 08:00:00"]
    assert timeparse.detect(vals)[0]["op_id"] == "us_datetime"
    ranked = timeparse.detect(vals, [{"id": 1, "name": "UK", "pattern": "dd/MM/yyyy HH:mm:ss"}])
    assert [r["op_id"] for r in ranked[:2]] == ["custom_pattern", "us_datetime"]


def test_detection_holds_a_saved_pattern_to_the_plausible_years():
    # Eight-digit record numbers that happen to be valid yyyyMMdd dates.
    ids = ["10000101", "10000102", "10000103"]
    assert read("yyyyMMdd", ids[0]) == "1000-01-01 00:00:00"   # picking it still reads it
    assert timeparse.detect(ids, [{"id": 1, "name": "d", "pattern": "yyyyMMdd"}]) == []


def test_a_broken_saved_pattern_ranks_nothing():
    assert timeparse.detect(EU, [{"id": 1, "name": "bad", "pattern": "dd-MM-yyyy MM"}]) == []


# ------------------------------------------------------------- columns

ROWS = [["Logged", "Action"],
        ["14.03.2026 13:22:01,482", "allow"],
        ["14.03.2026 13:24:11", "deny"],
        ["garbage", "drop"],
        ["", "allow"]]


def _ingest(store, write_csv, rows=ROWS, name="fw.csv"):
    return store.ingest_csv(write_csv(rows, name=name), name=name, build_fts=False)["id"]


def _values(store, source_id, column):
    view = store.build_view(source_id, {})
    cols = [c["name"] for c in store.get_source(source_id)["columns"]]
    return [r["cells"][cols.index(column)] for r in store.fetch_rows(view["view_id"], 0, 100)["rows"]]


def test_a_custom_column_fills_in_and_counts_its_failures(store, write_csv):
    sid = _ingest(store, write_csv)
    res = store.add_derived_column(sid, "When", "Logged", "custom_pattern", {"pattern": "dd.mm.yyyy hh:mm:ss,fff"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert _values(store, sid, "When") == ["2026-03-14 13:22:01.482000", "2026-03-14 13:24:11", None, None]
    col = next(c for c in store.get_source(sid)["columns"] if c["name"] == "When")
    assert (col["type"], col["parse_failures"]) == ("datetime", 1)
    assert col["derived_params"] == {"pattern": "dd.mm.yyyy hh:mm:ss,fff"}


def test_a_bad_pattern_is_refused_before_any_column_exists(store, write_csv):
    sid = _ingest(store, write_csv)
    with pytest.raises(ValueError, match="two months"):
        store.add_derived_column(sid, "When", "Logged", "custom_pattern", {"pattern": "dd.MM.yyyy MM"})
    assert [c["name"] for c in store.get_source(sid)["columns"]] == ["Logged", "Action"]


def test_re_derive_with_a_corrected_pattern(store, write_csv):
    sid = _ingest(store, write_csv)
    res = store.add_derived_column(sid, "When", "Logged", "custom_pattern", {"pattern": "dd.mm.yyyy hh:mm"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert _values(store, sid, "When")[:2] == [None, None]   # the seconds are past the pattern's end
    def_id = res["definition"]["id"]
    out = store.rederive_column(def_id, {"pattern": "dd.mm.yyyy hh:mm:ss.fff"}, sid)
    store.wait_for_ingest_job(out["job_id"], timeout=30)
    assert _values(store, sid, "When")[:2] == ["2026-03-14 13:22:01.482000", "2026-03-14 13:24:11"]


def test_a_merge_derives_on_every_member(store, write_csv):
    a = _ingest(store, write_csv, name="a.csv")
    b = _ingest(store, write_csv, [["Logged", "Action"], ["01.04.2026 00:00:05", "allow"]], name="b.csv")
    mid = store.create_merge("both", [a, b])["id"]
    res = store.add_derived_column(mid, "When", "Logged", "custom_pattern", {"pattern": "dd.mm.yyyy hh:mm:ss.fff"})
    for jid in res["job_ids"]:
        store.wait_for_ingest_job(jid, timeout=30)
    assert sorted(v for v in _values(store, mid, "When") if v) == [
        "2026-03-14 13:22:01.482000", "2026-03-14 13:24:11", "2026-04-01 00:00:05"]


def test_session_round_trip_carries_the_pattern_not_a_saved_format(store, write_csv, tmp_path):
    from winnow.store import Store

    sid = _ingest(store, write_csv)
    res = store.add_derived_column(sid, "When", "Logged", "custom_pattern", {"pattern": "dd.mm.yyyy hh:mm:ss.fff"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    session = store.export_session(sid)
    assert session["derived_columns"] == [{"name": "When", "input_column": "Logged", "op_id": "custom_pattern",
                                           "params": {"pattern": "dd.mm.yyyy hh:mm:ss.fff"}}]
    other = Store(str(tmp_path / "other.db"))
    try:
        osid = _ingest(other, write_csv, name="again.csv")
        assert other.import_session(osid, session)["derived_columns_added"] == 1
        other.wait_for_ingest_job(timeout=30)
        assert _values(other, osid, "When")[0] == "2026-03-14 13:22:01.482000"
    finally:
        other.close()


def test_store_detection_passes_saved_formats_through(store, write_csv):
    sid = _ingest(store, write_csv, [["Logged"], *[[v] for v in EU]])
    saved = [{"id": 3, "name": "Firewall (EU)", "pattern": "dd.mm.yyyy hh:mm:ss,fff"}]
    assert store.detect_timestamp_format(sid, "Logged") == []
    best = store.detect_timestamp_format(sid, "Logged", custom_formats=saved)[0]
    assert (best["label"], best["format_id"]) == ("Firewall (EU)", 3)
    assert best["preview"][0] == {"input": EU[0], "output": "2026-03-14 13:22:01.482000"}
    hint = store.detect_source_suggestions(sid, custom_formats=saved)
    assert [(h["column"], h["label"], h["format_id"]) for h in hint] == [("Logged", "Firewall (EU)", 3)]


# ------------------------------------------------------------- saved formats

def test_saved_formats_create_update_delete():
    rec = WS.timestamp_formats.create("  Firewall (EU) ", " dd.mm.yyyy hh:mm:ss,fff ")
    assert (rec["id"], rec["name"], rec["pattern"]) == (1, "Firewall (EU)", "dd.mm.yyyy hh:mm:ss,fff")
    two = WS.timestamp_formats.create("Oracle", "dd-MMM-yy hh.mm.ss.ffffff tt")
    assert two["id"] == 2
    WS.timestamp_formats.update(1, "Firewall", "dd.MM.yyyy HH:mm:ss")
    assert [(f["name"], f["pattern"]) for f in WS.timestamp_formats.list()] == [
        ("Firewall", "dd.MM.yyyy HH:mm:ss"), ("Oracle", "dd-MMM-yy hh.mm.ss.ffffff tt")]
    WS.timestamp_formats.delete(1)
    assert [f["id"] for f in WS.timestamp_formats.list()] == [2]
    # An id is not handed out again while the record that had it is there.
    assert WS.timestamp_formats.create("Third", "yyyyMMdd")["id"] == 3
    with pytest.raises(KeyError):
        WS.timestamp_formats.delete(1)
    with pytest.raises(KeyError):
        WS.timestamp_formats.update(1, "x", "yyyyMMdd")


@pytest.mark.parametrize("name,pattern,message", [
    ("", "yyyyMMdd", "Give the format a name"),
    ("x" * 81, "yyyyMMdd", "at most 80"),
    ("ORACLE", "yyyyMMdd", "already a format called"),
    ("New", "dd-MM-yyyy MM", "two months"),
    ("New", "", "Write a pattern"),
])
def test_a_saved_format_is_checked_before_it_is_kept(name, pattern, message):
    WS.timestamp_formats.create("Oracle", "dd-MMM-yy")
    with pytest.raises(ValueError, match=message):
        WS.timestamp_formats.create(name, pattern)
    assert len(WS.timestamp_formats.list()) == 1


def test_a_format_may_be_renamed_to_its_own_name_in_other_capitals():
    WS.timestamp_formats.create("Oracle", "dd-MMM-yy")
    assert WS.timestamp_formats.update(1, "ORACLE", "dd-MMM-yy")["name"] == "ORACLE"


def test_a_hand_broken_record_is_skipped_and_kept():
    path = WS.WORKSPACE_DIR / WS.TimestampFormats.FILE
    WS.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"formats": [
        {"id": 4, "name": "no pattern"},
        {"id": 5, "name": "fine", "pattern": "yyyyMMdd"},
    ]}), encoding="utf-8")
    assert [f["id"] for f in WS.timestamp_formats.list()] == [5]
    # The next id is past the broken record's, and a write keeps it.
    assert WS.timestamp_formats.create("new", "ddMMyyyy")["id"] == 6
    kept = json.loads(path.read_text(encoding="utf-8"))["formats"]
    assert {"id": 4, "name": "no pattern"} in kept


def test_a_damaged_file_is_set_aside_not_overwritten():
    WS.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    (WS.WORKSPACE_DIR / WS.TimestampFormats.FILE).write_text("{not json", encoding="utf-8")
    assert WS.timestamp_formats.list() == []
    assert list(WS.WORKSPACE_DIR.glob("timestamp_formats.json.corrupt-*"))


# ------------------------------------------------------------- routes

def test_routes(client):
    r = client.post("/api/timestamp_formats", json={"name": "Firewall (EU)", "pattern": "dd.mm.yyyy hh:mm:ss,fff"})
    assert r.status_code == 200
    rec = r.json()
    assert (rec["example"], rec["error"]) == ("14.03.2026 13:22:01,482", None)
    assert client.post("/api/timestamp_formats", json={"name": "x", "pattern": "dd-MM-yyyy MM"}).status_code == 400
    r = client.put(f"/api/timestamp_formats/{rec['id']}", json={"name": "Firewall", "pattern": "dd.MM.yyyy"})
    assert r.json()["example"] == "14.03.2026"
    assert client.put("/api/timestamp_formats/99", json={"name": "n", "pattern": "yyyyMMdd"}).status_code == 404
    assert [f["name"] for f in client.get("/api/timestamp_formats").json()] == ["Firewall"]
    assert client.delete(f"/api/timestamp_formats/{rec['id']}").json() == {"ok": True}
    assert client.delete(f"/api/timestamp_formats/{rec['id']}").status_code == 404
    assert client.get("/api/timestamp_formats").json() == []


def test_the_list_route_says_what_is_wrong_with_a_hand_broken_pattern(client):
    WS.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    (WS.WORKSPACE_DIR / WS.TimestampFormats.FILE).write_text(json.dumps(
        {"formats": [{"id": 1, "name": "bad", "pattern": "dd-MM-yyyy MM"}]}), encoding="utf-8")
    [rec] = client.get("/api/timestamp_formats").json()
    assert rec["example"] is None and "two months" in rec["error"]


def test_the_try_route(client):
    r = client.post("/api/timestamp_formats/try", json={
        "pattern": "dd-mm-yyyy hh:mm:ss", "values": ["14-03-2026 13:22:01", "nope"], "utc_offset": "+01:00"})
    assert r.json() == {"example": "14-03-2026 13:22:01", "results": [
        {"input": "14-03-2026 13:22:01", "output": "2026-03-14 12:22:01"}, {"input": "nope", "output": None}]}
    r = client.post("/api/timestamp_formats/try", json={"pattern": "HH:mm"})
    assert r.status_code == 400 and "no year, month or day" in r.json()["detail"]
    assert client.post("/api/timestamp_formats/try", json={"pattern": "dd-MM-yyyy", "utc_offset": "soon"}).status_code == 400


def test_detect_routes_try_the_saved_formats(client, store, write_csv):
    sid = _ingest(store, write_csv, [["Logged"], *[[v] for v in EU]])
    assert client.post("/api/derived/detect", json={"source_id": sid, "column": "Logged"}).json() == []
    rec = client.post("/api/timestamp_formats", json={"name": "Firewall (EU)", "pattern": "dd.mm.yyyy hh:mm:ss,fff"}).json()
    [best] = client.post("/api/derived/detect", json={"source_id": sid, "column": "Logged"}).json()
    assert (best["label"], best["format_id"]) == ("Firewall (EU)", rec["id"])
    [hint] = client.get(f"/api/derived/suggestions?source_id={sid}").json()
    assert (hint["column"], hint["format_id"]) == ("Logged", rec["id"])


def test_the_parser_is_total_without_a_pattern():
    # validate_params refuses this before a column exists; a caller that
    # skipped it (a plugin, a hand-built probe) gets nothing read, not a raise.
    assert OP["parse"]("14-03-2026 13:22:01", {}, {}) is None
    assert OP["parse"]("14-03-2026 13:22:01", {"pattern": "dd-MM MM"}, {}) is None
