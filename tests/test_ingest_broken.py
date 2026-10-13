"""Incomplete and broken evidence files. Every case here was first
demonstrated live against the store during a bug review — these pin the
recovery contracts, which nothing else in the suite did:

- keep-what-committed is really keep-what-PARSED: the rows sitting in the
  uncommitted batch when a bad line appears must land too (losing them
  silently cost up to 20k good rows before the failure);
- a failure before anything committed leaves NO source, not a 0-row husk;
- UTF-16 (PowerShell's default export encoding) is decoded, not shredded
  into NUL-riddled headers that abort CREATE TABLE;
- one broken JSONL line is counted and skipped, never the whole file;
- the quote-swallow signature (one field eating the lines after it) is
  at least *flagged*, since the ragged counter reads zero for it;
- a field past csv's own 128 KB limit is a real field, not a broken
  file, and imports whole."""

from __future__ import annotations

import csv
import os

import pytest
from fastapi.testclient import TestClient

from winnow.store import sanitize_columns

HEADERS = {"X-Timeline-Lite-Client": "1"}

# A field over csv's own 131072-character default limit. Store raises the
# limit to CSV_FIELD_LIMIT, so this is an ordinary (big) field there; the
# poison-line tests put the old limit back for their duration, which is
# what keeps a poison line cheap to write — one over the real cap would be
# a 64 MB fixture.
BIG_FIELD = "x" * 200_000
OLD_CSV_FIELD_LIMIT = 131_072


@pytest.fixture
def poison_field_limit():
    """The reliable way to hard-stop the csv reader mid-file is a field
    over the field limit; lower the limit so BIG_FIELD is one."""
    before = csv.field_size_limit(OLD_CSV_FIELD_LIMIT)
    yield
    csv.field_size_limit(before)


# ------------------------------------------------------------------ csv


def test_mid_file_error_keeps_every_parsed_row(store, tmp_path, poison_field_limit):
    """25,000 good rows then a poison line: 20,000 had committed, 5,000
    sat in the pending batch. All 25,000 must survive — and the error has
    to say where it stopped and what was kept, because 'field larger than
    field limit' alone reads as if the import produced nothing."""
    p = tmp_path / "poison.csv"
    with open(p, "w", encoding="utf-8") as f:
        f.write("a,b\n")
        for i in range(25_000):
            f.write(f"r{i},x\n")
        f.write(f"bad,{BIG_FIELD}\n")
    with pytest.raises(ValueError) as ei:
        store.ingest_csv(str(p), build_fts=False)
    src = store.list_sources()[0]
    assert src["row_count"] == 25_000
    msg = str(ei.value)
    assert "25,000" in msg and "kept" in msg
    assert "Line" in msg


def test_error_on_the_first_data_line_leaves_no_husk(store, tmp_path, poison_field_limit):
    """Nothing parsed, nothing kept — and no 0-row source in the table
    list masquerading as a real (empty) import."""
    p = tmp_path / "instant.csv"
    p.write_text(f"a,b\nbad,{BIG_FIELD}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        store.ingest_csv(str(p), build_fts=False)
    assert store.list_sources() == []


def test_a_field_over_the_csv_default_limit_imports_whole(store, tmp_path):
    """csv's default limit is 131072 characters, and one field past it
    stopped the import at that line ("field larger than field limit") —
    an EvtxECmd payload with a PowerShell script block in it is routinely
    bigger. The field lands whole, the rows after it land too, and the
    preview reads the file the same way the import does."""
    p = tmp_path / "payload.csv"
    p.write_text(f"a,b\nbefore,x\nbig,{BIG_FIELD}\nafter,y\n", encoding="utf-8")
    rec = store.ingest_csv(str(p), build_fts=False)
    assert rec["row_count"] == 3
    rows = store.db.execute(f"SELECT a, length(b) FROM src_{rec['id']} ORDER BY rid").fetchall()
    assert [tuple(r) for r in rows] == [("before", 1), ("big", len(BIG_FIELD)), ("after", 1)]
    prev = store.preview_csv_text(p.read_text(encoding="utf-8"), name=str(p))
    assert [r[0] for r in prev["sample_rows"]] == ["before", "big", "after"]


def test_csv_field_limit_fits_a_windows_c_long():
    """field_size_limit takes a C long — 32 bits on Windows — so the
    obvious sys.maxsize raises OverflowError there at import time."""
    from winnow.store import CSV_FIELD_LIMIT
    assert csv.field_size_limit() == CSV_FIELD_LIMIT
    assert OLD_CSV_FIELD_LIMIT < CSV_FIELD_LIMIT <= 2**31 - 1


def test_stray_quote_swallowing_past_the_old_limit_is_flagged(store, tmp_path):
    """With the old limit a stray quote that swallowed more than 128 KB of
    the lines after it hard-stopped the import. Now the swallow runs on to
    the end of the file — and the many-lines-in-one-row heuristic is the
    signal the analyst gets, so it has to fire for it."""
    p = tmp_path / "swallow_big.csv"
    with open(p, "w", encoding="utf-8") as f:
        f.write("a,b\n")
        for i in range(10):
            f.write(f"r{i},x\n")
        f.write('oops,"unclosed\n')
        for i in range(3000):
            f.write(f"swallowed{i},{'y' * 60}\n")   # ~200 KB swallowed
    rec = store.ingest_csv(str(p), build_fts=False)
    assert rec["row_count"] == 11
    assert rec["suspect_quote_rows"] >= 1


def test_utf16_csv_is_decoded_not_shredded(store, tmp_path):
    """Windows PowerShell 5.1's Out-File writes UTF-16LE by default, so
    these are routine DFIR inputs. Decoded as UTF-8-with-replacement the
    header became NULs and CREATE TABLE died with 'the query contains a
    null character' — an error no analyst can act on."""
    for codec in ("utf-16", "utf-16-be"):
        p = tmp_path / f"{codec}.csv"
        # utf-16/utf-16-be both write a BOM via .encode with codecs.open?
        # write bytes explicitly so the BOM is guaranteed present.
        bom = b"\xff\xfe" if codec == "utf-16" else b"\xfe\xff"
        body = "Host,User\nDC01,alice\nWS02,bob\n".encode(
            "utf-16-le" if codec == "utf-16" else "utf-16-be")
        p.write_bytes(bom + body)
        rec = store.ingest_csv(str(p), build_fts=False)
        assert [c["name"] for c in rec["columns"]] == ["Host", "User"], codec
        rows = store.fetch_rows(store.build_view(
            rec["id"], {"source_id": rec["id"], "filters": [], "sort": []})["view_id"], 0, 5)
        assert "DC01" in rows["rows"][0]["cells"], codec


def test_nul_bytes_in_headers_are_stripped(store, tmp_path):
    p = tmp_path / "nulhdr.csv"
    p.write_bytes(b"Ho\x00st,User\nDC01,alice\n")
    rec = store.ingest_csv(str(p), build_fts=False)
    assert [c["name"] for c in rec["columns"]] == ["Host", "User"]


def test_sanitize_columns_strips_control_characters():
    assert sanitize_columns(["Ho\x00st", "a\x1fb", ""]) == ["Host", "ab", "col_3"]


def test_unbalanced_quote_swallow_is_flagged(store, write_csv, tmp_path):
    """A stray quote makes csv fold every following line into one field —
    the rows vanish from the grid while ragged_rows reads zero. The
    many-newlines-in-one-field signature is counted so the jobs panel can
    warn; an ordinary quoted multi-line payload must NOT trip it."""
    p = tmp_path / "swallow.csv"
    with open(p, "w", encoding="utf-8") as f:
        f.write("a,b\n")
        for i in range(100):
            f.write(f"r{i},x\n")
        f.write('oops,"unclosed\n')
        for i in range(50):
            f.write(f"swallowed{i},y\n")
    rec = store.ingest_csv(str(p), build_fts=False)
    assert rec["row_count"] == 101          # the 50 lines really are gone…
    assert rec["ragged_rows"] == 0          # …and this counter can't see it
    assert rec["suspect_quote_rows"] >= 1   # but this one does

    legit = tmp_path / "legit.csv"
    legit.write_text('a,b\nr1,"line one\nline two\nline three"\nr2,x\n', encoding="utf-8")
    rec2 = store.ingest_csv(str(legit), build_fts=False)
    assert rec2["row_count"] == 2
    assert rec2["suspect_quote_rows"] == 0


# ----------------------------------------------------------------- json


def test_jsonl_bad_lines_are_skipped_and_counted(store, tmp_path):
    """One truncated line (a crashed writer, a rotation seam) must not
    cost the analyst the good lines around it — same philosophy as ragged
    CSV rows: tolerated, counted, surfaced."""
    p = tmp_path / "seam.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for i in range(100):
            f.write('{"n": %d}\n' % i)
        f.write('{"n": 100, "u": "bo\n')       # truncated mid-string
        f.write('not json at all\n')
        f.write('{"n": 101}\n')
    rec = store.ingest_json(str(p), build_fts=False)
    assert rec["row_count"] == 101
    assert rec["bad_records"] == 2
    assert rec["first_bad_line"] == 101


def test_jsonl_all_bad_reports_the_real_file_line(store, tmp_path):
    """json.loads on a single line says 'line 1' — the position inside
    that one line, which as a file position is actively wrong."""
    p = tmp_path / "allbad.jsonl"
    p.write_text("\n\n{broken\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 3"):
        store.ingest_json(str(p), build_fts=False)
    assert store.list_sources() == []


def test_truncated_json_document_is_still_a_hard_error(store, tmp_path):
    """A .json document is ONE value — half of it isn't a partial table,
    it's an unparseable file. No tolerance, and no source left behind."""
    p = tmp_path / "cut.json"
    p.write_text('[{"a": 1}, {"a": 2}, {"a"', encoding="utf-8")
    with pytest.raises(Exception):
        store.ingest_json(str(p), build_fts=False)
    assert store.list_sources() == []


def test_utf16_jsonl_is_decoded(store, tmp_path):
    p = tmp_path / "u16.jsonl"
    p.write_bytes(b"\xff\xfe" + '{"Host": "DC01"}\n{"Host": "WS02"}\n'.encode("utf-16-le"))
    rec = store.ingest_json(str(p), build_fts=False)
    assert rec["row_count"] == 2
    assert [c["name"] for c in rec["columns"]] == ["Host"]


# --------------------------------------------------------------- routes


@pytest.fixture
def client(store, monkeypatch, tmp_path):
    import server
    monkeypatch.setattr(server, "STORE", None)
    monkeypatch.setattr(server.WS.machine_prefs, "get", lambda k, d=None: str(tmp_path / "cases"))
    return server, TestClient(server.app)


def test_assoc_open_skips_a_corrupt_file_instead_of_500(client, tmp_path):
    """sqlite3.DatabaseError is not ValueError: a corrupt .db arriving by
    double-click used to 500 this route (and crash the launcher server at
    startup). One unreadable file skips, the readable one still lands."""
    server, c = client
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"SQLite format 3\x00" + os.urandom(2048))
    good = tmp_path / "good.csv"
    good.write_text("a,b\n1,2\n", encoding="utf-8")
    res = c.post("/api/assoc/open", json={"files": [str(corrupt), str(good)]}, headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert [s["file"] for s in body["skipped"]] == ["corrupt.db"]
    assert "database" in body["skipped"][0]["reason"]
    assert [s["file"] for s in body["started"]] == ["good.csv"]
    server.STORE.close()


def test_preview_path_decodes_utf16(client, store, monkeypatch, tmp_path):
    server, c = client
    monkeypatch.setattr(server, "STORE", store)   # preview needs an open case
    p = tmp_path / "u16.csv"
    p.write_bytes(b"\xff\xfe" + "Host,User\nDC01,alice\n".encode("utf-16-le"))
    res = c.post("/api/ingest/preview/path", json={"path": str(p), "kind": "csv"},
                 headers=HEADERS)
    assert res.status_code == 200
    assert res.json()["columns"] == ["Host", "User"]
