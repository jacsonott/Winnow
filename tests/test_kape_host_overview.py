"""The KAPE profile's second board, "KAPE host overview": hostname, IPs,
domain, OS, function, Sysmon / PowerShell-logging yes-no chips, Security
log coverage and Defender alerts — each widget run against synthetic
RECmd- and EvtxECmd-shaped tables, plus the apply path that lands both
boards."""

from __future__ import annotations

import pytest

from winnow import defaults

REG_COLS = dict(defaults.headers()["nicknames"])["Registry (RECmd batch)"]
EVTX_COLS = dict(defaults.headers()["nicknames"])["Event logs (EvtxECmd)"]


def _reg(**kw):
    d = {c: "" for c in REG_COLS}
    d.update(kw)
    return [d[c] for c in REG_COLS]


def _ev(**kw):
    d = {c: "" for c in EVTX_COLS}
    d.update(kw)
    return [d[c] for c in EVTX_COLS]


REG_ROWS = [
    _reg(HiveType="SYSTEM", KeyPath="ControlSet001\\Control\\ComputerName\\ComputerName",
         ValueName="ComputerName", ValueData="WKSTN-014"),
    _reg(HiveType="SYSTEM", KeyPath="ControlSet001\\Services\\Tcpip\\Parameters",
         ValueName="Hostname", ValueData="wkstn-014"),
    _reg(HiveType="SYSTEM", KeyPath="ControlSet001\\Services\\Tcpip\\Parameters",
         ValueName="Domain", ValueData="corp.example.com"),
    _reg(HiveType="SYSTEM", KeyPath="ControlSet001\\Services\\Tcpip\\Parameters\\Interfaces\\{9ec42dd6}",
         ValueName="DhcpIPAddress", ValueData="10.0.0.5"),
    _reg(HiveType="SYSTEM", KeyPath="ControlSet001\\Services\\Tcpip\\Parameters\\Interfaces\\{dead}",
         ValueName="DhcpIPAddress", ValueData="0.0.0.0"),
    _reg(HiveType="SOFTWARE", KeyPath="Microsoft\\Windows NT\\CurrentVersion",
         ValueName="ProductName", ValueData="Windows 11 Enterprise"),
    _reg(HiveType="SOFTWARE", KeyPath="Microsoft\\Windows NT\\CurrentVersion",
         ValueName="CurrentBuild", ValueData="22631"),
    _reg(HiveType="SYSTEM", KeyPath="ControlSet001\\Control\\ProductOptions",
         ValueName="ProductType", ValueData="LanmanNT"),
    _reg(HiveType="SYSTEM", KeyPath="ControlSet001\\Services\\Sysmon64",
         ValueName="ImagePath", ValueData="C:\\Windows\\Sysmon64.exe"),
    _reg(HiveType="SOFTWARE", KeyPath="Policies\\Microsoft\\Windows\\PowerShell\\ScriptBlockLogging",
         ValueName="EnableScriptBlockLogging", ValueData="1"),
    _reg(HiveType="SOFTWARE", KeyPath="Policies\\Microsoft\\Windows\\PowerShell\\ModuleLogging",
         ValueName="EnableModuleLogging", ValueData="0"),
]

EVTX_ROWS = [
    _ev(TimeCreated="2024-01-05 13:22:01", EventId="4624", Channel="Security", Provider="Microsoft-Windows-Security-Auditing"),
    _ev(TimeCreated="2024-01-15 01:00:00", EventId="4625", Channel="Security", Provider="Microsoft-Windows-Security-Auditing"),
    _ev(TimeCreated="2023-12-31 23:59:59", EventId="7045", Channel="System", Provider="Service Control Manager"),  # not Security
    _ev(TimeCreated="2024-01-10 09:00:00", EventId="1116", Channel="Microsoft-Windows-Windows Defender/Operational",
        Provider="Microsoft-Windows-Windows Defender", MapDescription="Malware detected", PayloadData1="Trojan:Win32/Emotet"),
    _ev(TimeCreated="2024-01-12 09:00:00", EventId="1117", Channel="Microsoft-Windows-Windows Defender/Operational",
        Provider="Microsoft-Windows-Windows Defender", MapDescription="", PayloadData1="Action taken: Quarantine"),
    _ev(TimeCreated="2024-01-11 09:00:00", EventId="5001", Channel="Microsoft-Windows-Windows Defender/Operational",
        Provider="Microsoft-Windows-Windows Defender", MapDescription="Real-time protection disabled"),
    _ev(TimeCreated="2024-01-13 09:00:00", EventId="2000", Channel="Microsoft-Windows-Windows Defender/Operational",
        Provider="Microsoft-Windows-Windows Defender", MapDescription="Signature update"),   # routine, not an alert
]


def _board():
    kape = next(p for p in defaults.profiles() if p["name"] == "KAPE triage")
    (board,) = kape["dashboards"]
    assert board["name"] == "KAPE host overview"
    return board


def _widget(title):
    return next(w for w in _board()["widgets"] if w["title"] == title)


def _rows(store, title):
    res = store.run_sql(store._resolve_table_placeholders(_widget(title)["query"]["sql"]), limit=100)
    return res["rows"]


@pytest.fixture
def host(store, write_csv):
    store.ingest_csv(write_csv([REG_COLS] + REG_ROWS, "recmd.csv"), name="recmd", build_fts=False)
    store.ingest_csv(write_csv([EVTX_COLS] + EVTX_ROWS, "evtx.csv"), name="evtx", build_fts=False)
    return store


def test_the_board_covers_the_asked_for_facts():
    titles = [w["title"] for w in _board()["widgets"]]
    for want in ["Hostname", "IP addresses", "Domain", "OS version", "System function",
                 "Sysmon enabled", "PowerShell logging enabled", "Security log coverage",
                 "Most recent Defender alerts"]:
        assert want in titles, want


def test_hostname_from_computername_and_tcpip(host):
    assert _rows(host, "Hostname") == [["Computer name", "WKSTN-014"], ["Hostname (TCP/IP)", "wkstn-014"]]


def test_hostname_fallback_when_absent(store, write_csv):
    store.ingest_csv(write_csv([REG_COLS, _reg(KeyPath="x", ValueName="y", ValueData="z")], "r.csv"), name="r", build_fts=False)
    assert _rows(store, "Hostname") == [["Hostname", "(not in this RECmd output)"]]


def test_ips_domain_os_and_function(host):
    assert _rows(host, "IP addresses") == [["IP (DHCP)", "10.0.0.5"]]
    assert _rows(host, "Domain") == [["Domain", "corp.example.com"]]
    assert _rows(host, "OS version") == [["ProductName", "Windows 11 Enterprise"], ["CurrentBuild", "22631"]]
    assert _rows(host, "System function") == [["Function", "Domain controller"]]


def test_yes_no_chips_for_sysmon_and_powershell(host):
    assert _rows(host, "Sysmon enabled") == [["Sysmon service", 1], ["Sysmon driver", 0]]
    assert _rows(host, "PowerShell logging enabled") == [["Script block", 1], ["Module", 0], ["Transcription", 0]]


def test_chips_read_off_when_nothing_is_configured(store, write_csv):
    store.ingest_csv(write_csv([REG_COLS, _reg(KeyPath="x", ValueName="y", ValueData="z")], "r.csv"), name="r", build_fts=False)
    assert _rows(store, "Sysmon enabled") == [["Sysmon service", 0], ["Sysmon driver", 0]]
    assert _rows(store, "PowerShell logging enabled") == [["Script block", 0], ["Module", 0], ["Transcription", 0]]


def test_security_log_coverage_is_security_channel_only(host):
    rows = dict((r[0], r[1]) for r in _rows(host, "Security log coverage"))
    assert rows["Oldest event"] == "2024-01-05 13:22:01"      # the System-channel 2023 event does not count
    assert rows["Newest event"] == "2024-01-15 01:00:00"
    assert rows["Span (days)"] == 9.5
    assert rows["Events"] == 2


def test_security_log_coverage_says_when_there_is_none(store, write_csv):
    store.ingest_csv(write_csv([EVTX_COLS, _ev(TimeCreated="2024-01-01 00:00:00", EventId="1", Channel="System")], "e.csv"),
                     name="e", build_fts=False)
    rows = dict((r[0], r[1]) for r in _rows(store, "Security log coverage"))
    assert rows["Oldest event"] == "(no Security channel events)" and rows["Events"] == 0


def test_defender_alerts_newest_first_with_description_fallback(host):
    rows = _rows(host, "Most recent Defender alerts")
    assert rows == [["2024-01-12 09:00:00", "1117 · Action taken: Quarantine"],
                    ["2024-01-11 09:00:00", "5001 · Real-time protection disabled"],
                    ["2024-01-10 09:00:00", "1116 · Malware detected"]]
    assert _rows(host, "Defender detections") == [[2]]


def test_defender_alerts_say_when_there_are_none(store, write_csv):
    store.ingest_csv(write_csv([EVTX_COLS, _ev(TimeCreated="2024-01-01 00:00:00", EventId="4624", Channel="Security")], "e.csv"),
                     name="e", build_fts=False)
    assert _rows(store, "Most recent Defender alerts") == [["—", "(no Defender alert events in the logs)"]]


def test_applying_the_profile_lands_both_boards(client, host):
    kape = next(b for b in client.get("/api/plugin_bundles").json() if b["name"] == "KAPE triage")
    assert [b["name"] for b in kape["dashboards"]] == ["KAPE host overview"]
    body = client.post(f"/api/plugin_bundles/{kape['id']}/apply").json()
    assert body["dashboard_applied"] is True and body["dashboards_applied"] == ["KAPE host overview"]
    boards = {b["name"]: b for b in client.get("/api/dashboards").json()}
    assert "KAPE triage" in boards and "KAPE host overview" in boards
    widgets = client.get(f"/api/dashboards/{boards['KAPE host overview']['id']}").json()["widgets"]
    assert len(widgets) == len(_board()["widgets"])
    # a second apply refreshes rather than duplicates
    client.post(f"/api/plugin_bundles/{kape['id']}/apply")
    assert sum(1 for b in client.get("/api/dashboards").json() if b["name"] == "KAPE host overview") == 1
    # every widget previews without error against the fixture
    for w in widgets:
        pv = client.post("/api/dashboard/widget/preview", json={"source": "sql", "query": w["query"]})
        assert pv.status_code == 200, (w["title"], pv.text)


def test_saved_bundles_keep_extra_boards(client):
    r = client.post("/api/plugin_bundles", json={
        "name": "Two boards", "plugins": [], "dashboard": [{"title": "a", "source": "tags", "render": "stat"}],
        "dashboards": [{"name": "Second", "widgets": [{"title": "b", "source": "tags", "render": "stat"}]}]})
    assert r.status_code == 200 and [b["name"] for b in r.json()["dashboards"]] == ["Second"]
