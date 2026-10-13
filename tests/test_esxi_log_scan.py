"""Folder import of an ESXi support bundle with the esxi_logs plugin on.

The bug: the plugin matched fourteen log stems by name and nothing by
extension, so every other .log in a bundle — vmkwarning, clomd,
sdrsinjector, vsanmgmt — was excluded by the folder scan's extension
gate, which no include pattern can reach; the plugin's parser had an
"other" type for exactly those files all along. Now the format claims
.log too, and the scan sees the whole bundle.
"""
import os
import re
from pathlib import Path

import pytest

from winnow.plugin_api import PluginRegistry

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "plugins"
LINE = "2026-03-14T08:00:00.123Z info clomd[2098] [Originator@6876 sub=Foo] hello from clomd\n"
BUNDLE = ["var/log/hostd.log", "var/log/hostd.1", "var/log/vpxa.log", "var/log/vmkwarning.log",
          "var/log/clomd.log", "var/log/sdrsinjector.log", "var/run/log/vsanmgmt.log", "commands/localcli_network-ip.txt"]


@pytest.fixture
def esxi_format():
    reg = PluginRegistry()
    reg.load([EXAMPLES])
    fmt = reg.get_format("esxi-logs.esxi_log")
    assert fmt is not None
    return fmt


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "esx-bundle"
    for rel in BUNDLE:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(LINE)
    return root


def test_the_format_claims_every_log_and_the_known_stems(esxi_format):
    assert ".log" in esxi_format.extensions
    for name in ("hostd.log", "vmkwarning.log", "clomd.log", "VSANMGMT.LOG", "hostd.1", "vmkernel.0"):
        assert esxi_format.matches(name), name
    assert not esxi_format.matches("localcli_network-ip.txt")
    assert not esxi_format.matches("report.csv")


def test_folder_scan_sees_the_whole_bundle(store, esxi_format, bundle):
    """The exact call the folder modal makes: default chips (which now
    carry the plugin's .log), the plugin's name patterns, no include."""
    from winnow.store import DEFAULT_IMPORT_EXTENSIONS

    chips = sorted(DEFAULT_IMPORT_EXTENSIONS) + esxi_format.extensions
    r = store.scan_import_directory(str(bundle), extensions=chips, filename_patterns=esxi_format.filename_patterns)
    kinds = {m["rel_path"]: m["kind"] for m in r["matched"]}
    assert kinds == {
        "var/log/hostd.log": "plugin", "var/log/hostd.1": "plugin", "var/log/vpxa.log": "plugin",
        "var/log/vmkwarning.log": "plugin", "var/log/clomd.log": "plugin", "var/log/sdrsinjector.log": "plugin",
        "var/run/log/vsanmgmt.log": "plugin",
        "commands/localcli_network-ip.txt": "csv",
    }
    assert r["excluded"] == []
    # An include of *.log narrows within that, as before
    r2 = store.scan_import_directory(str(bundle), extensions=chips, filename_patterns=esxi_format.filename_patterns,
                                     include_patterns=["*.log"])
    assert {m["rel_path"].split("/")[-1] for m in r2["matched"]} == {
        "hostd.log", "vpxa.log", "vmkwarning.log", "clomd.log", "sdrsinjector.log", "vsanmgmt.log"}


def test_an_unknown_stem_parses_as_other(esxi_format, bundle):
    out = esxi_format.parse(str(bundle / "var/log/clomd.log"), esxi_format.resolve_options({}))
    (row,) = list(out["rows"])
    by = dict(zip(out["columns"], row))
    assert by["Log"] == "other"
    assert by["Timestamp"] == "2026-03-14T08:00:00.123Z"
    assert by["Severity"] == "info" and by["Component"] == "clomd" and by["PID"] == "2098"
    assert by["Message"] == "hello from clomd"


def test_no_chips_means_no_extensions_not_the_defaults(store, bundle, esxi_format):
    r = store.scan_import_directory(str(bundle), extensions=[], filename_patterns=esxi_format.filename_patterns)
    # only the name patterns get anything past the gate now
    assert {m["rel_path"].split("/")[-1] for m in r["matched"]} == {"hostd.log", "hostd.1", "vpxa.log"}
    assert all(e["reason"] == "extension" for e in r["excluded"])
    r_default = store.scan_import_directory(str(bundle), filename_patterns=esxi_format.filename_patterns)
    assert "commands/localcli_network-ip.txt" in {m["rel_path"] for m in r_default["matched"]}


def test_a_log_with_no_esxi_timestamps_is_one_row_per_line(esxi_format, tmp_path):
    """The format claims every .log, so an Apache or application log lands
    here too. Its lines carry no ESXi timestamp; the continuation fold
    (a timestamp-less line joins the row before it) used to fold the whole
    file into ONE row. A continuation only joins a row that HAS a
    timestamp; everything else is a row of its own."""
    p = tmp_path / "access.log"
    p.write_text("".join(f'10.0.0.{i % 250} - - [14/Mar/2026:08:00:{i % 60:02d} +0000] "GET /{i} HTTP/1.1" 200 12\n' for i in range(300)))
    out = esxi_format.parse(str(p), esxi_format.resolve_options({}))
    rows = list(out["rows"])
    assert len(rows) == 300
    # …while a real continuation still folds onto its timestamped line
    p2 = tmp_path / "hostd.log"
    p2.write_text(LINE + "    at Foo.bar (foo.py:1)\n" + LINE)
    out = esxi_format.parse(str(p2), esxi_format.resolve_options({}))
    rows = list(out["rows"])
    assert len(rows) == 2 and rows[0][-1].endswith("at Foo.bar (foo.py:1)")


def _module(fmt):
    import sys
    return sys.modules[fmt.parse.__module__]


def test_continuation_folding_is_linear_and_capped(esxi_format, tmp_path):
    """The format claims every .log, so a dated header followed by a
    100k-line dump folded into one row by repeated `message + " " + line`.
    That is quadratic: 25k lines took 15 s, 100k took 157 s, and the cell
    came out 9 MB. Folding now collects the lines and joins once, and
    stops at FOLD_MAX_CHARS. What doesn't fit becomes rows of its own (the
    shape a timestamp-less file always had), so nothing is lost."""
    import time

    cap = _module(esxi_format).FOLD_MAX_CHARS
    frame = "    at com.vmware.vim.vmomi.core.impl.SomeFrame.invoke(SomeFrame.java:{}) payload=AAAA\n"
    n = 40_000
    p = tmp_path / "app.log"
    with open(p, "w") as f:
        f.write("2024-01-05T10:00:00Z hostd[1]: Exception in thread main\n")
        f.writelines(frame.format(i) for i in range(n))
        f.write(LINE)
    t0 = time.monotonic()
    out = esxi_format.parse(str(p), esxi_format.resolve_options({}))
    rows = list(out["rows"])
    elapsed = time.monotonic() - t0
    assert elapsed < 10, f"{n} continuation lines took {elapsed:.1f}s — folding is quadratic again"

    first = rows[0][-1]
    assert first.startswith("Exception in thread main at com.vmware") and len(first) <= cap
    assert rows[-1][0] == "2026-03-14T08:00:00.123Z" and rows[-1][-1] == "hello from clomd"
    # Every frame exactly once, in order, across the folded row and the overflow rows.
    seen = [int(x) for r in rows[:-1] for x in re.findall(r"SomeFrame\.java:(\d+)\)", r[-1])]
    assert seen == list(range(n))
    assert all(not r[0] for r in rows[1:-1])     # overflow rows don't borrow a timestamp


def _write_syslog(tmp_path, name, lines, mtime_utc):
    import calendar
    p = tmp_path / name
    p.write_text("".join(line + "\n" for line in lines))
    ts = calendar.timegm(mtime_utc.timetuple())
    os.utime(p, (ts, ts))
    return p


def _stamps(esxi_format, p):
    out = esxi_format.parse(str(p), esxi_format.resolve_options({}))
    return [r[0] for r in out["rows"]]


def test_yearless_syslog_stamps_get_their_year_from_the_file(esxi_format, tmp_path, store):
    """Linux syslog stamps carry no year, and the plugin put "Jan  5
    10:00:01" in a column it declares datetime. TS_NORMALIZE can't read a
    year-less stamp, so every Linux line dropped out of the timeframe
    filter and the board's own "Activity over time" chart. The year comes
    from the file instead: its last line belongs to the year of the file's
    modified time (the year before, if that day hasn't come yet), and each
    Dec→Jan wrap is counted back from there."""
    from datetime import datetime

    # Over New Year, written on Jan 3.
    p = _write_syslog(tmp_path, "secure", [
        "Dec 30 23:59:58 host1 sshd[811]: Failed password for root from 10.9.9.9 port 4022 ssh2",
        "Dec 31 10:00:00 host1 sshd[811]: Accepted password for bob from 10.9.9.8 port 4022 ssh2",
        "Jan  1 00:00:01 host1 CRON[900]: (root) CMD (run-parts /etc/cron.hourly)",
        "Jan  2 08:15:00 host1 sudo[901]: bob : TTY=pts/0 ; USER=root ; COMMAND=/bin/sh",
    ], datetime(2025, 1, 3, 12, 0, 0))
    stamps = _stamps(esxi_format, p)
    assert stamps == ["2024-12-30 23:59:58", "2024-12-31 10:00:00", "2025-01-01 00:00:01", "2025-01-02 08:15:00"]
    for s in stamps:
        assert store.db.execute("SELECT TS_NORMALIZE(?)", (s,)).fetchone()[0] is not None, s

    # Two wraps: the first line is two years before the last.
    p = _write_syslog(tmp_path, "messages", [
        "Mar  1 00:00:00 h k: a", "Dec  1 00:00:00 h k: b", "Jan  1 00:00:00 h k: c",
        "Dec  1 00:00:00 h k: d", "Jan  1 00:00:00 h k: e",
    ], datetime(2025, 1, 2))
    assert [s[:7] for s in _stamps(esxi_format, p)] == ["2023-03", "2023-12", "2024-01", "2024-12", "2025-01"]

    # A last line later in the year than the file's mtime is last year's.
    p = _write_syslog(tmp_path, "auth.log", ["Nov  5 09:00:00 h sshd[1]: x"], datetime(2025, 3, 1))
    assert _stamps(esxi_format, p) == ["2024-11-05 09:00:00"]

    # ISO lines are left exactly as written; a stamp that is no date in the
    # resolved year (Feb 29 of 2025) keeps its own text rather than a guess.
    p = _write_syslog(tmp_path, "syslog.log", [
        "2025-02-27T10:00:00Z hostd[1]: iso line",
        "Feb 28 10:00:00 h k: ok",
        "Feb 29 10:00:00 h k: no such day",
    ], datetime(2025, 3, 1))
    assert _stamps(esxi_format, p) == ["2025-02-27T10:00:00Z", "2025-02-28 10:00:00", "Feb 29 10:00:00"]


def test_a_stamp_that_is_no_real_time_never_stops_the_parse(esxi_format, tmp_path):
    """The year pre-pass orders the last stamp against the file's mtime. A
    last line reading hour 99, or a file whose mtime is nonsense, must
    still parse; the stamp that is no time keeps its own text."""
    from datetime import datetime

    p = _write_syslog(tmp_path, "messages", ["Jan  4 10:00:00 h k: a", "Jan  5 99:00:00 h k: b"],
                      datetime(2025, 1, 6))
    assert _stamps(esxi_format, p) == ["2025-01-04 10:00:00", "Jan  5 99:00:00"]
    os.utime(p, (0, 2 ** 62))
    assert len(_stamps(esxi_format, p)) == 2
