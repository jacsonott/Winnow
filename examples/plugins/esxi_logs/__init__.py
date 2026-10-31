"""ESXi / Linux host-log ingest — the parser behind the "ESXi / UAC triage"
profile.

An ESXi support bundle or a UAC Linux collection is a pile of text logs in
several formats. This registers ONE ingest format that recognises the
known log filenames (hostd, vmkernel, auth, shell, vobd, vpxa, syslog,
rhttpproxy, esxupdate — plus their rotated `.0`/`.1`/`.log.1` copies) and
parses each line into ONE shared column schema, so every log in the case
answers the same questions and an overview dashboard can union across all
of them (the store's {{all:header_set:...}} placeholder).

Shared columns, in every table this format makes:

    Timestamp  Log  Severity  Component  PID  CPU  User  SourceIP  Message

`Log` carries the log type (hostd/vmkernel/auth/…), derived from the file
name — that's what lets one merged/union view be sliced back into "SSH
logins" vs "power operations". The rest come from the line itself.

**Read the way IIS logs are (winnow/iisread.py): the line says what its
fields are, and nothing it says is thrown away.** Each line is taken apart
by what it declares, detected per line rather than per file (a syslog.log
holds several shapes):

- **ESXi 7.0 U2 and later**: an ISO stamp, a severity and syslog priority
  (`In(166)`), `app[pid]:`, then the message — `2023-04-18T08:39:57.468Z
  In(166) Hostd[2099552]: [Originator@6876 sub=… opID=… user=root] …`.
- **ESXi 6.x / 7.0**: an ISO stamp, a severity word, `app[pid]` with no
  colon on the vSphere daemons (`info hostd[2099552] [Originator@6876 …]`),
  `app[pid]:` or `app:` on the rest, or vmkernel's `cpuN:world)` prefix.
- **ESXi 5.x / 6.0**: hostd's `[FFF5FB70 info 'Hostsvc.HostSystem' opID=…]`.
- **Linux syslog** (UAC, or a collector's copy of ESXi's): a year-less
  `Mmm dd HH:MM:SS` or an RFC 3339 stamp, a host, `app[pid]:`, the message;
  also RFC 5424 (`<PRI>1 stamp host app procid msgid [SD] msg`) and a
  `<PRI>` in front of any of them.

The structured parts go to columns of their own. VMware's
`[Originator@6876 key=value …]` block is RFC 5424 structured data, and it is
read as W3C's `#Fields:` are: every key any line carries becomes a column,
named as logged (`sub`, `opID`, `sid`), the union across the file in
first-seen order and matched case-insensitively, with `user` going to the
shared User column. vmkernel's world ID is its PID (ESXi's ps calls the
same number the world ID), and the opID in its `cpuN:world opID=…)` prefix
lands in the same opID column hostd's does, which is what ties a task to
the kernel's work on it. A vobd event ID (`esx.audit.ssh.enabled`) is lifted
into EventType, a hostname into Host, a syslog priority's facility into
Facility. Each of those columns exists only in a table whose file has one:
hostd.log gets sub/opID, auth.log from a Linux box gets Host, a file of
plain lines gets the nine shared columns and nothing else.

**One timestamp an analyst can sort by, and the stamp as logged beside it
wherever it had to be worked out.** Timestamp is written in timeparse's
canonical shape, `YYYY-MM-DD HH:MM:SS[.ffffff]`: ESXi's `…T…Z` and syslog's
stamps used to sit side by side as written, and a sort of a merged bundle
(text order, ' ' before 'T', 'Z' after '.') interleaved them wrong. A stamp
with an offset is converted to UTC (every ESXi stamp is already UTC); one
without keeps its own clock. A year-less syslog stamp gets its year from
the file (see `_SyslogYears`). When any stamp in the file had its year
filled in or its zone applied, a "Timestamp (as logged)" column holds
every line's stamp exactly as written — IIS's NCSA name for the same thing
— so the inference sits beside the evidence it came from. A stamp that is
no real time (Feb 29 in a year without one) leaves Timestamp empty and is
still there in the logged column.

Type-specific fields are lifted into the shared columns where they're
cheap and unambiguous: a source IP from auth/rhttpproxy/sshd lines (the
peer of rhttpproxy's `<TCP 'local'>, <TCP 'peer'>` pair, the first address
that isn't loopback elsewhere, IPv6 from sshd's `from … port`), the acting
user from `sudo`/`su`/`Accepted … for <user>`, shell.log's `[user]:`
prefix and hostd/vobd's `user@address`. Everything the parser is unsure of
stays in Message — nothing is dropped.

The file is read as iisread reads one: its encoding sniffed
(store.sniff_text_encoding, so a Windows-1252 byte is a character, not
U+FFFD), lines split on \\n only (a \\r in an attacker-chosen username is
that line's text, not a forged line of its own), and a run of NUL bytes —
what a host that lost power mid-write leaves — read as a gap. Two passes,
as IIS: the first works out the columns (and the year anchor), the second
reads exactly as many lines as the first did, so a log a live host is
still writing can be imported in place.

The parser is a generator, so a multi-GB vmkernel.log stays flat in memory
(ingest_rows streams it).
"""

import ipaddress
import os
import re
from datetime import datetime, timedelta, timezone
from itertools import islice

from winnow import iisread
from winnow import log as wlog
from winnow.store import TEXT_DECODE_ERRORS, sniff_text_encoding

PLUGIN = {
    "name": "esxi-logs",
    "version": "2.0.0",
    "description": "Parse ESXi support-bundle and UAC Linux host logs into one schema for triage.",
}

WINNOW_API_VERSION = 1

# The nine every table has, in this order; the columns a file adds go in
# front of Message (see _columns), so the wide one stays last.
COLUMNS = ["Timestamp", "Log", "Severity", "Component", "PID", "CPU", "User", "SourceIP", "Message"]
COLUMN_TYPES = ["datetime", "text", "text", "text", "text", "text", "text", "text", "text"]

AS_LOGGED = "Timestamp (as logged)"
HOST = "Host"
FACILITY = "Facility"
THREAD = "Thread"
MSGID = "MSGID"
EVENT_TYPE = "EventType"

# base name (lowercased, before the first dot) -> the Log value. Rotated
# copies (hostd.0, auth.log.1) reduce to the same stem, so they carry the
# same Log without extra patterns.
_LOG_TYPES = {
    "hostd": "hostd", "vmkernel": "vmkernel", "auth": "auth", "shell": "shell",
    "vobd": "vobd", "vpxa": "vpxa", "syslog": "syslog", "rhttpproxy": "rhttpproxy",
    "esxupdate": "esxupdate", "vmksummary": "vmksummary", "fdm": "fdm", "sudo": "auth",
    "messages": "syslog", "secure": "auth",
}

# Filename patterns the import UI matches (extension gate + directory scan).
FILENAME_PATTERNS = [f"{stem}*" for stem in _LOG_TYPES]

# How much one row's Message may grow by folding continuation lines onto it.
# The format claims every .log, so a dated header over a 100k-line dump is a
# real input. Folding it all made one 9 MB cell nobody can read, and building
# it by repeated concatenation took minutes. Past the cap, lines are rows of
# their own, as they are in a file with no timestamps at all.
FOLD_MAX_CHARS = 64 * 1024

# ------------------------------------------------------------ line shapes

# The stamp, then a colon that is only punctuation (ESXi 6/7 vobd writes
# "…Z: [UserLevelCorrelator] …"), then the gap before the rest.
_ISO = re.compile(
    r"(?P<stamp>(?P<date>\d{4}-\d{2}-\d{2})[T ](?P<clock>\d{2}:\d{2}:\d{2})(?:[.,](?P<frac>\d+))?"
    r"(?P<zone>Z|[+-]\d{2}(?::?\d{2})?)?)(?::(?=\s))?\s*")
_SYSLOG = re.compile(r"(?P<stamp>(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<clock>\d{2}:\d{2}:\d{2}))\s+")
_PRI = re.compile(r"<(?P<pri>\d{1,3})>")
# RFC 5424 after its <PRI>: VERSION SP TIMESTAMP SP HOSTNAME SP APP-NAME SP
# PROCID SP MSGID SP — each "-" when nil.
_RFC5424 = re.compile(r"1 (?P<ts>\S+) (?P<host>\S+) (?P<app>\S+) (?P<pid>\S+) (?P<msgid>\S+)(?: |$)")

# ESXi 7.0 U2+: "In(166) Hostd[2099552]: " — the severity's first two
# letters and the syslog PRI they came from.
_ESX_PRI = re.compile(r"(?P<abbr>Em|Al|Cr|Er|Wa|No|In|Db)\((?P<pri>\d{1,3})\) (?P<app>[^\s\[\]:]+)(?:\[(?P<pid>\d+)\])?:\s*")
_SEVERITY_WORDS = r"emerg|alert|crit|critical|panic|error|err|warning|warn|notice|info|verbose|trivia|debug"
# ESXi 6.x's vSphere daemons: "info hostd[2099552] [Originator@6876 …]".
_SEV_APP = re.compile(rf"(?P<sev>(?i:{_SEVERITY_WORDS})) (?P<app>[^\s\[\]:]+)\[(?P<pid>\d+)\]:?\s+")
# "sshd[123]: " / "vobd: " — the app named with its colon, no host before it.
_APP = re.compile(r"(?P<app>[A-Za-z][^\s\[\]:]*)(?:\[(?P<pid>\d+)\])?:(?:\s+|$)")
# "hostd[123] " — a pid, so an app, even with no colon after it.
_APP_PID = re.compile(r"(?P<app>[^\s\[\]:]+)\[(?P<pid>\d+)\]\s+")
# "web01 sshd[123]: " — a host first (Linux syslog, a collector's copy).
_HOST_APP = re.compile(r"(?P<host>[^\s\[\]:]+) (?P<app>[^\s\[\]:]+)(?:\[(?P<pid>\d+)\])?:(?:\s+|$)")
# A severity word heading a line with no app: "WARNING: …", "info …".
_SEVERITY = re.compile(rf"(?P<sev>{_SEVERITY_WORDS})\b:?\s*", re.I)
# vmkernel: "cpu8:2097152 opID=6c3b7e5f)WARNING: Comp: msg" — cpu, world,
# and key=value pairs up to the ')'.
_VMK = re.compile(r"cpu(?P<cpu>\d+):(?P<world>\d+)(?P<params>[^)]*)\)\s*")
# vmkernel bodies lead with "Module:" after the (stripped) severity.
_VMK_COMP = re.compile(r"(?P<comp>[A-Za-z][A-Za-z0-9_]*):\s*")
# RFC 5424 structured data — what VMware's [Originator@6876 …] is. Its ID,
# then key=value pairs; values unquoted as VMware writes them (and may hold
# spaces: "sub=Proxy Req 00012"), or quoted as the RFC writes them. An ID
# with no @ and no pairs is not one: shell.log's "[root]:", vobd's
# "[UserLevelCorrelator]".
_SD = re.compile(
    r'\[(?P<id>[^\s\]="]+@\d+|[^\s\]="]+(?= [^\s\]="]+=))'
    r'(?P<params>[^\]"\\]*(?:(?:\\.|"(?:[^"\\]|\\.)*")[^\]"\\]*)*)\]\s*')
# ESXi 5.x / 6.0 hostd: "[FFF5FB70 info 'Hostsvc.HostSystem' opID=… user=…]".
_LEGACY = re.compile(
    r"\[(?P<thread>[0-9A-Fa-f]{4,16}) (?P<sev>[a-z]+) '(?P<sub>[^']*)'(?P<params>[^\]]*)\]\s*")
# esxupdate (Python logging): "esxupdate: 2100637: root: INFO: Command = …".
_ESXUPDATE = re.compile(r"(?P<pid>\d+): (?P<logger>[\w.-]+): (?P<sev>DEBUG|INFO|WARNING|ERROR|CRITICAL): ")
_SHELL_USER = re.compile(r"\[(?P<user>[^\]]+)\]:\s*")
_EVENT_TYPE = re.compile(r"\[(?P<event>(?:esx|vob)\.[A-Za-z0-9_.-]+)\]")

# The shapes nearly every line of a support bundle has, each read by one
# regex: the general path (_parse_line_slow) spends most of its time in
# Python between its regexes, and a vmkernel.log runs to millions of
# lines. Each says what _parse_line_slow would — anything outside the
# narrow shape (an offset, a second bracket, a message starting "[" or
# "cpu", esxupdate) is left to the general path, and
# tests/test_esxi_log_shapes.py holds the two to the same answer. Wherever a
# later part can fail, what comes before it can't give back a space or a
# bracket to make it pass (the lookaheads), so the regex can only match the
# way the general path reads the line.
_FAST_STAMP = r"(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z) "
_FAST_ESX_PRI = (r"(?P<abbr>Em|Al|Cr|Er|Wa|No|In|Db)\((?P<pri>\d{1,3})\) "
                 r"(?P<app8>[^\s\[\]:]+)(?:\[(?P<pid8>\d+)\])?:\s*")
_FAST_APP = re.compile(
    _FAST_STAMP
    + "(?:" + _FAST_ESX_PRI
    + rf"|(?P<sev>(?i:{_SEVERITY_WORDS})) (?P<app6>[^\s\[\]:]+)\[(?P<pid6>\d+)\]:?\s+"
    + rf"|(?!(?i:{_SEVERITY_WORDS})[: ])(?P<app>[A-Za-z][^\s\[\]:]*)(?:\[(?P<pid>\d+)\])?:(?:\s+|$))"
    + r"(?:\[Originator@(?P<orig>[^\]\"]*)(?<!\\)\]\s*)?"
    + r"(?![\[:\s]|cpu)(?P<msg>.*)")
_FAST_VMK = re.compile(
    _FAST_STAMP
    + "(?:" + _FAST_ESX_PRI + ")?"
    + r"cpu(?P<cpu>\d+):(?P<world>\d+)\)\s*(?![\[\s])"
    + rf"(?:(?P<sev>(?i:{_SEVERITY_WORDS}))\b(?=[: ]):?\s*)?"
    + r"(?:(?P<mod>[A-Za-z][A-Za-z0-9_]*):\s*)?"
    + r"(?P<msg>.*)")

_ABBR = {"Em": "emerg", "Al": "alert", "Cr": "crit", "Er": "error",
         "Wa": "warning", "No": "notice", "In": "info", "Db": "debug"}
_PRI_SEVERITY = ("emerg", "alert", "crit", "error", "warning", "notice", "info", "debug")
_FACILITIES = ("kern", "user", "mail", "daemon", "auth", "syslog", "lpr", "news", "uucp", "cron",
               "authpriv", "ftp", "ntp", "audit", "alert", "clock",
               "local0", "local1", "local2", "local3", "local4", "local5", "local6", "local7")

_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}

# --------------------------------------------------- who and from where

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_DOTTED = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}")
_USER_FROM = re.compile(r"\bfor (?:invalid user )?(?P<user>[^\s]+) from\b")
_SUDO_USER = re.compile(r"\b(?:sudo|su)(?:\[\d+\])?:\s*(?P<user>[A-Za-z0-9._-]+)\s*:")
# pam: "session opened for user root by (uid=0)", "… rhost=10.9.9.9  user=root".
_PAM_USER = re.compile(r"\bfor user (?P<user>[^\s(]+)|(?<![\w-])user=(?P<field>[^\s;]+)")
# hostd's "User root@10.0.0.5 logged in", vobd's "'root@10.0.0.5'".
_USER_AT = re.compile(r"(?P<user>[\w.\\$-]+)@(?P<ip>(?:\d{1,3}\.){3}\d{1,3})\b")
# rhttpproxy: "<TCP '127.0.0.1 : 443'>, <TCP '10.0.0.9 : 52344'>" — this end, then the peer.
_TCP_PAIR = re.compile(r"<TCP '[^']*'>, <TCP '(?P<peer>[^' ]+) : \d+'>")
_FROM_PORT = re.compile(r"\bfrom (?P<addr>[0-9A-Fa-f:.%\w]+) port \d+")
_RHOST = re.compile(r"\brhost=(?P<addr>\S+)")

# The " (N)" Winnow's archive expansion puts before a name's last
# extension when the name is taken (`messages` beside the `messages.gz`
# rotation it expands to, or a tar naming one file twice): `messages (2)`,
# `hostd (2).log`, `auth.log (2).1`.
_COPY_MARK = re.compile(r" \(\d+\)$")


def log_type_for(filename: str) -> str:
    # The stem is the name up to its first dot. A renamed duplicate is still
    # the log it was, so the copy mark comes off first; left on, it made
    # `messages (2)` a stem nothing knows, filed as 'other'.
    root, ext = os.path.splitext(os.path.basename(filename))
    stem = (_COPY_MARK.sub("", root) + ext).lower().split(".")[0]
    return _LOG_TYPES.get(stem, "other")


def _address(text: str) -> str:
    """`text` if it is an IP address a connection could come from (an IPv6
    zone suffix allowed), else ""."""
    if _DOTTED.fullmatch(text):
        # The common case, without ipaddress's cost (most of a row's).
        octets = text.split(".")
        if octets[0] in ("127", "0") or any(int(o) > 255 for o in octets):
            return ""
        return text
    if ":" not in text:
        return ""
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return ""
    return "" if ip.is_loopback or ip.is_unspecified else text


def _extract(logtype: str, component: str, message: str) -> tuple[str, str]:
    """(user, source_ip) lifted from the message where the log type makes
    them unambiguous — best-effort, never at the cost of the raw Message."""
    user = ""
    ip = ""
    comp = (component or "").lower()

    if logtype in ("auth", "syslog", "shell") or "ssh" in comp or "sudo" in comp or "su" == comp:
        m = _USER_FROM.search(message)
        if m:
            user = m.group("user")
        m = _SUDO_USER.search(f"{comp}: {message}")
        if m:
            user = user or m.group("user")
        if not user and "user" in message:
            m = _PAM_USER.search(message)
            if m:
                user = m.group("user") or m.group("field")

    if "@" in message and logtype in ("hostd", "vpxa", "vobd", "auth", "syslog", "rhttpproxy"):
        m = _USER_AT.search(message)
        if m and _address(m.group("ip")):
            user = user or m.group("user")
            ip = m.group("ip")

    if not ip and (logtype in ("auth", "rhttpproxy", "vobd", "syslog") or "proxy" in comp or "ssh" in comp):
        m = _TCP_PAIR.search(message) if "<TCP '" in message else None
        if m:
            ip = _address(m.group("peer"))
        else:
            m = _FROM_PORT.search(message) or _RHOST.search(message)
            if m:
                ip = _address(m.group("addr"))
            if not ip:
                ip = next((a for a in _IPV4.findall(message) if _address(a)), "")

    return user, ip


# ------------------------------------------------------------ one line

# A parsed line: what _parse_line found, before the file-level steps
# (the year, the user and address lifted from the message) are applied.
#   kind: "" no stamp, "naive", "utc", "offset" (ISO), "syslog" (year-less),
#   "unread" (RFC 5424's, when it is not ISO)
(_STAMP, _KIND, _SEV, _APPNAME, _PID, _CPU, _USER, _HOSTNAME, _FAC, _THREAD,
 _MSGID, _PARAMS, _EVENT, _MSG) = range(14)


def _iso_kind(zone) -> str:
    """"naive", "utc" or "offset" for an _ISO match's zone group."""
    if not zone:
        return "naive"
    return "utc" if zone == "Z" or not zone.strip("+-0:") else "offset"


def _iso_canonical(stamp: str, kind: str) -> str:
    """An ISO stamp _ISO matched, as "YYYY-MM-DD HH:MM:SS[.ffffff]":
    converted to UTC when it carries an offset that isn't zero, "" when
    that conversion meets a date that isn't one. Sliced rather than parsed
    for the rest, nearly every line: the stamp's parts sit at fixed places
    up to the seconds, and only the fraction's length varies."""
    zone = ""
    if kind != "naive":
        cut = 19
        while cut < len(stamp) and stamp[cut] not in "Z+-":
            cut += 1
        stamp, zone = stamp[:cut], stamp[cut:]
    out = f"{stamp[:10]} {stamp[11:19]}"
    frac = stamp[20:]
    if frac:
        out += "." + (frac[:6] if len(frac) >= 6 else frac.ljust(6, "0"))
    if kind != "offset":
        return out
    digits = zone[1:].replace(":", "")
    offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
    try:
        dt = datetime(int(out[:4]), int(out[5:7]), int(out[8:10]),
                      int(out[11:13]), int(out[14:16]), int(out[17:19]),
                      int(out[20:]) if frac else 0)
        dt = dt - offset if zone[0] == "+" else dt + offset
    except (ValueError, OverflowError):
        return ""
    text = (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d} "
            f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}")
    return f"{text}.{dt.microsecond:06d}" if frac else text


def _params(text: str, into: list) -> None:
    """key=value pairs (an SD element's, a vmkernel prefix's) onto `into`.
    Split at spaces, a word with no "=" joining the value before it:
    VMware leaves values unquoted, and one may hold spaces ("sub=Proxy Req
    00012"). A quoted value, the RFC's way, runs to its closing quote and
    is unquoted. Words before the first pair name nothing and are left."""
    words = text.split()
    if '"' not in text and " =" not in text and text[:1] != "=":
        # VMware's own, nearly always: a key=value per word.
        pairs = [w.split("=", 1) for w in words]
        if pairs and min(map(len, pairs)) == 2:
            into.extend(pairs)
            return
    key = value = None
    for word in words:
        eq = word.find("=")
        if key is not None and (eq <= 0 or _open_quote(value)):
            value += " " + word
            continue
        if key is not None:
            into.append([key, _unquote(value)])
        key, value = (word[:eq], word[eq + 1:]) if eq > 0 else (None, None)
    if key is not None:
        into.append([key, _unquote(value)])


def _open_quote(value: str) -> bool:
    return value[:1] == '"' and (len(value) == 1 or value[-1] != '"' or value[-2:] == '\\"')


def _unquote(value: str) -> str:
    if len(value) > 1 and value[0] == '"' and value[-1] == '"':
        return re.sub(r"\\(.)", r"\1", value[1:-1])
    return value


def _parse_line(line: str) -> list:
    """One logical line (no line ending, not blank) taken apart — see the
    module docstring for the shapes. Message keeps everything not lifted."""
    if line[:1].isdigit():
        m = _FAST_VMK.match(line) if "cpu" in line[20:64] else None
        if m:
            # groups() is every group, in the order the regex names them.
            stamp, abbr, pri, app8, pid8, cpu, world, sev, mod, msg = m.groups()
            fac = ""
            if abbr:
                fac = _facility(int(pri))
                sev = sev.lower() if sev else _ABBR[abbr]
            else:
                sev = sev.lower() if sev else ""
            if msg[:1] == ":":
                msg = msg[1:]
            params = []
            app = mod or app8 or ""
            pid = pid8 or world
        else:
            m = _FAST_APP.match(line)
            if not m:
                return _parse_line_slow(line)
            stamp, abbr, pri, app8, pid8, sev, app6, pid6, app, pid, orig, msg = m.groups()
            app = app8 or app6 or app
            if app[:1] in "eE" and app.lower() == "esxupdate":
                return _parse_line_slow(line)
            fac = cpu = ""
            if abbr:
                sev = _ABBR[abbr]
                fac = _facility(int(pri))
            else:
                sev = sev.lower() if sev else ""
            params = []
            if orig:
                space = orig.find(" ")
                if space >= 0:
                    _params(orig[space:], params)
            pid = pid8 or pid6 or pid or ""
        msg = msg.strip().lstrip("\ufeff")
        rec = [stamp, "utc", sev, app, pid, cpu, "", "", fac, "", "", params, "", msg]
        if "[esx." in msg or "[vob." in msg:
            m = _EVENT_TYPE.search(msg)
            if m:
                rec[_EVENT] = m.group("event")
        return rec
    return _parse_line_slow(line)


def _facility(pri: int) -> str:
    return _FACILITIES[pri >> 3] if pri >> 3 < len(_FACILITIES) else str(pri >> 3)


def _parse_line_slow(line: str) -> list:
    """_parse_line for every shape: the reference the fast paths match."""
    rec = ["", "", "", "", "", "", "", "", "", "", "", [], "", ""]
    rest = line
    pri = None

    if rest[:1] == "<":
        m = _PRI.match(rest)
        if m:
            pri = int(m.group("pri"))
            rest = rest[m.end():]
            m = _RFC5424.match(rest)
            if m:
                return _parse_rfc5424(rec, pri, m, rest[m.end():])

    host_first = numeric_zone = False
    c0 = rest[:1]
    if c0.isdigit():
        m = _ISO.match(rest)
        if m:
            rec[_STAMP], zone = m.group("stamp", "zone")
            rec[_KIND] = _iso_kind(zone)
            numeric_zone = zone not in (None, "Z")
            rest = rest[m.end():]
    elif c0.isupper():
        m = _SYSLOG.match(rest)
        if m and m.group("mon") in _MONTHS:
            rec[_STAMP], rec[_KIND] = m.group("stamp"), "syslog"
            rest = rest[m.end():]
            host_first = True

    if pri is not None:
        rec[_FAC] = _facility(pri)
        rec[_SEV] = _PRI_SEVERITY[pri & 7]

    app = False
    if rest[:3] == "cpu" and _VMK.match(rest):
        pass                       # vmkernel's own prefix, read below
    elif host_first:
        # RFC 3164 as written to a file: the host always follows the stamp.
        first, _, after = rest.partition(" ")
        if first and not first.endswith(":") and after:
            rec[_HOSTNAME], rest = first, after
        m = _APP.match(rest) or _APP_PID.match(rest)
        if m:
            rec[_APPNAME], rec[_PID], rest, app = m.group("app"), m.group("pid") or "", rest[m.end():], True
    else:
        m = _ESX_PRI.match(rest)
        if m:
            rec[_SEV] = _ABBR[m.group("abbr")]
            rec[_FAC] = _facility(int(m.group("pri")))
        else:
            m = _SEV_APP.match(rest)
            if m:
                rec[_SEV] = m.group("sev").lower()
            elif _severity_word(rest):
                m = None                   # "ERROR: …" names no app; _parse_body reads it
            else:
                m = _APP.match(rest) or _APP_PID.match(rest)
                if not m:
                    m = _HOST_APP.match(rest)
                    # A host only where the line can't be a sentence that
                    # happens to have a colon in it: a pid says app, and a
                    # numeric offset is rsyslog's RFC 3339 file format.
                    if m and (m.group("pid") or numeric_zone):
                        rec[_HOSTNAME] = m.group("host")
                    else:
                        m = None
        if m:
            rec[_APPNAME], rec[_PID], rest, app = m.group("app"), m.group("pid") or "", rest[m.end():], True

    return _parse_body(rec, rest, app)


def _severity_word(rest: str):
    """The _SEVERITY match heading `rest` when the word stands alone —
    followed by a colon or a space, not "information" — else None."""
    m = _SEVERITY.match(rest)
    if m and rest[m.end("sev"):m.end("sev") + 1] in (":", " "):
        return m
    return None


def _parse_rfc5424(rec: list, pri: int, m, rest: str) -> list:
    rec[_FAC] = _facility(pri)
    rec[_SEV] = _PRI_SEVERITY[pri & 7]
    ts = m.group("ts")
    if ts != "-":
        im = _ISO.fullmatch(ts)
        rec[_STAMP] = ts
        # A stamp that isn't one to read is kept as logged only.
        rec[_KIND] = _iso_kind(im.group("zone")) if im else "unread"
    for field, slot in (("host", _HOSTNAME), ("app", _APPNAME), ("pid", _PID), ("msgid", _MSGID)):
        if m.group(field) != "-":
            rec[slot] = m.group(field)
    if rest[:2] == "- " or rest == "-":
        rest = rest[2:]
    return _parse_body(rec, rest, bool(rec[_APPNAME]))


def _parse_body(rec: list, rest: str, app: bool) -> list:
    """What follows the stamp and the app: vmkernel's cpu prefix, structured
    data, the 5.x bracket, shell's [user]:, a severity word, a module."""
    vmk = False
    if rest[:3] == "cpu":
        m = _VMK.match(rest)
        if m:
            vmk = True
            rec[_CPU] = m.group("cpu")
            rec[_PID] = rec[_PID] or m.group("world")
            if m.group("params").strip():
                _params(m.group("params"), rec[_PARAMS])
            rest = rest[m.end():]

    if rest[:1] == "[":
        m = _LEGACY.match(rest)
        if m:
            rec[_THREAD] = m.group("thread")
            rec[_SEV] = rec[_SEV] or m.group("sev").lower()
            rec[_PARAMS].append(["sub", m.group("sub")])
            _params(m.group("params"), rec[_PARAMS])
            rest = rest[m.end():]
        else:
            while rest[:1] == "[":
                end = rest.find("]")
                if end > 0 and rest.startswith("[Originator@") and '"' not in rest[:end] \
                        and rest[end - 1] != "\\":
                    # VMware's own block, on nearly every vSphere line: no
                    # quoting to honor, so read without the regex.
                    space = rest.find(" ", 0, end)
                    if space > 0:
                        _params(rest[space:end], rec[_PARAMS])
                    rest = rest[end + 1:].lstrip()
                    continue
                m = _SD.match(rest)
                if not m:
                    break
                _params(m.group("params"), rec[_PARAMS])
                rest = rest[m.end():]
        if rest[:1] == "[":
            m = _SHELL_USER.match(rest)
            if m:                                  # shell.log "[user]: command"
                rec[_USER], rest = m.group("user").strip(), rest[m.end():]

    if vmk or not app:
        m = _severity_word(rest)
        if m:
            # vmkernel's own "WARNING:" outranks the info its syslog
            # priority says; elsewhere a priority the line has wins.
            if vmk or not rec[_SEV]:
                rec[_SEV] = m.group("sev").lower()
            rest = rest[m.end():]
    if vmk:
        m = _VMK_COMP.match(rest)
        if m:
            rec[_APPNAME], rest = m.group("comp"), rest[m.end():]
    elif rec[_APPNAME].lower() == "esxupdate":
        m = _ESXUPDATE.match(rest)
        if m:
            rec[_PID] = rec[_PID] or m.group("pid")
            rec[_SEV] = rec[_SEV] or m.group("sev").lower()
            rec[_PARAMS].append(["sub", m.group("logger")])
            rest = rest[m.end():]

    if rest[:1] == ":":
        rest = rest[1:]
    rest = rest.strip().lstrip("\ufeff")   # RFC 5424's BOM before a UTF-8 MSG
    if "[esx." in rest or "[vob." in rest:
        m = _EVENT_TYPE.search(rest)
        if m:
            rec[_EVENT] = m.group("event")
    rec[_MSG] = rest
    return rec


# ------------------------------------------------------------ the year

def _syslog_stamp(stamp: str):
    """(month, day, hour, minute, second) of a year-less stamp _SYSLOG
    matched, or None for one whose month isn't a month."""
    month = _MONTHS.get(stamp[:3])
    if month is None:
        return None
    day, clock = stamp[3:].split()
    h, mi, s = clock.split(":")
    return (month, int(day), int(h), int(mi), int(s))


class _SyslogYears:
    """Puts the year back on year-less syslog stamps, the way timeparse's
    syslog_bsd op does: walk the file in order and add a year at each
    Dec→Jan wrap (a month lower than the last one). That op asks the
    analyst for the first line's year. A parser can't ask, so the year is
    anchored at the other end. Syslog is appended as it happens, so the
    LAST stamp was written shortly before the file was last modified: it
    belongs to the year of the file's mtime, or to the year before if that
    month and day haven't come yet in the mtime's year (a day of slack
    covers the zone the stamp was written in). Counting the wraps back from
    there gives the first line's year.

    The wraps and the last stamp are counted in the first pass (`see`),
    which reads the whole file anyway; the second pass asks `iso` in the
    same order. The anchor is only as good as the mtime: an import by path
    keeps the file's own, while a browser upload's spool is stamped with
    the upload time (the browser doesn't send it), so a log uploaded long
    after it was written lands in the wrong year."""

    def __init__(self, path: str):
        self.path = path
        self.wraps = 0
        self.last = None            # the last stamp `see` met, as parts
        self.year = None
        self.last_month = None

    def see(self, stamp: str) -> None:
        parts = _syslog_stamp(stamp)
        if parts is None:
            return
        if self.last is not None and parts[0] < self.last[0]:
            self.wraps += 1
        self.last = parts

    def iso(self, stamp: str) -> str:
        parts = _syslog_stamp(stamp)
        if parts is None:
            return ""
        if self.year is None:
            self.year = self._first_year()
        if self.last_month is not None and parts[0] < self.last_month:
            self.year += 1
        self.last_month = parts[0]
        try:
            dt = datetime(self.year, *parts)
        except ValueError:
            # Feb 29 against a resolved year that has none, hour 99: no real
            # time, so no Timestamp; the logged column still holds it.
            return ""
        return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"

    def _first_year(self) -> int:
        try:
            anchor = datetime.fromtimestamp(os.path.getmtime(self.path), timezone.utc)
        except (OSError, OverflowError, ValueError):
            anchor = datetime.now(timezone.utc)
        year = anchor.year
        # Compared as (month, day, h, m, s) tuples, not datetimes, so a
        # stamp that is no real time (Feb 29 in the wrong year, hour 99)
        # still orders instead of raising. A limit that a day of slack has
        # pushed into the next year is later than every stamp in this one.
        limit = anchor + timedelta(days=1)
        if (self.last is not None and limit.year == year
                and self.last > (limit.month, limit.day, limit.hour, limit.minute, limit.second)):
            year -= 1
        return year - self.wraps


# ------------------------------------------------------------ the file

def _records(lines, logtype: str):
    """Rows-to-be, in file order: each a parsed line plus the continuation
    lines folded onto it (a list of raw texts). A line with no timestamp
    after a timestamped line is almost always a continuation (a stack trace,
    a wrapped message), and so is ESXi 7.0 U2+'s "-->" line, which repeats
    its prefix — folded onto that row's Message rather than emitted as an
    orphan. Only onto a row that HAS a timestamp, though: a file with no
    timestamps at all (any .log this plugin claims by extension) used to
    fold into ONE row. Collected in a list and joined once (concatenation
    per line was quadratic), and only up to FOLD_MAX_CHARS."""
    pending = None
    folded: list = []
    size = 0
    for line in lines:
        rec = _parse_line(line)
        if pending is not None and pending[_STAMP]:
            text = None
            if not rec[_STAMP]:
                text = line.strip()
            elif (rec[_MSG][:3] == "-->" and rec[_APPNAME] == pending[_APPNAME]
                  and rec[_PID] == pending[_PID]):
                text = rec[_MSG]
            if text is not None and size + 1 + len(text) <= FOLD_MAX_CHARS:
                folded.append(text)
                size += 1 + len(text)
                continue
        if pending is not None:
            yield pending, folded
        pending, folded, size = rec, [], len(rec[_MSG])
    if pending is not None:
        yield pending, folded


def plan(lines, logtype: str, years: _SyslogYears) -> dict:
    """Pass one over a file's logical lines: the columns its rows need.
    The shared nine always; Timestamp (as logged), Host, Facility, Thread,
    each structured-data key, MSGID and EventType when a line has one.
    Year-less stamps are shown to `years` on the way."""
    need = {AS_LOGGED: False, HOST: False, FACILITY: False, THREAD: False, MSGID: False, EVENT_TYPE: False}
    keys: dict = {}                 # lowercased key -> the spelling first seen
    spelled: set = set()            # every spelling met, so each is lowered once
    for rec, _ in _records(lines, logtype):
        kind = rec[_KIND]
        if kind == "syslog":
            years.see(rec[_STAMP])
            need[AS_LOGGED] = True
        elif kind in ("offset", "unread"):
            need[AS_LOGGED] = True
        if rec[_HOSTNAME] or rec[_FAC] or rec[_THREAD] or rec[_MSGID] or rec[_EVENT]:
            for slot, column in ((_HOSTNAME, HOST), (_FAC, FACILITY), (_THREAD, THREAD),
                                 (_MSGID, MSGID), (_EVENT, EVENT_TYPE)):
                if rec[slot]:
                    need[column] = True
        for key, _ in rec[_PARAMS]:
            if key not in spelled:
                spelled.add(key)
                if key.lower() != "user":
                    keys.setdefault(key.lower(), key)
    return {"need": need, "keys": keys}


def _columns(plan: dict) -> tuple[list, list, dict]:
    """(columns, types, where): the table a plan makes, and the index of
    every column a row fills by name. A key whose name a fixed column
    already has (case-insensitively) gets _1, _2 as sanitize_columns would,
    but here, so the fixed column keeps its own name."""
    need = plan["need"]
    cols = ["Timestamp"] + ([AS_LOGGED] if need[AS_LOGGED] else []) + COLUMNS[1:8]
    cols += [c for c in (HOST, FACILITY, THREAD) if need[c]]
    taken = {c.lower() for c in cols} | {MSGID.lower(), EVENT_TYPE.lower(), "message"}
    key_cols = {}
    for key, spelling in plan["keys"].items():
        name, n = spelling, 0
        while name.lower() in taken:
            n += 1
            name = f"{spelling}_{n}"
        taken.add(name.lower())
        key_cols[key] = len(cols)
        cols.append(name)
    cols += [c for c in (MSGID, EVENT_TYPE) if need[c]]
    cols.append("Message")
    types = ["datetime"] + ["text"] * (len(cols) - 1)
    where = {c: i for i, c in enumerate(cols)}
    where["keys"] = key_cols
    return cols, types, where


def rows(lines, logtype: str, plan: dict, years: _SyslogYears):
    """Pass two: one row per record, aligned to _columns(plan)."""
    cols, _, where = _columns(plan)
    width = len(cols)
    key_at = where["keys"]
    i_logged, i_host, i_fac, i_thread = (where.get(c) for c in (AS_LOGGED, HOST, FACILITY, THREAD))
    i_msgid, i_event = where.get(MSGID), where.get(EVENT_TYPE)
    i_sev, i_comp, i_pid, i_cpu, i_user, i_ip, i_msg = (
        where[c] for c in ("Severity", "Component", "PID", "CPU", "User", "SourceIP", "Message"))
    i_log = where["Log"]
    extras = [(slot, at) for slot, at in ((_HOSTNAME, i_host), (_FAC, i_fac), (_THREAD, i_thread),
                                          (_MSGID, i_msgid), (_EVENT, i_event))]
    # A key's column by its spelling on the line, filled as spellings turn
    # up (-1 for user, which goes to User): one lookup per key, not a
    # lower() and a lookup.
    spelled: dict = {}
    for rec, folded in _records(lines, logtype):
        row = [""] * width
        kind = rec[_KIND]
        if kind in ("utc", "naive", "offset"):
            row[0] = _iso_canonical(rec[_STAMP], kind)
        elif kind == "syslog":
            row[0] = years.iso(rec[_STAMP])
        if i_logged is not None:
            row[i_logged] = rec[_STAMP]
        elif kind in ("syslog", "offset", "unread"):
            raise iisread.FileChanged()
        row[i_log] = logtype
        row[i_sev] = rec[_SEV]
        comp = rec[_APPNAME] or ("" if logtype == "other" else logtype)
        row[i_comp] = comp
        row[i_pid] = rec[_PID]
        row[i_cpu] = rec[_CPU]
        user = rec[_USER]
        for key, value in rec[_PARAMS]:
            at = spelled.get(key)
            if at is None:
                low = key.lower()
                at = spelled[key] = -1 if low == "user" else key_at.get(low)
                if at is None:
                    raise iisread.FileChanged()
            if at < 0:
                user = user or value
            else:
                row[at] = row[at] + " " + value if row[at] else value
        if rec[_HOSTNAME] or rec[_FAC] or rec[_THREAD] or rec[_MSGID] or rec[_EVENT]:
            for slot, at in extras:
                if rec[slot]:
                    if at is None:
                        raise iisread.FileChanged()
                    row[at] = rec[slot]
        message = rec[_MSG]
        if folded:
            message = " ".join([message, *folded]) if message else " ".join(folded)
        found_user, row[i_ip] = _extract(logtype, comp, rec[_MSG])
        row[i_user] = user or found_user
        row[i_msg] = message
        yield row


def _open_log(path: str, encoding: str):
    # newline="\n": a lone \r is the line's own text (store._text_line),
    # and iisread.logical_lines takes one \r off a CRLF line's end.
    return open(path, "r", encoding=encoding, errors=TEXT_DECODE_ERRORS, newline="\n", buffering=1 << 20)


def parse(path, options):
    logtype = (options or {}).get("log_type") or log_type_for(path)
    if logtype == "auto":
        logtype = log_type_for(path)
    encoding = sniff_text_encoding(path)
    years = _SyslogYears(path)
    counted = [0]

    def counting(lines):
        for line in lines:
            counted[0] += 1
            yield line

    with _open_log(path, encoding) as fh:
        planned = plan(counting(iisread.logical_lines(fh, {})), logtype, years)
    columns, types, _ = _columns(planned)

    def read():
        # Exactly the lines pass one planned for: a log still being written
        # has more by now, and a row for a line the plan never saw could
        # need a column the table doesn't have.
        stats: dict = {}
        with _open_log(path, encoding) as fh:
            yield from rows(islice(iisread.logical_lines(fh, stats), counted[0]), logtype, planned, years)
        if stats.get("nul_lines"):
            # Worth knowing on a host: a vmkernel.log padded with NULs is
            # one the host stopped writing without closing — a PSOD, a
            # power cut. The wording is ingest_iis's for the same gap.
            wlog.record("warn", f"{os.path.basename(path)}: {stats['nul_lines']:,} line(s) held runs of "
                                "NUL bytes — the gap a host leaves in its log when it stops without "
                                "closing it (a crash or power loss). The text either side of each run "
                                "was imported.")

    return {
        "columns": columns,
        "column_types": types,
        "rows": read(),
        "name": os.path.basename(path),
    }


# The header set these logs land in, as a placeholder the widgets below
# resolve per case: a bundle arrives as many files, so a board has to span
# every table carrying the set rather than bind to whichever imported first.
_ALL = "{{all:header_set:ESXi / Linux host logs}}"


def register(api):
    api.register_ingest_format(
        id="esxi_log",
        label="ESXi / Linux host log",
        # Both: the known stems by NAME (rotated copies too — hostd.1,
        # vmkernel.0.gz once expanded), and every .log by EXTENSION, parsed
        # as the "other" type. A bundle holds dozens of logs beyond the
        # fourteen stems (vmkwarning, clomd, sdrsinjector, vsanmgmt…) and
        # the parser always handled them — but patterns alone left them
        # behind the folder scan's extension gate, where no include
        # pattern can reach, so "import the bundle" silently imported a
        # third of it. Claiming the extension is also what puts a .log
        # chip in the folder-import modal.
        extensions=[".log"],
        filename_patterns=FILENAME_PATTERNS,
        description=(
            "ESXi support-bundle and UAC Linux logs (hostd, vmkernel, auth, "
            "shell, vobd, vpxa, syslog, rhttpproxy, esxupdate, and rotated "
            "copies), plus any other .log in the bundle, parsed into one "
            "schema: Timestamp, Log type, Severity, Component, PID, CPU, "
            "User, SourceIP, Message — and, where a file has them, the "
            "Originator fields (sub, opID, …), Host, Facility and the vobd "
            "EventType. ESXi 5.x through 8.x and Linux syslog, RFC 3164 and "
            "5424. Powers the ESXi / UAC triage profile's overview dashboard."
        ),
        options=[
            {"name": "log_type", "label": "Log type", "type": "choice",
             "choices": ["auto", "hostd", "vmkernel", "auth", "shell", "vobd",
                         "vpxa", "syslog", "rhttpproxy", "esxupdate", "other"],
             "default": "auto"},
        ],
        parse=parse,
    )

    # The reference implementation of register_dashboard: the overview an
    # analyst would otherwise rebuild on every ESXi case. The full 17-widget
    # board still ships as the "ESXi / UAC triage" PROFILE (plugins +
    # watchlist + board, applied as a unit); this is the board on its own,
    # offered under Dashboards ▸ Library for a case that just needs the
    # overview. Every query is written against {{all:header_set:…}} rather
    # than a table id, because a support bundle arrives as many log files
    # and the ids differ in every case.
    api.register_dashboard(
        id="overview",
        label="ESXi / Linux host overview",
        description="When the host was active, what it logged, and who reached it.",
        widgets=[
            {"title": "Log lines", "source": "sql", "render": "stat",
             "query": {"sql": f"SELECT COUNT(*) FROM {_ALL}"},
             "drill": {"table": _ALL, "where": []}},
            {"title": "Log types", "source": "sql", "render": "bar",
             "query": {"sql": f"SELECT Log AS label, COUNT(*) c FROM {_ALL}"
                              " GROUP BY Log ORDER BY c DESC"},
             "drill": {"table": _ALL, "column": "Log"}},
            {"title": "Failed SSH logins", "source": "sql", "render": "stat",
             "query": {"sql": f"SELECT COUNT(*) FROM {_ALL} WHERE Log='auth'"
                              " AND (Message LIKE '%Failed%' OR Message LIKE '%Invalid user%')"},
             "drill": {"table": _ALL, "tree": {"type": "group", "op": "AND", "children": [
                 {"type": "cond", "column": "Log", "op": "equals", "value": "auth"},
                 {"type": "group", "op": "OR", "children": [
                     {"type": "cond", "column": "Message", "op": "contains", "value": "Failed"},
                     {"type": "cond", "column": "Message", "op": "contains", "value": "Invalid user"}]}]}}},
            {"title": "Top source IPs", "source": "sql", "render": "bar",
             "query": {"sql": f"SELECT SourceIP AS label, COUNT(*) c FROM {_ALL}"
                              " WHERE SourceIP <> '' GROUP BY SourceIP ORDER BY c DESC LIMIT 10"},
             "drill": {"table": _ALL, "column": "SourceIP",
                       "where": [{"column": "SourceIP", "op": "not_empty", "value": ""}]}},
            {"title": "Activity over time", "source": "sql", "render": "histogram",
             "query": {"sql": "SELECT strftime('%Y-%m-%d %H', TS_NORMALIZE(Timestamp)) AS hr,"
                              f" COUNT(*) c FROM {_ALL} WHERE Timestamp <> '' GROUP BY hr ORDER BY hr"},
             "drill": {"table": _ALL, "column": "Timestamp", "bucket": "hour",
                       "where": [{"column": "Timestamp", "op": "not_empty", "value": ""}]}},
        ],
    )
