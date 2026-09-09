"""The KAPE board's registry host-fact cards: OS version, system function,
domain, IPs, Sysmon presence and PowerShell logging posture, each keyed
on KeyPath/ValueName suffixes so they hold for any RECmd batch (and any
ControlSet). Driven against a synthetic RECmd-shaped table with the
traps a real batch has — a CurrentVersion subkey that must not leak into
the OS card, a disconnected 0.0.0.0 interface, an empty NV Domain, a
Sysmon Parameters row beside its ImagePath. (These cards replaced six
older ones on the triage board; the traps carried over with them.)"""

from __future__ import annotations

import pytest

from winnow import defaults

REG_COLS = dict(defaults.headers()["nicknames"])["Registry (RECmd batch)"]


def _row(**kw):
    d = {c: "" for c in REG_COLS}
    d.update(kw)
    return [d[c] for c in REG_COLS]


ROWS = [
    _row(HiveType="SOFTWARE", KeyPath="Microsoft\\Windows NT\\CurrentVersion",
         ValueName="ProductName", ValueData="Windows 10 Pro"),
    _row(HiveType="SOFTWARE", KeyPath="Microsoft\\Windows NT\\CurrentVersion",
         ValueName="DisplayVersion", ValueData="22H2"),
    _row(HiveType="SOFTWARE", KeyPath="Microsoft\\Windows NT\\CurrentVersion",
         ValueName="CurrentBuild", ValueData="19045"),
    # A subkey of CurrentVersion must NOT leak into the OS-version card.
    _row(HiveType="SOFTWARE", KeyPath="Microsoft\\Windows NT\\CurrentVersion\\Winlogon",
         ValueName="ProductName", ValueData="decoy"),
    _row(HiveType="SYSTEM", KeyPath="ControlSet001\\Control\\ProductOptions",
         ValueName="ProductType", ValueData="WinNT"),
    _row(HiveType="SYSTEM", KeyPath="ControlSet001\\Services\\Tcpip\\Parameters",
         ValueName="Hostname", ValueData="WKSTN-014"),
    _row(HiveType="SYSTEM", KeyPath="ControlSet001\\Services\\Tcpip\\Parameters",
         ValueName="Domain", ValueData="corp.example.com"),
    _row(HiveType="SYSTEM", KeyPath="ControlSet001\\Services\\Tcpip\\Parameters",
         ValueName="NV Domain", ValueData=""),
    _row(HiveType="SYSTEM",
         KeyPath="ControlSet001\\Services\\Tcpip\\Parameters\\Interfaces\\{9ec42dd6}",
         ValueName="DhcpIPAddress", ValueData="10.0.0.5"),
    _row(HiveType="SYSTEM",
         KeyPath="ControlSet001\\Services\\Tcpip\\Parameters\\Interfaces\\{9ec42dd6}",
         ValueName="DhcpDefaultGateway", ValueData="10.0.0.1"),
    # A disconnected interface's 0.0.0.0 must not show.
    _row(HiveType="SYSTEM",
         KeyPath="ControlSet001\\Services\\Tcpip\\Parameters\\Interfaces\\{dead}",
         ValueName="DhcpIPAddress", ValueData="0.0.0.0"),
    _row(HiveType="SYSTEM", KeyPath="ControlSet001\\Services\\Sysmon64",
         ValueName="ImagePath", ValueData="C:\\Windows\\Sysmon64.exe"),
    _row(HiveType="SYSTEM", KeyPath="ControlSet001\\Services\\Sysmon64\\Parameters",
         ValueName="HashingAlgorithm", ValueData="SHA256"),
    _row(HiveType="SYSTEM", KeyPath="ControlSet001\\Services\\SysmonDrv",
         ValueName="ImagePath", ValueData="SysmonDrv.sys"),
    _row(HiveType="SOFTWARE",
         KeyPath="Policies\\Microsoft\\Windows\\PowerShell\\ScriptBlockLogging",
         ValueName="EnableScriptBlockLogging", ValueData="1"),
    _row(HiveType="SOFTWARE",
         KeyPath="Policies\\Microsoft\\Windows\\PowerShell\\ModuleLogging",
         ValueName="EnableModuleLogging", ValueData="0"),
]


@pytest.fixture
def reg_store(store, write_csv):
    store.ingest_csv(write_csv([REG_COLS] + ROWS, "recmd.csv"), name="recmd", build_fts=False)
    return store


def _widget(title):
    kape = next(p for p in defaults.profiles() if p["name"] == "KAPE triage")
    return next(w for w in kape["dashboard"] if w["title"] == title)


def _rows(store, title):
    res = store.dashboard_widget_preview("sql", _widget(title)["query"])
    return [tuple(r) for r in res["rows"]]


def _kv(store, title):
    return {r[0]: r[1] for r in _rows(store, title)}


def test_os_version_reads_currentversion_only(reg_store):
    rows = _rows(reg_store, "OS version")
    kv = dict(rows)
    assert kv["ProductName"] == "Windows 10 Pro"
    assert kv["DisplayVersion"] == "22H2"
    assert kv["CurrentBuild"] == "19045"
    assert "decoy" not in kv.values()                       # Winlogon subkey excluded
    assert [r[0] for r in rows][:2] == ["ProductName", "DisplayVersion"]


def test_system_function_maps_producttype(reg_store):
    assert _kv(reg_store, "System function") == {"Function": "Workstation"}


def test_system_function_says_when_the_batch_lacks_it(store, write_csv):
    store.ingest_csv(write_csv([REG_COLS] + [ROWS[0]], "bare.csv"), name="bare", build_fts=False)
    assert _kv(store, "System function") == {"Function": "(ProductType not in this RECmd output)"}


def test_hostname_and_domain_cards(reg_store):
    assert _kv(reg_store, "Hostname") == {"Hostname (TCP/IP)": "WKSTN-014"}   # no ComputerName key in this batch
    kv = _kv(reg_store, "Domain")
    assert kv["Domain"] == "corp.example.com"
    assert kv["NV Domain"] == "(none — workgroup)"          # empty value reads honestly


def test_ip_card_lists_real_interfaces_only(reg_store):
    kv = _kv(reg_store, "IP addresses")
    assert kv["IP (DHCP)"] == "10.0.0.5"
    assert kv["Gateway"] == "10.0.0.1"
    assert "0.0.0.0" not in kv.values()


def test_sysmon_chips_need_the_service_and_the_driver(reg_store):
    assert _rows(reg_store, "Sysmon enabled") == [("Sysmon service", 1), ("Sysmon driver", 1)]


def test_sysmon_absent_reads_off(store, write_csv):
    store.ingest_csv(write_csv([REG_COLS] + [ROWS[0]], "bare.csv"), name="bare", build_fts=False)
    assert _rows(store, "Sysmon enabled") == [("Sysmon service", 0), ("Sysmon driver", 0)]


def test_powershell_logging_posture(reg_store):
    assert _rows(reg_store, "PowerShell logging enabled") == [("Script block", 1), ("Module", 0), ("Transcription", 0)]


def test_powershell_unconfigured_is_all_off(store, write_csv):
    store.ingest_csv(write_csv([REG_COLS] + [ROWS[0]], "bare2.csv"), name="bare2", build_fts=False)
    assert _rows(store, "PowerShell logging enabled") == [("Script block", 0), ("Module", 0), ("Transcription", 0)]
