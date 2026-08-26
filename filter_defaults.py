"""Default saved filters: a working analyst's Timeline Explorer filter set,
converted to Winnow's filter trees.

The source material is a set of TLE (DevExpress) filter expressions used
against EZ Tools output during real IR triage — log-clearing and Defender
tampering, logon sweeps, RDP both directions, service installs, scheduled
tasks, persistence run keys, executables in the places TAs put them. They
convert cleanly because both languages are the same shape: `[Event Id]
In (...)` / `Contains()` / `Not` / `<>` / `IsNullOrEmpty` map one-to-one
onto the filter tree's in/contains/not_contains/not_equals/empty ops, and
DevExpress gives And tighter binding than Or, so each expression's
parenthesization is preserved in the tree nesting rather than re-derived.

Each filter binds to the exact header set in header_defaults.py that its
tool writes (TLE's "Event Id" display name is EvtxECmd's EventId column,
"Payload Data1" is PayloadData1, and so on) — so the suggestion banner and
the [ / ] cycle offer "EVTX — Logons" the moment an EvtxECmd table opens,
with nothing configured.

Seeded once into workspace.SavedFilters (ensure_seeded), same contract as
the header nicknames: seeded rows become ordinary saved filters — rename,
edit, reorder, delete, all stick — and a DEFAULTS_VERSION bump adds only
filters not already present (matched by name + column set, the same
identity import_all uses).

Data only. Imports header_defaults for the column lists; nothing here
executes.
"""

import header_defaults

FILTER_DEFAULTS_VERSION = 1

_COLS = {name: cols for name, cols in header_defaults.DEFAULT_HEADER_NICKNAMES}
EVTX = _COLS["Event logs (EvtxECmd)"]
RECMD = _COLS["Registry (RECmd batch)"]
MFT = _COLS["NTFS $MFT (MFTECmd)"]


def _c(column, op, value=""):
    return {"type": "cond", "column": column, "op": op, "value": value}


def _and(*children):
    return {"type": "group", "op": "AND", "children": list(children)}


def _or(*children):
    return {"type": "group", "op": "OR", "children": list(children)}


def _tree(root):
    """A payload whose only content is the guided filter tree — the shape
    currentFilterPayload() saves and applyPreset() restores. The root must
    be a GROUP: the builder renders root.children and the client's spec
    gate checks children.length, so a bare-condition root reads as "no
    filter" there while the ★ button says it's applied. Single conditions
    get wrapped here (and workspace normalizes any already-seeded ones)."""
    if root.get("type") != "group":
        root = {"type": "group", "op": "AND", "children": [root]}
    return {"filter_tree": root, "search": "", "search_mode": "contains", "search_terms": []}


def _eids(*ids):
    return _c("EventId", "in", [str(i) for i in ids])


# (name, column set, payload) — grouped by artifact, EVTX first since it's
# the highest-traffic set. Names lead with the artifact so the saved-filters
# list and the [ / ] cycle read as a triage checklist.
DEFAULT_SAVED_FILTERS = [
    # ------------------------------------------------------------- EVTX
    ("EVTX — Defense evasion: log clearing / Defender off", EVTX, _tree(_or(
        _and(_eids(104, 6006), _c("Channel", "equals", "System")),
        _and(_eids(1100, 1102), _c("Channel", "equals", "Security")),
        _and(_c("EventId", "equals", "5001"),
             _c("Provider", "equals", "Microsoft-Windows-Windows Defender/Operational")),
    ))),
    ("EVTX — Defender detections & tamper", EVTX, _tree(_or(
        _and(_eids(5004, 5008, 5010, 5012, 1116, 1117, 1006, 1118, 1119),
             _c("Channel", "contains", "defender")),
        _and(_c("EventId", "equals", "85"), _c("Channel", "not_contains", "Microsoft")),
    ))),
    # The source notes say this one is noisy and rarely useful — shipped
    # anyway, labelled, because "look and dismiss" beats "didn't know".
    ("EVTX — Defender config changes (noisy)", EVTX, _tree(_and(
        _eids(1013, 5007), _c("Provider", "contains", "Microsoft-Windows-Windows Defender"),
    ))),
    ("EVTX — Logons", EVTX, _tree(_and(
        _eids(4624, 4778, 4779, 4776, 4625, 4634, 4647, 4648, 4672, 4720, 4726, 4768, 4769, 4771),
        _c("PayloadData1", "not_contains", "UMFD-"),
        _c("PayloadData1", "not_contains", "DWM-"),
    ))),
    ("EVTX — Incoming RDP", EVTX, _tree(_and(
        _or(
            _and(_eids(98, 1149, 25, 21, 22, 23, 24, 39, 40), _c("Channel", "contains", "Terminal")),
            _c("EventId", "equals", "131"),
        ),
        _c("Provider", "not_contains", "Directory Synchronization"),
        _c("Provider", "not_contains", "Microsoft-Windows-DeviceSetupManager"),
    ))),
    # The TLE original repeats Contains(Channel,'Terminal') per arm; one
    # `in` against the shared Contains is the same predicate, flattened.
    ("EVTX — Outgoing RDP", EVTX, _tree(_and(
        _eids(1102, 1027, 1024), _c("Channel", "contains", "Terminal"),
    ))),
    ("EVTX — Sysmon present?", EVTX, _tree(_c("Provider", "contains", "Microsoft-Windows-Sysmon"))),
    ("EVTX — Network share access (5140/5145)", EVTX, _tree(_eids(5140, 5145))),
    ("EVTX — Service installs & state changes", EVTX, _tree(_and(
        _eids(1033, 1034, 7045, 11707, 11708, 11724, 4697, 7034, 7035, 7036, 7040),
        _c("PayloadData1", "not_contains", "MpKs"),
    ))),
    ("EVTX — WMI activity", EVTX, _tree(_eids(5860, 5861, 5858, 5857, 5859, 91))),
    ("EVTX — Process create/exit (4688/4689)", EVTX, _tree(_eids(4688, 4689))),
    ("EVTX — Crashed applications", EVTX, _tree(_or(
        _and(_eids(1000, 1001, 1002, 1006),
             _c("Channel", "equals", "Application"), _c("Provider", "contains", "Error")),
        _and(_c("EventId", "equals", "4688"), _c("Channel", "contains", "Security")),
        _and(_c("EventId", "equals", "1001"), _c("Channel", "contains", "System")),
    ))),
    ("EVTX — User/group changes", EVTX,
     _tree(_eids(4738, 4726, 4724, 4720, 4740, 4728, 4732, 4723))),
    # The source notes call for grouping PowerShell by PayloadData1 "for a
    # better view of what's happening" — grouping travels with a saved
    # filter, so it's baked in here rather than left as a tip.
    ("EVTX — PowerShell", EVTX, {**_tree(_and(
        _eids(400, 600, 4104, 4103, 6, 168, 4688, 4100),
        _c("Provider", "contains", "powershell"),
    )), "group_by": ["PayloadData1"], "group_sort": "count", "group_sort_dir": "desc"}),
    ("EVTX — Firewall rule changes", EVTX, _tree(_and(
        _eids(2004, 2005, 2006),
        _c("Provider", "contains", "Microsoft-Windows-Windows Firewall With Advanced Security"),
    ))),
    ("EVTX — Scheduled tasks", EVTX, _tree(_or(
        _c("Provider", "equals", "Microsoft-Windows-TaskScheduler"),
        _eids(4698, 4702, 4699, 201, 4701),
    ))),
    ("EVTX — BITS transfers", EVTX, {**_tree(_c("MapDescription", "contains", "BITS")),
     "group_by": ["PayloadData1"], "group_sort": "count", "group_sort_dir": "desc"}),
    ("EVTX — Group Policy (client)", EVTX, _tree(_eids(4016, 5016))),
    ("EVTX — Group Policy (DC: GPO create/modify)", EVTX, _tree(_eids(5136, 5137))),
    ("EVTX — System shutdowns", EVTX, _tree(_and(
        _eids(6006, 6008, 41, 1074),
        _c("Channel", "not_equals", "Microsoft-Windows-TerminalServices-LocalSessionManager/Operational"),
    ))),

    # ------------------------------------------------- Registry (RECmd)
    ("Registry — Basic system info", RECMD, _tree(_or(
        _c("ValueName", "contains", "IPAddress"),
        _and(_c("Description", "equals", "Network Interfaces"),
             _c("ValueName", "contains", "ipaddr"), _c("ValueData", "not_equals", "0.0.0.0")),
        _and(_c("Description", "equals", "Services"),
             _c("ValueName", "equals", "Domain"), _c("ValueData", "not_empty")),
        _c("ValueName", "in",
           ["ProductName", "InstallDate", "HostName", "TimeZoneKeyName", "DhcpIPAddress"]),
        _c("ValueName", "contains", "ComputerName"),
        _c("KeyPath", "contains", "ControlSet001\\Control\\ProductOptions"),
    ))),
    ("Registry — Audit & PowerShell policy", RECMD, _tree(_or(
        _and(_c("KeyPath", "contains", "\\Microsoft\\Windows\\CurrentVersion\\Policies\\System\\Audit"),
             _c("ValueName", "contains", "ProcessCreationIncludeCmdLine_Enabled")),
        _c("KeyPath", "contains", "\\Policies\\Microsoft\\Windows\\PowerShell"),
    ))),
    ("Registry — TypedPaths / RecentDocs", RECMD, _tree(_or(
        _c("Description", "contains", "TypedPaths"),
        _c("Description", "contains", "RecentDocs"),
    ))),
    # Grouped by hive: UserAssist/BAM are per-user (NTUSER.DAT), and the
    # source notes call the grouping "a useful move to look at each user".
    ("Registry — UserAssist / BAM", RECMD, {**_tree(_or(
        _c("Description", "contains", "UserAssist"),
        _c("Description", "equals", "Bam"),
    )), "group_by": ["HivePath"], "group_sort": "count", "group_sort_dir": "desc"}),
    ("Registry — Run keys (autoruns)", RECMD, _tree(
        _c("KeyPath", "contains", "ROOT\\Software\\Microsoft\\Windows\\CurrentVersion\\Run"),
    )),

    # ------------------------------------------------------ MFT (MFTECmd)
    ("MFT — Executables in odd places", MFT, _tree(_and(
        _or(
            _and(_c("ParentPath", "contains", "roaming"), _c("Extension", "contains", "exe")),
            _c("ParentPath", "contains", "music"),
            _and(_c("ParentPath", "contains", "public"), _c("Extension", "contains", "exe")),
            _and(_c("ParentPath", "contains", "downloads"),
                 _or(_c("Extension", "contains", "exe"), _c("Extension", "contains", "ps1"),
                     _c("Extension", "contains", "bat"))),
            _and(_c("ParentPath", "contains", ".\\windows\\temp"),
                 _or(_c("Extension", "contains", "exe"), _c("Extension", "contains", "ps1"),
                     _c("Extension", "contains", "bat"))),
        ),
        _c("Extension", "not_equals", ".ini"),
    ))),
    ("MFT — Startup folder & run-key paths", MFT, _tree(_and(
        _or(_c("ParentPath", "contains", "\\Microsoft\\Windows\\CurrentVersion\\Run"),
            _c("ParentPath", "contains", "\\Windows\\Start Menu\\Programs\\Startup")),
        _c("FileName", "not_equals", "desktop.ini"),
    ))),
    # The tool sweep is a *search*, not a column filter — every remote
    # access/exfil tool name from the source list, OR'd across all columns
    # via the advanced search mode a saved filter already carries. The
    # `-cyvera` exclusion can NOT ride the same term chain: connectors
    # follow SQL precedence (the builder's own docstring), so
    # `a OR b ... AND NOT cyvera` guards only the last term. The search
    # clause and the filter tree AND together in _compile_where, so the
    # exclusion lives in the tree — which is exactly TLE's
    # "search minus term" semantics.
    ("MFT — Suspicious tool sweep (search)", MFT, {
        "filter_tree": _and(
            _c("ParentPath", "not_contains", "cyvera"),
            _c("FileName", "not_contains", "cyvera"),
        ),
        "search": "", "search_mode": "advanced",
        "search_terms": [{"term": t, "connector": "OR", "exclude": False} for t in [
            "anydesk", "psexec", "atera", "air explorer", "connectwise", "screenconnect",
            "cyberduck", "filezilla", "logmein", "pcloud", "rclone", "splashtop",
            "teamviewer", "yandex.disk", "cuteftp", "google drive", "winscp", "putty",
            "chrome remote desktop", "ultravnc", "ultraviewer", "megasync", "ngrok",
            "superputty", "onionshare", "psexesvc",
        ]],
    }),
]
