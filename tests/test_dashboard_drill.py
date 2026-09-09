"""Drilldown from a widget to its rows. A widget may carry `drill`: the
table its rows live in (src_N or a {{evtx}}-style placeholder), the
conditions that select them (`where`, ANDed, or `tree`, a whole filter
tree for the OR-shaped ones), and for top-N / over-time widgets the
column a clicked bar or row pivots on. The client resolves the table
through /api/dashboard/resolve and opens the grid with the conditions as
a filter tree — so a drill is only as good as its match with the SQL.
The shipped KAPE board's drills are checked here two ways: every column
they name exists in the header set, and on a fixture every counting
widget's drill selects exactly the rows its SQL counted."""

from __future__ import annotations

import pytest

from winnow import defaults

HEADER_SETS = dict(defaults.headers()["nicknames"])
SHORTHANDS = {"evtx": "Event logs (EvtxECmd)", "registry": "Registry (RECmd batch)"}
VALID_OPS = {"equals", "not_equals", "in", "contains", "not_contains", "starts", "regex", "empty", "not_empty", ">", ">=", "<", "<="}
REG_COLS = HEADER_SETS["Registry (RECmd batch)"]
EVTX_COLS = HEADER_SETS["Event logs (EvtxECmd)"]


def _kape():
    return next(p for p in defaults.profiles() if p["name"] == "KAPE triage")


def _columns_for(table: str) -> list[str]:
    key = table.strip("{} ").lower()
    return HEADER_SETS[SHORTHANDS[key]]


def _conds(node):
    """Every condition in a drill: the `where` list and any `tree` nodes."""
    out = list(node.get("where", []))
    def walk(n):
        if not n:
            return
        if n.get("type") == "cond":
            out.append(n)
        for c in n.get("children", []):
            walk(c)
    walk(node.get("tree"))
    return out


def test_every_kape_sql_widget_has_a_drill_that_names_real_columns():
    for w in _kape()["dashboard"]:
        if w["source"] != "sql":
            continue
        drill = w.get("drill")
        assert drill, w["title"]
        cols = _columns_for(drill["table"])
        for cond in _conds(drill):
            assert cond["column"] in cols, (w["title"], cond)
            assert cond["op"] in VALID_OPS, (w["title"], cond)
            if cond["op"] == "in":
                assert isinstance(cond["value"], list) and cond["value"], (w["title"], cond)
        if drill.get("column"):
            assert drill["column"] in cols, (w["title"], drill["column"])
        if drill.get("bucket"):
            assert drill["bucket"] in ("hour", "day") and drill.get("column"), w["title"]


def test_top_n_widgets_pivot_on_the_column_they_group_by():
    by_title = {w["title"]: w for w in _kape()["dashboard"]}
    assert by_title["Top remote hosts (4624)"]["drill"]["column"] == "RemoteHost"
    assert by_title["Rarest logon accounts (long tail)"]["drill"]["column"] == "UserName"
    assert by_title["Registry entries by category"]["drill"]["column"] == "Category"
    lv = by_title["Logon volume over time (4624/4625/4648)"]["drill"]
    assert (lv["table"], lv["column"], lv["bucket"]) == ("{{evtx}}", "TimeCreated", "hour")
    assert {"column": "EventId", "op": "in", "value": ["4624", "4625", "4648"]} in lv["where"]


# ---------------------------------------------------------- drill == SQL

def _reg(**kw):
    d = {c: "" for c in REG_COLS}
    d.update(kw)
    return [d[c] for c in REG_COLS]


def _ev(**kw):
    d = {c: "" for c in EVTX_COLS}
    d.update(kw)
    return [d[c] for c in EVTX_COLS]


# Shaped to exercise every exclusion the SQL makes: a decoy CurrentVersion
# subkey, a disconnected 0.0.0.0 interface, an empty NV Domain, Sysmon
# parameters beside its ImagePath, a non-Security 4624, '-' peers, and
# Defender events both inside and outside the alert list.
REG_ROWS = [
    _reg(KeyPath="Microsoft\\Windows NT\\CurrentVersion", ValueName="ProductName", ValueData="Windows 10 Pro", Category="OS"),
    _reg(KeyPath="Microsoft\\Windows NT\\CurrentVersion", ValueName="DisplayVersion", ValueData="22H2", Category="OS"),
    _reg(KeyPath="Microsoft\\Windows NT\\CurrentVersion", ValueName="InstallDate", ValueData="1700000000", Category="OS"),
    _reg(KeyPath="Microsoft\\Windows NT\\CurrentVersion\\Winlogon", ValueName="ProductName", ValueData="decoy", Category="OS"),
    _reg(KeyPath="ControlSet001\\Control\\ComputerName\\ComputerName", ValueName="ComputerName", ValueData="WKSTN-014", Category="System"),
    _reg(KeyPath="ControlSet001\\Control\\ProductOptions", ValueName="ProductType", ValueData="WinNT", Category="System"),
    _reg(KeyPath="ControlSet001\\Control\\ProductOptions", ValueName="ProductSuite", ValueData="Terminal Server", Category="System"),
    _reg(KeyPath="ControlSet001\\Services\\Tcpip\\Parameters", ValueName="Hostname", ValueData="wkstn-014", Category="Network"),
    _reg(KeyPath="ControlSet001\\Services\\Tcpip\\Parameters", ValueName="Domain", ValueData="corp.example.com", Category="Network"),
    _reg(KeyPath="ControlSet001\\Services\\Tcpip\\Parameters", ValueName="NV Domain", ValueData="", Category="Network"),
    _reg(KeyPath="ControlSet001\\Services\\Tcpip\\Parameters\\Interfaces\\{9ec42dd6}", ValueName="DhcpIPAddress", ValueData="10.0.0.5", Category="Network"),
    _reg(KeyPath="ControlSet001\\Services\\Tcpip\\Parameters\\Interfaces\\{9ec42dd6}", ValueName="DhcpDefaultGateway", ValueData="10.0.0.1", Category="Network"),
    _reg(KeyPath="ControlSet001\\Services\\Tcpip\\Parameters\\Interfaces\\{dead}", ValueName="DhcpIPAddress", ValueData="0.0.0.0", Category="Network"),
    _reg(KeyPath="ControlSet001\\Services\\Sysmon64", ValueName="ImagePath", ValueData="C:\\Windows\\Sysmon64.exe", Category="Services"),
    _reg(KeyPath="ControlSet001\\Services\\Sysmon64\\Parameters", ValueName="HashingAlgorithm", ValueData="SHA256", Category="Services"),
    _reg(KeyPath="ControlSet001\\Services\\SysmonDrv", ValueName="ImagePath", ValueData="SysmonDrv.sys", Category="Services"),
    _reg(KeyPath="Policies\\Microsoft\\Windows\\PowerShell\\ScriptBlockLogging", ValueName="EnableScriptBlockLogging", ValueData="1", Category="Policy"),
    _reg(KeyPath="Policies\\Microsoft\\Windows\\PowerShell\\ModuleLogging", ValueName="EnableModuleLogging", ValueData="0", Category="Policy"),
    _reg(KeyPath="Microsoft\\Windows\\CurrentVersion\\Run", ValueName="OneDrive", ValueData="onedrive.exe", Category="Autostart"),
    _reg(KeyPath="Microsoft\\Windows\\CurrentVersion\\RunOnce", ValueName="Setup", ValueData="setup.exe", Category="Autostart"),
    _reg(KeyPath="Software\\Vendor", ValueName="Path", ValueData="x", Category="-"),
]
EVTX_ROWS = [
    _ev(TimeCreated="2024-01-05 13:22:01", EventId="4624", Channel="Security", Computer="WKSTN-014", UserName="jsmith", RemoteHost="WKS07", Provider="Microsoft-Windows-Security-Auditing"),
    _ev(TimeCreated="2024-01-05 13:25:00", EventId="4624", Channel="Security", Computer="WKSTN-014", UserName="admin", RemoteHost="-", Provider="Microsoft-Windows-Security-Auditing"),
    _ev(TimeCreated="2024-01-05 14:00:00", EventId="4624", Channel="Security", Computer="-", UserName="", RemoteHost="- (-)", Provider="Microsoft-Windows-Security-Auditing"),
    _ev(TimeCreated="2024-01-06 09:00:00", EventId="4624", Channel="Microsoft-Windows-TerminalServices-LocalSessionManager/Operational", Computer="SRV-01", UserName="jsmith", RemoteHost="WKS01"),
    _ev(TimeCreated="2024-01-15 01:00:00", EventId="4625", Channel="Security", Computer="WKSTN-014", UserName="admin", RemoteHost="WKS07", Provider="Microsoft-Windows-Security-Auditing"),
    _ev(TimeCreated="2024-01-15 01:00:30", EventId="4648", Channel="Security", Computer="WKSTN-014", UserName="admin", Provider="Microsoft-Windows-Security-Auditing"),
    _ev(TimeCreated="2024-01-15 02:00:00", EventId="21", Channel="Microsoft-Windows-TerminalServices-LocalSessionManager/Operational", Computer="SRV-01", UserName="jsmith"),
    _ev(TimeCreated="2024-01-15 02:00:01", EventId="21", Channel="Security", Computer="SRV-01", UserName="jsmith"),   # 21 outside the RDP channel
    _ev(TimeCreated="2024-01-16 00:00:00", EventId="1102", Channel="Security", Computer="WKSTN-014", UserName="admin"),
    _ev(TimeCreated="2024-01-16 00:00:01", EventId="4719", Channel="Security", Computer="WKSTN-014", UserName="admin"),
    _ev(TimeCreated="2024-01-10 09:00:00", EventId="1116", Channel="Microsoft-Windows-Windows Defender/Operational", Provider="Microsoft-Windows-Windows Defender", MapDescription="Malware detected"),
    _ev(TimeCreated="2024-01-12 09:00:00", EventId="1117", Channel="Microsoft-Windows-Windows Defender/Operational", Provider="Microsoft-Windows-Windows Defender", PayloadData1="Quarantine"),
    _ev(TimeCreated="2024-01-11 09:00:00", EventId="5001", Channel="Microsoft-Windows-Windows Defender/Operational", Provider="Microsoft-Windows-Windows Defender", MapDescription="Real-time protection disabled"),
    _ev(TimeCreated="2024-01-13 09:00:00", EventId="2000", Channel="Microsoft-Windows-Windows Defender/Operational", Provider="Microsoft-Windows-Windows Defender", MapDescription="Signature update"),
    _ev(TimeCreated="2023-12-31 23:59:59", EventId="7045", Channel="System", Computer="SRV-02", UserName="SYSTEM", Provider="Service Control Manager"),
]


@pytest.fixture
def host(store, write_csv):
    reg = store.ingest_csv(write_csv([REG_COLS] + REG_ROWS, "recmd.csv"), name="recmd", build_fts=False)["id"]
    ev = store.ingest_csv(write_csv([EVTX_COLS] + EVTX_ROWS, "evtx.csv"), name="evtx", build_fts=False)["id"]
    return store, {"{{registry}}": reg, "{{evtx}}": ev}


def _drill_rows(store, sid, drill, value=None):
    """The rows a drill opens — what dashboard.js drillInto builds, compiled
    by the store the same way the grid's own filter tree is."""
    children = [{"type": "cond", **c} for c in drill.get("where", [])]
    if drill.get("tree"):
        children.append(drill["tree"])
    if value is not None and drill.get("column"):
        children.append({"type": "cond", "column": drill["column"], "op": "equals", "value": value})
    tree = {"type": "group", "op": "AND", "children": children}
    sql = store.spec_sql(sid, {"source_id": sid, "filter_tree": tree})
    res = store.run_sql(sql, limit=10000)
    # By name: the view's column order is its own, not the source's.
    return [dict(zip(res["columns"], r)) for r in res["rows"]]


def _preview(store, w):
    return store.dashboard_widget_preview(w["source"], w["query"])["rows"]


def test_every_counting_drill_selects_exactly_the_rows_its_sql_counted(host):
    """A stat that says 3 opens 3 rows; a distinct count opens rows with
    exactly that many distinct values. Anything looser or tighter is a
    number the click contradicts."""
    store, ids = host
    checked = 0
    for w in _kape()["dashboard"]:
        if w["source"] != "sql" or w["render"] != "stat":
            continue
        sql = w["query"]["sql"].replace("\n", " ")
        drill = w["drill"]
        rows = _drill_rows(store, ids[drill["table"]], drill)
        (n,) = _preview(store, w)[0]
        if "COUNT(DISTINCT" in sql:
            assert len({r[drill["column"]] for r in rows}) == n, (w["title"], n, len(rows))
        else:
            assert len(rows) == n, (w["title"], n, len(rows))
        checked += 1
    assert checked >= 9, checked


def test_top_n_drills_open_exactly_the_rows_behind_each_bar(host):
    store, ids = host
    by_title = {w["title"]: w for w in _kape()["dashboard"]}
    for title in ("Top remote hosts (4624)", "Rarest logon accounts (long tail)", "Registry entries by category"):
        w = by_title[title]
        for label, count in _preview(store, w):
            rows = _drill_rows(store, ids[w["drill"]["table"]], w["drill"], value=str(label))
            assert len(rows) == count, (title, label, count, len(rows))


def test_host_fact_drills_open_the_rows_the_cards_read(host):
    """The kv and chips cards on the registry: the drill lands on the rows
    the card was computed from, and on nothing else (the Winlogon decoy,
    the 0.0.0.0 interface, Sysmon's Parameters subkey, Category '-')."""
    store, ids = host
    reg = ids["{{registry}}"]
    by_title = {w["title"]: w for w in _kape()["dashboard"]}
    def opened(title):
        return sorted((r["KeyPath"], r["ValueName"]) for r in _drill_rows(store, reg, by_title[title]["drill"]))
    assert opened("OS version") == [("Microsoft\\Windows NT\\CurrentVersion", "DisplayVersion"),
                                    ("Microsoft\\Windows NT\\CurrentVersion", "ProductName")]
    assert opened("Hostname") == [("ControlSet001\\Control\\ComputerName\\ComputerName", "ComputerName"),
                                  ("ControlSet001\\Services\\Tcpip\\Parameters", "Hostname")]
    assert opened("Domain") == [("ControlSet001\\Services\\Tcpip\\Parameters", "Domain"),
                                ("ControlSet001\\Services\\Tcpip\\Parameters", "NV Domain")]
    assert opened("IP addresses") == [("ControlSet001\\Services\\Tcpip\\Parameters\\Interfaces\\{9ec42dd6}", "DhcpDefaultGateway"),
                                      ("ControlSet001\\Services\\Tcpip\\Parameters\\Interfaces\\{9ec42dd6}", "DhcpIPAddress")]
    assert opened("System function") == [("ControlSet001\\Control\\ProductOptions", "ProductType")]
    assert opened("Sysmon enabled") == [("ControlSet001\\Services\\Sysmon64", "ImagePath"),
                                        ("ControlSet001\\Services\\SysmonDrv", "ImagePath")]
    assert opened("PowerShell logging enabled") == [("Policies\\Microsoft\\Windows\\PowerShell\\ModuleLogging", "EnableModuleLogging"),
                                                    ("Policies\\Microsoft\\Windows\\PowerShell\\ScriptBlockLogging", "EnableScriptBlockLogging")]
    # Defender's kv card lists alert events only; its drill opens the same set.
    ev = ids["{{evtx}}"]
    assert sorted(r["EventId"] for r in _drill_rows(store, ev, by_title["Most recent Defender alerts"]["drill"])) == ["1116", "1117", "5001"]


# ---------------------------------------------------------- resolve

def test_resolve_binds_a_placeholder_to_the_first_matching_source(client, store, write_csv):
    evtx_cols = HEADER_SETS["Event logs (EvtxECmd)"]
    store.ingest_csv(write_csv([["a", "b"], ["1", "2"]], "other.csv"), name="other", build_fts=False)
    sid = store.ingest_csv(write_csv([evtx_cols, ["" for _ in evtx_cols]], "evtx.csv"), name="evtx", build_fts=False)["id"]
    r = client.post("/api/dashboard/resolve", json={"table": "{{evtx}}"})
    assert r.status_code == 200 and r.json()["source_id"] == sid and r.json()["source_ids"] == [sid]
    assert client.post("/api/dashboard/resolve", json={"table": "{{header_set:Event logs (EvtxECmd)}}"}).json()["source_id"] == sid
    assert client.post("/api/dashboard/resolve", json={"table": f"src_{sid}"}).json()["source_id"] == sid
    r = client.post("/api/dashboard/resolve", json={"sql": "SELECT COUNT(*) FROM {{evtx}} WHERE EventId='4625'"})
    assert r.status_code == 200 and f'"src_{sid}"' in r.json()["sql"] and "{{" not in r.json()["sql"]


def test_resolve_lists_every_source_a_union_placeholder_spans(client, store, write_csv):
    """A {{all:…}} widget counted across every matching table; the drill
    gets all of them and asks which to open rather than picking one."""
    evtx_cols = HEADER_SETS["Event logs (EvtxECmd)"]
    a = store.ingest_csv(write_csv([evtx_cols, ["" for _ in evtx_cols]], "a.csv"), name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv([evtx_cols, ["" for _ in evtx_cols]], "b.csv"), name="b", build_fts=False)["id"]
    r = client.post("/api/dashboard/resolve", json={"table": "{{all:evtx}}"}).json()
    assert r["source_ids"] == [a, b] and r["source_id"] == a
    assert client.post("/api/dashboard/resolve", json={"table": "{{evtx}}"}).json()["source_ids"] == [a]


def test_resolve_says_when_the_case_has_no_such_table(client, store, write_csv):
    store.ingest_csv(write_csv([["a", "b"], ["1", "2"]], "other.csv"), name="other", build_fts=False)
    r = client.post("/api/dashboard/resolve", json={"table": "{{registry}}"})
    assert r.status_code == 400 and "Registry (RECmd batch)" in r.json()["detail"]
    r = client.post("/api/dashboard/resolve", json={"table": "{{all:registry}}"})
    assert r.status_code == 400 and "Registry (RECmd batch)" in r.json()["detail"]
    r = client.post("/api/dashboard/resolve", json={"table": "src_999"})
    assert r.status_code == 400 and "no longer" in r.json()["detail"]
    r = client.post("/api/dashboard/resolve", json={"table": "DROP TABLE x"})
    assert r.status_code == 400


def test_header_sets_lists_the_shorthands_and_their_columns(client):
    body = client.get("/api/header_sets").json()
    assert body["shorthands"]["evtx"] == "Event logs (EvtxECmd)"
    names = {s["name"]: s["columns"] for s in body["sets"]}
    assert "EventId" in names["Event logs (EvtxECmd)"]
    assert "KeyPath" in names["Registry (RECmd batch)"]
