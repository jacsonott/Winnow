"""IIS web server logs (winnow/iisread.py, Store.ingest_iis): recognised by
the name IIS gives them, wherever a file's type is decided — the server's
router, the folder scan, a session re-import, Open With — and read into
one table.

The load-bearing W3C tests are the header ones: a site's fields are its
own configuration and a #Fields: line can change them partway through a
file, so the columns are the union of every directive and each line is
read against the one in effect where it sits. A fixed column list passes
every test that uses IIS's default fields and puts values under the wrong
names on the first server somebody configured.
"""
from __future__ import annotations

import pytest

from winnow import iisread
from winnow.store import IIS_IMPORT_TYPE, IngestCancelled, Store

DEFAULT_FIELDS = ("date time s-ip cs-method cs-uri-stem cs-uri-query s-port cs-username c-ip "
                  "cs(User-Agent) cs(Referer) sc-status sc-substatus sc-win32-status time-taken")

HEADER = [
    "#Software: Microsoft Internet Information Services 10.0",
    "#Version: 1.0",
    "#Date: 2024-05-01 00:00:01",
    f"#Fields: {DEFAULT_FIELDS}",
]
W3C = HEADER + [
    "2024-05-01 00:00:01 10.0.0.5 GET /owa/auth/logon.aspx - 443 - 203.0.113.9 "
    "Mozilla/5.0+(Windows+NT+10.0) - 200 0 0 15",
    "2024-05-01 00:00:02 10.0.0.5 POST /aspnet_client/shell.aspx cmd=whoami 443 - 203.0.113.9 "
    "python-requests/2.31 - 200 0 0 31",
]


def _write(tmp_path, name, lines, *, sub="W3SVC1", raw=None, encoding="utf-8"):
    d = tmp_path / sub
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    if raw is not None:
        p.write_bytes(raw)
    else:
        p.write_text("\r\n".join(lines) + "\r\n", encoding=encoding, newline="")
    return str(p)


def _rows(store, rec):
    cols = [c["name"] for c in rec["columns"]]
    out = []
    for r in store.db.execute(f"SELECT * FROM src_{rec['id']} ORDER BY rid"):
        out.append(dict(zip(cols, tuple(r)[1:])))
    return out


# ------------------------------------------------------------------ names

@pytest.mark.parametrize("name, fmt", [
    ("u_ex240501.log", "w3c"),          # daily, UTF-8
    ("ex240501.log", "w3c"),            # daily, ANSI
    ("u_ex24050113.log", "w3c"),        # hourly
    ("u_ex2405.log", "w3c"),            # monthly
    ("u_ex240502.log", "w3c"),          # weekly (YYMMWW)
    ("u_extend1.log", "w3c"),           # by size / never rolls over
    ("extend12.log", "w3c"),
    ("U_EX240501.LOG", "w3c"),
    ("u_ex240501 (2).log", "w3c"),      # archive.py's second copy
    ("nc240501.log", "ncsa"),
    ("u_nc2405.log", "ncsa"),
    ("ncsa1.log", "ncsa"),
    ("in240501.log", "native"),
    ("u_in24050100.log", "native"),
    ("inetsv3.log", "native"),
    (r"C:\inetpub\logs\LogFiles\W3SVC2\u_ex240501.log", "w3c"),
    ("/evidence/W3SVC1/u_ex240501.log", "w3c"),
])
def test_iis_names_are_recognised(name, fmt):
    assert iisread.log_format(name) == fmt


@pytest.mark.parametrize("name", [
    "hostd.log", "index.log", "ex.log", "extend.log", "u_ex.log",
    "u_ex240501.log.1", "u_ex240501.txt", "u_ex240501.log.gz",
    "u_ex2405011.log",                  # seven digits is no IIS period
    "ex241301.log", "in1234.log",       # month 13, month 34
    "ex240532.log", "ex240500.log",     # day 32, day 0
    "ex24050124.log",                   # hour 24
    "xu_ex240501.log", "my_ex240501.log",
])
def test_other_names_are_not(name):
    assert iisread.log_format(name) is None


# -------------------------------------------------------------------- W3C

def test_w3c_columns_come_from_the_fields_line(store, tmp_path):
    rec = store.ingest_iis(_write(tmp_path, "u_ex240501.log", W3C), build_fts=False)
    assert rec["log_format"] == "w3c" and rec["row_count"] == 2 and rec["ragged_rows"] == 0
    names = [c["name"] for c in rec["columns"]]
    assert names == ["Timestamp (UTC)", *DEFAULT_FIELDS.split()]
    types = {c["name"]: c["type"] for c in rec["columns"]}
    assert types["Timestamp (UTC)"] == "datetime"
    assert types["date"] == "text"          # only the combined column is a datetime
    assert types["sc-status"] == "number" and types["time-taken"] == "number"
    rows = _rows(store, rec)
    assert rows[0]["Timestamp (UTC)"] == "2024-05-01 00:00:01"
    assert rows[1]["cs-method"] == "POST" and rows[1]["cs-uri-query"] == "cmd=whoami"
    # Kept as logged: "-" for an empty field, "+" for a space.
    assert rows[0]["cs-uri-query"] == "-"
    assert rows[0]["cs(User-Agent)"] == "Mozilla/5.0+(Windows+NT+10.0)"
    assert rec["name"] == "u_ex240501.log"


def test_a_fields_line_partway_through_reshapes_the_lines_after_it(store, tmp_path):
    """The site was reconfigured at 01:00 — cs(Referer) dropped, s-sitename
    and X-Forwarded-For added, in a different order. Every value has to
    land under the field the header IN EFFECT names, and a field one header
    lacks is blank on that header's lines, not shifted into."""
    lines = W3C + [
        "#Software: Microsoft Internet Information Services 10.0",
        "#Date: 2024-05-01 01:00:00",
        "#Fields: s-sitename date time c-ip cs-method cs-uri-stem sc-status X-Forwarded-For time-taken",
        "W3SVC1 2024-05-01 01:00:05 10.0.0.9 GET /default.aspx 404 198.51.100.7 1",
        "#Fields: date time cs-method cs-uri-stem sc-status",
        "2024-05-01 02:00:00 HEAD / 200",
    ]
    rec = store.ingest_iis(_write(tmp_path, "u_ex240501.log", lines), build_fts=False)
    names = [c["name"] for c in rec["columns"]]
    assert names == ["Timestamp (UTC)", *DEFAULT_FIELDS.split(), "s-sitename", "X-Forwarded-For"]
    rows = _rows(store, rec)
    assert len(rows) == 4 and rec["ragged_rows"] == 0
    after = rows[2]
    assert after["s-sitename"] == "W3SVC1" and after["c-ip"] == "10.0.0.9"
    assert after["cs-uri-stem"] == "/default.aspx" and after["sc-status"] == "404"
    assert after["X-Forwarded-For"] == "198.51.100.7" and after["time-taken"] == "1"
    assert after["cs(Referer)"] == "" and after["s-ip"] == ""
    assert after["Timestamp (UTC)"] == "2024-05-01 01:00:05"
    assert rows[3] == {**{n: "" for n in names}, "Timestamp (UTC)": "2024-05-01 02:00:00",
                       "date": "2024-05-01", "time": "02:00:00", "cs-method": "HEAD",
                       "cs-uri-stem": "/", "sc-status": "200"}
    assert rows[0]["s-sitename"] == "" and rows[0]["X-Forwarded-For"] == ""


def test_field_names_match_case_insensitively(store, tmp_path):
    lines = ["#Fields: date time cs-uri-stem", "2024-05-01 00:00:01 /a",
             "#Fields: Date Time CS-URI-STEM", "2024-05-01 00:00:02 /b"]
    rec = store.ingest_iis(_write(tmp_path, "u_ex240501.log", lines), build_fts=False)
    assert [c["name"] for c in rec["columns"]] == ["Timestamp (UTC)", "date", "time", "cs-uri-stem"]
    assert [r["cs-uri-stem"] for r in _rows(store, rec)] == ["/a", "/b"]


def test_values_the_header_does_not_name_are_kept_not_dropped(store, tmp_path):
    """A line with more values than its #Fields: names, and one before any
    header at all, keep everything in an "Unnamed values" column (made only
    for a file that needs it); a short line pads. All three count as
    ragged."""
    lines = [
        "2024-05-01 00:00:00 orphan line",
        "#Fields: date time cs-uri-stem",
        "2024-05-01 00:00:01 /a extra1 extra2",
        "2024-05-01 00:00:02",
        "2024-05-01 00:00:03 /ok",
    ]
    rec = store.ingest_iis(_write(tmp_path, "u_ex240501.log", lines), build_fts=False)
    assert [c["name"] for c in rec["columns"]] == ["Timestamp (UTC)", "date", "time", "cs-uri-stem",
                                                   "Unnamed values"]
    rows = _rows(store, rec)
    assert rows[0]["Unnamed values"] == "2024-05-01 00:00:00 orphan line" and rows[0]["date"] == ""
    assert rows[1]["cs-uri-stem"] == "/a" and rows[1]["Unnamed values"] == "extra1 extra2"
    assert rows[2]["time"] == "00:00:02" and rows[2]["cs-uri-stem"] == ""
    assert rows[3]["Unnamed values"] == ""
    assert rec["ragged_rows"] == 3
    # …and a file that never needs it doesn't get the column.
    rec2 = store.ingest_iis(_write(tmp_path, "u_ex240502.log", W3C), build_fts=False)
    assert "Unnamed values" not in [c["name"] for c in rec2["columns"]]


def test_the_timestamp_is_canonical_or_blank():
    assert iisread._w3c_timestamp("2024-05-01", "13:07:00") == "2024-05-01 13:07:00"
    assert iisread._w3c_timestamp("2024-05-01", "13:07:00.5") == "2024-05-01 13:07:00.500000"
    assert iisread._w3c_timestamp("2024-05-01", "13:07:00.123456") == "2024-05-01 13:07:00.123456"
    for date, clock in (("-", "13:07:00"), ("2024-05-01", "-"), ("05/01/2024", "13:07:00"),
                        ("2024-05-01", "13:07")):
        assert iisread._w3c_timestamp(date, clock) == ""


def test_no_date_or_time_field_means_no_timestamp_column(store, tmp_path):
    lines = ["#Fields: c-ip cs-uri-stem sc-status", "10.0.0.1 /a 200"]
    rec = store.ingest_iis(_write(tmp_path, "u_extend1.log", lines), build_fts=False)
    assert [c["name"] for c in rec["columns"]] == ["c-ip", "cs-uri-stem", "sc-status"]


def test_a_nul_gap_splits_the_line_it_cut(store, tmp_path, monkeypatch):
    """Power lost mid-write: the tail IIS never flushed is zeros, and the
    restart's header block is written straight after them — on the same
    physical line as the half-written entry. Both halves are read; the
    zeros are not."""
    from winnow import log as wlog

    logged = []
    monkeypatch.setattr(wlog, "record", lambda level, msg: logged.append((level, msg)))
    head = ("\r\n".join(W3C) + "\r\n").encode()
    cut = b"2024-05-01 00:00:03 10.0.0.5 GET /cut" + b"\x00" * 4096
    restart = ("\r\n".join(HEADER + [
        "2024-05-01 03:00:00 10.0.0.5 GET /after 443 - 203.0.113.9 - - 200 0 0 2"]) + "\r\n").encode()
    rec = store.ingest_iis(_write(tmp_path, "u_ex240501.log", None, raw=head + cut + restart),
                           build_fts=False)
    rows = _rows(store, rec)
    assert [r["cs-uri-stem"] for r in rows] == ["/owa/auth/logon.aspx", "/aspnet_client/shell.aspx",
                                                "/cut", "/after"]
    assert rows[3]["Timestamp (UTC)"] == "2024-05-01 03:00:00"
    assert not any("\x00" in v for r in rows for v in r.values())
    assert any(level == "warn" and "NUL" in msg for level, msg in logged)


def test_an_ansi_log_reads_as_windows_1252(store, tmp_path):
    """No u_ prefix: IIS wrote it in the server's code page."""
    lines = ["#Fields: date time cs-uri-stem", "2024-05-01 00:00:01 /caf\u00e9"]
    rec = store.ingest_iis(_write(tmp_path, "ex240501.log", lines, encoding="cp1252"), build_fts=False)
    assert rec["encoding"] == "cp1252"
    assert _rows(store, rec)[0]["cs-uri-stem"] == "/caf\u00e9"


def test_a_line_appended_between_the_passes_is_not_read(store, tmp_path, monkeypatch):
    """A live server's current log grows while it is imported in place.
    The second pass reads as many lines as the first planned for, so a
    header the plan never saw can't arrive mid-insert."""
    p = _write(tmp_path, "u_ex240501.log", W3C)
    real = iisread.plan

    def plan_then_grow(fmt, lines):
        out = real(fmt, lines)
        with open(p, "a", newline="") as f:
            f.write("#Fields: date time brand-new-field\r\n2024-05-01 09:00:00 x\r\n")
        return out

    monkeypatch.setattr(iisread, "plan", plan_then_grow)
    rec = store.ingest_iis(p, build_fts=False)
    assert rec["row_count"] == 2
    assert "brand-new-field" not in [c["name"] for c in rec["columns"]]


def test_cancel_leaves_no_table(store, tmp_path):
    many = W3C + [W3C[-1]] * 45_000
    p = _write(tmp_path, "u_ex240501.log", many)
    for calls_before_yes in (0, 2):   # pass one (before the table exists), then pass two
        seen = [0]

        def cancel():
            seen[0] += 1
            return seen[0] > calls_before_yes

        with pytest.raises(IngestCancelled):
            store.ingest_iis(p, build_fts=False, cancel=cancel)
        assert store.list_sources() == []


def test_empty_and_misnamed_files_are_clean_errors(store, tmp_path):
    with pytest.raises(ValueError, match="File is empty"):
        store.ingest_iis(_write(tmp_path, "u_ex240501.log", None, raw=b""))
    with pytest.raises(ValueError, match="not named like an IIS log"):
        store.ingest_iis(_write(tmp_path, "access.log", W3C))
    assert store.list_sources() == []


# ----------------------------------------------------- NCSA and IIS native

def test_ncsa_lines_split_and_the_offset_is_applied(store, tmp_path):
    lines = [
        '172.21.13.45 - REDMOND\\fred [08/Apr/2001:17:39:04 -0800] "GET /scripts/iisadmin/ism.dll?http/serv HTTP/1.0" 200 3401',
        "not an access log line",
    ]
    rec = store.ingest_iis(_write(tmp_path, "nc010408.log", lines), build_fts=False)
    assert rec["log_format"] == "ncsa"
    assert [c["name"] for c in rec["columns"]] == ["Timestamp (UTC)", *iisread.NCSA_COLUMNS, "Unparsed line"]
    rows = _rows(store, rec)
    assert rows[0]["Timestamp (UTC)"] == "2001-04-09 01:39:04"   # 17:39 at -08:00
    assert rows[0]["User name"] == "REDMOND\\fred"
    assert rows[0]["Request"] == "GET /scripts/iisadmin/ism.dll?http/serv HTTP/1.0"
    assert rows[0]["Status"] == "200" and rows[0]["Bytes"] == "3401"
    assert rows[1]["Unparsed line"] == "not an access log line" and rows[1]["Remote host"] == ""
    assert rec["ragged_rows"] == 1


def test_native_lines_split_and_stay_in_local_time(store, tmp_path):
    lines = [
        "192.168.114.201, -, 03/20/01, 7:55:20, W3SVC2, SALES1, 172.21.13.45, 4502, 163, 3223, 200, 0, GET, /DeptLogo.gif, -,",
        "10.0.0.7, CONTOSO\\amy, 5/21/2024, 13:01:02, W3SVC1, WEB01, 10.0.0.5, 15, 400, 900, 200, 0, GET, /a, b, c.aspx, q=1,",
    ]
    rec = store.ingest_iis(_write(tmp_path, "in010320.log", lines), build_fts=False)
    assert rec["log_format"] == "native" and rec["ragged_rows"] == 0
    assert [c["name"] for c in rec["columns"]] == ["Timestamp (local)", *iisread.NATIVE_COLUMNS]
    types = {c["name"]: c["type"] for c in rec["columns"]}
    assert types["Timestamp (local)"] == "datetime" and types["Date"] == "text"
    rows = _rows(store, rec)
    assert rows[0]["Timestamp (local)"] == "2001-03-20 07:55:20"
    assert rows[0]["Target of operation"] == "/DeptLogo.gif" and rows[0]["Parameters"] == "-"
    assert rows[1]["Timestamp (local)"] == "2024-05-21 13:01:02"
    # A ", " inside the target stays in the target.
    assert rows[1]["Target of operation"] == "/a, b, c.aspx" and rows[1]["Parameters"] == "q=1"


# -------------------------------------------------------- header sets

def test_a_default_w3c_table_is_recognised_for_its_timeline_summary(store, tmp_path):
    lines = HEADER[:-1] + [f"#Fields: {DEFAULT_FIELDS} sc-bytes"] + [W3C[-1] + " 512"]
    rec = store.ingest_iis(_write(tmp_path, "u_ex240501.log", lines), build_fts=False)
    match = store._timeline_summary_match(store.get_source(rec["id"]))
    assert match and match[0] == "IIS web logs (W3C)"   # contains the defaults; one more is fine
    for name in ("nc010408.log", "in010320.log"):
        rec = store.ingest_iis(_write(tmp_path, name, ['1.2.3.4 - - [08/Apr/2001:17:39:04 -0800] "GET / HTTP/1.0" 200 1']
                                      if name.startswith("nc") else
                                      ["1.2.3.4, -, 3/20/01, 7:55:20, W3SVC2, S, 5.6.7.8, 1, 2, 3, 200, 0, GET, /, -,"]),
                               build_fts=False)
        match = store._timeline_summary_match(store.get_source(rec["id"]))
        assert match and match[0].startswith("IIS web logs")


# ------------------------------------------------------------- routing

def test_the_server_routes_iis_names_to_the_iis_reader():
    import server

    assert server._ingest_kind_for_path("/e/W3SVC1/u_ex240501.log") == "iis"
    assert server._ingest_kind_for_path("/e/nc240501.log") == "iis"
    assert server._ingest_kind_for_path("/e/hostd.log") == "text"
    assert Store._guess_session_reader("/e/u_ex240501.log") == {"kind": "iis"}


def test_folder_scan_matches_iis_logs_by_name(store, tmp_path):
    _write(tmp_path / "logs", "u_ex240501.log", W3C)
    _write(tmp_path / "logs", "nc240501.log", ["x"], sub="W3SVC2")
    _write(tmp_path / "logs", "hostd.log", ["x"], sub="other")
    root = str(tmp_path / "logs")

    r = store.scan_import_directory(root)   # the defaults include IIS logs
    assert {m["rel_path"]: m["kind"] for m in r["matched"]} == {
        "W3SVC1/u_ex240501.log": "iis", "W3SVC2/nc240501.log": "iis"}
    assert {e["rel_path"]: e["reason"] for e in r["excluded"]} == {"other/hostd.log": "extension"}

    # A list without the token (a profile saved before the chip existed)
    # leaves them out, and says why.
    r = store.scan_import_directory(root, extensions=[".csv"])
    assert {e["rel_path"]: e["reason"] for e in r["excluded"]}["W3SVC1/u_ex240501.log"] == "IIS logs not selected"
    r = store.scan_import_directory(root, extensions=[IIS_IMPORT_TYPE])
    assert {m["rel_path"] for m in r["matched"]} == {"W3SVC1/u_ex240501.log", "W3SVC2/nc240501.log"}

    # However one gets in — a plugin's .log chip, "other text files" — it is
    # still read as an IIS log: built-in recognition wins.
    for exts in ([".log"], ["*"]):
        kinds = {m["rel_path"]: m["kind"] for m in store.scan_import_directory(root, extensions=exts)["matched"]}
        assert kinds["W3SVC1/u_ex240501.log"] == "iis", exts


def test_jobs_path_route_imports_an_iis_log(client, store, tmp_path):
    p = _write(tmp_path, "u_ex240501.log", W3C)
    r = client.post("/api/ingest/jobs/path", json={"path": p})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "iis"
    done = store.wait_for_ingest_job(r.json()["job_id"], timeout=30)
    assert done["status"] == "done", done
    src = store.get_source(done["source_ids"][0])
    assert src["row_count"] == 2 and src["name"] == "u_ex240501.log"
    # …and the upload transport, whose spool keeps the file's name.
    with open(p, "rb") as f:
        r = client.post("/api/ingest/jobs/upload", data={"kind": "iis"},
                        files={"file": ("u_ex240502.log", f, "text/plain")})
    assert r.status_code == 200, r.text
    done = store.wait_for_ingest_job(r.json()["job_id"], timeout=30)
    assert done["status"] == "done", done
    assert store.get_source(done["source_ids"][0])["columns"][0]["name"] == "Timestamp (UTC)"


def test_path_preview_reads_an_iis_log(client, tmp_path):
    p = _write(tmp_path, "u_ex240501.log", W3C)
    r = client.post("/api/ingest/preview/path", json={"path": p})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["columns"][0] == "Timestamp (UTC)" and len(body["sample_rows"]) == 2
    assert body["log_format"] == "w3c"


def test_open_with_prefers_iis_over_a_plugin_claiming_log(client, store, tmp_path, monkeypatch):
    """esxi_logs claims .log; a double-clicked u_ex*.log is still IIS's."""
    import server

    claimed = []
    monkeypatch.setattr(server.PLUGINS, "format_for_filename",
                        lambda name: claimed.append(name) or object())
    out = server._assoc_ingest(_write(tmp_path, "u_ex240501.log", W3C))
    assert out["kind"] == "iis" and claimed == []
    store.wait_for_ingest_job(out["job_id"], timeout=30)


def test_a_session_reimports_an_iis_log_as_one(store, tmp_path):
    p = _write(tmp_path, "u_ex240501.log", W3C)
    sid = store.ingest_iis(p, build_fts=False)["id"]
    tag = store.upsert_tag(None, "Webshell", "#ff0000", None)["id"]
    store.set_tags(sid, [2], tag, True)
    session = store.export_case_session()

    other = Store(str(tmp_path / "b.db"))
    try:
        res = other.import_case_session(session)
        assert res["sources_reimported"] == 1 and res["warnings"] == []
        src = other.list_sources()[0]
        assert src["columns"][0]["name"] == "Timestamp (UTC)"
        rid = other.db.execute("SELECT rid FROM row_tags WHERE source_id=?", (src["id"],)).fetchone()[0]
        stem = other.db.execute(f'SELECT "cs-uri-stem" FROM {src["table_name"]} WHERE rid=?', (rid,)).fetchone()[0]
        assert stem == "/aspnet_client/shell.aspx"
    finally:
        other.close()


# ------------------------------------------- the import preview's settings
# The queue's "Preview & configure": the format the name says, or another
# of the three, and a type per column. The types are by column NAME,
# because the preview reads the start of the log and a #Fields: line
# further in can add columns ahead of the ones it showed.

RESHAPED = [
    "#Fields: cs-method cs-uri-stem sc-status",
    "GET /a 200",
    "#Fields: date time cs-method cs-uri-stem sc-status time-taken",
    "2024-05-01 00:00:02 POST /b 500 31",
]


def test_the_upload_preview_reads_an_iis_log_as_named_or_as_picked(client, tmp_path):
    p = _write(tmp_path, "u_ex240501.log", W3C)

    def preview(name, **form):
        with open(p, "rb") as f:
            return client.post("/api/ingest/preview", data={"kind": "iis", **form},
                               files={"file": (name, f, "text/plain")})

    r = preview("u_ex240501.log")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["log_format"] == "w3c" and body["complete"] is True
    assert body["columns"][:3] == ["Timestamp (UTC)", "date", "time"] and len(body["sample_rows"]) == 2
    # Picked as NCSA, W3C lines don't split: they are kept whole, not lost.
    body = preview("u_ex240501.log", log_format="ncsa").json()
    assert body["log_format"] == "ncsa" and body["columns"][1:8] == iisread.NCSA_COLUMNS
    assert body["columns"][-1] == iisread.UNPARSED_LINE
    assert preview("u_ex240501.log", log_format="apache").status_code == 400
    assert preview("access.log").status_code == 400          # no IIS name, no format picked


def test_the_path_preview_takes_the_picked_format_and_says_when_it_saw_only_the_start(
        client, tmp_path, monkeypatch):
    import server

    p = _write(tmp_path, "u_ex240501.log", W3C)
    body = client.post("/api/ingest/preview/path", json={"path": p, "kind": "iis", "log_format": "native"}).json()
    assert body["log_format"] == "native" and body["columns"][1:16] == iisread.NATIVE_COLUMNS
    assert client.post("/api/ingest/preview/path",
                       json={"path": p, "kind": "iis", "log_format": "apache"}).status_code == 400
    monkeypatch.setattr(server, "PREVIEW_SAMPLE_BYTES", 200)
    body = client.post("/api/ingest/preview/path", json={"path": p, "kind": "iis"}).json()
    assert body["complete"] is False


def test_column_types_from_the_preview_apply_by_name(store, tmp_path):
    p = _write(tmp_path, "u_ex240501.log", RESHAPED)
    # What the preview of the first two lines offers: three columns.
    assert Store.preview_iis_text("\n".join(RESHAPED[:2]), "w3c")["columns"] == ["cs-method", "cs-uri-stem", "sc-status"]
    rec = store.ingest_iis(p, build_fts=False, column_types={"sc-status": "text", "cs-method": "text"})
    types = {c["name"]: c["type"] for c in rec["columns"]}
    # The later header put Timestamp first: a list by position would have
    # typed it text. By name, sc-status is text and the rest are inferred.
    assert [c["name"] for c in rec["columns"]][:2] == ["Timestamp (UTC)", "cs-method"]
    assert types == {"Timestamp (UTC)": "datetime", "cs-method": "text", "cs-uri-stem": "text",
                     "sc-status": "text", "date": "text", "time": "text", "time-taken": "number"}
    assert store.get_source(rec["id"])["ingest"] == {
        "kind": "iis", "format": "w3c", "column_types": {"sc-status": "text", "cs-method": "text"}}
    with pytest.raises(ValueError, match="name their columns"):
        store.ingest_iis(p, column_types=["text", "text"])
    with pytest.raises(ValueError, match="name their columns"):
        store.ingest_iis(p, column_types={"sc-status": "ipv4"})
    with pytest.raises(ValueError, match="Unknown IIS log format"):
        store.ingest_iis(p, log_format="apache")


def test_the_jobs_routes_carry_the_picked_format_and_types(client, store, tmp_path):
    p = _write(tmp_path, "u_ex240501.log", W3C)
    r = client.post("/api/ingest/jobs/path", json={
        "path": p, "kind": "iis", "log_format": "w3c", "column_types": {"sc-status": "text"}})
    assert r.status_code == 200, r.text
    done = store.wait_for_ingest_job(r.json()["job_id"], timeout=30)
    assert done["status"] == "done", done
    src = store.get_source(done["source_ids"][0])
    assert {c["name"]: c["type"] for c in src["columns"]}["sc-status"] == "text"
    assert src["ingest"]["column_types"] == {"sc-status": "text"}
    # The upload transport, the format picked against the file's name.
    with open(p, "rb") as f:
        r = client.post("/api/ingest/jobs/upload",
                        data={"kind": "iis", "log_format": "ncsa", "column_types": '{"Status": "number"}'},
                        files={"file": ("u_ex240502.log", f, "text/plain")})
    assert r.status_code == 200, r.text
    done = store.wait_for_ingest_job(r.json()["job_id"], timeout=30)
    assert done["status"] == "done", done
    src = store.get_source(done["source_ids"][0])
    assert src["ingest"]["format"] == "ncsa"
    assert [c["name"] for c in src["columns"]][1:8] == iisread.NCSA_COLUMNS
    assert {c["name"]: c["type"] for c in src["columns"]}["Status"] == "number"


def test_a_session_reimport_keeps_the_types_the_preview_set(store, tmp_path):
    p = _write(tmp_path, "u_ex240501.log", W3C)
    store.ingest_iis(p, build_fts=False, column_types={"sc-status": "text"})
    session = store.export_case_session()
    other = Store(str(tmp_path / "b.db"))
    try:
        assert other.import_case_session(session)["sources_reimported"] == 1
        src = other.list_sources()[0]
        assert {c["name"]: c["type"] for c in src["columns"]}["sc-status"] == "text"
    finally:
        other.close()
