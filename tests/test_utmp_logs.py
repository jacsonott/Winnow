"""Linux login records (wtmp, btmp, utmp): every record a row, read in the
layout the file's own bytes say — 384 or 400 bytes a record, either byte
order — recognized by name (and, renamed, by their records), and routed
as their own ingest kind end to end: the server's router, the folder
scan, the jobs routes, both previews, Open With, session re-import.

The records here are built field by field at glibc's offsets (checked
against a C compiler's offsetof for both forms of struct utmp), not with
utmpread's own struct strings — a test that packed with the reader's
format would agree with any mistake in it. Where util-linux's utmpdump
is installed, one test also has it write the file."""

from __future__ import annotations

import io
import os
import shutil
import struct
import subprocess

import pytest

from winnow import log as wlog
from winnow import utmpread
from winnow.store import UTMP_IMPORT_TYPE, IngestCancelled, Store


# ---------------------------------------------------------------- fixtures

def record(layout: str, typ: int, *, pid: int = 0, line: bytes = b"", ut_id: bytes = b"",
           user: bytes = b"", host: bytes = b"", term: int = 0, exit_: int = 0, session: int = 0,
           sec: int = 0, usec: int = 0, addr: bytes = b"") -> bytes:
    """One struct utmp. Offsets are glibc's: ut_type 0, ut_pid 4, ut_line
    8, ut_id 40, ut_user 44, ut_host 76, ut_exit 332; then, with 32-bit
    time (384 bytes), ut_session 336, ut_tv 340/344, ut_addr_v6 348 — and
    with 64-bit time (400 bytes), ut_session 336, ut_tv 344/352,
    ut_addr_v6 360."""
    size, order = int(layout[:3]), "<" if layout.endswith("le") else ">"
    wide = size == 400
    buf = bytearray(size)
    struct.pack_into(order + "h", buf, 0, typ)
    struct.pack_into(order + "i", buf, 4, pid)
    for at, width, text in ((8, 32, line), (40, 4, ut_id), (44, 32, user), (76, 256, host)):
        buf[at:at + len(text[:width])] = text[:width]
    struct.pack_into(order + "hh", buf, 332, term, exit_)
    if wide:
        struct.pack_into(order + "qqq", buf, 336, session, sec, usec)
        buf[360:360 + len(addr)] = addr
    else:
        struct.pack_into(order + "iIi", buf, 336, session, sec, usec)
        buf[348:348 + len(addr)] = addr
    return bytes(buf)


T0 = 1_714_557_598   # 2024-05-01 09:59:58 UTC

# A boot, a console login prompt, two SSH logins (one over IPv6), one of
# them ending, and a slot nobody has used — as a wtmp has them.
SESSION = [
    dict(typ=2, line=b"~", ut_id=b"~~", user=b"reboot", host=b"6.1.0-18-amd64", sec=T0, usec=1),
    dict(typ=1, pid=0x35, line=b"~", ut_id=b"~~", user=b"runlevel", host=b"6.1.0-18-amd64",
         sec=T0 + 4, usec=500_000),
    dict(typ=6, pid=1234, line=b"tty1", ut_id=b"tty1", user=b"LOGIN", sec=T0 + 5),
    dict(typ=7, pid=4321, line=b"pts/0", ut_id=b"ts/0", user=b"root", host=b"203.0.113.9",
         session=4321, sec=T0 + 62, usec=123_456, addr=bytes([203, 0, 113, 9])),
    dict(typ=7, pid=4400, line=b"pts/1", ut_id=b"ts/1", user=b"alice", host=b"2001:db8::5",
         sec=T0 + 122, addr=bytes.fromhex("20010db8000000000000000000000005")),
    dict(typ=8, pid=4321, line=b"pts/0", ut_id=b"ts/0", term=0, exit_=255, sec=T0 + 3602, usec=654_321),
    dict(typ=0),
]
ROWS = [
    # Timestamp (UTC), Type, User, Terminal, Host, IP address, PID, ID, Session, Exit status, Termination status
    ["2024-05-01 09:59:58.000001", "BOOT_TIME", "reboot", "~", "6.1.0-18-amd64", "", "0", "~~", "0", "0", "0"],
    ["2024-05-01 10:00:02.500000", "RUN_LVL", "runlevel", "~", "6.1.0-18-amd64", "", "53", "~~", "0", "0", "0"],
    ["2024-05-01 10:00:03.000000", "LOGIN_PROCESS", "LOGIN", "tty1", "", "", "1234", "tty1", "0", "0", "0"],
    ["2024-05-01 10:01:00.123456", "USER_PROCESS", "root", "pts/0", "203.0.113.9", "203.0.113.9",
     "4321", "ts/0", "4321", "0", "0"],
    ["2024-05-01 10:02:00.000000", "USER_PROCESS", "alice", "pts/1", "2001:db8::5", "2001:db8::5",
     "4400", "ts/1", "0", "0", "0"],
    ["2024-05-01 11:00:00.654321", "DEAD_PROCESS", "", "pts/0", "", "", "4321", "ts/0", "0", "255", "0"],
    ["", "EMPTY", "", "", "", "", "0", "", "0", "0", "0"],
]
LAYOUT_KEYS = ["384-le", "400-le", "384-be", "400-be"]


def build(layout: str, records=SESSION) -> bytes:
    return b"".join(record(layout, **r) for r in records)


def write(tmp_path, name: str, data: bytes) -> str:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return str(p)


def table(store, source_id: int) -> list[list[str]]:
    src = store.get_source(source_id)
    cols = ", ".join(f'"{c["name"]}"' for c in src["columns"])
    return [list(r) for r in store.db.execute(f'SELECT {cols} FROM "{src["table_name"]}" ORDER BY rid')]


# ----------------------------------------------------------------- layouts

@pytest.mark.parametrize("layout", LAYOUT_KEYS)
def test_every_layout_reads_to_the_same_rows(store, tmp_path, layout):
    data = build(layout)
    assert utmpread.detect(data, len(data)).key == layout
    rec = store.ingest_utmp(write(tmp_path, "wtmp", data), build_fts=False)
    assert rec["record_layout"] == layout and rec["torn_bytes"] == 0
    size = int(layout[:3])
    want = [r + [str(i * size)] for i, r in enumerate(ROWS)]
    assert table(store, rec["id"]) == want
    src = store.get_source(rec["id"])
    assert [c["name"] for c in src["columns"]] == utmpread.COLUMNS
    assert [c["type"] for c in src["columns"]] == utmpread.COLUMN_TYPES
    assert src["row_count"] == len(SESSION)
    # The hover text says how the bytes were read.
    assert src["origin_meta"] == {"record_layout": utmpread.LAYOUTS_BY_KEY[layout].label}


def test_a_size_both_record_sizes_divide_is_settled_by_the_records(tmp_path):
    # 25 records of 384 bytes is 24 of 400: the size can't say which.
    recs = [dict(SESSION[3], sec=T0 + i) for i in range(25)]
    data = build("384-le", recs)
    assert len(data) % 400 == 0
    assert utmpread.detect(data, len(data)).key == "384-le"
    recs = [dict(SESSION[3], sec=T0 + i) for i in range(24)]
    assert utmpread.detect(build("400-be", recs), 9600).key == "400-be"


def test_a_file_with_nothing_to_judge_gets_the_layout_its_size_fits():
    assert utmpread.detect(bytes(384 * 3), 384 * 3).key == "384-le"
    assert utmpread.detect(bytes(400 * 3), 400 * 3).key == "400-le"


@pytest.mark.skipif(shutil.which("utmpdump") is None, reason="util-linux's utmpdump is not installed")
def test_records_util_linux_writes_read_as_utmpdump_reads_them(store, tmp_path):
    """utmpdump -r writes the machine's own struct utmp — 384 bytes on
    x86-64, 400 on 64-bit ARM — and the reader has to find either."""
    text = (
        "[2] [00000] [~~  ] [reboot  ] [~           ] [6.1.0-18-amd64      ] [0.0.0.0        ] [2024-05-01T09:59:58,000001+00:00]\n"
        "[7] [04321] [ts/0] [root    ] [pts/0       ] [203.0.113.9         ] [203.0.113.9    ] [2024-05-01T10:01:00,123456+00:00]\n"
        "[7] [04400] [ts/1] [alice   ] [pts/1       ] [2001:db8::5         ] [2001:db8::5    ] [2024-05-01T10:02:00,000000+00:00]\n"
        "[8] [04321] [ts/0] [        ] [pts/0       ] [                    ] [0.0.0.0        ] [2024-05-01T11:00:00,654321+00:00]\n"
    )
    out = tmp_path / "wtmp"
    with open(out, "wb") as fh:
        subprocess.run(["utmpdump", "-r"], input=text.encode(), stdout=fh, stderr=subprocess.DEVNULL, check=True)
    rec = store.ingest_utmp(str(out), build_fts=False)
    rows = [r[:8] for r in table(store, rec["id"])]
    assert rows == [
        ["2024-05-01 09:59:58.000001", "BOOT_TIME", "reboot", "~", "6.1.0-18-amd64", "", "0", "~~  "],
        ["2024-05-01 10:01:00.123456", "USER_PROCESS", "root", "pts/0", "203.0.113.9", "203.0.113.9", "4321", "ts/0"],
        ["2024-05-01 10:02:00.000000", "USER_PROCESS", "alice", "pts/1", "2001:db8::5", "2001:db8::5", "4400", "ts/1"],
        ["2024-05-01 11:00:00.654321", "DEAD_PROCESS", "", "pts/0", "", "", "4321", "ts/0"],
    ]


# ----------------------------------------------------------------- fields

def test_text_fields_keep_what_the_bytes_say():
    lay = utmpread.LAYOUTS_BY_KEY["384-le"]
    raw = record("384-le", 6, pid=77, line=b"ssh:notty", user=b"r\xf6\xf6t", host=b"198.51.100.7",
                 sec=T0, addr=bytes([198, 51, 100, 7]))
    row = next(utmpread.iter_rows(io.BytesIO(raw), lay, {}))
    # A btmp user name is whatever the other side sent: a byte that isn't
    # UTF-8 stays visible rather than becoming a replacement character.
    assert row[2] == "r\\xf6\\xf6t"
    # A field that fills its width has no NUL to stop at.
    raw = record("384-le", 7, user=b"u" * 32, line=b"pts/12345678901234567890123456789", sec=T0)
    row = next(utmpread.iter_rows(io.BytesIO(raw), lay, {}))
    assert row[2] == "u" * 32 and row[3] == "pts/12345678901234567890123456789"[:32]
    # A UTF-8 name reads as one.
    raw = record("384-le", 7, user="José".encode(), sec=T0)
    assert next(utmpread.iter_rows(io.BytesIO(raw), lay, {}))[2] == "José"


def test_addresses_and_times_that_say_nothing_or_too_much():
    lay = utmpread.LAYOUTS_BY_KEY["384-le"]

    def one(**kw):
        return next(utmpread.iter_rows(io.BytesIO(record("384-le", 7, **kw)), lay, {}))

    assert one(sec=T0, addr=bytes(16))[5] == ""
    assert one(sec=T0, addr=bytes.fromhex("00000000000000000000ffffcb007109"))[5] == "::ffff:203.0.113.9"
    # Microseconds past a second: the raw numbers, not a made-up time.
    assert one(sec=T0, usec=2_000_000)[0] == f"<invalid time: {T0} s, 2000000 us>"
    # glibc declares the 32-bit seconds unsigned (the year-2038 fix): past
    # 2038 is a time, not a negative number.
    assert one(sec=0x80000000)[0] == "2038-01-19 03:14:08.000000"
    # A type glibc doesn't define keeps its number.
    raw = record("384-le", 42, sec=T0)
    assert next(utmpread.iter_rows(io.BytesIO(raw), lay, {}))[1] == "UNKNOWN (42)"


def test_a_torn_tail_is_left_out_counted_and_logged(store, tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(wlog, "record", lambda level, msg: logged.append((level, msg)))
    data = build("384-le") + record("384-le", 7, user=b"cut", sec=T0)[:100]
    rec = store.ingest_utmp(write(tmp_path, "btmp", data), build_fts=False)
    assert rec["row_count"] == len(SESSION) and rec["torn_bytes"] == 100
    assert store.get_source(rec["id"])["origin_meta"]["torn_bytes"] == 100
    assert any(level == "warn" and "the last 100 bytes" in msg for level, msg in logged)


def test_an_empty_btmp_is_an_empty_table(store, tmp_path):
    rec = store.ingest_utmp(write(tmp_path, "btmp", b""), build_fts=False)
    assert rec["row_count"] == 0
    assert [c["name"] for c in store.get_source(rec["id"])["columns"]] == utmpread.COLUMNS


def test_files_that_are_not_login_records_are_clean_errors(store, tmp_path):
    with pytest.raises(ValueError, match="shorter than one login record"):
        store.ingest_utmp(write(tmp_path, "wtmp", b"x" * 100))
    # `last` output saved under the name.
    text = b"root     pts/0        203.0.113.9      Wed May  1 10:01 - 11:00  (00:59)\n" * 20
    with pytest.raises(ValueError, match="does not hold Linux login records"):
        store.ingest_utmp(write(tmp_path, "wtmp.1", text))
    with pytest.raises(ValueError, match="Unknown login record layout"):
        store.ingest_utmp(write(tmp_path, "wtmp.2", build("384-le")), layout="512-le")
    assert store.list_sources() == []


def test_cancel_leaves_no_table(store, tmp_path, monkeypatch):
    import winnow.store as store_mod

    monkeypatch.setattr(store_mod, "BATCH", 3)
    with pytest.raises(IngestCancelled):
        store.ingest_utmp(write(tmp_path, "wtmp", build("384-le")), build_fts=False, cancel=lambda: True)
    assert store.list_sources() == []


def test_a_login_records_table_gets_its_timeline_summary(store, tmp_path):
    rec = store.ingest_utmp(write(tmp_path, "wtmp", build("384-le")), build_fts=False)
    match = store._timeline_summary_match(store.get_source(rec["id"]))
    assert match is not None


# --------------------------------------------------------------- recognition

@pytest.mark.parametrize("name, kind", [
    ("wtmp", "wtmp"), ("btmp", "btmp"), ("utmp", "utmp"), ("WTMP", "wtmp"),
    ("wtmp.1", "wtmp"), ("btmp.1", "btmp"), ("wtmp-20240501", "wtmp"), ("btmp-20240501.1", "btmp"),
    ("wtmp.1.old", "wtmp"), ("wtmp (2)", "wtmp"), ("wtmp (2).1", "wtmp"),
    ("/mnt/img/var/log/wtmp", "wtmp"), (r"C:\uac\[root]\var\log\btmp.1", "btmp"),
])
def test_login_record_names_are_recognized(name, kind):
    assert utmpread.log_kind(name) == kind


@pytest.mark.parametrize("name", [
    "wtmp.db", "lastlog", "wtmpx", "wtmp.txt", "my_wtmp", "wtmp.1.gz", "btmp.log", "utmpdump.out", "wtmp-",
])
def test_other_names_are_not(name):
    assert utmpread.log_kind(name) is None


def test_records_under_another_name_are_recognized_by_their_bytes():
    for layout in LAYOUT_KEYS:
        data = build(layout, SESSION[:-1])
        assert utmpread.looks_like(data, len(data)), layout
    data = build("384-le", SESSION[:-1])
    # Held to more than detect is: a whole number of records…
    assert not utmpread.looks_like(data, len(data) + 7)
    # …with no EMPTY slot holding leftovers, or stray bytes after a
    # string's end (a slot of all zeros says nothing either way)…
    assert utmpread.looks_like(build("384-le"), len(build("384-le")))
    leftovers = build("384-le", SESSION[:-1] + [dict(typ=0, user=b"old", sec=T0)])
    assert not utmpread.looks_like(leftovers, len(leftovers))
    dirty = bytearray(data)
    dirty[44 + 10] = 0x41   # user "reboot\0\0\0\0A…"
    assert not utmpread.looks_like(bytes(dirty), len(dirty))
    # …and nothing that merely has NULs in it.
    assert not utmpread.looks_like(bytes(384 * 4), 384 * 4)
    png = b"\x89PNG\r\n\x1a\n" + bytes(376) + os.urandom(384)
    assert not utmpread.looks_like(png, len(png))


def test_the_server_routes_login_records_to_their_reader(store, tmp_path):
    import server

    assert server._ingest_kind_for_path("/mnt/img/var/log/wtmp") == "utmp"
    assert server._ingest_kind_for_path("/mnt/img/var/log/btmp.1") == "utmp"
    # wtmpdb's database is SQLite, and stays the SQLite reader's.
    assert server._ingest_kind_for_path("/mnt/img/var/lib/wtmpdb/wtmp.db") == "sqlite"
    renamed = write(tmp_path, "evidence.bin", build("400-le", SESSION[:-1]))
    assert server._ingest_kind_for_path(renamed) == "utmp"
    assert server._ingest_kind_for_path(write(tmp_path, "blob.bin", os.urandom(4096))) == "text"
    assert Store._guess_session_reader("/e/wtmp.1") == {"kind": "utmp"}


def test_the_recognise_route_names_renamed_login_records(client, tmp_path):
    renamed = write(tmp_path, "evidence.bin", build("384-be", SESSION[:-1]))
    other = write(tmp_path, "notes", b"just text\n")
    r = client.post("/api/ingest/recognise", json={"paths": [renamed, other]})
    assert r.status_code == 200, r.text
    assert r.json() == {"kinds": ["utmp", None]}


def test_folder_scan_matches_login_records_by_name(store, tmp_path):
    root = tmp_path / "img"
    write(root, "var/log/wtmp", build("384-le"))
    write(root, "var/log/btmp.1", build("384-le", SESSION[3:5]))
    write(root, "var/log/syslog", b"May  1 10:00:00 host x\n")
    write(root, "home/alice/evidence.bin", build("384-le", SESSION[:-1]))

    r = store.scan_import_directory(str(root))   # the defaults include login records
    assert {m["rel_path"]: m["kind"] for m in r["matched"]} == {
        "var/log/btmp.1": "utmp", "var/log/wtmp": "utmp"}
    # A list without the token (a profile saved before the chip existed)
    # leaves them out, and says why.
    r = store.scan_import_directory(str(root), extensions=[".csv"])
    reasons = {e["rel_path"]: e["reason"] for e in r["excluded"]}
    assert reasons["var/log/wtmp"] == "wtmp/btmp/utmp not selected"
    assert reasons["home/alice/evidence.bin"] == "extension"
    r = store.scan_import_directory(str(root), extensions=[UTMP_IMPORT_TYPE])
    assert {m["rel_path"] for m in r["matched"]} == {"var/log/btmp.1", "var/log/wtmp"}
    # "Other text files" turns binaries away — but asks a binary's bytes
    # first, so renamed login records get in as what they are.
    r = store.scan_import_directory(str(root), extensions=["*"])
    kinds = {m["rel_path"]: m["kind"] for m in r["matched"]}
    assert kinds["home/alice/evidence.bin"] == "utmp" and kinds["var/log/wtmp"] == "utmp"
    assert kinds["var/log/syslog"] == "text"


# -------------------------------------------------------- routes and jobs

def test_the_jobs_routes_import_login_records(client, store, tmp_path):
    p = write(tmp_path, "wtmp", build("400-le"))
    r = client.post("/api/ingest/jobs/path", json={"path": p})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "utmp"
    done = store.wait_for_ingest_job(r.json()["job_id"], timeout=30)
    assert done["status"] == "done", done
    assert done["result"][0]["record_layout"] == "400-le"
    assert store.get_source(done["source_ids"][0])["row_count"] == len(SESSION)
    # …and the upload transport, whose spool keeps the file's name.
    with open(p, "rb") as f:
        r = client.post("/api/ingest/jobs/upload", data={"kind": "utmp"},
                        files={"file": ("btmp.1", f, "application/octet-stream")})
    assert r.status_code == 200, r.text
    done = store.wait_for_ingest_job(r.json()["job_id"], timeout=30)
    assert done["status"] == "done", done
    src = store.get_source(done["source_ids"][0])
    assert src["name"] == "btmp.1" and src["row_count"] == len(SESSION)


def test_the_previews_read_the_head_and_say_the_layout(client, tmp_path):
    data = build("384-be") + b"\0" * 10
    p = write(tmp_path, "wtmp", data)
    r = client.post("/api/ingest/preview/path", json={"path": p})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["columns"] == utmpread.COLUMNS and body["inferred_types"] == utmpread.COLUMN_TYPES
    assert body["layout"] == "384-be" and body["record_count"] == len(SESSION) and body["torn_bytes"] == 10
    assert body["sample_rows"][3][:3] == ["2024-05-01 10:01:00.123456", "USER_PROCESS", "root"]
    # An upload sends only the head, so the size rides along.
    r = client.post("/api/ingest/preview", data={"kind": "utmp", "size": str(len(data))},
                    files={"file": ("wtmp", data[:384 * 2], "application/octet-stream")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["record_count"] == len(SESSION) and len(body["sample_rows"]) == 2
    r = client.post("/api/ingest/preview", data={"kind": "utmp"},
                    files={"file": ("wtmp", b"not login records " * 40, "application/octet-stream")})
    assert r.status_code == 400 and "does not hold Linux login records" in r.json()["detail"]


def test_open_with_prefers_login_records_over_a_plugin(client, store, tmp_path, monkeypatch):
    import server

    claimed = []
    monkeypatch.setattr(server.PLUGINS, "format_for_filename",
                        lambda name: claimed.append(name) or object())
    out = server._assoc_ingest(write(tmp_path, "wtmp", build("384-le")))
    assert out["kind"] == "utmp" and claimed == []
    assert store.wait_for_ingest_job(out["job_id"], timeout=30)["status"] == "done"


def test_a_session_reimports_login_records_in_the_layout_they_were_read(store, tmp_path, monkeypatch):
    p = write(tmp_path, "wtmp", build("400-be"))
    sid = store.ingest_utmp(p, build_fts=False)["id"]
    tag = store.upsert_tag(None, "Intrusion", "#ff0000", None)["id"]
    store.set_tags(sid, [4], tag, True)   # root's login
    session = store.export_case_session()

    # Even if detection would now answer differently, the re-import reads
    # the layout the table was read with.
    monkeypatch.setattr(utmpread, "detect", lambda head, size=None: None)
    other = Store(str(tmp_path / "b.db"))
    try:
        res = other.import_case_session(session)
        assert res["sources_reimported"] == 1 and res["warnings"] == []
        src = other.list_sources()[0]
        rid = other.db.execute("SELECT rid FROM row_tags WHERE source_id=?", (src["id"],)).fetchone()[0]
        user = other.db.execute(f'SELECT "User" FROM {src["table_name"]} WHERE rid=?', (rid,)).fetchone()[0]
        assert user == "root"
    finally:
        other.close()
