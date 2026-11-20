"""Imported numbers that sorted as text.

A column is a number column when the import types it one (infer_type),
and only then does the grid sort it by value (_numeric_expr); a text
column sorts as text, so a byte count put 10 before 9 and 1,048,576
before 2. The report was file sizes and requests' bytes in and out, and
it came from three shapes of real data:

- a cell with no value spelled "-" (W3C and NCSA logs when nothing was
  sent, Zeek) or "N/A" in the first 500 rows made the whole column text;
- a column with nothing at all in the first 500 rows was text however
  full of numbers it was after them: a FortiGate log's traffic lines after
  its event lines, an IIS log's second #Fields: line, a JSON key the first
  records don't carry;
- a number written with spaces round it, a sign or an exponent.

Plus the two ways around the import's own typing: the import preview
sending its reading of 50 rows as the analyst's choice for EVERY column
once one was touched, and every table imported before the fix, which
keeps its types until the case is opened by this version.
"""

from __future__ import annotations

import csv
import json
import sqlite3

import pytest

from winnow import store as store_mod
from winnow.store import DEFAULT_TAGS, NUM_RE, SAMPLE_ROWS, Store, infer_type


def _types(rec_or_src) -> dict:
    return {c["name"]: c["type"] for c in rec_or_src["columns"] if not c.get("derived")}


def _sorted(store, sid, column, direction="asc", count=10_000):
    cols = [c["name"] for c in store.get_source(sid)["columns"]]
    v = store.build_view(sid, {"source_id": sid, "filters": [], "sort": [{"column": column, "dir": direction}]})
    return [r["cells"][cols.index(column)] for r in store.fetch_rows(v["view_id"], 0, count)["rows"]]


def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return str(path)


# ------------------------------------------------------------ infer_type

def test_a_placeholder_among_numbers_leaves_a_number_column():
    assert infer_type(["100", "-", "2048"]) == "number"
    assert infer_type(["100", "N/A", "n/a", " NA ", "null", "None", "(empty)", "2048"]) == "number"
    assert infer_type(["100", "   ", "9"]) == "number"   # whitespace is blank


def test_placeholders_alone_are_not_a_number_column():
    assert infer_type(["-", "-", "N/A"]) == "text"
    assert infer_type([]) == "text"
    assert infer_type(["", None]) == "text"


def test_a_word_still_makes_it_text():
    assert infer_type(["100", "unknown", "9"]) == "text"
    assert infer_type(["100", "12 KB"]) == "text"


@pytest.mark.parametrize("value", [" 12", "12 ", "+5", "-5", "1.5E+09", "1e20", "2.5e-3", "007"])
def test_numbers_the_way_exporters_write_them(value):
    assert infer_type(["100", value]) == "number"


@pytest.mark.parametrize("value", ["1,024", "0x1F", "1.", ".5", "1_000", "1e", "--5", "Infinity"])
def test_not_numbers(value):
    assert infer_type(["100", value]) == "text"


@pytest.mark.parametrize("value", ["12", " 12 ", "+5", "-5", "1.5E+09", "1e20", "2.5e-3", "007", "-0.25"])
def test_every_number_NUM_RE_accepts_is_one_CAST_reads_in_full(value):
    """The point of NUM_RE's shape: _numeric_expr gates CAST(... AS REAL)
    on it, so a cell it accepts must be worth what Python reads it as —
    never CAST's 1.0 for "1,024" or 0.0 for junk."""
    assert NUM_RE.match(value)
    with sqlite3.connect(":memory:") as c:
        assert c.execute("SELECT CAST(? AS REAL)", (value,)).fetchone()[0] == pytest.approx(float(value))


# ------------------------------------------------------ placeholders, CSV

def test_a_byte_count_with_dashes_sorts_by_value(store, tmp_path):
    """The report: a bytes column, a "-" where nothing was sent."""
    rows = [["2026-01-01 00:00:0%d" % (i % 10), b, s] for i, (b, s) in enumerate(
        [("9", "1048576"), ("-", "2"), ("100", "N/A"), ("10", "30"), ("-", "100000")])]
    p = _write_csv(tmp_path / "proxy.csv", ["Time", "Bytes out", "File size"], rows)
    rec = store.ingest_csv(p, build_fts=False)
    assert _types(rec) == {"Time": "datetime", "Bytes out": "number", "File size": "number"}
    sid = rec["id"]
    # The cells with no value sort before every number ascending, as a
    # number column's non-numbers always have.
    assert _sorted(store, sid, "Bytes out") == ["-", "-", "9", "10", "100"]
    assert _sorted(store, sid, "Bytes out", "desc")[:3] == ["100", "10", "9"]
    assert _sorted(store, sid, "File size") == ["N/A", "2", "30", "100000", "1048576"]


def test_a_numeric_filter_on_such_a_column_skips_the_dashes(store, tmp_path):
    p = _write_csv(tmp_path / "f.csv", ["Bytes"], [["9"], ["-"], ["100"], ["N/A"], ["70"]])
    sid = store.ingest_csv(p, build_fts=False)["id"]
    v = store.build_view(sid, {"source_id": sid, "filters": [{"column": "Bytes", "op": ">", "value": "60"}], "sort": []})
    assert sorted(r["cells"][0] for r in store.fetch_rows(v["view_id"], 0, 10)["rows"]) == ["100", "70"]


def test_padded_signed_and_exponent_numbers_sort_by_value(store, tmp_path):
    p = _write_csv(tmp_path / "p.csv", ["Size"], [[" 100"], ["+9"], ["1.5E+03"], ["20 "]])
    rec = store.ingest_csv(p, build_fts=False)
    assert _types(rec)["Size"] == "number"
    assert _sorted(store, rec["id"], "Size") == ["+9", "20 ", " 100", "1.5E+03"]


# ------------------------------------------ nothing in the first rows

def _sparse_csv(tmp_path, late, n=3000, first=900, name="sparse.csv"):
    rows = [[f"2026-01-01 00:{i // 60 % 60:02d}:{i % 60:02d}", "" if i < first else late(i)] for i in range(n)]
    return _write_csv(tmp_path / name, ["Time", "Size"], rows)


def test_a_column_empty_in_the_first_rows_is_typed_from_further_in(store, tmp_path):
    rec = store.ingest_csv(_sparse_csv(tmp_path, lambda i: str((i * 7919) % 100_000)), build_fts=False)
    assert _types(rec)["Size"] == "number"
    ordered = [v for v in _sorted(store, rec["id"], "Size", "desc") if v]
    assert ordered == sorted(ordered, key=int, reverse=True)


def test_a_column_only_filled_at_the_very_end_is_found(store, tmp_path):
    """The runs are spread to the last row: a column the last few hundred
    rows of a big table fill is still seen."""
    p = _sparse_csv(tmp_path, lambda i: str(i), n=20_000, first=19_800)
    assert _types(store.ingest_csv(p, build_fts=False))["Size"] == "number"


def test_a_column_still_empty_further_in_stays_text(store, tmp_path):
    p = _write_csv(tmp_path / "e.csv", ["N", "Empty", "Dash"], [[str(i), "", "-"] for i in range(2000)])
    assert _types(store.ingest_csv(p, build_fts=False)) == {"N": "number", "Empty": "text", "Dash": "text"}


def test_words_further_in_keep_it_text(store, tmp_path):
    rec = store.ingest_csv(_sparse_csv(tmp_path, lambda i: "C:\\Windows\\x.exe" if i % 2 else "12"),
                           build_fts=False)
    assert _types(rec)["Size"] == "text"


def test_timestamps_further_in_are_not_made_a_datetime_column(store, tmp_path):
    """Only ever to number: the first datetime column is the one the
    Timeline reads a table by, and a column first filled 900 rows down
    must not take that over from the one the sample found."""
    rec = store.ingest_csv(_sparse_csv(tmp_path, lambda i: "2026-02-01 10:00:00"), build_fts=False)
    assert _types(rec) == {"Time": "datetime", "Size": "text"}


def test_the_analysts_type_from_the_preview_is_kept(store, tmp_path):
    p = _sparse_csv(tmp_path, lambda i: str(i))
    assert _types(store.ingest_csv(p, name="t", build_fts=False, column_types=["datetime", "text"]))["Size"] == "text"


def test_a_preview_type_of_null_leaves_the_column_to_the_import(store, tmp_path):
    """The preview now sends only the types the analyst picked, None for the
    rest; those columns get the import's own typing, look further in
    included."""
    p = _sparse_csv(tmp_path, lambda i: str(i))
    rec = store.ingest_csv(p, build_fts=False, column_types=["text", None])
    assert _types(rec) == {"Time": "text", "Size": "number"}
    assert rec["ingest"]["column_types"] == ["text", None]


def test_the_ingest_route_takes_a_null_type(client, store, tmp_path):
    p = _sparse_csv(tmp_path, lambda i: str(i))
    r = client.post("/api/ingest/path", json={"path": p, "build_fts": False, "column_types": [None, None]})
    assert r.status_code == 200, r.text
    assert _types(store.get_source(r.json()["id"]))["Size"] == "number"


def test_a_short_table_is_all_sample(store, tmp_path):
    """Under SAMPLE_ROWS rows the sample was the whole table: nothing
    further in to look at, and nothing looked at."""
    p = _write_csv(tmp_path / "s.csv", ["A", "B"], [[str(i), ""] for i in range(SAMPLE_ROWS - 1)])
    assert _types(store.ingest_csv(p, build_fts=False)) == {"A": "number", "B": "text"}


# ----------------------------------------------------- the other readers

def test_fortigate_traffic_after_event_lines(store, tmp_path):
    p = tmp_path / "fortigate.log"
    with open(p, "w") as f:
        for i in range(2000):
            if i < 800:
                f.write(f'date=2026-01-01 time=00:{i // 60 % 60:02d}:{i % 60:02d} type="event" tz="-0500" msg="m{i}"\n')
            else:
                f.write(f'date=2026-01-01 time=01:{i // 60 % 60:02d}:{i % 60:02d} type="traffic" tz="-0500" '
                        f'sentbyte={i * 13 % 9973} rcvdbyte={i * 31 % 99991} duration={i % 300}\n')
    t = _types(store.ingest_kv(str(p), build_fts=False))
    assert (t["sentbyte"], t["rcvdbyte"], t["duration"]) == ("number", "number", "number")
    assert t["tz"] == "text"     # kvread.TEXT_KEYS: an offset, not a quantity


IIS_HEAD = "#Software: Microsoft Internet Information Services 10.0\n#Version: 1.0\n"


def test_an_iis_log_whose_later_fields_line_adds_the_byte_counts(store, tmp_path):
    p = tmp_path / "u_ex260102.log"
    with open(p, "w") as f:
        f.write(IIS_HEAD + "#Fields: date time cs-method cs-uri-stem sc-status time-taken\n")
        for i in range(600):
            f.write(f"2026-01-02 00:{i // 60:02d}:{i % 60:02d} GET /a 200 {i}\n")
        f.write("#Fields: date time cs-method cs-uri-stem sc-status sc-bytes cs-bytes time-taken\n")
        for i in range(600):
            f.write(f"2026-01-02 01:{i // 60:02d}:{i % 60:02d} GET /a 200 {i * 977 % 100003} {i * 7 % 999} {i}\n")
    rec = store.ingest_iis(str(p), build_fts=False)
    t = _types(rec)
    assert (t["sc-bytes"], t["cs-bytes"]) == ("number", "number")
    ordered = [v for v in _sorted(store, rec["id"], "sc-bytes", "desc") if v]
    assert ordered == sorted(ordered, key=int, reverse=True)


def test_an_ncsa_log_with_dashes_for_bytes(store, tmp_path):
    p = tmp_path / "nc260101.log"
    with open(p, "w") as f:
        for i in range(50):
            b = "-" if i % 7 == 0 else str(i * 4099 % 70001)
            f.write(f'10.0.0.2 - - [01/Jan/2026:00:00:{i:02d} +0000] "GET /x HTTP/1.1" 200 {b}\n')
    t = _types(store.ingest_iis(str(p), build_fts=False))
    assert t["Bytes"] == "number"
    assert t["Remote log name"] == "text"   # nothing but "-"


def test_a_json_key_the_first_records_dont_carry(store, tmp_path):
    p = tmp_path / "proxy.jsonl"
    with open(p, "w") as f:
        for i in range(1500):
            o = {"ts": f"2026-01-01T00:00:{i % 60:02d}Z", "bytes_in": i * 3 % 1000}
            if i >= 700:
                o["bytes_out"] = i * 11 % 100_000
            if i == 5:
                o["big"] = 1e20
            f.write(json.dumps(o) + "\n")
    t = _types(store.ingest_json(str(p), build_fts=False))
    assert (t["bytes_in"], t["bytes_out"]) == ("number", "number")


def test_a_plugin_tables_declared_text_is_kept(store):
    rows = [["x", ""] for _ in range(SAMPLE_ROWS)] + [["x", str(i)] for i in range(1000)]
    rec = store.ingest_rows(["A", "B"], rows, name="p", build_fts=False, column_types=[None, "text"])
    assert _types(rec)["B"] == "text"
    rec = store.ingest_rows(["A", "B"], rows, name="q", build_fts=False)
    assert _types(rec)["B"] == "number"


# ------------------------------------------- tables imported before this

def _as_imported_before(case_path, sid, **types):
    """Put a table back the way an import before this typed it, and the
    case back to never having been looked at."""
    with sqlite3.connect(case_path) as c:
        cols = json.loads(c.execute("SELECT columns FROM sources WHERE id=?", (sid,)).fetchone()[0])
        for col in cols:
            if col["name"] in types:
                col["type"] = types[col["name"]]
        c.execute("UPDATE sources SET columns=? WHERE id=?", (json.dumps(cols), sid))
        c.execute("DELETE FROM case_upgrades WHERE name=?", (Store.NUMBER_TYPING_UPGRADE,))


def _upgraded(case_path) -> bool:
    with sqlite3.connect(case_path) as c:
        return bool(c.execute("SELECT 1 FROM case_upgrades WHERE name=?", (Store.NUMBER_TYPING_UPGRADE,)).fetchone())


def _reopen(case_path):
    s = Store(case_path, default_tags=DEFAULT_TAGS)
    s.wait_for_number_types(30)
    return s


def test_an_older_tables_numbers_are_typed_when_the_case_opens(case_path, tmp_path):
    s = Store(case_path, default_tags=DEFAULT_TAGS)
    rows = [[str(i), "-" if i % 9 == 0 else str(i * 37 % 1000), "" if i < 900 else str(i), "x"] for i in range(2000)]
    sid = s.ingest_csv(_write_csv(tmp_path / "old.csv", ["N", "Bytes", "Late", "Word"], rows), build_fts=False)["id"]
    s.close()
    _as_imported_before(case_path, sid, Bytes="text", Late="text")

    s = _reopen(case_path)
    try:
        assert _types(s.get_source(sid)) == {"N": "number", "Bytes": "number", "Late": "number", "Word": "text"}
        assert _upgraded(case_path)
        assert s.get_case_settings() == {}   # nothing an analyst set, so nothing shown
        ordered = [v for v in _sorted(s, sid, "Bytes", "desc") if v != "-"]
        assert ordered == sorted(ordered, key=int, reverse=True)
    finally:
        s.close()

    # Once per case: the next open starts no pass at all.
    s = Store(case_path, default_tags=DEFAULT_TAGS)
    try:
        assert s._number_typer is None
    finally:
        s.close()


def test_what_was_declared_text_stays_text(case_path, tmp_path):
    """The analyst's preview choice, a key=value log's tz, and a column the
    old rule would itself have called a number (so text because something
    said so) are left alone; so is a table no file reader made."""
    s = Store(case_path, default_tags=DEFAULT_TAGS)
    rows = [[str(i), "-" if i % 9 == 0 else str(i)] for i in range(1000)]
    chosen = s.ingest_csv(_write_csv(tmp_path / "c.csv", ["A", "B"], rows), name="chosen", build_fts=False,
                          column_types=[None, "text"])["id"]
    digits = s.ingest_csv(_write_csv(tmp_path / "d.csv", ["Code"], [[f"{i:04d}"] for i in range(10)]),
                          name="digits", build_fts=False)["id"]
    made = s.ingest_rows(["A", "B"], rows, name="made", build_fts=False)["id"]
    p = tmp_path / "fw.log"
    p.write_text("".join(f'date=2026-01-01 time=00:00:{i:02d} tz="{"-" if i % 2 else "-0500"}" '
                         f'sentbyte={"-" if i % 5 == 0 else i}\n' for i in range(40)))
    kv = s.ingest_kv(str(p), build_fts=False)["id"]
    s.close()
    _as_imported_before(case_path, digits, Code="text")
    _as_imported_before(case_path, made, B="text")
    _as_imported_before(case_path, kv, sentbyte="text")

    s = _reopen(case_path)
    try:
        assert _types(s.get_source(chosen))["B"] == "text"
        assert _types(s.get_source(digits))["Code"] == "text"
        assert _types(s.get_source(made))["B"] == "text"
        kv_types = _types(s.get_source(kv))
        assert (kv_types["tz"], kv_types["sentbyte"]) == ("text", "number")
    finally:
        s.close()


def test_a_table_that_fails_to_read_is_looked_at_again_next_open(case_path, tmp_path, monkeypatch):
    s = Store(case_path, default_tags=DEFAULT_TAGS)
    sid = s.ingest_csv(_write_csv(tmp_path / "x.csv", ["A"], [["1"], ["-"]]), build_fts=False)["id"]
    s.close()
    _as_imported_before(case_path, sid, A="text")

    def boom(self, src):
        raise sqlite3.DatabaseError("damaged")

    monkeypatch.setattr(Store, "_numbers_typed_as_text", boom)
    s = _reopen(case_path)
    try:
        assert not _upgraded(case_path)
        assert _types(s.get_source(sid))["A"] == "text"
    finally:
        s.close()
    monkeypatch.undo()
    s = _reopen(case_path)
    try:
        assert _types(s.get_source(sid))["A"] == "number"
    finally:
        s.close()


def test_the_first_last_plugin_reads_numbers_by_the_same_test():
    """first_last gates its own CAST: on a copy of the old pattern, a column
    typed a number for its "+5" would total without it."""
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    path = Path(store_mod.__file__).resolve().parent.parent / "examples" / "plugins" / "first_last" / "__init__.py"
    spec = spec_from_file_location("_first_last_under_test", path)
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.NUM_RE_SQL == NUM_RE.pattern
