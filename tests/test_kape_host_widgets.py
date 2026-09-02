"""The KAPE profile's registry host-fact widgets: OS version, system role,
domain, IPs, Sysmon presence, and PowerShell logging posture, each keyed
on KeyPath/ValueName suffixes so they hold for any RECmd batch (and any
ControlSet). Driven against a synthetic RECmd-shaped table."""

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


def _preview(store, title):
    w = _widget(title)
    res = store.dashboard_widget_preview(w["source"], w["query"])
    return {tuple(r[:1])[0]: r[1] for r in res["rows"]}, [tuple(r) for r in res["rows"]]


def test_os_version_reads_currentversion_only(reg_store):
    kv, rows = _preview(reg_store, "OS version")
    assert kv["ProductName"] == "Windows 10 Pro"
    assert kv["DisplayVersion"] == "22H2"
    assert kv["CurrentBuild"] == "19045"
    assert "decoy" not in kv.values()                       # Winlogon subkey excluded
    assert [r[0] for r in rows][:2] == ["ProductName", "DisplayVersion"]


def test_system_role_maps_producttype(reg_store):
    kv, _ = _preview(reg_store, "System role")
    assert kv == {"Role": "Workstation"}


def test_domain_card_shows_hostname_and_workgroup_fallback(reg_store):
    kv, rows = _preview(reg_store, "Domain")
    assert kv["Hostname"] == "WKSTN-014"
    assert kv["Domain"] == "corp.example.com"
    assert kv["NV Domain"] == "(none — workgroup)"          # empty value reads honestly
    assert rows[0][0] == "Hostname"


def test_ip_card_lists_real_interfaces_only(reg_store):
    kv, rows = _preview(reg_store, "IP addresses")
    assert kv["IP (DHCP)"] == "10.0.0.5"
    assert kv["Gateway"] == "10.0.0.1"
    assert "0.0.0.0" not in [r[1] for r in rows]


def test_sysmon_counts_service_entries(reg_store):
    kv, _ = _preview(reg_store, "Sysmon")
    assert kv["Sysmon"] == "Installed — 3 service registry entries"


def test_sysmon_absent_reads_not_present(store, write_csv):
    store.ingest_csv(write_csv([REG_COLS] + [ROWS[0]], "bare.csv"), name="bare", build_fts=False)
    kv, _ = _preview(store, "Sysmon")
    assert kv["Sysmon"] == "Not present in registry"


def test_powershell_logging_posture_and_fallback(reg_store, store, write_csv):
    kv, _ = _preview(reg_store, "PowerShell logging")
    assert kv["Script block"] == "Enabled"
    assert kv["Module logging"] == "Disabled"
    assert "Transcription" not in kv                        # value genuinely absent


def test_powershell_unconfigured_says_so(store, write_csv):
    store.ingest_csv(write_csv([REG_COLS] + [ROWS[0]], "bare2.csv"), name="bare2", build_fts=False)
    kv, _ = _preview(store, "PowerShell logging")
    assert kv == {"Policy": "(not configured — no policy keys)"}
