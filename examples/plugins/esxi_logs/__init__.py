"""ESXi / Linux host-log ingest — the parser behind the "ESXi / UAC triage"
profile.

An ESXi support bundle or a UAC Linux collection is a pile of text logs in
several formats. This registers ONE ingest format that recognises the
known log filenames (hostd, vmkernel, auth, shell, vobd, vpxa, syslog,
rhttpproxy, esxupdate — plus their rotated `.0`/`.1`/`.log.1` copies) and
parses each line into ONE shared column schema, so every log in the case
answers the same questions and an overview dashboard can union across all
of them (the store's {{all:header_set:...}} placeholder).

Shared columns:

    Timestamp  Log  Severity  Component  PID  CPU  User  SourceIP  Message

`Log` carries the log type (hostd/vmkernel/auth/…), derived from the file
name — that's what lets one merged/union view be sliced back into "SSH
logins" vs "power operations". The rest come from the line itself.

Two line shapes cover the corpus, detected per line, not per file (a
syslog.log holds both):

- **ESXi / vSphere**: an ISO-8601 timestamp, an optional severity word, a
  `component[pid]` (or vmkernel's `cpuN:world)` prefix), then the message.
- **Linux syslog** (UAC): a year-less `Mmm dd HH:MM:SS`, a host, a
  `process[pid]:`, then the message. Timestamp is a datetime column, so the
  year is resolved rather than left out (see `_SyslogYears`) and the stamp
  is written as ISO `YYYY-MM-DD HH:MM:SS`, no offset, because syslog
  doesn't say which zone it was in.

Type-specific fields are lifted into the shared columns where they're
cheap and unambiguous: a source IP from auth/rhttpproxy/sshd lines, the
acting user from `sudo`/`su`/`Accepted … for <user>` and shell.log's
`[user]:` prefix. Everything the parser is unsure of stays in Message —
nothing is dropped.

Stdlib only (airgap rule). The parser is a generator, so a multi-GB
vmkernel.log stays flat in memory (ingest_rows streams it).
"""

import os
import re
from datetime import datetime, timedelta, timezone

PLUGIN = {
    "name": "esxi-logs",
    "version": "1.0.0",
    "description": "Parse ESXi support-bundle and UAC Linux host logs into one schema for triage.",
}

WINNOW_API_VERSION = 1

COLUMNS = ["Timestamp", "Log", "Severity", "Component", "PID", "CPU", "User", "SourceIP", "Message"]
COLUMN_TYPES = ["datetime", "text", "text", "text", "text", "text", "text", "text", "text"]

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

_ISO = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s*(?P<rest>.*)$")
_SYSLOG = re.compile(
    r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(?P<rest>.*)$")
# vmkernel: "cpu8:2097152 opID=...)WARNING: Comp: msg" — the cpu:world up to ')'
_VMK = re.compile(r"^cpu(?P<cpu>\d+):(?P<world>\d+)[^)]*\)\s*(?P<rest>.*)$")
# "component[pid]" or "component[pid] [Originator...]" (vSphere) / "proc[pid]:" (syslog)
_COMP_PID = re.compile(r"^(?P<comp>[A-Za-z0-9_./-]+)\[(?P<pid>\d+)\]:?\s*(?P<rest>.*)$")
_SEVERITY = re.compile(r"^(?P<sev>error|warning|warn|info|verbose|debug|notice|crit|alert|emerg)\b\s*(?P<rest>.*)$", re.I)
# An "[Originator@... sub=Foo]" tag vSphere logs put before the message —
# matched narrowly so shell.log's own [user]: prefix is never mistaken for it.
_ORIGINATOR = re.compile(r"^\[[^\]]*(?:Originator@|sub=|opID=)[^\]]*\]\s*(?P<rest>.*)$")
# vmkernel bodies lead with "Component:" after the (stripped) severity.
_VMK_COMP = re.compile(r"^(?P<comp>[A-Za-z0-9_]+):\s*(?P<rest>.*)$")

# Year-less syslog stamp, split. Only ever applied to a ts _SYSLOG matched.
_SYSLOG_PARTS = re.compile(r"^([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})$")
_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}

# How much one row's Message may grow by folding continuation lines onto it.
# The format claims every .log, so a dated header over a 100k-line dump is a
# real input. Folding it all made one 9 MB cell nobody can read, and building
# it by repeated concatenation took minutes. Past the cap, lines are rows of
# their own, as they are in a file with no timestamps at all.
FOLD_MAX_CHARS = 64 * 1024

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_USER_FROM = re.compile(r"\bfor (?:invalid user )?(?P<user>[^\s]+) from\b")
_SUDO_USER = re.compile(r"\b(?:sudo|su)(?:\[\d+\])?:\s*(?P<user>[A-Za-z0-9._-]+)\s*:")
_SHELL_USER = re.compile(r"^\[(?P<user>[^\]]+)\]:\s*(?P<rest>.*)$")


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


def _extract(logtype: str, component: str, message: str) -> tuple[str, str]:
    """(user, source_ip) lifted from the message where the log type makes
    them unambiguous — best-effort, never at the cost of the raw Message."""
    user = ""
    ip = ""
    comp = (component or "").lower()

    if logtype == "shell":
        m = _SHELL_USER.match(message)
        if m:
            user = m.group("user").strip()

    if logtype in ("auth", "syslog", "shell") or "ssh" in comp or "sudo" in comp or "su" == comp:
        m = _USER_FROM.search(message)
        if m:
            user = user or m.group("user")
        m = _SUDO_USER.search(f"{comp}: {message}")
        if m:
            user = user or m.group("user")

    if logtype in ("auth", "rhttpproxy", "vobd", "syslog") or "proxy" in comp or "ssh" in comp:
        m = _IPV4.search(message)
        if m and m.group(0) not in ("0.0.0.0", "127.0.0.1"):
            ip = m.group(0)

    return user, ip


def _parse_line(line: str, logtype: str) -> list | None:
    line = line.rstrip("\n").rstrip("\r")
    if not line.strip():
        return None

    ts = ""
    rest = line
    m = _ISO.match(line)
    if m:
        ts, rest = m.group("ts"), m.group("rest")
    else:
        m = _SYSLOG.match(line)
        if m:
            ts, rest = m.group("ts"), m.group("rest")
            # syslog: drop the hostname token that follows the timestamp
            parts = rest.split(None, 1)
            if len(parts) == 2 and not parts[0].endswith(":"):
                rest = parts[1]

    cpu = ""
    m = _VMK.match(rest)
    if m:
        cpu, rest = m.group("cpu"), m.group("rest")

    severity = ""
    m = _SEVERITY.match(rest)
    if m:
        severity, rest = m.group("sev").lower(), m.group("rest")

    component = ""
    pid = ""
    m = _COMP_PID.match(rest)
    if m:
        component, pid, rest = m.group("comp"), m.group("pid"), m.group("rest")
    elif logtype not in ("other",):
        # No pid: the log's own name is the best component label we have.
        component = logtype

    # Strip a real [Originator@…] tag off vSphere message bodies (never a
    # plain [user] — shell.log needs that).
    m = _ORIGINATOR.match(rest)
    if m:
        rest = m.group("rest")

    # "WARNING:"/"info" and vmkernel's "Component:" leave a leading colon.
    shell_user = ""
    m = _SHELL_USER.match(rest)
    if m:                                    # shell.log "[user]: command"
        shell_user, rest = m.group("user").strip(), m.group("rest")
    elif cpu and not component or component == logtype and cpu:
        cm = _VMK_COMP.match(rest.lstrip(": "))
        if cm:
            component, rest = cm.group("comp"), cm.group("rest")
    rest = rest.lstrip(": ").strip() if rest[:2] in (": ", ":	") or rest[:1] == ":" else rest.strip()

    user, ip = _extract(logtype, component, rest)
    user = shell_user or user
    return [ts, logtype, severity, component, pid, cpu, user, ip, rest]


def _open_log(path: str):
    # One place, so the year pre-pass and the parse split lines identically.
    return open(path, "r", encoding="utf-8", errors="replace")


def _syslog_stamp(stamp: str):
    """(month, day, hour, minute, second) of a year-less stamp, or None
    for one whose month isn't a month ("Foo  5 10:00:00")."""
    m = _SYSLOG_PARTS.match(stamp)
    month = _MONTHS.get(m.group(1)) if m else None
    if month is None:
        return None
    return (month,) + tuple(int(g) for g in m.groups()[1:])


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

    Knowing the wraps before the first row is emitted means reading the
    file once more. That happens only for a file that has a year-less
    stamp, on the first one met; an ESXi log with ISO stamps pays nothing.
    The anchor is only as good as the mtime: an import by path keeps the
    file's own, while a browser upload's spool is stamped with the upload
    time (the browser doesn't send it), so a log uploaded long after it
    was written lands in the wrong year."""

    def __init__(self, path: str):
        self.path = path
        self.year = None
        self.last_month = None

    def iso(self, stamp: str) -> str:
        parts = _syslog_stamp(stamp)
        if parts is None:
            return stamp
        if self.year is None:
            self.year = self._first_year()
        if self.last_month is not None and parts[0] < self.last_month:
            self.year += 1
        self.last_month = parts[0]
        try:
            return datetime(self.year, *parts).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            # Feb 29 against a resolved year that has none: the evidence is
            # ambiguous, so it keeps its own text rather than a guessed date.
            return stamp

    def _first_year(self) -> int:
        wraps, last_month, last = 0, None, None
        with _open_log(self.path) as f:
            for raw in f:
                # The same test _parse_line applies, on the same line.
                m = _SYSLOG.match(raw.rstrip("\n").rstrip("\r"))
                parts = _syslog_stamp(m.group("ts")) if m else None
                if parts is None:
                    continue
                if last_month is not None and parts[0] < last_month:
                    wraps += 1
                last_month, last = parts[0], parts
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
        if (last is not None and limit.year == year
                and last > (limit.month, limit.day, limit.hour, limit.minute, limit.second)):
            year -= 1
        return year - wraps


def _iter_rows(path: str, logtype: str):
    years = _SyslogYears(path)
    with _open_log(path) as f:
        pending = None
        folded = []      # continuation lines for pending, joined once when it is emitted
        size = 0
        for raw in f:
            row = _parse_line(raw, logtype)
            if row is None:
                continue
            # A line with no timestamp after a timestamped line is almost
            # always a continuation (a stack trace, a wrapped message) — fold
            # it onto that row's Message rather than emitting a
            # timestamp-less orphan. Only onto a row that HAS a timestamp,
            # though: a file with no ESXi timestamps at all (any .log this
            # plugin claims by extension) used to fold into ONE row. Collected
            # in a list, not concatenated per line (that was quadratic), and
            # only up to FOLD_MAX_CHARS.
            if not row[0] and pending is not None and pending[0]:
                text = raw.strip()
                if size + 1 + len(text) <= FOLD_MAX_CHARS:
                    folded.append(text)
                    size += 1 + len(text)
                    continue
            if pending is not None:
                if folded:
                    pending[-1] = " ".join([pending[-1], *folded]) if pending[-1] else " ".join(folded)
                yield pending
            if row[0][:1].isalpha():   # year-less syslog; ISO stamps start with a digit
                row[0] = years.iso(row[0])
            pending, folded, size = row, [], len(row[-1])
        if pending is not None:
            if folded:
                pending[-1] = " ".join([pending[-1], *folded]) if pending[-1] else " ".join(folded)
            yield pending


def parse(path, options):
    logtype = (options or {}).get("log_type") or log_type_for(path)
    if logtype == "auto":
        logtype = log_type_for(path)
    return {
        "columns": COLUMNS,
        "column_types": COLUMN_TYPES,
        "rows": _iter_rows(path, logtype),
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
            "User, SourceIP, Message. Powers the ESXi / UAC triage "
            "profile's overview dashboard."
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
