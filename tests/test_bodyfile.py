"""Body files (winnow/bodyread.py, Store.ingest_bodyfile): the
MD5|name|inode|mode|UID|GID|size|atime|mtime|ctime|crtime listing that
TSK's fls -m writes and mactime reads, which UAC collects as
bodyfile.txt. Recognized by name (bodyfile, bodyfile.txt, *.body) and by
the first line wherever a file's type is decided (the server's router,
the folder scan, Open With, the recognize route, session re-import), and
read into one table, a row per line, under the body file's own headers
(TSK's names, or the file's header line when it has one), with the four
times in UTC.

The load-bearing tests are the pipe in a name and the TSK 2.x line. A
reader that splits on every pipe passes every test made of ordinary
paths and shifts the size into the mode column on the first file whose
name holds one; a reader that only checks the field types reads a 2.x
line, all of whose fields are numbers, one or two columns off and never
says so.
"""
from __future__ import annotations

import pytest

from winnow import bodyread
from winnow import log as wlog
from winnow.store import BODYFILE_IMPORT_TYPE, IngestCancelled, Store

# fls -m on an ext4 image: crtime recorded.
PASSWD = "0|/etc/passwd|1835063|r/rrw-r--r--|0|0|2913|1714557598|1714473600|1714473700|1714000000"
# fls -m on NTFS: the inode is entry-type-id, the name a $FILE_NAME entry.
NTFS = ("0|C:/Users/alice/AppData/Local/Temp/evil.exe ($FILE_NAME)|102345-48-2|r/rrwxrwxrwx|0|0|72704"
        "|1714557598|1714557598|1714557598|1714557598")
# A deleted entry: inode 0, no mode, no times.
DELETED = "0|/tmp/.x (deleted)|0|r/----------|0|0|0|0|0|0|0"
# UAC's find -printf: a mode without the name type, ten-digit fractions,
# no birth time.
UAC = ("0|/home/alice/.bash_history|2621455|-rw-------|1000|1000|5532"
       "|1714557598.1234567890|1714473600.9999999990|1714473600.0000000000|0")
# A symlink, as fls writes it, and an MD5 (fls -m with a hash database).
LINK = "d41d8cd98f00b204e9800998ecf8427e|/bin/sh -> dash|1835200|l/lrwxrwxrwx|0|0|4|1700000000|1700000000|1700000000|0"
# Volatility's timeliner: a process, no inode, a fifteen-character mode.
VOL = ("0|[PROCESS] lsass.exe PID: 512/PPID: 400/POffset: 0x01e9b020|0|---------------|0|0|0"
       "|1300000000|1300000000|1300000000|1300000000")
# A name with pipes in it, which Linux allows.
PIPES = "0|/tmp/a|b|c|1835300|r/rrw-r--r--|0|0|10|1714557598|1714557598|1714557598|0"
# The same entry in TSK 2.x's sixteen fields.
TSK2 = "0|/etc/passwd|2049|1835063|33188|-/-rw-r--r--|1|0|0|0|2913|1714557598|1714473600|1714473700|4096|8"

BODY = [PASSWD, NTFS, DELETED, UAC, LINK, VOL, PIPES]

# The header line some tools write above the first file, in TSK's words,
# and one in a tool's own.
HEADER = "MD5|name|inode|mode_as_string|UID|GID|size|atime|mtime|ctime|crtime"
OWN_HEADER = "md5|file|st_ino|st_ls|st_uid|st_gid|st_size|st_atime|st_mtime|st_ctime|st_crtime"


def _write(tmp_path, name, lines, *, raw=None, encoding="utf-8", newline="\n"):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        p.write_bytes(raw)
    else:
        p.write_text(newline.join(lines) + newline, encoding=encoding, newline="")
    return str(p)


def _rows(store, rec):
    cols = [c["name"] for c in rec["columns"]]
    return [dict(zip(cols, tuple(r)[1:]))
            for r in store.db.execute(f"SELECT * FROM src_{rec['id']} ORDER BY rid")]


# ------------------------------------------------------------ recognition

@pytest.mark.parametrize("line", BODY + [
    "0|/x|1|r/r---------|0|0|0|-|-|-|-",          # stat's "-" for an unknown time
    "0|/x|||||||||",                               # every field after the name empty
    "|/x|1|r/r---------|0|0|0|1|1|1|1",            # no MD5 at all
    "0|/x|1|r/r---------|0|0|0|-86400|1|1|1",      # a time before 1970
    "\n\n  " + PASSWD,                             # blank lines first
    "\ufeff" + PASSWD,                             # a BOM
    PASSWD + "\r\n" + NTFS,                        # CRLF
    HEADER + "\n" + PASSWD,                         # a header line first
    "\ufeff" + HEADER + "\r\n\r\n" + PASSWD,         # …after a BOM, before a blank line
    OWN_HEADER + "\n" + PASSWD,                     # a header with names of its own
])
def test_body_file_lines_are_recognized(line):
    assert bodyread.looks_like(line)


@pytest.mark.parametrize("text", [
    "", "\n\n",
    HEADER, HEADER + "\n",                                # a header and nothing under it
    HEADER + "\nfls: error\n" + PASSWD,                   # a header, then something else
    HEADER + "\n" + HEADER + "\n" + PASSWD,               # two headers
    "MD5|name|inode|mode|UID|GID|size|atime|mtime|ctime\n" + PASSWD,   # ten names
    "MD5|name|inode|mode|UID|GID|size|1|mtime|ctime|crtime\n" + PASSWD,  # a number for a name
    "path|name|inode|mode|UID|GID|size|atime|mtime|ctime|crtime\n" + PASSWD,  # no hash first
    "Date,Size,Type,Mode,UID,GID,Meta,File Name\n"                                    # mactime's CSV
    "Wed May 01 2024 09:59:58,2913,.a..,r/rrw-r--r--,0,0,1835063,/etc/passwd",
    "a|b|c\n1|2|3",                                       # a pipe-delimited table
    "0|/x|1|r|0|0|big|1|1|1|1",                           # a size that isn't a number
    "0|/x|1|r|0|0|1|yesterday|1|1|1",                     # a time that isn't one
    "zz|/x|1|r|0|0|1|1|1|1|1",                            # an MD5 that isn't hex
    "0|/x|1|r|0|0|1|1|1|1",                               # ten fields
    "0|/x|1|r|0|0|1|1e9|1|1|1",                           # exponent notation
    "0|/x|1|r|0|0|1|١٧١٤٥٥٧٥٩٨|1|1|1",                    # digits, but not ASCII ones
    TSK2,
    "date=2026-10-23 time=05:39:00 devname=FG x=1",
    "Oct 23 05:39:00 host sshd[42]: Accepted password for root from 10.0.0.1",
    "some header line\n" + PASSWD,
])
def test_other_text_is_not(text):
    assert not bodyread.looks_like(text)


@pytest.mark.parametrize("name", [
    "bodyfile", "bodyfile.txt", "BODYFILE.TXT", "Bodyfile", "bodyfile (2).txt", "bodyfile (2)",
    "mft.body", "timeliner.body", "host1.BODY", "/mnt/uac/bodyfile/bodyfile.txt",
    r"C:\cases\uac-host1\bodyfile\bodyfile.txt",
])
def test_body_file_names_are_recognized(name):
    assert bodyread.name_says(name)


@pytest.mark.parametrize("name", [
    "body", "body.txt", "bodyfile.gz", "bodyfile.csv", "bodyfile.txt.gz", "mybodyfile.txt",
    "notes.txt", ".body", "x.body.txt", "bodyfile.d",
])
def test_other_names_are_not(name):
    assert not bodyread.name_says(name)


# --------------------------------------------------------- the fields

def test_each_field_lands_in_its_column():
    """The body file's own headers, in its own order: TSK's names for the
    eleven fields, which are what a header line says when a file has one."""
    assert bodyread.COLUMNS == HEADER.split("|")
    row = dict(zip(bodyread.COLUMNS, bodyread.split_line(PASSWD)))
    assert row == {
        "MD5": "0", "name": "/etc/passwd", "inode": "1835063", "mode_as_string": "r/rrw-r--r--",
        "UID": "0", "GID": "0", "size": "2913",
        "atime": "2024-05-01 09:59:58", "mtime": "2024-04-30 10:40:00",
        "ctime": "2024-04-30 10:41:40", "crtime": "2024-04-24 23:06:40"}
    ntfs = dict(zip(bodyread.COLUMNS, bodyread.split_line(NTFS)))
    assert ntfs["inode"] == "102345-48-2" and ntfs["name"].endswith("evil.exe ($FILE_NAME)")
    link = dict(zip(bodyread.COLUMNS, bodyread.split_line(LINK)))
    assert link["MD5"] == "d41d8cd98f00b204e9800998ecf8427e" and link["name"] == "/bin/sh -> dash"


def test_a_name_can_hold_a_pipe():
    row = dict(zip(bodyread.COLUMNS, bodyread.split_line(PIPES)))
    assert row["name"] == "/tmp/a|b|c"
    assert (row["inode"], row["mode_as_string"], row["size"]) == ("1835300", "r/rrw-r--r--", "10")


def test_a_tsk2_line_is_not_read_one_column_off():
    """Every one of a 2.x line's fields after the name is a number in the
    place 3.x expects one, so only its shape can say what it is."""
    assert bodyread.split_line(TSK2) is None
    assert not bodyread.is_line(TSK2)


@pytest.mark.parametrize("value, want", [
    ("1714557598", "2024-05-01 09:59:58"),
    ("1714557598.1234567890", "2024-05-01 09:59:58.123456"),
    ("1714557598.9999999999", "2024-05-01 09:59:58.999999"),    # truncated, never rounded up
    ("1714557598.0000000000", "2024-05-01 09:59:58.000000"),    # .000000 is a tell: kept
    ("1714557598.5", "2024-05-01 09:59:58.500000"),
    ("0", ""), ("0.0000000000", ""), ("-", ""), ("", ""),       # not recorded
    ("1", "1970-01-01 00:00:01"),
    ("-1", "1969-12-31 23:59:59"),
    ("-1.25", "1969-12-31 23:59:58.750000"),
    ("-86400", "1969-12-31 00:00:00"),
    ("4102444800", "2100-01-01 00:00:00"),
    ("32503680000", "3000-01-01 00:00:00"),                     # past gmtime's fast path
    ("253402300799", "9999-12-31 23:59:59"),
    ("253402300800", "253402300800"),                           # no real time: kept as written
    ("-62135596801", "-62135596801"),
    ("99999999999999999999999", "99999999999999999999999"),
])
def test_times_are_utc_text_or_kept_as_written(value, want):
    assert bodyread.epoch_text(value) == want


# ------------------------------------------------------------ the import

def test_a_body_file_imports_a_row_per_line(store, tmp_path):
    rec = store.ingest_bodyfile(_write(tmp_path, "bodyfile.txt", BODY), build_fts=False)
    assert rec["row_count"] == len(BODY) and rec["unparsed_lines"] == 0
    assert [(c["name"], c["type"]) for c in rec["columns"]] == list(zip(bodyread.COLUMNS, bodyread.COLUMN_TYPES))
    rows = _rows(store, rec)
    assert [r["name"] for r in rows] == [
        "/etc/passwd", NTFS.split("|")[1], "/tmp/.x (deleted)", "/home/alice/.bash_history",
        "/bin/sh -> dash", VOL.split("|")[1], "/tmp/a|b|c"]
    deleted, uac = rows[2], rows[3]
    assert [deleted[c] for c in ("atime", "mtime", "ctime", "crtime")] == ["", "", "", ""]
    assert (uac["atime"], uac["mtime"], uac["ctime"], uac["crtime"]) == (
        "2024-05-01 09:59:58.123456", "2024-04-30 10:40:00.999999", "2024-04-30 10:40:00.000000", "")
    assert store.get_source(rec["id"])["ingest"] == {"kind": "bodyfile"}


def test_a_body_file_table_gets_its_timeline_summary(store, tmp_path):
    rec = store.ingest_bodyfile(_write(tmp_path, "bodyfile", BODY), build_fts=False)
    match = store._timeline_summary_match(store.get_source(rec["id"]))
    assert match is not None and match[0] == "Body files (TSK / mactime)"


def test_lines_that_are_not_body_lines_are_kept_whole(store, tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(wlog, "record", lambda level, msg: logged.append((level, msg)))
    rec = store.ingest_bodyfile(_write(tmp_path, "mixed.body", [PASSWD, "fls: error reading inode 12", TSK2, NTFS]),
                                build_fts=False)
    assert [c["name"] for c in rec["columns"]] == bodyread.COLUMNS + ["Unparsed line"]
    assert rec["unparsed_lines"] == 2
    rows = _rows(store, rec)
    assert [r["Unparsed line"] for r in rows] == ["", "fls: error reading inode 12", TSK2, ""]
    assert rows[1]["name"] == "" and rows[3]["name"].endswith("($FILE_NAME)")
    assert any(level == "warn" and "2 lines were not a body file line" in msg for level, msg in logged)
    # A file with none gets no such column.
    clean = store.ingest_bodyfile(_write(tmp_path, "clean.body", [PASSWD]), build_fts=False)
    assert "Unparsed line" not in [c["name"] for c in clean["columns"]]


def test_crlf_bom_and_ansi_files_read_as_written(store, tmp_path):
    rec = store.ingest_bodyfile(_write(tmp_path, "crlf.body", [PASSWD, NTFS], encoding="utf-8-sig", newline="\r\n"),
                                build_fts=False)
    rows = _rows(store, rec)
    assert rec["unparsed_lines"] == 0 and rows[0]["MD5"] == "0" and rows[1]["crtime"] == "2024-05-01 09:59:58"
    # A name in Windows-1252 (an image mounted on Windows, a collector that
    # wrote ANSI) is a character, not U+FFFD.
    raw = "0|/srv/caf\u00e9.txt|12|r/rrw-r--r--|0|0|1|1714557598|1714557598|1714557598|0\n".encode("cp1252")
    rec = store.ingest_bodyfile(_write(tmp_path, "ansi.body", None, raw=raw), build_fts=False)
    assert _rows(store, rec)[0]["name"] == "/srv/caf\u00e9.txt"


def test_a_header_line_names_the_columns_and_is_no_row(store, tmp_path):
    """A header line above the first file is the table's headers, not a
    row and not an Unparsed line; a header in a tool's own words keeps
    those words; and a copy of it further down (body files joined with
    cat) is skipped as the first one was."""
    rec = store.ingest_bodyfile(_write(tmp_path, "with-header.body", [HEADER] + BODY), build_fts=False)
    assert [c["name"] for c in rec["columns"]] == bodyread.COLUMNS
    assert rec["row_count"] == len(BODY) and rec["unparsed_lines"] == 0
    assert _rows(store, rec)[0]["name"] == "/etc/passwd"

    rec = store.ingest_bodyfile(_write(tmp_path, "own.body", [OWN_HEADER, PASSWD, OWN_HEADER, NTFS]),
                                build_fts=False)
    assert [(c["name"], c["type"]) for c in rec["columns"]] == list(zip(OWN_HEADER.split("|"), bodyread.COLUMN_TYPES))
    assert rec["row_count"] == 2 and rec["unparsed_lines"] == 0
    assert [r["st_mtime"] for r in _rows(store, rec)] == ["2024-04-30 10:40:00", "2024-05-01 09:59:58"]

    # Only the first line is a header: one further down that isn't a copy
    # of it is a line like any other, kept whole.
    rec = store.ingest_bodyfile(_write(tmp_path, "late.body", [PASSWD, HEADER]), build_fts=False)
    assert [c["name"] for c in rec["columns"]] == bodyread.COLUMNS + ["Unparsed line"]
    assert rec["unparsed_lines"] == 1 and _rows(store, rec)[1]["Unparsed line"] == HEADER

    # A header with no file under it is still no body file.
    with pytest.raises(ValueError, match="no body file lines"):
        store.ingest_bodyfile(_write(tmp_path, "only.body", [HEADER]))

    # The preview reads it the same way, and so does the router.
    body = Store.preview_bodyfile_text(OWN_HEADER + "\n" + PASSWD + "\n")
    assert body["columns"] == OWN_HEADER.split("|") and len(body["sample_rows"]) == 1
    import server
    assert server._ingest_kind_for_path(_write(tmp_path, "fls-with-header.txt", [HEADER, PASSWD])) == "bodyfile"


def test_a_file_with_no_body_lines_is_refused(store, tmp_path):
    with pytest.raises(ValueError, match="File is empty"):
        store.ingest_bodyfile(_write(tmp_path, "empty.body", None, raw=b""))
    with pytest.raises(ValueError, match="no body file lines"):
        store.ingest_bodyfile(_write(tmp_path, "prose.txt", ["just words", "a|b|c"]))
    assert store.list_sources() == []


def test_cancel_leaves_no_table(store, tmp_path):
    p = _write(tmp_path, "big.body", [PASSWD] * 45_000)
    for calls_before_yes in (0, 2):   # pass one (before the table exists), then pass two
        seen = [0]

        def cancel():
            seen[0] += 1
            return seen[0] > calls_before_yes

        with pytest.raises(IngestCancelled):
            store.ingest_bodyfile(p, build_fts=False, cancel=cancel)
        assert store.list_sources() == []


def test_a_line_appended_between_the_passes_is_not_read(store, tmp_path, monkeypatch):
    """fls still writing into the file: the second pass reads only the
    lines the first one planned for, so a line the plan never saw (here,
    one that would need the Unparsed line column) can't arrive mid-insert."""
    p = _write(tmp_path, "live.body", [PASSWD, NTFS])
    real = bodyread.plan

    def plan_then_grow(lines):
        out = real(lines)
        with open(p, "a", newline="") as f:
            f.write("fls: interrupted\n")
        return out

    monkeypatch.setattr(bodyread, "plan", plan_then_grow)
    rec = store.ingest_bodyfile(p, build_fts=False)
    assert rec["row_count"] == 2 and "Unparsed line" not in [c["name"] for c in rec["columns"]]


def test_the_preview_is_the_start_of_the_table():
    body = Store.preview_bodyfile_text("\n".join(BODY) + "\n")
    assert body["columns"] == bodyread.COLUMNS and body["inferred_types"] == bodyread.COLUMN_TYPES
    assert len(body["sample_rows"]) == len(BODY) and body["unparsed_lines"] == 0 and body["complete"] is True
    # A sample that isn't the whole file loses its last, maybe cut, line.
    body = Store.preview_bodyfile_text(PASSWD + "\n" + NTFS[:40], complete=False)
    assert len(body["sample_rows"]) == 1 and body["complete"] is False
    body = Store.preview_bodyfile_text(PASSWD + "\nnot one\n")
    assert body["columns"][-1] == "Unparsed line" and body["unparsed_lines"] == 1
    with pytest.raises(ValueError, match="No body file lines"):
        Store.preview_bodyfile_text("just words\n")


# ------------------------------------------------------------- routing

def test_the_server_routes_body_files_to_their_reader(tmp_path):
    import server

    # By name, whatever is in them.
    for name in ("bodyfile", "bodyfile.txt", "mft.body"):
        assert server._ingest_kind_for_path(_write(tmp_path, name, [PASSWD])) == "bodyfile", name
    # By the first line, for a name no reader claims.
    for name in ("fls-output.txt", "fls", "disk1.log"):
        assert server._ingest_kind_for_path(_write(tmp_path, name, [PASSWD])) == "bodyfile", name
    # Never a file an extension or a name gives to another reader…
    assert server._ingest_kind_for_path(_write(tmp_path, "body.csv", [PASSWD])) == "csv"
    assert server._ingest_kind_for_path(_write(tmp_path, "u_ex240501.log", [PASSWD])) == "iis"
    # …and only when the first line says so.
    assert server._ingest_kind_for_path(_write(tmp_path, "later.txt", ["x", PASSWD])) == "csv"
    assert Store._guess_session_reader("/e/bodyfile.txt") == {"kind": "bodyfile"}
    assert Store._guess_session_reader("/e/fls.txt") == {"kind": "csv"}


def test_the_recognise_route_names_body_files(client, tmp_path):
    paths = [_write(tmp_path, "fls.txt", [PASSWD]), _write(tmp_path, "plain.txt", ["words"]),
             _write(tmp_path, "fortigate.log", ["date=2026-10-23 time=05:39:00 devname=FG x=1"])]
    r = client.post("/api/ingest/recognise", json={"paths": paths})
    assert r.status_code == 200, r.text
    assert r.json() == {"kinds": ["bodyfile", None, "kv"]}


def test_folder_scan_matches_body_files_by_name_and_first_line(store, tmp_path):
    root = tmp_path / "uac"
    _write(root, "bodyfile/bodyfile.txt", BODY)
    _write(root, "vol/timeliner.body", [VOL])
    _write(root, "live_response/fls-sda1.txt", [PASSWD])
    _write(root, "live_response/ps.txt", ["PID TTY TIME CMD", "1 ? 00:00:01 systemd"])
    _write(root, "fls-sda2", [PASSWD])

    r = store.scan_import_directory(str(root))   # the defaults include body files
    assert {m["rel_path"]: m["kind"] for m in r["matched"]} == {
        "bodyfile/bodyfile.txt": "bodyfile", "vol/timeliner.body": "bodyfile",
        "live_response/fls-sda1.txt": "bodyfile", "live_response/ps.txt": "csv"}
    # A list without the token (a profile saved before the chip existed)
    # leaves the named ones out, and says why; a .txt still gets in by
    # its extension and is still read by its first line.
    r = store.scan_import_directory(str(root), extensions=[".txt"])
    reasons = {e["rel_path"]: e["reason"] for e in r["excluded"]}
    assert reasons["vol/timeliner.body"] == "body files not selected"
    assert reasons["fls-sda2"] == "extension"
    kinds = {m["rel_path"]: m["kind"] for m in r["matched"]}
    assert kinds["bodyfile/bodyfile.txt"] == "bodyfile" and kinds["live_response/fls-sda1.txt"] == "bodyfile"
    r = store.scan_import_directory(str(root), extensions=[BODYFILE_IMPORT_TYPE])
    assert {m["rel_path"] for m in r["matched"]} == {"bodyfile/bodyfile.txt", "vol/timeliner.body"}
    # "Other text files" lets in the extensionless one, and its first line
    # says what it is.
    r = store.scan_import_directory(str(root), extensions=["*"])
    assert {m["rel_path"]: m["kind"] for m in r["matched"]}["fls-sda2"] == "bodyfile"


# -------------------------------------------------------- routes and jobs

def test_the_jobs_routes_import_a_body_file(client, store, tmp_path):
    p = _write(tmp_path, "bodyfile.txt", BODY + ["fls: error"])
    r = client.post("/api/ingest/jobs/path", json={"path": p})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "bodyfile"
    done = store.wait_for_ingest_job(r.json()["job_id"], timeout=30)
    assert done["status"] == "done", done
    assert done["result"][0]["unparsed_lines"] == 1
    assert store.get_source(done["source_ids"][0])["row_count"] == len(BODY) + 1
    # …and the upload transport, whose spool keeps the file's name.
    with open(p, "rb") as f:
        r = client.post("/api/ingest/jobs/upload", data={"kind": "bodyfile"},
                        files={"file": ("host1.body", f, "text/plain")})
    assert r.status_code == 200, r.text
    done = store.wait_for_ingest_job(r.json()["job_id"], timeout=30)
    assert done["status"] == "done", done
    src = store.get_source(done["source_ids"][0])
    assert src["name"] == "host1.body" and src["columns"][0]["name"] == "MD5"


def test_the_previews_read_a_body_file(client, tmp_path, monkeypatch):
    import server

    p = _write(tmp_path, "fls.txt", BODY)
    body = client.post("/api/ingest/preview/path", json={"path": p}).json()   # kind from the first line
    assert body["columns"] == bodyread.COLUMNS and len(body["sample_rows"]) == len(BODY)
    assert body["sample_rows"][0][1] == "/etc/passwd"
    # Read as Body file: any text file, asked for by kind.
    other = _write(tmp_path, "renamed.csv", BODY)
    assert client.post("/api/ingest/preview/path",
                       json={"path": other, "kind": "bodyfile"}).json()["columns"] == bodyread.COLUMNS
    with open(p, "rb") as f:
        r = client.post("/api/ingest/preview", data={"kind": "bodyfile"}, files={"file": ("fls.txt", f, "text/plain")})
    assert r.status_code == 200, r.text
    assert r.json()["inferred_types"] == bodyread.COLUMN_TYPES
    r = client.post("/api/ingest/preview", data={"kind": "bodyfile"},
                    files={"file": ("notes.txt", b"just words\n", "text/plain")})
    assert r.status_code == 400 and "No body file lines" in r.json()["detail"]
    monkeypatch.setattr(server, "PREVIEW_SAMPLE_BYTES", 200)
    assert client.post("/api/ingest/preview/path", json={"path": p, "kind": "bodyfile"}).json()["complete"] is False


def test_open_with_prefers_a_body_file_over_a_plugin_claiming_log(client, store, tmp_path, monkeypatch):
    """esxi_logs claims .log; a double-clicked fls listing saved as .log is
    still a body file."""
    import server

    claimed = []
    monkeypatch.setattr(server.PLUGINS, "format_for_filename",
                        lambda name: claimed.append(name) or object())
    out = server._assoc_ingest(_write(tmp_path, "fls.log", BODY))
    assert out["kind"] == "bodyfile" and claimed == []
    assert store.wait_for_ingest_job(out["job_id"], timeout=30)["status"] == "done"


def test_a_session_reimports_a_body_file_as_one(store, tmp_path):
    p = _write(tmp_path, "fls.txt", BODY)
    sid = store.ingest_bodyfile(p, build_fts=False)["id"]
    tag = store.upsert_tag(None, "Dropped binary", "#ff0000", None)["id"]
    store.set_tags(sid, [2], tag, True)
    session = store.export_case_session()

    other = Store(str(tmp_path / "b.db"))
    try:
        res = other.import_case_session(session)
        assert res["sources_reimported"] == 1 and res["warnings"] == []
        src = other.list_sources()[0]
        assert src["ingest"] == {"kind": "bodyfile"}
        rid = other.db.execute("SELECT rid FROM row_tags WHERE source_id=?", (src["id"],)).fetchone()[0]
        name = other.db.execute(f'SELECT "name" FROM {src["table_name"]} WHERE rid=?', (rid,)).fetchone()[0]
        assert name.endswith("evil.exe ($FILE_NAME)")
    finally:
        other.close()
