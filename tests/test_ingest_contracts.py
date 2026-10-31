"""The import contracts every reader carries a copy of, held for each one.

ingest_csv set three promises, and every reader written since carries its
own copy of the code that keeps them:

- **every row lands, in order**, however many batches the file spans;
- **a cancel discards the import**: the analyst asked for this table not
  to exist, and a half-table looks exactly like a complete one in every
  list;
- **a failure part-way keeps what was read before it** and says so, with
  how many rows, rather than either losing the lot or leaving an empty
  table behind that looks like a real (empty) import.

Each copy was tested for some readers and not others. Here every reader
takes the same three tests on a small file, with BATCH shrunk so that file
spans several batches, and with the failure injected where a real one
comes from: the reader itself, raising part-way through the file (a
corrupt workbook member, a bad Plaso record, a disk error).
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from openpyxl import Workbook

from winnow import iisread, kvread, plasoread, store as store_mod, xlsxread
from winnow.store import IngestCancelled

BATCH = 7
N = 30          # rows in every fixture: four full batches and a partial one
FAIL_AT = 17    # two batches committed, three rows parsed into the third


class Poison(Exception):
    """What the reader raises: the file is broken from here on."""


def mark(i: int) -> str:
    return f"r{i:04d}"


@pytest.fixture(autouse=True)
def small_batches(monkeypatch):
    monkeypatch.setattr(store_mod, "BATCH", BATCH)


# ----------------------------------------------------------- the readers
#
# Each one: a fixture writer, the import call, and how to make the reader
# fail at the row holding mark(k). `keeps` is what a failure keeps: every
# row read before it, or only the batches committed before it.

def _poison_rows(monkeypatch, module, attr, k, header_rows=0):
    """Wrap an iterator-returning reader function so the iterator raises
    at the row that holds mark(k)."""
    real = getattr(module, attr)

    def wrapped(*a, **kw):
        out = real(*a, **kw)
        parts = out if isinstance(out, tuple) else None
        rows = parts[-1] if parts else out

        def gen():
            for row in rows:
                if mark(k) in " ".join(map(str, row)):
                    raise Poison(f"bad record at {mark(k)}")
                yield row
        g = gen()
        return (*parts[:-1], g) if parts else g
    monkeypatch.setattr(module, attr, wrapped)


def _csv_make(tmp_path):
    p = tmp_path / "events.csv"
    p.write_text("When,Msg\n" + "".join(f"2026-01-01 00:00:{i % 60:02d},{mark(i)}\n" for i in range(1, N + 1)),
                 encoding="utf-8")
    return str(p)


def _csv_poison(monkeypatch, k):
    real = store_mod._csv_rows

    def wrapped(*a, **kw):
        reader, rows = real(*a, **kw)

        def gen():
            for row in rows:
                if mark(k) in row:
                    raise Poison(f"bad record at {mark(k)}")
                yield row
        return reader, gen()
    monkeypatch.setattr(store_mod, "_csv_rows", wrapped)


def _text_make(tmp_path):
    p = tmp_path / "hostd.log"
    p.write_text("".join(f"2026-01-01T00:00:{i % 60:02d}Z info hostd {mark(i)}\n" for i in range(1, N + 1)),
                 encoding="utf-8")
    return str(p)


def _text_poison(monkeypatch, k):
    real = store_mod._text_line

    def wrapped(line):
        if mark(k) in line:
            raise Poison(f"bad record at {mark(k)}")
        return real(line)
    monkeypatch.setattr(store_mod, "_text_line", wrapped)


def _json_make(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text("".join(json.dumps({"When": f"2026-01-01 00:00:{i % 60:02d}", "Msg": mark(i)}) + "\n"
                         for i in range(1, N + 1)), encoding="utf-8")
    return str(p)


def _json_poison(monkeypatch, k):
    real = store_mod._json_column_plan

    def wrapped(seen):
        cols, row_of = real(seen)

        def poisoned(flat):
            if mark(k) in flat.values():
                raise Poison(f"bad record at {mark(k)}")
            return row_of(flat)
        return cols, poisoned
    monkeypatch.setattr(store_mod, "_json_column_plan", wrapped)


def _iis_make(tmp_path):
    p = tmp_path / "u_ex260101.log"
    p.write_text("#Software: Microsoft Internet Information Services 10.0\n#Version: 1.0\n"
                 "#Date: 2026-01-01 00:00:00\n#Fields: date time s-ip cs-method cs-uri-stem\n"
                 + "".join(f"2026-01-01 00:00:{i % 60:02d} 10.0.0.5 GET /{mark(i)}\n" for i in range(1, N + 1)),
                 encoding="utf-8")
    return str(p)


def _kv_make(tmp_path):
    p = tmp_path / "fortigate.log"
    p.write_text("".join(f'date=2026-01-01 time=00:00:{i % 60:02d} devname="FG" type="traffic" msg="{mark(i)}"\n'
                         for i in range(1, N + 1)), encoding="utf-8")
    return str(p)


def _xlsx_make(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Events"
    ws.append(["When", "Msg"])
    for i in range(1, N + 1):
        ws.append([f"2026-01-01 00:00:{i % 60:02d}", mark(i)])
    p = tmp_path / "events.xlsx"
    wb.save(p)
    return str(p)


def _sqlite_make(tmp_path):
    p = tmp_path / "evidence.db"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE t (id INTEGER, msg TEXT)")
    db.executemany("INSERT INTO t VALUES (?,?)", [(i, mark(i)) for i in range(1, N + 1)])
    db.commit()
    db.close()
    return str(p)


def _sqlite_poison(monkeypatch, k):
    real = store_mod._lenient_text_factory

    def factory(*a, **kw):
        decode = real(*a, **kw)

        def poisoned(b):
            s = decode(b)
            if s == mark(k):
                raise Poison(f"bad record at {mark(k)}")
            return s
        return poisoned
    monkeypatch.setattr(store_mod, "_lenient_text_factory", factory)


def _plaso_make(tmp_path):
    """acstore-era storage with one event per row, in timestamp order."""
    p = tmp_path / "timeline.plaso"
    db = sqlite3.connect(p)
    db.executescript("""
        CREATE TABLE metadata (key TEXT, value TEXT);
        CREATE TABLE event (_identifier INTEGER PRIMARY KEY AUTOINCREMENT,
                            _event_data_identifier TEXT, date_time TEXT,
                            timestamp BIGINT, timestamp_desc TEXT);
        CREATE TABLE event_data (_identifier INTEGER PRIMARY KEY AUTOINCREMENT, _data TEXT);
    """)
    db.executemany("INSERT INTO metadata VALUES (?,?)", [
        ("format_version", "20230327"), ("serialization_format", "json"), ("storage_type", "session")])
    for i in range(1, N + 1):
        db.execute("INSERT INTO event_data(_data) VALUES (?)", (json.dumps({
            "data_type": "windows:prefetch:execution", "parser": "prefetch",
            "executable_filename": mark(i)}),))
        db.execute("INSERT INTO event(_event_data_identifier, date_time, timestamp, timestamp_desc)"
                   " VALUES (?,?,?,?)", (f"event_data.{i}", json.dumps({"__type__": "DateTimeValues"}),
                                         1_700_000_000_000_000 + i * 1_000_000, "Last Time Executed"))
    db.commit()
    db.close()
    return str(p)


READERS = {
    "csv": dict(make=_csv_make, ingest=lambda s, p, **kw: s.ingest_csv(p, name="events.csv", **kw),
                poison=_csv_poison, keeps="parsed"),
    "text": dict(make=_text_make, ingest=lambda s, p, **kw: s.ingest_text(p, **kw),
                 poison=_text_poison, keeps="parsed"),
    "json": dict(make=_json_make, ingest=lambda s, p, **kw: s.ingest_json(p, **kw),
                 poison=_json_poison, keeps="parsed"),
    "iis": dict(make=_iis_make, ingest=lambda s, p, **kw: s.ingest_iis(p, **kw),
                poison=lambda mp, k: _poison_rows(mp, iisread, "rows", k), keeps="parsed"),
    "kv": dict(make=_kv_make, ingest=lambda s, p, **kw: s.ingest_kv(p, **kw),
               poison=lambda mp, k: _poison_rows(mp, kvread, "rows", k), keeps="parsed"),
    "xlsx": dict(make=_xlsx_make, ingest=lambda s, p, **kw: s.ingest_xlsx_sheet(p, "Events", **kw),
                 poison=lambda mp, k: _poison_rows(mp, xlsxread, "sheet_reader", k), keeps="parsed"),
    "plaso": dict(make=_plaso_make, ingest=lambda s, p, **kw: s.ingest_plaso(p, **kw),
                  poison=lambda mp, k: _poison_rows(mp, plasoread, "iter_plaso_rows", k), keeps="parsed"),
    # sqlite3 fetches a batch at a time, and a fetchmany that fails part-way
    # returns none of the rows it read; only the batches before it remain.
    "sqlite": dict(make=_sqlite_make, ingest=lambda s, p, **kw: s.ingest_sqlite_table(p, "t", **kw),
                   poison=_sqlite_poison, keeps="committed"),
}


@pytest.fixture(params=READERS, ids=READERS)
def reader(request, tmp_path):
    r = READERS[request.param]
    return {**r, "name": request.param, "path": r["make"](tmp_path)}


def _tables(store) -> set[str]:
    with store.lock:
        return {r[0] for r in store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'src\\_%' ESCAPE '\\'")}


def _marks_in_order(store, sid) -> list[int]:
    """The mark each row holds, in rid order: which source row it is."""
    table = store.get_source(sid)["table_name"]
    with store.lock:
        rows = store.db.execute(f'SELECT * FROM "{table}" ORDER BY rid').fetchall()
    found = []
    for row in rows:
        text = " ".join(str(v) for v in tuple(row)[1:])
        hits = [i for i in range(1, N + 1) if mark(i) in text]
        assert len(hits) == 1, text
        found.append(hits[0])
    return found


# ------------------------------------------------------------- the tests

def test_a_file_bigger_than_a_batch_lands_every_row_in_order(store, reader):
    ticks = []
    rec = reader["ingest"](store, reader["path"], build_fts=False, progress=lambda *a: ticks.append(a))
    assert rec["row_count"] == N
    assert _marks_in_order(store, rec["id"]) == list(range(1, N + 1))
    assert len(ticks) >= N // BATCH, "progress is reported per batch"


def test_a_cancel_after_the_first_batch_leaves_no_table(store, reader):
    before_sources = {s["id"] for s in store.list_sources()}
    before_tables = _tables(store)
    committed = []
    with pytest.raises(IngestCancelled):
        # Asked once rows have committed (progress runs after a commit),
        # so there is a partial table for the cancel to take back.
        reader["ingest"](store, reader["path"], build_fts=False,
                         progress=lambda *a: committed.append(a), cancel=lambda: bool(committed))
    assert committed, "the cancel came before anything committed, so this proved nothing"
    assert {s["id"] for s in store.list_sources()} == before_sources
    assert _tables(store) == before_tables


def test_a_failure_part_way_keeps_the_rows_before_it_and_says_how_many(store, reader, monkeypatch):
    reader["poison"](monkeypatch, FAIL_AT)
    before = {s["id"] for s in store.list_sources()}
    with pytest.raises(Exception) as e:
        reader["ingest"](store, reader["path"], build_fts=False)
    kept = FAIL_AT - 1 if reader["keeps"] == "parsed" else (FAIL_AT - 1) // BATCH * BATCH
    new = [s for s in store.list_sources() if s["id"] not in before]
    assert len(new) == 1, "the rows before the failure were not kept"
    assert new[0]["row_count"] == kept
    assert _marks_in_order(store, new[0]["id"]) == list(range(1, kept + 1))
    assert f"bad record at {mark(FAIL_AT)}" in str(e.value)
    assert f"{kept:,}" in str(e.value), f"the message doesn't say how many rows were kept: {e.value}"
    assert store.get_source(new[0]["id"])["columns"], "a partial table is still typed and browsable"


def test_a_failure_on_the_first_row_leaves_no_empty_table(store, reader, monkeypatch):
    reader["poison"](monkeypatch, 1)
    before_sources = {s["id"] for s in store.list_sources()}
    before_tables = _tables(store)
    with pytest.raises(Exception) as e:
        reader["ingest"](store, reader["path"], build_fts=False)
    assert f"bad record at {mark(1)}" in str(e.value)
    assert {s["id"] for s in store.list_sources()} == before_sources
    assert _tables(store) == before_tables
