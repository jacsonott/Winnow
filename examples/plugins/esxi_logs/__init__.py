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
  `process[pid]:`, then the message.

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

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_USER_FROM = re.compile(r"\bfor (?:invalid user )?(?P<user>[^\s]+) from\b")
_SUDO_USER = re.compile(r"\b(?:sudo|su)(?:\[\d+\])?:\s*(?P<user>[A-Za-z0-9._-]+)\s*:")
_SHELL_USER = re.compile(r"^\[(?P<user>[^\]]+)\]:\s*(?P<rest>.*)$")


def log_type_for(filename: str) -> str:
    stem = os.path.basename(filename).lower().split(".")[0]
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


def _iter_rows(path: str, logtype: str):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        pending = None
        for raw in f:
            row = _parse_line(raw, logtype)
            if row is None:
                continue
            # A line with no timestamp that isn't itself parseable is almost
            # always a continuation (a stack trace, a wrapped message) — fold
            # it onto the previous row's Message rather than emitting a
            # timestamp-less orphan.
            if not row[0] and pending is not None:
                pending[-1] = (pending[-1] + " " + raw.strip()).strip()
                continue
            if pending is not None:
                yield pending
            pending = row
        if pending is not None:
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


def register(api):
    api.register_ingest_format(
        id="esxi_log",
        label="ESXi / Linux host log",
        extensions=[],                       # matched by name, not extension
        filename_patterns=FILENAME_PATTERNS,
        description=(
            "ESXi support-bundle and UAC Linux logs (hostd, vmkernel, auth, "
            "shell, vobd, vpxa, syslog, rhttpproxy, esxupdate, and rotated "
            "copies) parsed into one schema: Timestamp, Log type, Severity, "
            "Component, PID, CPU, User, SourceIP, Message. Powers the "
            "ESXi / UAC triage profile's overview dashboard."
        ),
        options=[
            {"name": "log_type", "label": "Log type", "type": "choice",
             "choices": ["auto", "hostd", "vmkernel", "auth", "shell", "vobd",
                         "vpxa", "syslog", "rhttpproxy", "esxupdate", "other"],
             "default": "auto"},
        ],
        parse=parse,
    )
