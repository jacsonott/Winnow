"""Key=value logs (winnow/kvread.py, Store.ingest_kv) — FortiGate's format
and the other Fortinet products': recognised by the first line, since the
files have no name of their own, wherever a file's type is decided (the
server's router, the folder scan, Open With, the import queue's recognise
route), and read into one table, a column per key.

The load-bearing tests are the quoting ones and the union of keys. A
reader that splits on spaces passes every test made of traffic lines and
cuts msg="Administrator admin logged in" into four columns on the first
event log; one that takes the first line's keys as the header puts an
event line's values under a traffic line's names.
"""
from __future__ import annotations

import itertools
import random

import pytest

from winnow import kvread, timeparse
from winnow.store import IngestCancelled, Store

# The line from the request, as FortiOS 6.0 writes it: no tz, and only
# two of the string fields quoted.
TRAFFIC = ("date=2026-10-23 time=05:39:00 devid=FG6H1ETB20902601 devname=Main-Firewall vd=root "
           "logid=0000000007 type=traffic subtype=forward level=notice srcip=192.168.1.50 srcport=54705 "
           'srcintf="internal" dstip=8.8.8.8 dstport=53 dstintf="wan1" sessionid=350696 action=accept '
           "policyid=10 service=DNS proto=6 duration=15 sentbyte=124 rcvdbyte=256")
TRAFFIC_KEYS = ["date", "time", "devid", "devname", "vd", "logid", "type", "subtype", "level", "srcip",
                "srcport", "srcintf", "dstip", "dstport", "dstintf", "sessionid", "action", "policyid",
                "service", "proto", "duration", "sentbyte", "rcvdbyte"]
# An event line: other keys, and a message with spaces and parentheses.
EVENT = ('date=2026-10-23 time=05:40:12 devname="Main-Firewall" devid="FG6H1ETB20902601" '
         'logid="0100032001" type="event" subtype="system" level="information" vd="root" '
         'logdesc="Admin login successful" user="admin" ui="https(10.0.0.7)" action="login" '
         'status="success" msg="Administrator admin logged in successfully from https(10.0.0.7)"')


def _write(tmp_path, name, lines, *, raw=None, encoding="utf-8"):
    p = tmp_path / name
    if raw is not None:
        p.write_bytes(raw)
    else:
        p.write_text("\n".join(lines) + "\n", encoding=encoding, newline="")
    return str(p)


def _rows(store, rec):
    cols = [c["name"] for c in rec["columns"]]
    return [dict(zip(cols, tuple(r)[1:]))
            for r in store.db.execute(f"SELECT * FROM src_{rec['id']} ORDER BY rid")]


# ------------------------------------------------------------ recognition

@pytest.mark.parametrize("line", [
    TRAFFIC,
    EVENT,
    '<189>date=2026-10-23 time=05:39:00 devname="FG" logid="0000000013"',         # syslog, no header
    "<189>Oct 23 05:39:00 10.0.0.1 date=2026-10-23 time=05:39:00 devname=FG x=1",  # syslog header
    "Oct 23 05:39:00 fw01 date=2026-10-23 time=05:39:00 devname=FG x=1",           # rsyslog's file
    '<189>1 2026-10-23T05:39:00Z fw01 - - - date=2026-10-23 time=05:39:00 devname="FG"',
    'date=2026-10-23,time=05:39:00,devname="FG",logid="0000000013"',               # set format csv
    'date="2026-10-23" time="05:39:00" devname="FG"',
    "date=2026-10-23 time=05:39:00.123 log_id=0001001623 type=event",               # FortiMail's keys
    "\n\n   " + TRAFFIC,                                                           # blank lines first
])
def test_fortinet_lines_are_recognised(line):
    assert kvread.looks_like(line + "\nanything at all\n")


@pytest.mark.parametrize("text", [
    "",
    "\n\n",
    "date,time,srcip,dstip\n2026-10-23,05:39:00,1.2.3.4,8.8.8.8",                  # a CSV header
    "Oct 23 05:39:00 host sshd[42]: Accepted password for root from 10.0.0.1",
    "time=05:39:00 date=2026-10-23 devname=FG",                                    # time first
    "date=2026-10-23 devname=FG time=05:39:00",
    "date=2026-10-23 time=05:39:00",                                               # no third pair
    "date=2026-10-23 time=05:39:00 and then some words",
    "date=23/10/2026 time=05:39:00 devname=FG",
    "devname=FG date=2026-10-23 time=05:39:00 x=1",                                # pairs first, then date
    "#Fields: date time cs-method\n2024-05-01 00:00:01 GET",
    "some header line\n" + TRAFFIC,                                                # only the first line counts
])
def test_other_text_is_not(text):
    assert not kvread.looks_like(text)


# ----------------------------------------------------------- one line

@pytest.mark.parametrize("line, keys, values", [
    # Quotes come off; a quoted value keeps its spaces, = and commas.
    ('a=1 msg="x = y, z" b=""', ("a", "msg", "b"), ["1", "x = y, z", ""]),
    # An escaped quote is a quote.
    (r'a=1 msg="said \"hi\" twice" b=2', ("a", "msg", "b"), ["1", 'said "hi" twice', "2"]),
    # A bare quote inside a quoted value doesn't end it: only one before
    # the next key= or the end of the line does.
    ('a=1 msg="said "hi" twice" b=2', ("a", "msg", "b"), ["1", 'said "hi" twice', "2"]),
    ('msg="ends "quoted"" b=2', ("msg", "b"), ['ends "quoted"', "2"]),
    # A trailing backslash escapes nothing.
    (r'path="C:\dir\" n=1', ("path", "n"), ["C:\\dir\\", "1"]),
    (r'path="C:\dir\" n="x"', ("path", "n"), ["C:\\dir\\", "x"]),
    # Bare values keep everything up to the next space, = included.
    ("url=/a?b=c&d=e n=1", ("url", "n"), ["/a?b=c&d=e", "1"]),
    # Comma-separated pairs, as FortiOS's `set format csv` sends them.
    ('a=1,msg="x, y",b=2,', ("a", "msg", "b"), ["1", "x, y", "2"]),
    ('a=1, b="x y" ,c=3', ("a", "b", "c"), ["1", "x y", "3"]),
    # Tabs and runs of spaces separate as one space does.
    ("a=1\t b=2   c=3  ", ("a", "b", "c"), ["1", "2", "3"]),
])
def test_values_are_read_as_written(line, keys, values):
    prefix, got_keys, got_values, stray, ragged = kvread.split_line(line)
    assert (prefix, got_keys, got_values, stray, ragged) == ("", keys, values, [], False)


def test_text_before_the_pairs_is_the_prefix():
    for line, prefix in [("<189>date=2026-10-23 time=05:39:00 x=1", "<189>"),
                         ("<189>Oct 23 05:39:00 10.0.0.1 date=2026-10-23 x=1", "<189>Oct 23 05:39:00 10.0.0.1"),
                         ("  Oct 23 05:39:00 fw01   date=2026-10-23 x=1", "Oct 23 05:39:00 fw01")]:
        got = kvread.split_line(line)
        assert got[0] == prefix and got[1][0] == "date" and not got[4], line


def test_what_isnt_a_pair_is_kept_and_counted():
    assert kvread.split_line("a=1 lonely words b=2") == ("", ("a", "b"), ["1", "2"], ["lonely", "words"], True)
    assert kvread.split_line("no pairs here") == ("", (), [], ["no pairs here"], True)
    # A quote that never closes takes the rest of the line.
    assert kvread.split_line('a=1 msg="never closed b=2') == ("", ("a", "msg"), ["1", "never closed b=2"], [], True)


TRICKY = [TRAFFIC, EVENT, *[kv[0] for kv in [
    ('a=1 msg="x = y, z" b=""',), (r'a=1 msg="said \"hi\" twice" b=2',), ('a=1 msg="said "hi" twice" b=2',),
    (r'path="C:\dir\" n=1',), ("url=/a?b=c&d=e n=1",), ('a=1,msg="x, y",b=2,',), ("a=1 lonely words b=2",),
    ('a=1 msg="never closed b=2',), ("<189>Oct 23 05:39:00 h date=2026-10-23 x=1",), ("no pairs",),
    ('a=1 b="2"',), ("a=1 b=2",), ('a="1" b=2',), ('a=1 b=""',), ("a=1 b=",), ('a=1 b="x"y c=3',),
]]]


def test_a_pass_reads_every_line_as_split_line_does():
    """The per-pass cache of key sequences is a speed-up only: a line it
    matches must come out exactly as split_line reads it, whatever came
    before it — every order of the tricky lines, twice over so each one
    meets the sequences the others taught."""
    rng = random.Random(0)
    for _ in range(60):
        order = rng.sample(TRICKY, len(TRICKY))
        split = kvread._Splitter()
        for line in order * 2:
            assert split(line) == kvread.split_line(line), line


# ------------------------------------------------------------ timestamps

def test_the_timestamp_is_what_timeparse_says():
    """kvread reads whole seconds itself (a third of the import's time
    otherwise); its answer has to be timeparse's ISO parse's."""
    iso = timeparse.OPERATIONS["iso8601"]
    dates = ["2026-10-23", "2024-02-29", "2023-02-29", "0001-01-01", "9999-12-31", "2026-13-01", "2026-12-32"]
    clocks = ["05:39:00", "00:00:00", "23:59:59", "24:00:00", "12:60:00", "05:39:00.5", "05:39:00.1234567"]
    zones = [None, "+0000", "-0000", "+0200", "-0500", "+05:30", "+1400", "-1400", "+1500", "+0060"]
    for date, clock, tz in itertools.product(dates, clocks, zones):
        want = timeparse.parse_or_none(iso, f"{date} {clock}{tz or ''}", {}, {}) or ""
        assert kvread.timestamp(date, clock, tz) == want, (date, clock, tz)
    assert kvread.timestamp("2026-10-23", "05:39:00", "-0500") == "2026-10-23 10:39:00"
    assert kvread.timestamp("10/23/2026", "05:39:00") == ""
    assert kvread.timestamp("2026-10-23", "05:39:00", "EST") == ""


# ----------------------------------------------------------------- import

def test_the_request_line_imports_a_column_per_key(store, tmp_path):
    rec = store.ingest_kv(_write(tmp_path, "fortigate.log", [TRAFFIC]), build_fts=False)
    names = [c["name"] for c in rec["columns"]]
    # No tz on the line: the device's clock, said so in the name.
    assert names == ["Timestamp (local)", *TRAFFIC_KEYS]
    types = {c["name"]: c["type"] for c in rec["columns"]}
    assert types["Timestamp (local)"] == "datetime"
    assert types["date"] == "text" and types["time"] == "text"     # only the joined column is a datetime
    assert types["srcport"] == "number" and types["srcip"] == "text"
    row = _rows(store, rec)[0]
    assert row["Timestamp (local)"] == "2026-10-23 05:39:00"
    assert row["srcintf"] == "internal" and row["dstintf"] == "wan1"   # quotes off
    assert row["devname"] == "Main-Firewall" and row["sentbyte"] == "124"
    assert rec["row_count"] == 1 and rec["ragged_rows"] == 0 and rec["name"] == "fortigate.log"
    assert store.get_source(rec["id"])["ingest"] == {"kind": "kv"}


def test_columns_are_every_key_in_the_file(store, tmp_path):
    """Traffic and event lines carry different fields: the table has all
    of them, first-seen order, and each line's values under its own keys."""
    rec = store.ingest_kv(_write(tmp_path, "mixed.log", [TRAFFIC, EVENT, TRAFFIC]), build_fts=False)
    names = [c["name"] for c in rec["columns"]]
    assert names[:len(TRAFFIC_KEYS) + 1] == ["Timestamp (local)", *TRAFFIC_KEYS]
    assert names[len(TRAFFIC_KEYS) + 1:] == ["logdesc", "user", "ui", "status", "msg"]
    rows = _rows(store, rec)
    assert rows[1]["msg"] == "Administrator admin logged in successfully from https(10.0.0.7)"
    assert rows[1]["ui"] == "https(10.0.0.7)" and rows[1]["action"] == "login"
    assert rows[1]["srcip"] == "" and rows[1]["Timestamp (local)"] == "2026-10-23 05:40:12"
    assert rows[0]["msg"] == "" and rows[2]["srcport"] == "54705"


def test_a_tz_on_every_line_makes_the_timestamp_utc(store, tmp_path):
    lines = ['date=2026-10-23 time=05:39:00 tz="-0400" devname="FG" x=1',
             'date=2026-10-23 time=23:59:30 tz="+0200" devname="FG" x=2']
    rec = store.ingest_kv(_write(tmp_path, "utc.log", lines), build_fts=False)
    assert rec["columns"][0]["name"] == "Timestamp (UTC)"
    assert {c["name"]: c["type"] for c in rec["columns"]}["tz"] == "text"   # an offset, not a number
    rows = _rows(store, rec)
    assert [r["Timestamp (UTC)"] for r in rows] == ["2026-10-23 09:39:00", "2026-10-23 21:59:30"]
    assert rows[0]["time"] == "05:39:00" and rows[0]["tz"] == "-0400"      # as logged, beside it


def test_a_tz_on_only_some_lines_leaves_every_timestamp_local(store, tmp_path):
    """One column, one meaning: converting the lines that say their offset
    and not the others would put two clocks in one column."""
    lines = ['date=2026-10-23 time=05:39:00 tz="-0400" x=1', "date=2026-10-23 time=05:40:00 x=2"]
    rec = store.ingest_kv(_write(tmp_path, "some.log", lines), build_fts=False)
    assert rec["columns"][0]["name"] == "Timestamp (local)"
    assert [r["Timestamp (local)"] for r in _rows(store, rec)] == ["2026-10-23 05:39:00", "2026-10-23 05:40:00"]


def test_no_date_and_time_means_no_timestamp_column(store, tmp_path):
    rec = store.ingest_kv(_write(tmp_path, "plain.log", ["a=1 b=2", "a=3 c=4"]), build_fts=False)
    assert [c["name"] for c in rec["columns"]] == ["a", "b", "c"]
    # Nor does a date in some other form: a column of blanks is no timestamp.
    rec = store.ingest_kv(_write(tmp_path, "us.log", ["date=10/23/2026 time=05:39:00 a=1"]), build_fts=False)
    assert [c["name"] for c in rec["columns"]] == ["date", "time", "a"]


def test_prefix_and_unparsed_columns_appear_only_when_needed(store, tmp_path):
    lines = ["<189>date=2026-10-23 time=05:39:00 devname=FG x=1",
             "Oct 23 05:39:01 10.0.0.1 date=2026-10-23 time=05:39:01 devname=FG x=2",
             "date=2026-10-23 time=05:39:02 devname=FG stray words x=3",
             "-- MARK --"]
    rec = store.ingest_kv(_write(tmp_path, "syslog.log", lines), build_fts=False)
    names = [c["name"] for c in rec["columns"]]
    assert names == ["Timestamp (local)", "date", "time", "devname", "x", "Line prefix", "Unparsed text"]
    rows = _rows(store, rec)
    assert rows[0]["Line prefix"] == "<189>" and rows[1]["Line prefix"] == "Oct 23 05:39:01 10.0.0.1"
    assert rows[2]["Unparsed text"] == "stray words" and rows[2]["x"] == "3"
    assert rows[3]["Unparsed text"] == "-- MARK --" and rows[3]["Timestamp (local)"] == ""
    assert rec["ragged_rows"] == 2
    rec = store.ingest_kv(_write(tmp_path, "clean.log", [TRAFFIC]), build_fts=False)
    assert "Line prefix" not in [c["name"] for c in rec["columns"]]


def test_keys_match_case_insensitively_and_a_repeat_gets_a_suffix(store, tmp_path):
    rec = store.ingest_kv(_write(tmp_path, "dup.log", ["srcip=1 SrcIP=2 srcip=3", "SRCIP=4"]), build_fts=False)
    assert [c["name"] for c in rec["columns"]] == ["srcip", "srcip_1", "srcip_2"]
    assert [list(r.values()) for r in _rows(store, rec)] == [["1", "2", "3"], ["4", "", ""]]


def test_keys_past_the_column_cap_fold_into_one_json_cell(store, tmp_path, monkeypatch):
    """SQLite refuses a table past 2,000 columns; a file with more keys
    than the cap keeps the first ones as columns and the rest per row as
    JSON, as JSON ingest does — nothing lost, the import not refused."""
    monkeypatch.setattr(kvread, "MAX_COLUMNS", 4)
    rec = store.ingest_kv(_write(tmp_path, "wide.log", ["a=1 b=2 c=3 d=4 e=5", "a=6 e=7"]), build_fts=False)
    assert [c["name"] for c in rec["columns"]] == ["a", "b", "c", "Other fields (JSON)"]
    rows = _rows(store, rec)
    assert rows[0]["Other fields (JSON)"] == '{"d": "4", "e": "5"}'
    assert rows[1] == {"a": "6", "b": "", "c": "", "Other fields (JSON)": '{"e": "7"}'}


def test_a_nul_gap_splits_the_line_it_cut(store, tmp_path):
    raw = (TRAFFIC + "\n" + "date=2026-10-23 time=05:39:01 x=1" + "\x00" * 40
           + "date=2026-10-23 time=05:45:00 x=2\n").encode()
    rec = store.ingest_kv(_write(tmp_path, "crash.log", None, raw=raw), build_fts=False)
    assert rec["row_count"] == 3
    assert [r["x"] for r in _rows(store, rec)] == ["", "1", "2"]


def test_an_ansi_log_reads_as_windows_1252(store, tmp_path):
    rec = store.ingest_kv(_write(tmp_path, "ansi.log", ['date=2026-10-23 time=05:39:00 user="Jos\u00e9" x=1'],
                                 encoding="cp1252"), build_fts=False)
    assert rec["encoding"] == "cp1252" and _rows(store, rec)[0]["user"] == "Jos\u00e9"


def test_a_line_appended_between_the_passes_is_not_read(store, tmp_path, monkeypatch):
    """A syslog server's file grows while it is imported in place. The
    second pass reads as many lines as the first planned for, so a key
    the plan never saw can't arrive mid-insert."""
    p = _write(tmp_path, "live.log", [TRAFFIC, EVENT])
    real = kvread.plan

    def plan_then_grow(lines):
        out = real(lines)
        with open(p, "a", newline="") as f:
            f.write("date=2026-10-23 time=09:00:00 brandnewkey=x\n")
        return out

    monkeypatch.setattr(kvread, "plan", plan_then_grow)
    rec = store.ingest_kv(p, build_fts=False)
    assert rec["row_count"] == 2
    assert "brandnewkey" not in [c["name"] for c in rec["columns"]]


def test_cancel_leaves_no_table(store, tmp_path):
    p = _write(tmp_path, "big.log", [TRAFFIC] * 45_000)
    for calls_before_yes in (0, 2):   # pass one (before the table exists), then pass two
        seen = [0]

        def cancel():
            seen[0] += 1
            return seen[0] > calls_before_yes

        with pytest.raises(IngestCancelled):
            store.ingest_kv(p, build_fts=False, cancel=cancel)
        assert store.list_sources() == []


def test_empty_and_pairless_files_are_clean_errors(store, tmp_path):
    with pytest.raises(ValueError, match="File is empty"):
        store.ingest_kv(_write(tmp_path, "empty.log", None, raw=b""))
    with pytest.raises(ValueError, match="no key=value pairs"):
        store.ingest_kv(_write(tmp_path, "prose.log", ["just words", "and more words"]))
    assert store.list_sources() == []


# ------------------------------------------------------------ the preview

def test_the_preview_is_the_start_of_the_table(store, tmp_path):
    body = Store.preview_kv_text("\n".join([TRAFFIC, EVENT]) + "\n")
    assert body["columns"][:2] == ["Timestamp (local)", "date"] and body["columns"][-1] == "msg"
    assert len(body["sample_rows"]) == 2 and body["complete"] is True
    assert body["inferred_types"][0] == "datetime"
    # A sample that isn't the whole file loses its last, maybe cut, line.
    body = Store.preview_kv_text(TRAFFIC + "\n" + EVENT[:60], complete=False)
    assert len(body["sample_rows"]) == 1 and body["complete"] is False
    with pytest.raises(ValueError, match="No key=value pairs"):
        Store.preview_kv_text("just words\n")


def test_column_types_from_the_preview_apply_by_name(store, tmp_path):
    """The preview saw a traffic line; an event line further in adds msg
    and the rest before Line prefix. Types by position would land on the
    wrong columns; by name they don't."""
    p = _write(tmp_path, "typed.log", ["<189>" + TRAFFIC, EVENT])
    rec = store.ingest_kv(p, build_fts=False, column_types={"srcport": "text", "Line prefix": "text"})
    types = {c["name"]: c["type"] for c in rec["columns"]}
    assert types["srcport"] == "text" and types["dstport"] == "number" and types["msg"] == "text"
    assert store.get_source(rec["id"])["ingest"] == {
        "kind": "kv", "column_types": {"srcport": "text", "Line prefix": "text"}}
    with pytest.raises(ValueError, match="name their columns"):
        store.ingest_kv(p, column_types=["text", "text"])
    with pytest.raises(ValueError, match="name their columns"):
        store.ingest_kv(p, column_types={"srcport": "ipv4"})


# ------------------------------------------------------------- routing

def test_the_server_routes_a_fortinet_first_line_to_the_kv_reader(tmp_path):
    import server

    for name in ("fortigate.log", "export.txt", "tlog", "disk-traffic-forward.log.1"):
        assert server._ingest_kind_for_path(_write(tmp_path, name, [TRAFFIC])) == "kv", name
    # Never a file a name or extension gives to another reader…
    assert server._ingest_kind_for_path(_write(tmp_path, "forti.csv", [TRAFFIC])) == "csv"
    assert server._ingest_kind_for_path(_write(tmp_path, "u_ex240501.log", [TRAFFIC])) == "iis"
    # …and only when the first line says so.
    assert server._ingest_kind_for_path(_write(tmp_path, "hostd.log", ["x", TRAFFIC])) == "text"
    assert server._ingest_kind_for_path(_write(tmp_path, "notes.txt", ["a,b", "1,2"])) == "csv"


def test_utf16_and_bom_files_are_recognised(tmp_path):
    from winnow.store import looks_like_kv_log

    for enc in ("utf-16", "utf-8-sig"):
        p = tmp_path / f"{enc}.log"
        p.write_text(TRAFFIC + "\n", encoding=enc)
        assert looks_like_kv_log(str(p)), enc
    assert not looks_like_kv_log(str(tmp_path / "missing.log"))


def test_folder_scan_reads_fortinet_logs_that_get_in(store, tmp_path):
    root = tmp_path / "logs"
    root.mkdir()
    _write(root, "fortigate.log", [TRAFFIC])
    _write(root, "export.txt", [TRAFFIC])
    _write(root, "notes.txt", ["a,b", "1,2"])
    _write(root, "auth.log", ["Oct 23 05:39:00 host sshd[1]: Accepted"])
    _write(root, "forti.csv", [TRAFFIC])

    kinds = {m["rel_path"]: m["kind"] for m in store.scan_import_directory(str(root))["matched"]}
    # The defaults: a .txt is let in, and its first line makes it kv. A
    # .log isn't let in by any default type, so it isn't opened.
    assert kinds == {"export.txt": "kv", "notes.txt": "csv", "forti.csv": "csv"}
    # However a .log gets in — other text files, or a plugin's .log chip —
    # its first line decides, ahead of the plugin's claim.
    for exts in (["*"], [".log"]):
        kinds = {m["rel_path"]: m["kind"] for m in store.scan_import_directory(str(root), extensions=exts)["matched"]}
        assert kinds["fortigate.log"] == "kv", exts
        assert kinds["auth.log"] == ("text" if exts == ["*"] else "plugin"), exts


def test_the_recognise_route_answers_per_path(client, tmp_path):
    paths = [_write(tmp_path, "a.log", [TRAFFIC]), _write(tmp_path, "b.log", ["plain"]),
             str(tmp_path / "missing.log"), str(tmp_path)]
    r = client.post("/api/ingest/recognise", json={"paths": paths})
    assert r.status_code == 200, r.text
    assert r.json() == {"kinds": ["kv", None, None, None]}


def test_jobs_routes_import_a_kv_log(client, store, tmp_path):
    p = _write(tmp_path, "fortigate.log", [TRAFFIC, EVENT])
    r = client.post("/api/ingest/jobs/path", json={"path": p})        # kind from the first line
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "kv"
    done = store.wait_for_ingest_job(r.json()["job_id"], timeout=30)
    assert done["status"] == "done", done
    src = store.get_source(done["source_ids"][0])
    assert src["row_count"] == 2 and src["columns"][0]["name"] == "Timestamp (local)"
    # The upload transport, with the preview's types by name.
    with open(p, "rb") as f:
        r = client.post("/api/ingest/jobs/upload",
                        data={"kind": "kv", "column_types": '{"srcport": "text"}'},
                        files={"file": ("fortigate-2.log", f, "text/plain")})
    assert r.status_code == 200, r.text
    done = store.wait_for_ingest_job(r.json()["job_id"], timeout=30)
    assert done["status"] == "done", done
    src = store.get_source(done["source_ids"][0])
    assert src["name"] == "fortigate-2.log"
    assert {c["name"]: c["type"] for c in src["columns"]}["srcport"] == "text"
    assert src["ingest"]["column_types"] == {"srcport": "text"}


def test_the_previews_read_a_kv_log(client, tmp_path, monkeypatch):
    import server

    p = _write(tmp_path, "fortigate.log", [TRAFFIC, EVENT])
    body = client.post("/api/ingest/preview/path", json={"path": p}).json()   # kind from the first line
    assert body["columns"][0] == "Timestamp (local)" and len(body["sample_rows"]) == 2
    assert body["complete"] is True
    # Read as Key=value: any text file, asked for by kind.
    plain = _write(tmp_path, "plain.log", ["a=1 b=2"])
    assert client.post("/api/ingest/preview/path", json={"path": plain, "kind": "kv"}).json()["columns"] == ["a", "b"]
    with open(p, "rb") as f:
        r = client.post("/api/ingest/preview", data={"kind": "kv"}, files={"file": ("fortigate.log", f, "text/plain")})
    assert r.status_code == 200, r.text
    assert r.json()["columns"][-1] == "msg"
    monkeypatch.setattr(server, "PREVIEW_SAMPLE_BYTES", 200)
    assert client.post("/api/ingest/preview/path", json={"path": p, "kind": "kv"}).json()["complete"] is False


def test_open_with_prefers_kv_over_a_plugin_claiming_log(client, store, tmp_path, monkeypatch):
    """esxi_logs claims .log; a double-clicked FortiGate log is still kv."""
    import server

    claimed = []
    monkeypatch.setattr(server.PLUGINS, "format_for_filename",
                        lambda name: claimed.append(name) or object())
    out = server._assoc_ingest(_write(tmp_path, "fortigate.log", [TRAFFIC]))
    assert out["kind"] == "kv" and claimed == []
    store.wait_for_ingest_job(out["job_id"], timeout=30)


def test_a_session_reimports_a_kv_log_as_one(store, tmp_path):
    p = _write(tmp_path, "fortigate.log", [TRAFFIC, EVENT])
    sid = store.ingest_kv(p, build_fts=False, column_types={"srcport": "text"})["id"]
    tag = store.upsert_tag(None, "Admin login", "#ff0000", None)["id"]
    store.set_tags(sid, [2], tag, True)
    session = store.export_case_session()

    other = Store(str(tmp_path / "b.db"))
    try:
        res = other.import_case_session(session)
        assert res["sources_reimported"] == 1 and res["warnings"] == []
        src = other.list_sources()[0]
        assert {c["name"]: c["type"] for c in src["columns"]}["srcport"] == "text"
        rid = other.db.execute("SELECT rid FROM row_tags WHERE source_id=?", (src["id"],)).fetchone()[0]
        user = other.db.execute(f'SELECT "user" FROM {src["table_name"]} WHERE rid=?', (rid,)).fetchone()[0]
        assert user == "admin"
    finally:
        other.close()
