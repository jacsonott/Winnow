r"""Reading Linux authentication logs for import: `/var/log/secure` (Red
Hat, CentOS, Rocky, Alma, Fedora, Amazon Linux) and its Debian and Ubuntu
twin `/var/log/auth.log`. Both are what rsyslog writes for the auth and
authpriv facilities: one syslog line per event, from sshd, sudo, su, PAM,
systemd-logind, useradd and the rest.

    Nov 19 10:43:56 web01 sshd[2211]: Accepted publickey for alice from 203.0.113.7 port 50122 ssh2: ED25519 SHA256:...
    2024-04-25T10:12:01.123456+00:00 web01 sudo:    alice : TTY=pts/0 ; PWD=/home/alice ; USER=root ; COMMAND=/bin/bash

Stdlib only. Imports iisread for FileChanged and nothing else from the
app: store.py reads the file with iisread's logical lines and hands them
here, as it does for a body file.

**One row per line, under fixed columns** (COLUMNS), so every secure log
and every auth.log in a case has the same shape, merges with the others,
and matches one header set. The syslog header is split into Timestamp,
Host, Process and PID, and Message keeps the rest of the line exactly as
logged. The events an analyst triages are read out of the message into
columns of their own (`classify`): Event (SSH login, SSH login failed,
sudo command, su, Session opened, User created, ...), User, Source IP,
Source port, Auth method, Target user, TTY and Command. A message nothing
here recognizes leaves those empty; nothing is ever taken out of Message.
A line with no syslog header at all is kept whole in Message, counted
(`stats["unparsed"]`), with the other columns empty.

Who is in which column: User is the account the event is about (the
account that logged in or failed to, the one that ran sudo or su, the one
a session was opened for). Target user is the account it acted on: the
account sudo ran a command as (USER=), the one su switched to, the one
useradd made or usermod changed. Source IP is the address as logged,
which is a host name when sshd's UseDNS is on. These logs record
attacker-chosen text (an SSH user name is whatever the other side sent),
so a pattern is matched from the end of the message, where sshd's own
words are, not from the user name forward: a user name that contains
"from 10.0.0.1 port 22" cannot move the address.

**Timestamps.** The traditional format, `Mmm dd hh:mm:ss`, has no year and
no time zone: it is the host's local time. The high-precision format newer
Ubuntu and Debian releases write (RFC 3339, `2024-04-25T10:12:01.123456+00:00`)
has both, and is converted to UTC. Either way Timestamp is written in
timeparse's canonical `YYYY-MM-DD HH:MM:SS[.ffffff]`, so it sorts and
filters like every other datetime column, and "Timestamp (as logged)"
keeps every line's stamp exactly as written beside it.

The year of a traditional stamp is worked out the way the ESXi example
plugin and timeparse's syslog_bsd operation do it: the file is walked in
order and the year goes up by one wherever the month goes down (Dec to
Jan). What is left is the year of the first line. The analyst can give it
(`year`, the import window's Year); otherwise it comes
from the file's modified time, since a log is appended as things happen:
the last line was written shortly before the file was last modified, so it
belongs to that year, or to the year before when its month and day have not
come round yet in that year (a day of slack covers the zone it was written
in). Counting the Dec to Jan wraps back from there gives the first line's
year. `plan` returns the year it settled on, and store.py records it with
the table, so a session re-import dates every line the same way whatever
the file's modified time is by then. A stamp that is no real time (Feb 29
in a year without one) leaves Timestamp empty; the logged stamp keeps it.

**Recognized by name.** `name_says` takes `secure` and `auth.log` and
what logrotate leaves of them (secure-20240505, secure.1, auth.log.1,
auth.log.2 once its .gz is expanded), plus an archive expansion's second
copy (`secure (2)`, `auth (2).log`). The name ranks below a plugin's
claim, unlike the other built-in names: the esxi_logs example plugin reads
these same files into the shared schema the "ESXi / UAC triage" profile's
board counts SSH logins from, and turning that plugin on has to keep doing
what it did. Any other text file can be read as one from the import
window's Read as. merge.js has the twin of name_says (secureLogNamed), and
tests/ui/test_secure_queue.py holds the two to one list.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator

from .iisread import FileChanged

__all__ = ["COLUMNS", "COLUMN_TYPES", "AS_LOGGED", "name_says", "split_line", "classify",
           "first_year", "plan", "rows"]

AS_LOGGED = "Timestamp (as logged)"
COLUMNS = [
    "Timestamp", "Host", "Process", "PID", "Event", "User", "Source IP", "Source port",
    "Auth method", "Target user", "TTY", "Command", "Message", AS_LOGGED,
]
COLUMN_TYPES = [
    "datetime", "text", "text", "number", "text", "text", "text", "number",
    "text", "text", "text", "text", "text", "text",
]
(_TS, _HOST, _PROC, _PID, _EVENT, _USER, _IP, _PORT, _METHOD, _TARGET, _TTY, _CMD,
 _MSG, _LOGGED) = range(len(COLUMNS))
# The eight classify fills, in column order from Event.
_FIELDS = ("event", "user", "ip", "port", "method", "target", "tty", "command")

# secure and auth.log, what logrotate leaves (secure-20240505, secure.1,
# auth.log.1, auth.log.2.old) and an archive expansion's " (N)", which lands
# before the extension (`auth (2).log`) or the rotation suffix
# (`auth.log (2).1`). Kept in step with SECURE_LOG_NAME in merge.js.
_NAME = re.compile(
    r"(?:secure(?: \(\d+\))?|auth(?: \(\d+\))?\.log(?: \(\d+\))?)(?:[.-](?:\d+|old))*(?: \(\d+\))?\Z",
    re.I | re.A)

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}

# The two stamps a line can start with. rsyslog's traditional one (the day
# space-padded: "Nov  9"), with a fraction when a template adds one, and
# RFC 3339 (rsyslog's RSYSLOG_FileFormat; Ubuntu 23.10 and later).
_BSD = re.compile(r"([A-Za-z]{3}) {1,2}(\d{1,2}) (\d{2}):(\d{2}):(\d{2})(\.\d+)? +", re.A)
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})? +",
                  re.A)
# ESXi's own auth.log (named the same) puts its severity and syslog PRI
# after the stamp, and no host: "2023-04-18T08:39:57.468Z In(38) sshd[...]".
_ESX_SEVERITY = re.compile(r"(?:Em|Al|Cr|Er|Wa|No|In|Db)\(\d{1,3}\) ")
# The tag: the program, its PID in brackets, a colon. A program name may
# hold a "]" (GNOME's "gdm-password][2345]:"), never a space or a "[".
_TAG = re.compile(r"([^\s:\[]+?)(?:\[(\d*)\])?:(?: +|\Z)")


def name_says(filename: str) -> bool:
    """Whether a file's name says it is a secure log or an auth.log
    (rotations included). Takes a bare name or a path."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    return _NAME.match(base) is not None


# ------------------------------------------------------------- the header

def _iso_utc(m: re.Match) -> str:
    """An RFC 3339 stamp _ISO matched, as canonical text: converted to UTC
    when it carries an offset, as written when it has none (that is the
    host's clock, as a traditional stamp's is). "" for a date that isn't one."""
    y, mo, d, h, mi, s, frac, zone = m.groups()
    usec = int((frac + "00000")[:6]) if frac else 0
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), usec)
        if zone and zone != "Z":
            digits = zone[1:].replace(":", "")
            offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
            dt = dt - offset if zone[0] == "+" else dt + offset
    except (ValueError, OverflowError):
        return ""
    out = (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d} "
           f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}")
    return f"{out}.{dt.microsecond:06d}" if frac else out


def split_line(line: str) -> tuple | None:
    """One line's syslog header taken apart: (stamp, parts, host, process,
    pid, message), where `stamp` is the stamp as written, `parts` is
    (month, day, h, m, s, usec) for a traditional stamp (year still to be
    worked out) or the canonical text for an RFC 3339 one. None for a line
    that doesn't start with a stamp."""
    m = _BSD.match(line)
    if m is not None:
        month = _MONTHS.get(m.group(1).lower())
        if month is None:
            return None
        frac = m.group(6)
        parts = (month, int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)),
                 int((frac[1:] + "00000")[:6]) if frac else 0, bool(frac))
    else:
        m = _ISO.match(line)
        if m is None:
            return None
        parts = _iso_utc(m)
    stamp = line[:m.end()].rstrip(" ")
    rest = line[m.end():]
    sev = _ESX_SEVERITY.match(rest)
    if sev is not None:
        rest = rest[sev.end():]
    # The host comes first, unless what comes first is already the tag
    # (no host: ESXi, a template that leaves it out).
    host = ""
    tag = _TAG.match(rest)
    if tag is None:
        host, _, rest = rest.partition(" ")
        rest = rest.lstrip(" ")
        tag = _TAG.match(rest)
    if tag is None:
        # "last message repeated 2 times" from an old syslogd: no tag.
        return stamp, parts, host, "", "", rest
    return stamp, parts, host, tag.group(1), tag.group(2) or "", rest[tag.end():]


def _bsd_text(year: int, parts: tuple) -> str:
    month, day, h, mi, s, usec, frac = parts
    try:
        dt = datetime(year, month, day, h, mi, s, usec)
    except ValueError:
        return ""
    out = (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d} "
           f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}")
    return f"{out}.{usec:06d}" if frac else out


# ------------------------------------------------------------- the events
#
# Every pattern runs to the end of the message (fullmatch), so a user name
# is whatever lies between sshd's words at the front and the address and
# port it ends with. An attacker-chosen name that holds "from 10.0.0.1
# port 22" can't move the address: only the tail sshd wrote fits the end.

_ADDR_PORT = r"(?P<ip>\S+) port (?P<port>\d+)"
_PREAUTH = r"(?: \[preauth\])?"
_WHO = r"(?:(?:invalid|authenticating|illegal) user (?P<user>.*?) |user (?P<user2>.*?) )?"

_SSH = {
    "Accepted": [
        (re.compile(rf"Accepted (?P<method>\S+) for (?P<user>.*) from {_ADDR_PORT}(?: ssh2)?(?:: .*)?", re.S),
         "SSH login"),
    ],
    "Failed": [
        (re.compile(rf"Failed (?P<method>\S+) for (?:invalid user )?(?P<user>.*) from {_ADDR_PORT}"
                    rf"(?: ssh2)?(?:: .*?)?{_PREAUTH}", re.S), "SSH login failed"),
    ],
    "Invalid": [
        (re.compile(rf"Invalid user (?P<user>.*) from (?P<ip>\S+)(?: port (?P<port>\d+))?{_PREAUTH}", re.S),
         "SSH invalid user"),
    ],
    "input_userauth_request:": [
        (re.compile(rf"input_userauth_request: invalid user (?P<user>.*?){_PREAUTH}", re.S), "SSH invalid user"),
    ],
    "User": [
        (re.compile(r"User (?P<user>.*) from (?P<ip>\S+) not allowed because .*", re.S), "SSH user not allowed"),
    ],
    "Connection": [
        (re.compile(rf"Connection from {_ADDR_PORT} on \S+ port \d+(?: rdomain .*)?", re.S), "SSH connection"),
        (re.compile(rf"Connection (?:closed|reset) by {_WHO}(?P<ip>\S+?)(?: port (?P<port>\d+))?{_PREAUTH}",
                    re.S), "SSH connection closed"),
    ],
    "Disconnected": [
        (re.compile(rf"Disconnected from {_WHO}{_ADDR_PORT}{_PREAUTH}", re.S), "SSH disconnected"),
    ],
    "Received": [
        (re.compile(rf"Received disconnect from {_ADDR_PORT}:\s*\d+: .*", re.S), "SSH disconnected"),
        (re.compile(r"Received disconnect from (?P<ip>\S+?):\s*\d+: .*", re.S), "SSH disconnected"),
        (re.compile(r"Received signal \d+; terminating\.?"), "SSH server stopped"),
    ],
    "Disconnecting": [
        (re.compile(rf"Disconnecting {_WHO}{_ADDR_PORT}: Too many authentication failures{_PREAUTH}", re.S),
         "SSH too many failures"),
    ],
    "error:": [
        (re.compile(rf"error: maximum authentication attempts exceeded for (?:invalid user )?(?P<user>.*) "
                    rf"from {_ADDR_PORT}(?: ssh2)?{_PREAUTH}", re.S), "SSH too many failures"),
    ],
    "Did": [
        (re.compile(r"Did not receive identification string from (?P<ip>\S+?)(?: port (?P<port>\d+))?"),
         "SSH no identification"),
    ],
    "Unable": [
        (re.compile(rf"Unable to negotiate with {_ADDR_PORT}: .*", re.S), "SSH negotiation failed"),
    ],
    "banner": [
        (re.compile(rf"banner exchange: Connection from {_ADDR_PORT}: .*", re.S), "SSH bad banner"),
    ],
    "Server": [
        (re.compile(r"Server listening on \S+ port \d+\.?"), "SSH server started"),
    ],
}

# "key=value" pairs a PAM failure line carries: "logname= uid=0 euid=0
# tty=ssh ruser= rhost=203.0.113.9  user=root". Values have no spaces.
_PAIRS = re.compile(r"(\w+)=(\S*)")
_PAM = re.compile(r"pam_(?P<mod>\w+)\((?P<svc>[^:()]+)(?::(?P<type>\w+))?\): (?P<rest>.*)", re.S)
_PAM_RULES = [
    # "session opened for user root(uid=0) by alice(uid=1000)"; before
    # systemd 249's PAM, "for user root by alice(uid=1000)".
    (re.compile(r"session opened for user (?P<user>[^\s(]+)(?:\(uid=\d+\))? by .*", re.S), "Session opened"),
    (re.compile(r"session closed for user (?P<user>[^\s(]+).*", re.S), "Session closed"),
    (re.compile(r"authentication failure; (?P<pairs>.*)", re.S), "Authentication failure"),
    (re.compile(r"check pass; user unknown"), "Unknown user"),
    (re.compile(r"password changed for (?P<target>\S+)"), "Password changed"),
    (re.compile(r"Consecutive login failures for user (?P<user>\S+) account temporarily locked"),
     "Account locked"),
]
_PAM_MORE = re.compile(r"PAM \d+ more authentication failures?; (?P<pairs>.*)", re.S)

# sudo's line: the invoking user, " : ", then fields separated by " ; ",
# COMMAND last (and free to hold " ; " itself). A reason comes first when
# sudo refused: "3 incorrect password attempts", "user NOT in sudoers".
_SUDO = re.compile(r"\s*(?P<user>\S+) : (?P<fields>.*)", re.S)

_SU = [
    (re.compile(r"\(to (?P<target>\S+)\) (?P<user>\S+) on (?P<tty>\S+)"), "su"),
    (re.compile(r"FAILED SU \(to (?P<target>\S+)\) (?P<user>\S+) on (?P<tty>\S+)"), "su failed"),
    (re.compile(r"Successful su for (?P<target>\S+) by (?P<user>\S+)"), "su"),
    (re.compile(r"FAILED su for (?P<target>\S+) by (?P<user>\S+)"), "su failed"),
    (re.compile(r"\+ (?P<tty>\S+) (?P<user>[^\s:]+):(?P<target>\S+)"), "su"),
    (re.compile(r"- (?P<tty>\S+) (?P<user>[^\s:]+):(?P<target>\S+)"), "su failed"),
]

# shadow-utils (useradd, groupadd, usermod, userdel, gpasswd, passwd).
_ACCOUNT_PROCS = {"useradd", "groupadd", "usermod", "userdel", "groupdel", "groupmod",
                  "passwd", "gpasswd", "chpasswd", "chage", "newusers", "adduser", "deluser"}
_ACCOUNT = [
    (re.compile(r"new user: name=(?P<target>[^,]+),.*", re.S), "User created"),
    (re.compile(r"new group: name=(?P<target>[^,]+),.*", re.S), "Group created"),
    (re.compile(r"delete user '(?P<target>[^']*)'"), "User deleted"),
    (re.compile(r"(?:removed group '(?P<target>[^']*)'.*|group '(?P<target2>[^']*)' removed.*)", re.S),
     "Group deleted"),
    (re.compile(r"add '(?P<target>[^']*)' to (?:shadow )?group '[^']*'"), "Group member added"),
    (re.compile(r"user (?P<target>\S+) added by (?P<user>\S+) to group \S+"), "Group member added"),
    (re.compile(r"delete '(?P<target>[^']*)' from (?:shadow )?group '[^']*'"), "Group member removed"),
    (re.compile(r"user (?P<target>\S+) removed by (?P<user>\S+) from group \S+"), "Group member removed"),
    (re.compile(r"change user '(?P<target>[^']*)' password"), "Password changed"),
    (re.compile(r"change user '(?P<target>[^']*)' (?:shell|home|UID|GID|name|expiration|inactive).*", re.S),
     "User changed"),
    (re.compile(r"(?:lock|unlock) user '(?P<target>[^']*)' password"), "User changed"),
    (re.compile(r"password for '(?P<target>[^']*)' changed by '(?P<user>[^']*)'"), "Password changed"),
]

_LOGIND = [
    (re.compile(r"New session (?:c?\d+|\S+) of user (?P<user>[^\s.]+)\.?"), "New session"),
    (re.compile(r"Removed session \S+?\.?"), "Session removed"),
]

_LOGIN = [
    (re.compile(r"ROOT LOGIN +ON (?P<tty>\S+)(?: FROM (?P<ip>\S+))?"), "Console login"),
    (re.compile(r"LOGIN ON (?P<tty>\S+) BY (?P<user>\S+)(?: FROM (?P<ip>\S+))?"), "Console login"),
    (re.compile(r"FAILED LOGIN (?:\d+|SESSION) FROM (?P<from>\S+) FOR (?P<user>.*?), .*", re.S),
     "Console login failed"),
]

_REPEATED = re.compile(r"message repeated \d+ times: \[ ?(?P<inner>.*)\]", re.S)


def _from_match(m: re.Match, event: str) -> dict:
    out = {"event": event}
    for k, v in m.groupdict().items():
        if v is None:
            continue
        if k in ("user2",):
            k = "user"
        elif k == "target2":
            k = "target"
        elif k == "from":
            # login's FAILED LOGIN names a terminal or a remote host here.
            k = "tty" if v.startswith(("tty", "pts", "/dev/", "console")) else "ip"
        elif k == "pairs":
            _pam_pairs(v, out)
            continue
        out[k] = v
    return out


def _pam_pairs(text: str, out: dict) -> None:
    """The user, remote host and terminal a PAM failure line names."""
    pairs = dict(_PAIRS.findall(text))
    if pairs.get("user"):
        out["user"] = pairs["user"]
    elif pairs.get("ruser"):
        out["user"] = pairs["ruser"]
    if pairs.get("rhost"):
        out["ip"] = pairs["rhost"]
    if pairs.get("tty"):
        out["tty"] = pairs["tty"]


def _first(rules, text: str) -> dict | None:
    for rx, event in rules:
        m = rx.fullmatch(text)
        if m is not None:
            return _from_match(m, event)
    return None


def _sudo(text: str) -> dict | None:
    m = _SUDO.fullmatch(text)
    if m is None:
        return None
    fields = m.group("fields")
    cut = fields.find("COMMAND=")
    command = fields[cut + 8:] if cut >= 0 else ""
    head = fields[:cut] if cut >= 0 else fields
    pairs: dict = {}
    reason = ""
    for part in head.split(" ; "):
        part = part.strip(" ;")
        key, eq, value = part.partition("=")
        if eq and key.isupper():
            pairs.setdefault(key, value)
        elif part and not pairs:
            reason = part
    if not pairs and not command:
        return None
    out = {"user": m.group("user")}
    if not reason:
        out["event"] = "sudo command"
    elif "incorrect password" in reason:
        out["event"] = "sudo wrong password"
    else:
        out["event"] = "sudo refused"
    if pairs.get("USER"):
        out["target"] = pairs["USER"]
    if pairs.get("TTY"):
        out["tty"] = pairs["TTY"]
    if command:
        out["command"] = command
    return out


def classify(process: str, message: str) -> dict:
    """What a message says happened: a dict with some of event, user, ip,
    port, method, target, tty and command, empty when nothing here
    recognizes it. `process` is the tag's program name."""
    m = _REPEATED.fullmatch(message)
    if m is not None:
        message = m.group("inner")
    proc = process.lower()
    if message.startswith("pam_"):
        p = _PAM.fullmatch(message)
        if p is not None:
            return _first(_PAM_RULES, p.group("rest")) or {}
        return {}
    if proc.startswith("sshd"):
        first = message.split(" ", 1)[0]
        rules = _SSH.get(first)
        if rules is not None:
            return _first(rules, message) or {}
        if first == "PAM":
            p = _PAM_MORE.fullmatch(message)
            if p is not None:
                return _from_match(p, "Authentication failure")
        return {}
    if proc == "sudo":
        return _sudo(message) or {}
    if proc in ("su", "su-l"):
        return _first(_SU, message) or {}
    if proc in _ACCOUNT_PROCS:
        return _first(_ACCOUNT, message) or {}
    if proc == "systemd-logind":
        return _first(_LOGIND, message) or {}
    if proc == "login":
        got = _first(_LOGIN, message) or {}
        if got and not got.get("user") and message.startswith("ROOT"):
            got["user"] = "root"
        return got
    return {}


# ------------------------------------------------------------- the year

def first_year(last: tuple | None, wraps: int, modified: float | None) -> int:
    """The year of a file's first traditional stamp, from the file's
    modified time: the last stamp (`last`, its (month, day, h, m, s)) is in
    that year, or the year before when it is later in the calendar than the
    modified time plus a day; `wraps` Dec to Jan crossings come before it.
    The current time stands in for a modified time that can't be had."""
    try:
        anchor = datetime.fromtimestamp(modified, timezone.utc) if modified is not None else None
    except (OverflowError, OSError, ValueError):
        anchor = None
    if anchor is None:
        anchor = datetime.now(timezone.utc)
    year = anchor.year
    limit = anchor + timedelta(days=1)
    if (last is not None and limit.year == year
            and last > (limit.month, limit.day, limit.hour, limit.minute, limit.second)):
        year -= 1
    return year - wraps


# ------------------------------------------------------------- the file

def plan(lines: Iterable[str], *, year: int | None = None, modified: float | None = None) -> dict:
    """Pass one (store._ingest_planned_lines): whether the file holds a
    syslog line at all (no columns when it doesn't, which the store
    refuses), how many lines don't, and the year its first traditional
    stamp is in: `year` when the analyst gave one, else worked out from
    `modified` (first_year). `year_from` says which ("set", "modified"),
    or is None when no stamp in the file needs a year."""
    good = bad = wraps = 0
    last = None
    for line in lines:
        head = split_line(line)
        if head is None:
            bad += 1
            continue
        good += 1
        parts = head[1]
        if isinstance(parts, tuple):
            if last is not None and parts[0] < last[0]:
                wraps += 1
            last = parts[:5]
    out = {"columns": [], "types": [], "unparsed": bad, "first_year": None, "year_from": None,
           "modified": modified}
    if not good:
        return out
    out["columns"], out["types"] = list(COLUMNS), list(COLUMN_TYPES)
    if last is not None:
        if year is not None:
            out["first_year"], out["year_from"] = int(year), "set"
        else:
            out["first_year"], out["year_from"] = first_year(last, wraps, modified), "modified"
    return out


def rows(lines: Iterable[str], plan: dict, stats: dict) -> Iterator[list[str]]:
    """Pass two: a row per line in COLUMNS order. A line with no syslog
    header is Message's whole, counted in `stats["unparsed"]`; one in a
    file whose pass one found no syslog line at all means the file changed
    between the passes (iisread.FileChanged)."""
    if not plan["columns"]:
        raise FileChanged()
    year = plan.get("first_year")
    last_month = None
    blank = [""] * len(COLUMNS)
    for line in lines:
        head = split_line(line)
        if head is None:
            stats["unparsed"] = stats.get("unparsed", 0) + 1
            row = blank[:]
            row[_MSG] = line
            yield row
            continue
        stamp, parts, host, process, pid, message = head
        if isinstance(parts, tuple):
            if year is None:
                # A traditional stamp pass one never saw: the last line,
                # grown into one between the passes.
                raise FileChanged()
            if last_month is not None and parts[0] < last_month:
                year += 1
            last_month = parts[0]
            ts = _bsd_text(year, parts)
        else:
            ts = parts
        got = classify(process, message)
        row = [ts, host, process, pid]
        row.extend(got.get(k, "") for k in _FIELDS)
        row.append(message)
        row.append(stamp)
        yield row
