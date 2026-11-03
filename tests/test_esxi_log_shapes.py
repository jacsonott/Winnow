"""The esxi_logs plugin reading each line shape the way iisread reads an
IIS log: the line says what its fields are, and nothing it says is thrown
away.

What it replaced read two shapes. On ESXi 7.0 U2 and later (`In(166)
Hostd[pid]:`) it found no severity and no app; it dropped hostd's
Originator block (sub, opID, user) and vmkernel's world ID and opID; it
left every timestamp as logged, so a merged bundle sorted ESXi's "T…Z"
stamps and syslog's apart; it never applied an offset; and it read a
Linux hostname as the app. Each test below is one of those, in the terms
an analyst would have met it.
"""
import itertools
import os
import sys
from pathlib import Path

import pytest

from winnow.plugin_api import PluginRegistry

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "plugins"
SHARED = ["Timestamp", "Log", "Severity", "Component", "PID", "CPU", "User", "SourceIP"]


@pytest.fixture(scope="module")
def esxi_format():
    reg = PluginRegistry()
    reg.load([EXAMPLES])
    fmt = reg.get_format("esxi-logs.esxi_log")
    assert fmt is not None
    return fmt


@pytest.fixture(scope="module")
def mod(esxi_format):
    return sys.modules[esxi_format.parse.__module__]


def _parse(esxi_format, path):
    out = esxi_format.parse(str(path), esxi_format.resolve_options({}))
    return out["columns"], list(out["rows"])


def _read(esxi_format, tmp_path, name, text):
    """The rows of a file holding `text`, as {column: value} dicts."""
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    cols, rows = _parse(esxi_format, p)
    return cols, [dict(zip(cols, r)) for r in rows]


# ------------------------------------------------------------ ESXi shapes

def test_esxi_8_hostd_line_is_read_in_full(esxi_format, tmp_path):
    cols, (row,) = _read(esxi_format, tmp_path, "hostd.log",
                         "2023-04-18T08:39:57.468Z In(166) Hostd[2099552]: [Originator@6876 "
                         "sub=Vimsvc.TaskManager opID=a1b2-c3 sid=52a1 user=vpxuser:VSPHERE.LOCAL\\Administrator] "
                         "Task Created : haTask-ha-root-pool\n")
    assert row["Timestamp"] == "2023-04-18 08:39:57.468000"
    assert (row["Severity"], row["Component"], row["PID"]) == ("info", "Hostd", "2099552")
    assert row["Facility"] == "local4"                      # 166 >> 3
    assert (row["sub"], row["opID"], row["sid"]) == ("Vimsvc.TaskManager", "a1b2-c3", "52a1")
    assert row["User"] == "vpxuser:VSPHERE.LOCAL\\Administrator"
    assert row["Message"] == "Task Created : haTask-ha-root-pool"
    # user went to User, so there's no user column of its own
    assert "user" not in [c.lower() for c in cols if c != "User"]


def test_esxi_8_continuation_lines_fold_onto_their_row(esxi_format, tmp_path):
    """ESXi 7.0 U2+ repeats the whole prefix on a "-->" continuation, so
    those lines carry a timestamp and used to come out as rows of their
    own — a fault's body scattered over a dozen rows that say nothing on
    their own."""
    _, rows = _read(esxi_format, tmp_path, "hostd.log", (
        "2023-04-18T08:39:57.468Z In(166) Hostd[2099552]: [Originator@6876 sub=Default] Fault raised\n"
        "2023-04-18T08:39:57.470Z In(166) Hostd[2099552]: --> (vmodl.fault.SystemError) {\n"
        "2023-04-18T08:39:57.470Z In(166) Hostd[2099552]: -->    reason = \"x\"\n"
        "2023-04-18T08:39:57.470Z In(166) Hostd[2099552]: --> }\n"
        "2023-04-18T08:39:58.000Z In(166) Hostd[2099552]: [Originator@6876 sub=Default] next\n"))
    assert len(rows) == 2
    assert rows[0]["Message"].startswith("Fault raised --> (vmodl.fault.SystemError) {")
    assert rows[0]["Message"].endswith("--> }")
    assert rows[1]["Message"] == "next"


def test_esxi_6_hostd_login_names_its_user_and_address(esxi_format, tmp_path):
    _, (row,) = _read(esxi_format, tmp_path, "hostd.log",
                      "2020-06-01T10:00:00.123Z info hostd[2099552] [Originator@6876 sub=Vimsvc.ha-eventmgr "
                      "opID=esxui-1a2b user=root] Event 123 : User root@10.1.2.3 logged in as VMware-client/6.5.0\n")
    assert (row["Severity"], row["Component"], row["PID"]) == ("info", "hostd", "2099552")
    assert (row["sub"], row["opID"]) == ("Vimsvc.ha-eventmgr", "esxui-1a2b")
    assert (row["User"], row["SourceIP"]) == ("root", "10.1.2.3")


def test_esxi_5_hostd_bracket(esxi_format, tmp_path):
    _, (row,) = _read(esxi_format, tmp_path, "hostd.log",
                      "2014-03-01T10:00:00.123Z [6CBE7B90 info 'Hostsvc.HaHost' "
                      "opID=HB-host-31@71-7e5d0c3c-a1 user=vpxuser] Successfully logged in\n")
    assert (row["Thread"], row["Severity"], row["sub"]) == ("6CBE7B90", "info", "Hostsvc.HaHost")
    assert (row["opID"], row["User"]) == ("HB-host-31@71-7e5d0c3c-a1", "vpxuser")
    assert row["Message"] == "Successfully logged in"


def test_vmkernel_world_and_opid_land_beside_hostds(esxi_format, tmp_path):
    """The opID in vmkernel's prefix is the one hostd logged for the same
    task; in the same column, one filter follows a task into the kernel."""
    cols, rows = _read(esxi_format, tmp_path, "vmkernel.log", (
        "2020-06-01T10:00:00.123Z cpu12:2099552 opID=7b1e2c3d)WARNING: NFS: 1234: Lost connection\n"
        "2023-04-18T08:39:57.468Z In(182) vmkernel: cpu3:2097729)ScsiDeviceIO: 4325: Cmd failed\n"))
    six, eight = rows
    assert (six["CPU"], six["PID"], six["opID"]) == ("12", "2099552", "7b1e2c3d")
    assert (six["Severity"], six["Component"], six["Message"]) == ("warning", "NFS", "1234: Lost connection")
    assert (eight["CPU"], eight["PID"], eight["Component"]) == ("3", "2097729", "ScsiDeviceIO")
    assert (eight["Severity"], eight["Facility"]) == ("info", "local6")


def test_vobd_event_id_and_session_user(esxi_format, tmp_path):
    _, rows = _read(esxi_format, tmp_path, "vobd.log", (
        "2020-06-01T10:00:00.123Z: [UserLevelCorrelator] 1234567us: [esx.audit.ssh.enabled] SSH access has been enabled.\n"
        "2020-06-01T10:00:00.456Z: [GenericCorrelator] 1234567us: [vob.user.ssh.session.opened] "
        "SSH session was opened for 'root@10.1.2.3'.\n"))
    assert [r["EventType"] for r in rows] == ["esx.audit.ssh.enabled", "vob.user.ssh.session.opened"]
    assert (rows[1]["User"], rows[1]["SourceIP"]) == ("root", "10.1.2.3")
    assert rows[0]["Timestamp"] == "2020-06-01 10:00:00.123000"


def test_esxi_8_shell_user(esxi_format, tmp_path):
    _, (row,) = _read(esxi_format, tmp_path, "shell.log",
                      "2023-04-18T08:39:57.468Z In(14) shell[2101234]: [root]: esxcli system ssh set\n")
    assert (row["User"], row["PID"], row["Facility"]) == ("root", "2101234", "user")
    assert row["Message"] == "esxcli system ssh set"


def test_rhttpproxy_names_the_peer_not_the_host(esxi_format, tmp_path):
    """The pair is <local>, <peer>: the first address on the line is the
    host's own, which made every proxied request look like it came from
    the host."""
    _, (row,) = _read(esxi_format, tmp_path, "rhttpproxy.log",
                      "2020-06-01T10:00:00.123Z info rhttpproxy[2099555] [Originator@6876 sub=Proxy Req 00012] "
                      "New proxy client <SSL(<io_obj p:0x1, h:23, <TCP '10.0.0.1 : 443'>, <TCP '10.9.8.7 : 55123'>>)>\n")
    assert row["SourceIP"] == "10.9.8.7"
    assert row["sub"] == "Proxy Req 00012"       # VMware's values aren't quoted, and may hold spaces


# ------------------------------------------------------------ Linux shapes

def test_rfc3339_syslog_gets_its_host_and_its_offset_applied(esxi_format, tmp_path):
    cols, (row,) = _read(esxi_format, tmp_path, "auth.log",
                         "2025-01-05T10:00:00.123456+02:00 web01 sshd[811]: Accepted publickey for bob "
                         "from 2001:db8::5 port 51022 ssh2\n")
    assert row["Timestamp"] == "2025-01-05 08:00:00.123456"
    assert row["Timestamp (as logged)"] == "2025-01-05T10:00:00.123456+02:00"
    assert (row["Host"], row["Component"], row["PID"]) == ("web01", "sshd", "811")
    assert (row["User"], row["SourceIP"]) == ("bob", "2001:db8::5")
    assert cols[:2] == ["Timestamp", "Timestamp (as logged)"]


def test_yearless_syslog_host_is_not_the_app(esxi_format, tmp_path):
    _, (row,) = _read(esxi_format, tmp_path, "auth.log",
                      "Jan  5 10:00:00 esxi01 sshd[811]: Failed password for root from 10.9.9.9 port 22 ssh2\n")
    assert (row["Host"], row["Component"], row["PID"]) == ("esxi01", "sshd", "811")
    assert (row["User"], row["SourceIP"]) == ("root", "10.9.9.9")
    assert row["Timestamp (as logged)"] == "Jan  5 10:00:00"


def test_rfc5424(esxi_format, tmp_path):
    _, (row,) = _read(esxi_format, tmp_path, "syslog.log",
                      '<34>1 2025-01-05T10:00:00.003Z mymachine.example.com su - ID47 [exampleSDID@32473 iut="3" '
                      'eventSource="Application" eventID="1011"] \'su root\' failed for lonvick on /dev/pts/8\n')
    assert (row["Facility"], row["Severity"]) == ("auth", "crit")
    assert (row["Host"], row["Component"], row["PID"], row["MSGID"]) == ("mymachine.example.com", "su", "", "ID47")
    assert (row["iut"], row["eventSource"], row["eventID"]) == ("3", "Application", "1011")
    assert row["Message"] == "'su root' failed for lonvick on /dev/pts/8"


# ------------------------------------------------------------ columns

def test_a_column_only_where_the_file_has_one(esxi_format, tmp_path):
    cols, _ = _read(esxi_format, tmp_path, "shell.log", "2024-01-05T13:22:10Z shell[9001]: [root]: ls\n")
    assert cols == SHARED + ["Message"]
    cols, _ = _read(esxi_format, tmp_path, "clomd.log", "plain text\nmore text\n")
    assert cols == SHARED + ["Message"]


def test_keys_are_a_union_in_first_seen_order_matched_case_insensitively(esxi_format, tmp_path):
    """#Fields: in an IIS log, the Originator block here: a key any line
    carries is a column, spelled as first logged, and a key a fixed column
    already names is suffixed rather than shadowing it."""
    cols, rows = _read(esxi_format, tmp_path, "hostd.log", (
        "2024-01-05T13:22:01Z info hostd[1] [Originator@6876 sub=a opID=x1] one\n"
        "2024-01-05T13:22:02Z info hostd[1] [Originator@6876 opid=x2 Message=m Log=l] two\n"
        "2024-01-05T13:22:03Z info hostd[1] [Originator@6876 sid=s] three\n"))
    assert cols == SHARED + ["sub", "opID", "Message_1", "Log_1", "sid", "Message"]
    assert [r["opID"] for r in rows] == ["x1", "x2", ""]
    assert (rows[1]["Message_1"], rows[1]["Log_1"], rows[1]["Log"]) == ("m", "l", "hostd")
    assert rows[1]["Message"] == "two"


def test_canonical_stamps_sort_in_time_order(esxi_format, tmp_path):
    """Text order is the grid's sort for a text column, and a merge of a
    bundle puts these side by side: the old "…T…Z" next to "Jan  5 …"
    sorted by letter, not by time."""
    import calendar
    from datetime import datetime

    p = tmp_path / "syslog.log"
    p.write_text("".join(line + "\n" for line in [
        "2025-01-05T10:00:00.5Z hostd[1]: d",
        "Jan  5 09:00:00 h k: b",
        "2025-01-05T11:30:00+02:00 h k[1]: c",       # 09:30 UTC
        "2025-01-05T08:59:59.999999Z hostd[1]: a",
        "2025-01-05T10:00:00Z hostd[1]: c2",
    ]))
    ts = calendar.timegm(datetime(2025, 1, 6).timetuple())
    os.utime(p, (ts, ts))
    cols, rows = _parse(esxi_format, p)
    by_stamp = [r[-1] for r in sorted(rows, key=lambda r: r[0])]
    assert by_stamp == ["a", "b", "c", "c2", "d"]


def test_an_offset_stamp_that_is_no_date_is_left_out(esxi_format, tmp_path):
    _, (row,) = _read(esxi_format, tmp_path, "auth.log", "2025-02-30T10:00:00+02:00 h sshd[1]: x\n")
    assert row["Timestamp"] == "" and row["Timestamp (as logged)"] == "2025-02-30T10:00:00+02:00"


# ------------------------------------------------------------ the file

def test_nul_runs_are_a_gap_and_say_so(esxi_format, tmp_path, monkeypatch):
    """What a host leaves in vmkernel.log when it loses power mid-write."""
    from winnow import log as wlog

    logged = []
    monkeypatch.setattr(wlog, "record", lambda level, msg: logged.append((level, msg)))
    p = tmp_path / "vmkernel.log"
    p.write_bytes(b"2024-01-05T10:00:00Z cpu1:2)before\n" + b"\0" * 4096
                  + b"\n2024-01-05T10:05:00Z cpu1:2)after\n")
    _, rows = _parse(esxi_format, p)
    assert [r[-1] for r in rows] == ["before", "after"]
    assert len(logged) == 1 and logged[0][0] == "warn" and "NUL" in logged[0][1]


def test_a_carriage_return_cannot_forge_a_row(esxi_format, tmp_path):
    """A username is attacker-chosen. Read with universal newlines, a \\r
    in one ended the line there, and whatever followed became a row of its
    own — timestamp, user and all."""
    p = tmp_path / "auth.log"
    p.write_bytes(b"Jan  5 10:00:00 h sshd[1]: Invalid user x\rJan  5 10:00:01 h sshd[1]: "
                  b"Accepted password for root from 10.6.6.6 port 22 ssh2\n")
    cols, rows = _parse(esxi_format, p)
    (row,) = rows
    row = dict(zip(cols, row))
    assert row["Timestamp (as logged)"] == "Jan  5 10:00:00"
    assert row["Message"].startswith("Invalid user x\rJan  5 10:00:01")


def test_a_windows_1252_byte_is_its_character(esxi_format, tmp_path):
    p = tmp_path / "shell.log"
    p.write_bytes(b"2024-01-05T13:22:10Z shell[9001]: [root]: echo caf\xe9\n")
    _, (row,) = _parse(esxi_format, p)
    assert row[-1] == "echo café"


def test_lines_written_after_the_plan_are_left_for_next_time(esxi_format, tmp_path):
    """A log a live host is still writing: the second pass reads as many
    lines as the first planned for, as iisread's does, so a line arriving
    between them can't need a column the table doesn't have."""
    p = tmp_path / "hostd.log"
    p.write_text("2024-01-05T13:22:01Z info hostd[1] [Originator@6876 sub=a] one\n")
    out = esxi_format.parse(str(p), esxi_format.resolve_options({}))
    with open(p, "a") as f:
        f.write("2024-01-05T13:22:02Z info hostd[1] [Originator@6876 brand_new_key=b] two\n")
    rows = list(out["rows"])
    assert [r[-1] for r in rows] == ["one"]


# ------------------------------------------------------------ fast paths

STAMPS = ["2023-04-18T08:39:57.468Z ", "2023-04-18T08:39:57Z ", "2023-04-18T08:39:57.468+02:00 ",
          "2023-04-18T08:39:57.468 ", "2023-04-18T08:39:57.468Z: ", "2023-04-18T08:39:57.468Z  ",
          "Jan  5 10:00:00 ", "<14>", "<14>2023-04-18T08:39:57Z ", ""]
HEADS = ["In(166) Hostd[2099552]: ", "Wa(180) vmkwarning: ", "In(182) vmkernel: ", "info hostd[2099552] ",
         "verbose vpxa[2099]: ", "sshd[5]: ", "Hostd: ", "critical hostd[1] ", "info: ", "information: ",
         "web01 sshd[811]: ", "", "In(999) foo: ", "In(14) esxupdate[1]: ", "esxupdate: ", "Er(11) shell[9]: ",
         "WARNING hostd[1]: ", "cpu: "]
BODIES = ["[Originator@6876 sub=Vimsvc opID=a user=root] msg", '[Originator@6876 sub="Vim svc"] msg',
          "[Originator@6876 sub=a\\] x] msg", "[Originator@6876] msg [esx.audit.x]",
          "[Originator@6876 sub=Proxy Req 00012] New proxy client",
          "[Originator@6876 sub=a] [Originator@6876 b=c] two blocks",
          "cpu3:2097152)WARNING: NetDVS: 1234: port down", "cpu5:1 opID=x)Activating plugin", "cpu5:1)",
          "cpu5:1)::x", "cpu5:1) [x] y", "cpu5:1)critical: x", "cpu5:1)Warning Something: x", "cpufreq: hello",
          "[root]: esxcli", " [root]: ls", "[6CBE7B90 info 'Hostsvc.HaHost' opID=a user=b] ok",
          "2100637: root: INFO: Command = x", "﻿bom", ":x", "", "WARNING: thing", "--> continuation",
          "Accepted password for bob from 10.0.0.5 port 22 ssh2", "[VpxLRO] -- BEGIN",
          "[esx.audit.ssh.enabled] SSH on", "Event 1 : User root@10.1.2.3 logged in", "a=b=c d"]
CORPUS = ["".join(parts) for parts in itertools.product(STAMPS, HEADS, BODIES)]


def test_the_fast_paths_read_every_line_as_the_general_path_does(mod):
    """Nearly every line of a bundle goes through one regex (_FAST_APP,
    _FAST_VMK) instead of _parse_line_slow's chain — it is most of what
    keeps a multi-million-line vmkernel.log's import time in line with the
    old parser's. They are only a speed-up if they never answer
    differently, so every combination of stamp, header and body the
    parser knows is put to both."""
    differ = [line for line in CORPUS if mod._parse_line(line) != mod._parse_line_slow(line)]
    assert not differ, differ[:5]


def test_every_shape_together_in_one_file(esxi_format, tmp_path):
    """Pass one plans what pass two fills: a line that needs a column the
    plan didn't make stops the import (iisread.FileChanged)."""
    p = tmp_path / "syslog.log"
    # utf-8, not the locale's: the corpus holds a BOM, which Windows'
    # cp1252 can't write.
    p.write_text("\n".join(line for line in CORPUS if line.strip()) + "\n", encoding="utf-8")
    cols, rows = _parse(esxi_format, p)
    assert rows and all(len(r) == len(cols) for r in rows)
