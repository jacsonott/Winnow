"""Linux authentication logs (winnow/secureread.py, Store.ingest_secure):
/var/log/secure on Red Hat and its relatives, /var/log/auth.log on Debian
and Ubuntu. Recognized by name (secure, auth.log and their rotations)
wherever a file's type is decided, below a plugin's claim, and read into
one table, a row per line, under fixed columns: the syslog header split
out, the sshd, sudo, su, PAM and account events read out of the message,
the message and the stamp as logged kept whole.

The load-bearing tests are the year and the attacker-chosen user name. A
traditional stamp has no year, and one worked out wrong puts a whole
intrusion in the wrong year with nothing on screen to say so; a session
re-import that works it out again from a file whose modified time has
since changed would move every row. And an SSH user name is whatever the
other side sent: a reader that takes the address after the first "from"
reads one the attacker typed.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import pytest

from winnow import log as wlog
from winnow import secureread
from winnow.store import SECURE_IMPORT_TYPE, IngestCancelled, Store

# A RHEL host's /var/log/secure across a new year: sshd starting, a key
# login, a brute force, sudo to a shell, a new account, su to it.
SECURE = [
    "Dec 30 23:59:01 web01 sshd[1010]: Server listening on 0.0.0.0 port 22.",
    "Dec 31 08:01:02 web01 sshd[2211]: Accepted publickey for alice from 203.0.113.7 port 50122 ssh2: "
    "ED25519 SHA256:Zm9vYmFyYmF6",
    "Dec 31 08:01:02 web01 sshd[2211]: pam_unix(sshd:session): session opened for user alice(uid=1000) by (uid=0)",
    "Jan  2 03:14:15 web01 sshd[3001]: Invalid user oracle from 198.51.100.4 port 41234",
    "Jan  2 03:14:17 web01 sshd[3001]: Failed password for invalid user oracle from 198.51.100.4 port 41234 ssh2",
    "Jan  2 03:14:18 web01 sshd[3002]: pam_unix(sshd:auth): authentication failure; logname= uid=0 euid=0 "
    "tty=ssh ruser= rhost=198.51.100.4  user=root",
    "Jan  2 03:14:20 web01 sshd[3002]: Failed password for root from 198.51.100.4 port 41240 ssh2",
    "Jan  2 03:14:22 web01 sshd[3002]: Accepted password for root from 198.51.100.4 port 41240 ssh2",
    "Jan  2 03:20:00 web01 sudo:    alice : TTY=pts/0 ; PWD=/home/alice ; USER=root ; COMMAND=/bin/bash",
    "Jan  2 03:21:00 web01 useradd[4100]: new user: name=svc_backup, UID=0, GID=0, home=/root, "
    "shell=/bin/bash, from=/dev/pts/0",
    "Jan  2 03:22:00 web01 su: (to svc_backup) alice on pts/0",
]
# Dec 30 to Jan 2 is one wrap: the first line is a year before the last.


def _write(tmp_path, name, lines, *, raw=None, encoding="utf-8", newline="\n", modified=None):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        p.write_bytes(raw)
    else:
        p.write_text(newline.join(lines) + newline, encoding=encoding, newline="")
    if modified is not None:
        t = datetime.fromisoformat(modified).replace(tzinfo=timezone.utc).timestamp()
        os.utime(p, (t, t))
    return str(p)


def _rows(store, rec):
    cols = [c["name"] for c in rec["columns"]]
    return [dict(zip(cols, tuple(r)[1:]))
            for r in store.db.execute(f"SELECT * FROM src_{rec['id']} ORDER BY rid")]


# ------------------------------------------------------------ recognition

@pytest.mark.parametrize("name", [
    "secure", "secure-20240505", "secure.1", "secure.1.old", "Secure", "secure (2)", "secure-20240505 (2)",
    "auth.log", "auth.log.1", "auth.log.2", "auth.log-20240505", "auth (2).log", "auth.log (2).1",
    "/mnt/uac/[root]/var/log/secure", r"C:\cases\web01\var\log\auth.log.1",
])
def test_secure_log_names_are_recognized(name):
    assert secureread.name_says(name)


@pytest.mark.parametrize("name", [
    "secure.gz", "secure.txt", "secure.log", "auth", "auth.log.gz", "auth.log.txt", "my-secure",
    "insecure", "auth.logs", "messages", "syslog", "secure-notes.md", "authlog",
])
def test_other_names_are_not(name):
    assert not secureread.name_says(name)


@pytest.mark.parametrize("line, want", [
    # The traditional stamp, a host, program[pid].
    ("Nov 19 10:43:56 web01 sshd[2211]: hello",
     ("Nov 19 10:43:56", (11, 19, 10, 43, 56, 0, False), "web01", "sshd", "2211", "hello")),
    # A space-padded day, and a program with no PID.
    ("Nov  9 01:02:03 web01 kernel: hello", ("Nov  9 01:02:03", (11, 9, 1, 2, 3, 0, False), "web01", "kernel", "", "hello")),
    # RFC 3339 (Ubuntu 23.10 and later): converted to UTC.
    ("2024-04-25T10:12:01.123456+02:00 web01 sudo: x",
     ("2024-04-25T10:12:01.123456+02:00", "2024-04-25 08:12:01.123456", "web01", "sudo", "", "x")),
    ("2024-04-25T23:30:00-05:00 web01 sshd[1]: x",
     ("2024-04-25T23:30:00-05:00", "2024-04-26 04:30:00", "web01", "sshd", "1", "x")),
    # ESXi's own auth.log: its severity after the stamp, and no host.
    ("2023-04-18T08:39:57.468Z In(38) sshd[2099552]: x",
     ("2023-04-18T08:39:57.468Z", "2023-04-18 08:39:57.468000", "", "sshd", "2099552", "x")),
    # GNOME's tag with a bracket in it.
    ("Nov 19 10:43:56 web01 gdm-password][2345]: x",
     ("Nov 19 10:43:56", (11, 19, 10, 43, 56, 0, False), "web01", "gdm-password]", "2345", "x")),
    # An old syslogd's own line, with no tag.
    ("Nov 19 10:43:56 web01 last message repeated 2 times",
     ("Nov 19 10:43:56", (11, 19, 10, 43, 56, 0, False), "web01", "", "", "last message repeated 2 times")),
])
def test_the_syslog_header_is_split(line, want):
    assert secureread.split_line(line) == want


@pytest.mark.parametrize("line", ["", "not a syslog line", "Foo 19 10:43:56 web01 sshd[1]: x",
                                  "-- Logs begin at Mon 2024-01-01 --", "2024-04-25 web01 sshd: x"])
def test_other_lines_have_no_header(line):
    assert secureread.split_line(line) is None


@pytest.mark.parametrize("process, message, want", [
    ("sshd", "Accepted publickey for alice from 203.0.113.7 port 50122 ssh2: ED25519 SHA256:abc",
     {"event": "SSH login", "user": "alice", "ip": "203.0.113.7", "port": "50122", "method": "publickey"}),
    ("sshd", "Accepted keyboard-interactive/pam for root from 10.0.0.5 port 22 ssh2",
     {"event": "SSH login", "user": "root", "ip": "10.0.0.5", "port": "22", "method": "keyboard-interactive/pam"}),
    # OpenSSH 9.8 moved the per-connection work to sshd-session.
    ("sshd-session", "Accepted password for root from 2001:db8::7 port 22 ssh2",
     {"event": "SSH login", "user": "root", "ip": "2001:db8::7", "port": "22", "method": "password"}),
    ("sshd", "Failed password for invalid user admin from 198.51.100.4 port 41234 ssh2",
     {"event": "SSH login failed", "user": "admin", "ip": "198.51.100.4", "port": "41234", "method": "password"}),
    ("sshd", "Failed publickey for root from 198.51.100.4 port 41234 ssh2: RSA SHA256:xyz",
     {"event": "SSH login failed", "user": "root", "ip": "198.51.100.4", "port": "41234", "method": "publickey"}),
    ("sshd", "Invalid user oracle from 198.51.100.4 port 41234",
     {"event": "SSH invalid user", "user": "oracle", "ip": "198.51.100.4", "port": "41234"}),
    ("sshd", "Invalid user oracle from 198.51.100.4", {"event": "SSH invalid user", "user": "oracle", "ip": "198.51.100.4"}),
    ("sshd", "input_userauth_request: invalid user oracle [preauth]", {"event": "SSH invalid user", "user": "oracle"}),
    ("sshd", "User bob from 10.0.0.9 not allowed because not listed in AllowUsers",
     {"event": "SSH user not allowed", "user": "bob", "ip": "10.0.0.9"}),
    ("sshd", 'Connection from 10.0.0.5 port 50022 on 10.0.0.1 port 22 rdomain ""',
     {"event": "SSH connection", "ip": "10.0.0.5", "port": "50022"}),
    ("sshd", "Connection closed by authenticating user root 198.51.100.4 port 41234 [preauth]",
     {"event": "SSH connection closed", "user": "root", "ip": "198.51.100.4", "port": "41234"}),
    ("sshd", "Connection closed by 198.51.100.4 port 41234 [preauth]",
     {"event": "SSH connection closed", "ip": "198.51.100.4", "port": "41234"}),
    ("sshd", "Connection reset by 198.51.100.4 port 41234 [preauth]",
     {"event": "SSH connection closed", "ip": "198.51.100.4", "port": "41234"}),
    ("sshd", "Disconnected from user alice 10.0.0.5 port 50022",
     {"event": "SSH disconnected", "user": "alice", "ip": "10.0.0.5", "port": "50022"}),
    ("sshd", "Disconnected from invalid user oracle 198.51.100.4 port 41234 [preauth]",
     {"event": "SSH disconnected", "user": "oracle", "ip": "198.51.100.4", "port": "41234"}),
    ("sshd", "Received disconnect from 10.0.0.5 port 50022:11: disconnected by user",
     {"event": "SSH disconnected", "ip": "10.0.0.5", "port": "50022"}),
    ("sshd", "Received disconnect from 10.0.0.5: 11: Bye Bye", {"event": "SSH disconnected", "ip": "10.0.0.5"}),
    ("sshd", "error: maximum authentication attempts exceeded for root from 1.2.3.4 port 5 ssh2 [preauth]",
     {"event": "SSH too many failures", "user": "root", "ip": "1.2.3.4", "port": "5"}),
    ("sshd", "Disconnecting authenticating user root 1.2.3.4 port 5: Too many authentication failures [preauth]",
     {"event": "SSH too many failures", "user": "root", "ip": "1.2.3.4", "port": "5"}),
    ("sshd", "Did not receive identification string from 1.2.3.4 port 5",
     {"event": "SSH no identification", "ip": "1.2.3.4", "port": "5"}),
    ("sshd", "Unable to negotiate with 1.2.3.4 port 5: no matching key exchange method found.",
     {"event": "SSH negotiation failed", "ip": "1.2.3.4", "port": "5"}),
    ("sshd", "banner exchange: Connection from 1.2.3.4 port 5: invalid format",
     {"event": "SSH bad banner", "ip": "1.2.3.4", "port": "5"}),
    ("sshd", "Server listening on 0.0.0.0 port 22.", {"event": "SSH server started"}),
    ("sshd", "Received signal 15; terminating.", {"event": "SSH server stopped"}),
    ("sshd", "PAM 2 more authentication failures; logname= uid=0 euid=0 tty=ssh ruser= rhost=1.2.3.4  user=root",
     {"event": "Authentication failure", "user": "root", "ip": "1.2.3.4", "tty": "ssh"}),
    # rsyslog's repeat reduction: the message it stands for is read.
    ("sshd", "message repeated 3 times: [ Failed password for root from 1.2.3.4 port 22 ssh2]",
     {"event": "SSH login failed", "user": "root", "ip": "1.2.3.4", "port": "22", "method": "password"}),
    # PAM, from any program.
    ("sshd", "pam_unix(sshd:session): session opened for user alice(uid=1000) by (uid=0)",
     {"event": "Session opened", "user": "alice"}),
    ("sudo", "pam_unix(sudo:session): session opened for user root by alice(uid=1000)",
     {"event": "Session opened", "user": "root"}),
    ("sshd", "pam_unix(sshd:session): session closed for user alice", {"event": "Session closed", "user": "alice"}),
    ("su", "pam_unix(su:auth): authentication failure; logname=alice uid=1000 euid=0 tty=pts/0 ruser=alice rhost=  user=root",
     {"event": "Authentication failure", "user": "root", "tty": "pts/0"}),
    ("unix_chkpwd", "pam_unix(sshd:auth): check pass; user unknown", {"event": "Unknown user"}),
    ("passwd", "pam_unix(passwd:chauthtok): password changed for bob", {"event": "Password changed", "target": "bob"}),
    ("sshd", "pam_faillock(sshd:auth): Consecutive login failures for user root account temporarily locked",
     {"event": "Account locked", "user": "root"}),
    # sudo: the command, and the two ways it says no.
    ("sudo", "   alice : TTY=pts/0 ; PWD=/home/alice ; USER=root ; COMMAND=/bin/sh -c id ; whoami",
     {"event": "sudo command", "user": "alice", "target": "root", "tty": "pts/0", "command": "/bin/sh -c id ; whoami"}),
    ("sudo", "alice : TTY=pts/0 ; PWD=/ ; USER=postgres ; GROUP=postgres ; ENV=A=1 ; COMMAND=/usr/bin/psql",
     {"event": "sudo command", "user": "alice", "target": "postgres", "tty": "pts/0", "command": "/usr/bin/psql"}),
    ("sudo", "bob : 3 incorrect password attempts ; TTY=pts/1 ; PWD=/home/bob ; USER=root ; COMMAND=/bin/bash",
     {"event": "sudo wrong password", "user": "bob", "target": "root", "tty": "pts/1", "command": "/bin/bash"}),
    ("sudo", "bob : user NOT in sudoers ; TTY=pts/1 ; PWD=/home/bob ; USER=root ; COMMAND=/bin/ls",
     {"event": "sudo refused", "user": "bob", "target": "root", "tty": "pts/1", "command": "/bin/ls"}),
    # su, in util-linux's words and shadow's.
    ("su", "(to root) alice on pts/0", {"event": "su", "user": "alice", "target": "root", "tty": "pts/0"}),
    ("su", "FAILED SU (to root) alice on pts/0", {"event": "su failed", "user": "alice", "target": "root", "tty": "pts/0"}),
    ("su", "Successful su for root by alice", {"event": "su", "user": "alice", "target": "root"}),
    ("su", "- pts/0 alice:root", {"event": "su failed", "user": "alice", "target": "root", "tty": "pts/0"}),
    # Accounts.
    ("useradd", "new user: name=svc_backup, UID=0, GID=0, home=/root, shell=/bin/bash, from=/dev/pts/0",
     {"event": "User created", "target": "svc_backup"}),
    ("groupadd", "new group: name=devs, GID=1005", {"event": "Group created", "target": "devs"}),
    ("userdel", "delete user 'svc_backup'", {"event": "User deleted", "target": "svc_backup"}),
    ("groupdel", "group 'devs' removed", {"event": "Group deleted", "target": "devs"}),
    ("usermod", "add 'svc_backup' to group 'wheel'", {"event": "Group member added", "target": "svc_backup"}),
    ("gpasswd", "user bob added by root to group wheel", {"event": "Group member added", "user": "root", "target": "bob"}),
    ("usermod", "change user 'bob' password", {"event": "Password changed", "target": "bob"}),
    ("usermod", "change user 'bob' shell from '/sbin/nologin' to '/bin/bash'", {"event": "User changed", "target": "bob"}),
    # Logins.
    ("systemd-logind", "New session 12 of user alice.", {"event": "New session", "user": "alice"}),
    ("systemd-logind", "Removed session 12.", {"event": "Session removed"}),
    ("login", "ROOT LOGIN  ON tty1", {"event": "Console login", "user": "root", "tty": "tty1"}),
    ("login", "FAILED LOGIN 1 FROM tty1 FOR root, Authentication failure",
     {"event": "Console login failed", "user": "root", "tty": "tty1"}),
    # Nothing recognized: nothing read out.
    ("polkitd", "Registered Authentication Agent for unix-process:1234:5678", {}),
    ("sshd", "Accepted password for root", {}),
    ("CROND", "Accepted password for root from 1.2.3.4 port 22 ssh2", {}),
    ("sudo", "alice : unable to resolve host web01: Name or service not known", {}),
])
def test_events_are_read_out_of_the_message(process, message, want):
    assert secureread.classify(process, message) == want


def test_an_attacker_chosen_user_name_cannot_move_the_address():
    """The user name sshd logs is whatever the other side sent. Taking the
    address after the first "from" would read one the attacker typed."""
    got = secureread.classify(
        "sshd", "Failed password for invalid user x from 10.9.9.9 port 1 from 198.51.100.4 port 41234 ssh2")
    assert (got["user"], got["ip"], got["port"]) == ("x from 10.9.9.9 port 1", "198.51.100.4", "41234")
    got = secureread.classify("sshd", "Invalid user a b from 10.9.9.9 port 1 from 198.51.100.4 port 41234")
    assert (got["user"], got["ip"]) == ("a b from 10.9.9.9 port 1", "198.51.100.4")


# ------------------------------------------------------------- the year

@pytest.mark.parametrize("last, wraps, modified, want", [
    ((5, 4, 12, 0, 0), 0, "2024-05-05T03:00:00", 2024),     # a few days before the file's date
    ((5, 5, 23, 0, 0), 0, "2024-05-05T03:00:00", 2024),     # later the same day: a zone ahead of UTC
    ((5, 6, 1, 0, 0), 0, "2024-05-05T03:00:00", 2024),      # a day of slack
    ((5, 7, 1, 0, 0), 0, "2024-05-05T03:00:00", 2023),      # not come round yet this year
    ((12, 31, 23, 0, 0), 0, "2025-01-02T09:00:00", 2024),   # a file copied just after New Year
    ((12, 31, 23, 0, 0), 0, "2024-12-31T20:00:00", 2024),   # a day of slack into next year
    ((1, 2, 3, 0, 0), 1, "2026-01-05T12:00:00", 2025),      # one Dec -> Jan wrap before the last line
    ((1, 2, 3, 0, 0), 3, "2026-01-05T12:00:00", 2023),
])
def test_the_first_year_comes_from_the_files_date(last, wraps, modified, want):
    t = datetime.fromisoformat(modified).replace(tzinfo=timezone.utc).timestamp()
    assert secureread.first_year(last, wraps, t) == want


def test_a_secure_log_imports_a_row_per_line(store, tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(wlog, "record", lambda level, msg: logged.append((level, msg)))
    rec = store.ingest_secure(_write(tmp_path, "secure", SECURE, modified="2026-01-05T12:00:00"), build_fts=False)
    assert rec["row_count"] == len(SECURE) and rec["unparsed_lines"] == 0
    assert [(c["name"], c["type"]) for c in rec["columns"]] == list(zip(secureread.COLUMNS, secureread.COLUMN_TYPES))
    # Dec 30 is the year before Jan 2, and Jan 2 is the year of the file's date.
    assert (rec["syslog_year"], rec["year_from"]) == (2025, "modified")
    assert store.get_source(rec["id"])["ingest"] == {"kind": "secure", "year": 2025}
    rows = _rows(store, rec)
    assert [r["Timestamp"] for r in rows[:4]] == [
        "2025-12-30 23:59:01", "2025-12-31 08:01:02", "2025-12-31 08:01:02", "2026-01-02 03:14:15"]
    assert [r["Timestamp (as logged)"] for r in rows[:4]] == [
        "Dec 30 23:59:01", "Dec 31 08:01:02", "Dec 31 08:01:02", "Jan  2 03:14:15"]
    assert [r["Event"] for r in rows] == [
        "SSH server started", "SSH login", "Session opened", "SSH invalid user", "SSH login failed",
        "Authentication failure", "SSH login failed", "SSH login", "sudo command", "User created", "su"]
    login = rows[7]
    assert (login["Host"], login["Process"], login["PID"], login["User"], login["Source IP"], login["Source port"],
            login["Auth method"]) == ("web01", "sshd", "3002", "root", "198.51.100.4", "41240", "password")
    sudo = rows[8]
    assert (sudo["User"], sudo["Target user"], sudo["TTY"], sudo["Command"]) == ("alice", "root", "pts/0", "/bin/bash")
    assert sudo["Message"] == "alice : TTY=pts/0 ; PWD=/home/alice ; USER=root ; COMMAND=/bin/bash"
    assert (rows[10]["User"], rows[10]["Target user"]) == ("alice", "svc_backup")
    # Said, since it is an inference.
    assert any(level == "info" and "dated 2025 from the file's modified date (2026-01-05)" in msg
               for level, msg in logged)


def test_the_year_can_be_given(store, tmp_path):
    rec = store.ingest_secure(_write(tmp_path, "secure", SECURE, modified="2026-01-05T12:00:00"),
                              build_fts=False, year=2019)
    assert (rec["syslog_year"], rec["year_from"]) == (2019, "set")
    rows = _rows(store, rec)
    assert rows[0]["Timestamp"] == "2019-12-30 23:59:01" and rows[-1]["Timestamp"] == "2020-01-02 03:22:00"
    assert store.get_source(rec["id"])["ingest"] == {"kind": "secure", "year": 2019}
    with pytest.raises(ValueError, match="between 1970 and 2100"):
        store.ingest_secure(_write(tmp_path, "secure.1", SECURE), year=24)


def test_a_day_that_is_no_day_in_that_year_is_kept_as_logged(store, tmp_path):
    rec = store.ingest_secure(_write(tmp_path, "secure", ["Feb 29 10:00:00 web01 sshd[1]: Server listening on :: port 22."]),
                              build_fts=False, year=2023)
    row = _rows(store, rec)[0]
    assert (row["Timestamp"], row["Timestamp (as logged)"], row["Event"]) == ("", "Feb 29 10:00:00", "SSH server started")


def test_rfc3339_stamps_need_no_year(store, tmp_path):
    lines = ["2024-04-25T10:12:01.123456+02:00 web01 sshd[9]: Accepted password for root from 10.0.0.5 port 22 ssh2",
             "2024-04-25T10:12:02.000001+02:00 web01 sudo:     root : TTY=pts/0 ; PWD=/root ; USER=root ; COMMAND=/bin/id"]
    rec = store.ingest_secure(_write(tmp_path, "auth.log", lines), build_fts=False)
    assert (rec["syslog_year"], rec["year_from"]) == (None, None)
    assert store.get_source(rec["id"])["ingest"] == {"kind": "secure"}
    rows = _rows(store, rec)
    assert [r["Timestamp"] for r in rows] == ["2024-04-25 08:12:01.123456", "2024-04-25 08:12:02.000001"]
    assert rows[1]["Event"] == "sudo command"


def test_lines_that_are_not_syslog_lines_are_kept_whole(store, tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(wlog, "record", lambda level, msg: logged.append((level, msg)))
    rec = store.ingest_secure(_write(tmp_path, "secure", [SECURE[1], "  continued text", SECURE[3]],
                                     modified="2026-01-05T12:00:00"), build_fts=False)
    assert rec["unparsed_lines"] == 1 and rec["row_count"] == 3
    odd = _rows(store, rec)[1]
    assert odd["Message"] == "  continued text" and odd["Timestamp"] == "" and odd["Event"] == ""
    assert any(level == "warn" and "1 line was not a syslog line" in msg for level, msg in logged)


def test_crlf_bom_and_ansi_files_read_as_written(store, tmp_path):
    rec = store.ingest_secure(_write(tmp_path, "secure", SECURE[:2], encoding="utf-8-sig", newline="\r\n"),
                              build_fts=False, year=2024)
    rows = _rows(store, rec)
    assert rows[0]["Timestamp"] == "2024-12-30 23:59:01" and rows[1]["Source IP"] == "203.0.113.7"
    raw = "Jan  2 03:14:15 web01 sshd[3001]: Invalid user jos\u00e9 from 198.51.100.4 port 41234\n".encode("cp1252")
    rec = store.ingest_secure(_write(tmp_path, "auth.log", None, raw=raw), build_fts=False, year=2024)
    assert _rows(store, rec)[0]["User"] == "jos\u00e9"


def test_a_file_with_no_syslog_lines_is_refused(store, tmp_path):
    with pytest.raises(ValueError, match="File is empty"):
        store.ingest_secure(_write(tmp_path, "secure", None, raw=b""))
    with pytest.raises(ValueError, match="no syslog lines"):
        store.ingest_secure(_write(tmp_path, "auth.log", ["just words", "a,b,c"]))
    assert store.list_sources() == []


def test_cancel_leaves_no_table(store, tmp_path):
    p = _write(tmp_path, "secure", SECURE * 4_000)
    for calls_before_yes in (0, 2):   # pass one (before the table exists), then pass two
        seen = [0]

        def cancel():
            seen[0] += 1
            return seen[0] > calls_before_yes

        with pytest.raises(IngestCancelled):
            store.ingest_secure(p, build_fts=False, cancel=cancel)
        assert store.list_sources() == []


def test_a_secure_log_table_gets_its_timeline_summary(store, tmp_path):
    rec = store.ingest_secure(_write(tmp_path, "secure", SECURE), build_fts=False)
    match = store._timeline_summary_match(store.get_source(rec["id"]))
    assert match is not None and match[0] == "Linux secure / auth.log"


def test_no_datetime_column_is_suggested_for_the_stamp_as_logged(store, tmp_path, write_csv):
    """Every import used to end on "Timestamp (as logged) looks like BSD
    syslog · Add datetime column": a second Timestamp, dated by the
    derived op's year rule rather than the one this table recorded. The
    raw stamp beside a datetime the reader made is not a hint; one with no
    datetime beside it still is."""
    rec = store.ingest_secure(_write(tmp_path, "secure", SECURE, modified="2026-01-05T12:00:00"),
                              build_fts=False)
    assert store.detect_source_suggestions(rec["id"]) == []
    alone = store.ingest_csv(write_csv([["Timestamp (as logged)", "Message"]]
                                       + [[line[:15], line[16:]] for line in SECURE]), name="raw.csv")
    assert [s["column"] for s in store.detect_source_suggestions(alone["id"])] == ["Timestamp (as logged)"]


def test_the_preview_is_the_start_of_the_table():
    t = datetime(2026, 1, 5, 12, tzinfo=timezone.utc).timestamp()
    body = Store.preview_secure_text("\n".join(SECURE) + "\n", modified=t)
    assert body["columns"] == secureread.COLUMNS and body["inferred_types"] == secureread.COLUMN_TYPES
    assert len(body["sample_rows"]) == len(SECURE) and body["unparsed_lines"] == 0 and body["complete"] is True
    assert (body["syslog_year"], body["year_from"], body["modified"]) == (2025, "modified", t)
    assert body["sample_rows"][1][:5] == ["2025-12-31 08:01:02", "web01", "sshd", "2211", "SSH login"]
    body = Store.preview_secure_text("\n".join(SECURE) + "\n", year=2001)
    assert (body["syslog_year"], body["year_from"]) == (2001, "set")
    assert body["sample_rows"][0][0] == "2001-12-30 23:59:01"
    # A sample that isn't the whole file loses its last, maybe cut, line.
    body = Store.preview_secure_text(SECURE[0] + "\n" + SECURE[1][:30], complete=False, modified=t)
    assert len(body["sample_rows"]) == 1 and body["complete"] is False
    with pytest.raises(ValueError, match="No syslog lines"):
        Store.preview_secure_text("just words\n")


# ------------------------------------------------------------- routing

def test_the_server_routes_secure_logs_to_their_reader(tmp_path):
    import server

    for name in ("secure", "secure-20240505", "auth.log", "auth.log.1"):
        assert server._ingest_kind_for_path(_write(tmp_path, name, SECURE[:1])) == "secure", name
    # Only by name: the same lines under another name are text, as before.
    assert server._ingest_kind_for_path(_write(tmp_path, "messages", SECURE[:1])) == "text"
    assert server._ingest_kind_for_path(_write(tmp_path, "secure.txt", SECURE[:1])) == "csv"
    # A session saved before tables recorded their reader was saved before
    # this reader, so its secure log was read as text.
    assert Store._guess_session_reader("/e/var/log/secure") == {"kind": "text"}


def test_folder_scan_matches_secure_logs_by_name_below_a_plugins_claim(store, tmp_path):
    root = tmp_path / "uac"
    _write(root, "[root]/var/log/secure", SECURE)
    _write(root, "[root]/var/log/secure-20240505", SECURE)
    _write(root, "[root]/var/log/auth.log.1", SECURE)
    _write(root, "[root]/var/log/messages", SECURE)

    r = store.scan_import_directory(str(root))   # the defaults include secure logs
    assert {m["rel_path"]: m["kind"] for m in r["matched"]} == {
        "[root]/var/log/secure": "secure", "[root]/var/log/secure-20240505": "secure",
        "[root]/var/log/auth.log.1": "secure"}
    reasons = {e["rel_path"]: e["reason"] for e in r["excluded"]}
    assert reasons["[root]/var/log/messages"] == "extension"
    # A list without the token (a profile saved before the chip existed)
    # leaves them out, and says why.
    r = store.scan_import_directory(str(root), extensions=[".txt"])
    assert {e["rel_path"]: e["reason"] for e in r["excluded"]}["[root]/var/log/secure"] == "secure logs not selected"
    r = store.scan_import_directory(str(root), extensions=[SECURE_IMPORT_TYPE])
    assert len(r["matched"]) == 3
    # A plugin that claims them (esxi_logs: .log and secure*, auth*) keeps
    # them: the ESXi / UAC triage board counts SSH logins from its tables.
    r = store.scan_import_directory(str(root), extensions=[".log", SECURE_IMPORT_TYPE],
                                    filename_patterns=["secure*", "auth*"])
    assert {m["rel_path"]: m["kind"] for m in r["matched"]} == {
        "[root]/var/log/secure": "plugin", "[root]/var/log/secure-20240505": "plugin",
        "[root]/var/log/auth.log.1": "plugin"}


def test_open_with_leaves_a_secure_log_to_a_plugin_that_claims_it(client, store, tmp_path, monkeypatch):
    """Unlike a body file or an IIS log: esxi_logs reads secure and
    auth.log for the ESXi / UAC triage board, so its claim stands."""
    import server

    asked = []
    monkeypatch.setattr(server.PLUGINS, "format_for_filename", lambda name: asked.append(name) or None)
    out = server._assoc_ingest(_write(tmp_path, "secure", SECURE))
    assert asked == ["secure"] and out["kind"] == "secure"
    assert store.wait_for_ingest_job(out["job_id"], timeout=30)["status"] == "done"


# -------------------------------------------------------- routes and jobs

def test_the_jobs_routes_import_a_secure_log(client, store, tmp_path):
    p = _write(tmp_path, "secure", SECURE + ["not one"], modified="2026-01-05T12:00:00")
    r = client.post("/api/ingest/jobs/path", json={"path": p})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "secure"
    done = store.wait_for_ingest_job(r.json()["job_id"], timeout=30)
    assert done["status"] == "done", done
    res = done["result"][0]
    assert (res["unparsed_lines"], res["syslog_year"], res["year_from"]) == (1, 2025, "modified")
    # The year given in the import window.
    r = client.post("/api/ingest/jobs/path", json={"path": p, "kind": "secure", "year": 2019})
    done = store.wait_for_ingest_job(r.json()["job_id"], timeout=30)
    assert (done["result"][0]["syslog_year"], done["result"][0]["year_from"]) == (2019, "set")
    r = client.post("/api/ingest/jobs/path", json={"path": p, "kind": "secure", "year": 19})
    assert r.status_code == 400 and "between 1970 and 2100" in r.json()["detail"]


def test_an_upload_keeps_the_picked_files_date(client, store, tmp_path):
    """A browser upload is spooled now, but the year is worked out from
    the picked file's date (lastModified), which the import window sends."""
    p = _write(tmp_path, "secure", SECURE)
    t = datetime(2026, 1, 5, 12, tzinfo=timezone.utc).timestamp()
    with open(p, "rb") as f:
        r = client.post("/api/ingest/jobs/upload", data={"kind": "secure", "modified": str(t)},
                        files={"file": ("secure-20260105", f, "text/plain")})
    assert r.status_code == 200, r.text
    done = store.wait_for_ingest_job(r.json()["job_id"], timeout=30)
    assert done["status"] == "done", done
    assert (done["result"][0]["syslog_year"], done["result"][0]["year_from"]) == (2025, "modified")
    src = store.get_source(done["source_ids"][0])
    assert src["name"] == "secure-20260105" and src["ingest"] == {"kind": "secure", "year": 2025}
    with open(p, "rb") as f:
        r = client.post("/api/ingest/jobs/upload", data={"kind": "secure", "year": "2001"},
                        files={"file": ("secure", f, "text/plain")})
    done = store.wait_for_ingest_job(r.json()["job_id"], timeout=30)
    assert done["result"][0]["syslog_year"] == 2001


def test_the_previews_read_a_secure_log(client, tmp_path, monkeypatch):
    import server

    p = _write(tmp_path, "secure", SECURE, modified="2026-01-05T12:00:00")
    body = client.post("/api/ingest/preview/path", json={"path": p}).json()   # kind from the name
    assert body["columns"] == secureread.COLUMNS and len(body["sample_rows"]) == len(SECURE)
    assert (body["syslog_year"], body["year_from"]) == (2025, "modified")
    body = client.post("/api/ingest/preview/path", json={"path": p, "year": 2011}).json()
    assert body["sample_rows"][0][0] == "2011-12-30 23:59:01"
    # Read as Secure log: any text file, asked for by kind.
    other = _write(tmp_path, "messages.txt", SECURE)
    assert client.post("/api/ingest/preview/path",
                       json={"path": other, "kind": "secure"}).json()["columns"] == secureread.COLUMNS
    t = datetime(2026, 1, 5, 12, tzinfo=timezone.utc).timestamp()
    with open(p, "rb") as f:
        r = client.post("/api/ingest/preview", data={"kind": "secure", "modified": str(t)},
                        files={"file": ("secure", f, "text/plain")})
    assert r.status_code == 200, r.text
    assert r.json()["inferred_types"] == secureread.COLUMN_TYPES and r.json()["syslog_year"] == 2025
    r = client.post("/api/ingest/preview", data={"kind": "secure"},
                    files={"file": ("notes.txt", b"just words\n", "text/plain")})
    assert r.status_code == 400 and "No syslog lines" in r.json()["detail"]
    monkeypatch.setattr(server, "PREVIEW_SAMPLE_BYTES", 200)
    assert client.post("/api/ingest/preview/path", json={"path": p, "kind": "secure"}).json()["complete"] is False


def test_a_session_reimports_a_secure_log_in_the_year_it_was_read(store, tmp_path):
    """The year is recorded with the table: the file's modified time has
    moved on by the time the session is loaded (copied to another
    machine, touched by a tool), and the rows are dated as they were."""
    p = _write(tmp_path, "secure", SECURE, modified="2026-01-05T12:00:00")
    sid = store.ingest_secure(p, build_fts=False)["id"]
    tag = store.upsert_tag(None, "Brute force", "#ff0000", None)["id"]
    store.set_tags(sid, [8], tag, True)
    session = store.export_case_session()

    later = time.time()
    os.utime(p, (later, later))
    other = Store(str(tmp_path / "b.db"))
    try:
        res = other.import_case_session(session)
        assert res["sources_reimported"] == 1 and res["warnings"] == []
        src = other.list_sources()[0]
        assert src["ingest"] == {"kind": "secure", "year": 2025}
        rid = other.db.execute("SELECT rid FROM row_tags WHERE source_id=?", (src["id"],)).fetchone()[0]
        ts, event = other.db.execute(f'SELECT "Timestamp", "Event" FROM {src["table_name"]} WHERE rid=?',
                                     (rid,)).fetchone()
        assert (ts, event) == ("2026-01-02 03:14:22", "SSH login")
    finally:
        other.close()
