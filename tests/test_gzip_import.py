"""Gzip-compressed single files (winnow/gzipped.py): a file.json.gz, a
file.tsv.gz, a rotated auth.log.1.gz read as the file INSIDE them, by the
reader that file would have had.

The load-bearing tests are the routing ones and the identity ones. Routing,
because the whole feature is "the inner name decides" and the one thing it
must not swallow is a TARBALL — a .tar.gz expands into many files and
belongs to archive.py, and a .gz that holds a tar under another name has
to be found by looking. Identity, because the reader reads a temporary
copy: the table has to record the .gz the analyst pointed at, the copy
has to be gone afterwards whichever way the import ended, and the
fingerprint has to be the one the same evidence has uncompressed, or a
session recorded against it can never be re-imported.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import struct
import tarfile
import tempfile

import pytest

from winnow import gzipped
from winnow.store import Store

# A FortiGate line, which no name says is one: the content probes have to
# read inside the .gz to see it (tests/test_kv_logs.py has the format).
FORTI = ("date=2026-10-23 time=05:39:00 devid=FG6H1ETB20902601 devname=Main-Firewall "
         'type=traffic subtype=forward srcip=192.168.1.50 dstintf="wan1" action=accept')
# fls -m, likewise.
BODY_LINE = "0|/Users/dana/notes.txt|64|r/rrwxrwxrwx|0|0|1024|1700000000|1700000001|1700000002|1699999999"


def gz(tmp_path, name: str, text: str, *, mtime: int | None = None) -> str:
    """`text` written to `name` through gzip. `mtime` is the time gzip
    stores for the file it compressed (0 is what logrotate's pipe leaves)."""
    p = tmp_path / name
    with open(p, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=mtime) as g:
        g.write(text.encode())
    return str(p)


def gz_bytes(tmp_path, name: str, data: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(gzip.compress(data))
    return str(p)


def rows_of(store: Store, source_id: int) -> list[tuple]:
    return [tuple(r)[1:] for r in store.db.execute(f"SELECT * FROM src_{source_id} ORDER BY rid")]


def run_job(store: Store, kind: str, path: str, **kw) -> dict:
    job = store.start_ingest_job(kind, path, **kw)
    return store.wait_for_ingest_job(job["job_id"], timeout=60)


def gunzip_tmpdirs() -> list[str]:
    return [n for n in os.listdir(tempfile.gettempdir()) if n.startswith("winnow-gunzip-")]


# ------------------------------------------------------------ routing by name

@pytest.mark.parametrize("name, inner", [
    ("events.json.gz", "events.json"),
    ("events.JSON.GZ", "events.JSON"),
    ("hosts.tsv.gz", "hosts.tsv"),
    ("auth.log.1.gz", "auth.log.1"),
    ("messages.gz", "messages"),                     # no extension inside: still one file
    ("/cases/42/evidence/events.csv.gz", "/cases/42/evidence/events.csv"),
    (r"C:\cases\events.csv.gz", r"C:\cases\events.csv"),
])
def test_the_name_inside_a_gzipped_single_file(name, inner):
    assert gzipped.inner_name(name) == inner
    assert gzipped.routing_name(name) == inner


@pytest.mark.parametrize("name", [
    "bundle.tar.gz",      # many files: archive.py's
    "bundle.TAR.gz",
    "bundle.tgz",         # not a .gz name at all
    ".gz",                # nothing inside to name
    "events.json",
    "events.zip",
    "",
])
def test_names_that_are_not_one_gzipped_file(name):
    assert gzipped.inner_name(name) is None
    assert gzipped.routing_name(name) == name


def test_the_router_reads_the_inner_name_for_every_reader(tmp_path):
    import server

    cases = {
        "events.json.gz": "json",
        "events.jsonl.gz": "json",
        "events.csv.gz": "csv",
        "hosts.tsv.gz": "csv",
        "u_ex240501.log.gz": "iis",
        "wtmp.gz": "utmp",
        "bodyfile.txt.gz": "bodyfile",
        "auth.log.1.gz": "secure",
        "History.db.gz": "sqlite",
        "book.xlsx.gz": "xlsx",
        "timeline.plaso.gz": "plaso",
        "hostd.0.gz": "text",          # no reader by name, no plugin loaded here
    }
    for name, kind in cases.items():
        path = gz(tmp_path, name, "nothing readable, the name is what routes\n")
        assert server._ingest_kind_for_path(path) == kind, name


def test_a_gzipped_log_whose_content_says_what_it_is(tmp_path):
    import server

    # Neither name says anything; the first line does, and it is inside
    # the .gz — the probes have to decompress to see it.
    assert server._ingest_kind_for_path(gz(tmp_path, "fw.gz", FORTI + "\n")) == "kv"
    assert server._ingest_kind_for_path(gz(tmp_path, "listing.gz", BODY_LINE + "\n")) == "bodyfile"


def test_a_gzipped_tarball_is_an_archive_however_it_is_named(tmp_path):
    import server

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("var/log/auth.log")
        info.size = 5
        tf.addfile(info, io.BytesIO(b"hello"))
    # The honest name, and the name that hides it.
    for name in ("bundle.tar.gz", "collection.gz"):
        p = tmp_path / name
        p.write_bytes(buf.getvalue())
        assert gzipped.holds_tar(str(p)) is True
        assert server._ingest_kind_for_path(str(p)) == "archive"
    # And a gzipped text file is not a tarball.
    assert gzipped.holds_tar(gz(tmp_path, "a.csv.gz", "a,b\n1,2\n")) is False


def test_a_gzipped_file_is_decided_by_its_bytes_not_its_name(tmp_path):
    plain = tmp_path / "not-really.csv.gz"
    plain.write_bytes(b"a,b\n1,2\n")
    assert gzipped.is_gzip(str(plain)) is False
    # Read as it is: nothing decompresses it, and nothing fails.
    assert gzipped.head(str(plain), 1 << 20) == b"a,b\n1,2\n"
    with gzipped.decompressed(str(plain)) as real:
        assert real == str(plain)
    # ...and a gzip stream under a name that doesn't say so still is one.
    named = gz(tmp_path, "events.csv", "a,b\n1,2\n")
    assert gzipped.is_gzip(named) is True


# ------------------------------------------------------------ reading one

def test_csv_gz_imports_as_the_csv_inside_it(store, tmp_path):
    path = gz(tmp_path, "events.csv.gz",
              "Timestamp,EventId,Details\n2024-01-05 13:00:00,4624,logon\n2024-01-05 13:01:00,4625,fail\n")
    done = run_job(store, "csv", path, name="events.csv.gz")
    assert done["status"] == "done"
    assert done["rows_done"] == 2
    src = store.get_source(done["source_ids"][0])
    assert [c["name"] for c in src["columns"]] == ["Timestamp", "EventId", "Details"]
    assert rows_of(store, src["id"]) == [("2024-01-05 13:00:00", "4624", "logon"),
                                         ("2024-01-05 13:01:00", "4625", "fail")]
    # The table says where it came from: the file the analyst pointed at.
    assert src["path"] == os.path.abspath(path)
    assert src["name"] == "events.csv.gz"
    assert gunzip_tmpdirs() == []


def test_tsv_gz_is_split_on_tabs_by_the_inner_extension(store, tmp_path):
    # The delimiter is read from the extension first (Store._sniff), so a
    # .tsv.gz has to reach the sniffer as a .tsv — a comma in a field is
    # exactly what goes wrong when it doesn't.
    path = gz(tmp_path, "hosts.tsv.gz", "Host\tNote\nws01\tfinance, west\n")
    rec = store.ingest_csv(path)
    assert [c["name"] for c in rec["columns"]] == ["Host", "Note"]
    assert rows_of(store, rec["id"]) == [("ws01", "finance, west")]


def test_json_gz_and_jsonl_gz(store, tmp_path):
    doc = gz(tmp_path, "events.json.gz", json.dumps([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]))
    lines = gz(tmp_path, "events.jsonl.gz", '{"a": 3}\n{"a": 4}\n')
    one = store.ingest_json(doc)
    many = store.ingest_json(lines)
    assert one["row_count"] == 2
    assert [c["name"] for c in one["columns"]] == ["a", "b"]
    assert many["row_count"] == 2
    # And the preview reads it the same way, from the same file.
    assert store.preview_json_file(doc)["record_count"] == 2


def test_text_gz_imports_one_line_per_row(store, tmp_path):
    path = gz(tmp_path, "hostd.log.gz", "first line\nsecond line\n")
    rec = store.ingest_text(path, name="hostd.log.gz")
    assert rec["row_count"] == 2
    assert rows_of(store, rec["id"]) == [("first line",), ("second line",)]


def test_iis_gz_is_read_by_the_format_its_inner_name_says(store, tmp_path):
    path = gz(tmp_path, "u_ex240501.log.gz",
              "#Software: Microsoft Internet Information Services 10.0\n"
              "#Fields: date time s-ip cs-method cs-uri-stem sc-status\n"
              "2024-05-01 00:00:01 10.0.0.5 GET /index.html 200\n")
    rec = store.ingest_iis(path)
    assert rec["log_format"] == "w3c"
    assert rec["row_count"] == 1
    assert "cs-uri-stem" in [c["name"] for c in rec["columns"]]


def test_kv_gz_and_bodyfile_gz(store, tmp_path):
    kv = store.ingest_kv(gz(tmp_path, "fw.log.gz", FORTI + "\n"))
    assert kv["row_count"] == 1
    assert "srcip" in [c["name"] for c in kv["columns"]]
    body = store.ingest_bodyfile(gz(tmp_path, "bodyfile.gz", BODY_LINE + "\n"))
    assert body["row_count"] == 1


def test_utmp_gz_reads_its_layout_off_the_records(store, tmp_path):
    # glibc's struct utmp, built the way tests/test_utmp_logs.py builds it.
    rec = bytearray(384)
    struct.pack_into("<h", rec, 0, 7)            # ut_type USER_PROCESS
    struct.pack_into("<i", rec, 4, 4321)         # ut_pid
    rec[8:13] = b"pts/0"                         # ut_line
    rec[40:44] = b"ts/0"                         # ut_id
    rec[44:48] = b"dana"                         # ut_user
    rec[76:87] = b"203.0.113.9"                  # ut_host
    struct.pack_into("<iIi", rec, 336, 4321, 1714557598, 123456)
    path = gz_bytes(tmp_path, "wtmp.gz", bytes(rec) * 3)
    rec = store.ingest_utmp(path)
    assert rec["row_count"] == 3
    assert rec["record_layout"]
    # The preview reads the same records, and counts them from gzip's own
    # trailer rather than from the compressed size.
    assert store.preview_utmp_file(path)["record_count"] == 3


def test_secure_gz_dates_its_year_less_stamps_from_the_gzip_header(store, tmp_path):
    # logrotate compresses the log in place; gzip stores the ORIGINAL's
    # time, which is the only thing left that says which year the lines
    # are from. 2022-06-01 12:00:00 UTC.
    path = gz(tmp_path, "secure-20220601.gz",
              "Jun  1 03:04:05 host sshd[1]: Accepted password for dana from 10.0.0.7 port 22 ssh2\n",
              mtime=1654084800)
    os.utime(path, (1900000000, 1900000000))   # the .gz's own date: a later year
    rec = store.ingest_secure(path)
    assert rec["syslog_year"] == 2022
    assert rec["year_from"] == "modified"


def test_sqlite_gz_imports_the_tables_inside_it(store, tmp_path):
    import sqlite3

    plain = tmp_path / "History.db"
    con = sqlite3.connect(plain)
    con.execute("CREATE TABLE urls(id INTEGER, url TEXT)")
    con.execute("INSERT INTO urls VALUES (1, 'https://example.test')")
    con.commit()
    con.close()
    path = gz_bytes(tmp_path, "History.db.gz", plain.read_bytes())
    plain.unlink()
    # The picker lists the tables...
    names = [t["name"] for t in store.preview_sqlite_tables(path)["tables"]]
    assert names == ["urls"]
    # ...and one job takes them out of one decompressed copy.
    done = run_job(store, "sqlite", path, name="History.db.gz",
                   options={"tables": [{"table": "urls"}]})
    assert done["status"] == "done"
    src = store.get_source(done["source_ids"][0])
    # Named from the file inside, as a path import names it: <stem>.<table>.
    assert src["name"] == "History.urls"
    assert src["path"] == os.path.abspath(path)
    assert gunzip_tmpdirs() == []


def test_xlsx_gz_imports_the_sheets_inside_it(store, tmp_path):
    from openpyxl import Workbook

    plain = tmp_path / "book.xlsx"
    wb = Workbook()
    wb.active.title = "Sheet1"
    wb.active.append(["Host", "Note"])
    wb.active.append(["ws01", "west"])
    wb.save(plain)
    path = gz_bytes(tmp_path, "book.xlsx.gz", plain.read_bytes())
    plain.unlink()
    assert [t["name"] for t in store.preview_xlsx_sheets(path)["tables"]] == ["Sheet1"]
    done = run_job(store, "xlsx", path, name="book.xlsx.gz",
                   options={"tables": [{"table": "Sheet1"}]})
    assert done["status"] == "done"
    assert store.get_source(done["source_ids"][0])["row_count"] == 1


# --------------------------------------------- what the table says it is

def test_the_fingerprint_is_the_one_the_uncompressed_file_has(store, tmp_path):
    """Why it matters: a session names its tables by file_hash, so an
    analyst who was sent events.json.gz has to match a session recorded
    by the colleague who had events.json."""
    body = json.dumps([{"a": 1}, {"a": 2}])
    plain = tmp_path / "events.json"
    plain.write_bytes(body.encode())
    compressed = gz(tmp_path, "events.json.gz", body)
    assert store.ingest_json(str(plain))["file_hash"] == store.ingest_json(compressed)["file_hash"]


def test_a_session_re_imports_from_a_gz(store, case_path, tmp_path):
    """The whole loop: tag a table imported from a .gz, save the session,
    and have a fresh case re-import the file from the session alone —
    which checks the fingerprint against the file before reading it."""
    path = gz(tmp_path, "events.csv.gz", "Host,Note\nws01,west\nws02,east\n")
    rec = store.ingest_csv(path, name="events.csv.gz")
    tag = store.list_tags()[0]
    store.set_tags(rec["id"], [1], tag["id"], True)
    doc = store.export_case_session()

    other = Store(str(tmp_path / "other.db"), default_tags=None)
    try:
        report = other.import_case_session(doc)
        assert report["warnings"] == [], report
        assert report["sources_reimported"] == 1
        src = other.list_sources()[0]
        assert src["path"] == os.path.abspath(path)
        assert src["row_count"] == 2
        assert other.tag_counts(src["id"])
    finally:
        other.close()


def test_the_copy_is_gone_and_the_path_is_right_after_a_failed_read(store, tmp_path):
    # A .gz that decompresses cleanly into something the reader refuses:
    # the rows it kept (none here) and the temp copy must not outlive it.
    path = gz(tmp_path, "empty.json.gz", "")
    with pytest.raises(ValueError):
        store.ingest_json(path)
    assert store.list_sources() == []
    assert gunzip_tmpdirs() == []


def test_a_cut_short_gz_is_refused_with_its_name(store, tmp_path):
    full = gzip.compress(os.urandom(200_000))
    path = tmp_path / "cut.csv.gz"
    path.write_bytes(full[:5000])         # the download stopped here
    done = run_job(store, "csv", str(path), name="cut.csv.gz")
    assert done["status"] == "error"
    assert "cut.csv.gz" in done["error"]
    assert "damaged or cut short" in done["error"]
    # Nothing half-imported, and no copy left behind.
    assert store.list_sources() == []
    assert gunzip_tmpdirs() == []


def test_a_decompression_bomb_stops_at_the_cap(store, tmp_path, monkeypatch):
    monkeypatch.setattr(gzipped, "MAX_BYTES", 4096)
    path = gz(tmp_path, "bomb.csv.gz", "a,b\n" + "1,2\n" * 50_000)
    done = run_job(store, "csv", str(path), name="bomb.csv.gz")
    assert done["status"] == "error"
    assert "decompression bomb" in done["error"]
    assert store.list_sources() == []
    assert gunzip_tmpdirs() == []


def test_decompressing_reports_progress_and_can_be_stopped(tmp_path, monkeypatch):
    """The reason the copy is made with a callback at all: on a multi-GB
    .gz it runs for minutes before the first row exists, so it has to
    report progress and answer a cancel. Driven through the callback
    rather than against a background thread, so the stopping point is
    where this test says it is."""
    from winnow.store import IngestCancelled

    monkeypatch.setattr(gzipped, "_CHUNK", 4096)
    path = gz(tmp_path, "big.csv.gz", "a,b\n" + "1,2\n" * 50_000)
    seen = []
    dest = str(tmp_path / "copy")
    gzipped.decompress(path, dest, on_chunk=lambda done, total: seen.append((done, total)))
    assert len(seen) > 1                                   # it reports as it goes
    assert seen[-1][0] <= seen[-1][1] == os.path.getsize(path)   # compressed bytes read, of the file
    os.unlink(dest)

    # Whatever the callback raises stops the copy, and the half-written
    # file never survives: a truncated table reads as a complete one.
    def stop(done, total):
        raise IngestCancelled("cancelled while decompressing")

    with pytest.raises(IngestCancelled):
        gzipped.decompress(path, dest, on_chunk=stop)
    assert not os.path.exists(dest)


def test_a_cancel_during_decompression_cancels_the_job(store, tmp_path, monkeypatch):
    from winnow.store import IngestCancelled

    def cancelled_midway(*a, **kw):
        raise IngestCancelled("cancelled while decompressing")

    monkeypatch.setattr(gzipped, "decompress", cancelled_midway)
    path = gz(tmp_path, "events.csv.gz", "a,b\n1,2\n")
    done = run_job(store, "csv", str(path), name="events.csv.gz")
    assert done["status"] == "cancelled"
    assert store.list_sources() == []
    assert gunzip_tmpdirs() == []


def test_the_jobs_panel_sees_a_decompressing_phase(store, tmp_path):
    path = gz(tmp_path, "events.csv.gz", "a,b\n" + "1,2\n" * 100)
    job = store.start_ingest_job("csv", str(path), name="events.csv.gz")
    assert job["decompressing"] is False          # queued: nothing has started
    done = store.wait_for_ingest_job(job["job_id"], timeout=60)
    assert done["status"] == "done"
    assert done["decompressing"] is False         # and it is over by the end
    assert "decompressing" in store.list_ingest_jobs()[0]


def test_gzip_dates_the_copy_as_the_original(tmp_path):
    # Stamped by gzip, and left at 0 by logrotate's pipe (then the .gz's
    # own time is the best there is) — archive.py's rule, kept here.
    stamped = gz(tmp_path, "a.log.gz", "x\n", mtime=1654084800)
    piped = gz(tmp_path, "b.log.gz", "x\n", mtime=0)
    os.utime(piped, (1700000000, 1700000000))
    assert gzipped.mtime(stamped) == 1654084800
    assert gzipped.mtime(piped) == 1700000000
    with gzipped.decompressed(stamped) as real:
        assert os.path.basename(real) == "a.log"
        assert int(os.path.getmtime(real)) == 1654084800


# ------------------------------------------------------------ the folder scan

def test_a_folder_scan_matches_by_the_inner_extension(store, tmp_path):
    tmp_path = tmp_path / "triage"      # not beside the case file the fixture made
    tmp_path.mkdir()
    gz(tmp_path, "events.csv.gz", "a,b\n1,2\n")
    gz(tmp_path, "events.json.gz", '{"a": 1}\n')
    gz(tmp_path, "auth.log.1.gz", "Jun  1 03:04:05 host sshd[1]: x\n")
    gz(tmp_path, "notes.docx.gz", "nothing Winnow reads\n")
    scan = store.scan_import_directory(str(tmp_path))
    by_name = {os.path.basename(m["path"]): m["kind"] for m in scan["matched"]}
    assert by_name == {"events.csv.gz": "csv", "events.json.gz": "json", "auth.log.1.gz": "secure"}
    assert [os.path.basename(e["path"]) for e in scan["excluded"]] == ["notes.docx.gz"]


def test_a_folder_scan_leaves_tarballs_to_the_archive_flow(store, tmp_path):
    tmp_path = tmp_path / "triage"
    tmp_path.mkdir()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("a.csv")
        info.size = 4
        tf.addfile(info, io.BytesIO(b"a,b\n"))
    (tmp_path / "bundle.tar.gz").write_bytes(buf.getvalue())
    scan = store.scan_import_directory(str(tmp_path))
    assert scan["matched"] == []
    assert scan["excluded"][0]["reason"] == "extension"


def test_a_gzipped_log_is_not_mistaken_for_a_binary(store, tmp_path):
    """Compressed bytes are full of NULs, so the "other text files" gate
    turned every .gz away before the probes read inside them."""
    tmp_path = tmp_path / "triage"
    tmp_path.mkdir()
    gz(tmp_path, "messages.gz", "Jun  1 03:04:05 host kernel: up\n")
    gz(tmp_path, "fw.gz", FORTI + "\n")
    gz_bytes(tmp_path, "tool.exe.gz", b"MZ\x90\x00" + b"\x00" * 4096)
    scan = store.scan_import_directory(str(tmp_path), extensions=["*"])
    by_name = {os.path.basename(m["path"]): m["kind"] for m in scan["matched"]}
    assert by_name == {"messages.gz": "text", "fw.gz": "kv"}
    assert [(os.path.basename(e["path"]), e["reason"]) for e in scan["excluded"]] \
        == [("tool.exe.gz", "binary")]


# ------------------------------------------------------------ the HTTP layer

def test_recognise_names_the_readers_inside_a_gz(client, tmp_path):
    paths = [gz(tmp_path, "fw.gz", FORTI + "\n"),
             gz(tmp_path, "listing.gz", BODY_LINE + "\n"),
             gz(tmp_path, "plain.gz", "nothing in particular\n")]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("a.csv")
        info.size = 4
        tf.addfile(info, io.BytesIO(b"a,b\n"))
    tarred = tmp_path / "collection.gz"
    tarred.write_bytes(buf.getvalue())
    r = client.post("/api/ingest/recognise", json={"paths": [*paths, str(tarred)]})
    assert r.status_code == 200
    assert r.json()["kinds"] == ["kv", "bodyfile", None, "archive"]


def test_the_path_preview_reads_inside_the_gz(client, tmp_path):
    path = gz(tmp_path, "events.csv.gz", "Host,Note\nws01,west\n")
    r = client.post("/api/ingest/preview/path", json={"path": path})
    assert r.status_code == 200, r.text
    assert r.json()["columns"] == ["Host", "Note"]
    assert r.json()["sample_rows"] == [["ws01", "west"]]
    # A UTF-16 export compressed for mailing: the encoding is decided from
    # what is inside the .gz, not from its compressed bytes.
    wide = tmp_path / "utf16.csv.gz"
    with open(wide, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb") as g:
        g.write("Host,Note\nws01,café\n".encode("utf-16"))
    r = client.post("/api/ingest/preview/path", json={"path": str(wide)})
    assert r.status_code == 200, r.text
    assert r.json()["sample_rows"] == [["ws01", "café"]]


def test_the_upload_preview_inflates_the_head_it_was_sent(client, tmp_path):
    path = gz(tmp_path, "events.csv.gz", "Host,Note\nws01,west\nws02,east\n")
    with open(path, "rb") as f:
        r = client.post("/api/ingest/preview", files={"file": ("events.csv.gz", f.read())},
                        data={"kind": "csv"})
    assert r.status_code == 200, r.text
    assert r.json()["columns"] == ["Host", "Note"]


def test_the_by_path_job_route_imports_a_gz(client, store, tmp_path):
    path = gz(tmp_path, "events.csv.gz", "Host,Note\nws01,west\n")
    r = client.post("/api/ingest/jobs/path", json={"path": path, "name": "events.csv.gz"})
    assert r.status_code == 200, r.text
    done = store.wait_for_ingest_job(r.json()["job_id"], timeout=60)
    assert done["status"] == "done"
    assert store.get_source(done["source_ids"][0])["path"] == os.path.abspath(path)


def test_an_upload_records_the_name_the_browser_sent(client, store, tmp_path):
    path = gz(tmp_path, "events.csv.gz", "Host,Note\nws01,west\n")
    with open(path, "rb") as f:
        r = client.post("/api/ingest/jobs/upload", files={"file": ("events.csv.gz", f.read())},
                        data={"kind": "csv"})
    assert r.status_code == 200, r.text
    done = store.wait_for_ingest_job(r.json()["job_id"], timeout=60)
    assert done["status"] == "done"
    src = store.get_source(done["source_ids"][0])
    # Not the spool, and not the decompressed copy: both are gone by now.
    assert src["path"] == "events.csv.gz"
    assert src["row_count"] == 1
    assert gunzip_tmpdirs() == []


def test_the_job_route_refuses_a_tarball_and_says_what_to_do(client, tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("a.csv")
        info.size = 4
        tf.addfile(info, io.BytesIO(b"a,b\n"))
    path = tmp_path / "collection.gz"
    path.write_bytes(buf.getvalue())
    r = client.post("/api/ingest/jobs/path", json={"path": str(path)})
    assert r.status_code == 400
    assert "collection.gz is an archive" in r.json()["detail"]
    assert "expands" in r.json()["detail"]


def test_a_gzipped_archive_still_expands_the_old_way(client, tmp_path):
    """The archive flow is untouched: a .tar.gz still expands into a tree,
    and a bare rotated .gz inside one still becomes a file."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (("var/log/auth.log", b"now\n"),
                           ("var/log/auth.log.1.gz", gzip.compress(b"then\n"))):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    path = tmp_path / "bundle.tar.gz"
    path.write_bytes(buf.getvalue())
    r = client.post("/api/ingest/archive/expand", json={"path": str(path)})
    assert r.status_code == 200, r.text
    rep = r.json()
    # Three: the two members, and the file the inner .gz expanded to.
    assert rep["files"] == 3
    assert sorted(os.listdir(os.path.join(rep["root"], "var", "log"))) == ["auth.log", "auth.log.1"]
