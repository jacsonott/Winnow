"""The ESXi / UAC triage profile end to end: the esxi_logs plugin parses
the support-bundle log formats into the shared schema, the {{all:...}}
union placeholder spans every imported log, and the overview dashboard's
widgets resolve and return data."""

from __future__ import annotations

from pathlib import Path

import pytest

from winnow import defaults
from winnow.plugin_api import PluginRegistry

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "plugins"

HOSTD = """\
2024-01-05T13:22:01.100Z info hostd[2098293] [Originator@6876 sub=Vimsvc] Task Created : PowerOnVM
2024-01-05T13:22:02.200Z info hostd[2098293] [Originator@6876 sub=Snapshot] Snapshot created for VM web01
2024-01-05T13:22:03.300Z verbose hostd[2098293] [Originator@6876] PowerOff requested for VM db01
"""

AUTH = """\
Jan  5 13:22:01 esxi01 sshd[12345]: Accepted password for root from 10.0.0.5 port 22 ssh2
Jan  5 13:22:05 esxi01 sshd[12346]: Failed password for root from 10.0.0.9 port 41888 ssh2
Jan  5 13:22:07 esxi01 sudo[12350]: dcui : TTY=pts/0 ; USER=root ; COMMAND=/bin/sh
"""

SHELL = """\
2024-01-05T13:22:10Z shell[9001]: [root]: esxcli network firewall set --enabled false
2024-01-05T13:22:11Z shell[9001]: [root]: vim-cmd vmsvc/getallvms
2024-01-05T13:22:12Z shell[9001]: [root]: esxcli network firewall set --enabled false
"""

VOBD = """\
2024-01-05T13:22:20Z vobd: [AccountManager] Account admin2 was created
2024-01-05T13:22:21Z vobd: [LockdownMode] Lockdown mode disabled
"""

RHTTP = """\
2024-01-05T13:22:30.000Z info rhttpproxy[9999] [Originator@6876] Connection from 10.0.0.9:52344
2024-01-05T13:22:31.000Z info rhttpproxy[9999] [Originator@6876] Connection from 10.0.0.9:52345
"""

ESXUPDATE = """\
2024-01-05T13:22:40Z esxupdate[7001]: VIB VMware_bootbank_esx-base installed
"""

LOGS = {"hostd.log": HOSTD, "auth.log": AUTH, "shell.log": SHELL,
        "vobd.log": VOBD, "rhttpproxy.log": RHTTP, "esxupdate.log": ESXUPDATE}


@pytest.fixture
def esxi_registry() -> PluginRegistry:
    reg = PluginRegistry()
    reg.load([EXAMPLES])
    return reg


@pytest.fixture
def esxi_store(store, esxi_registry, tmp_path):
    """A case with every ESXi log ingested through the plugin format."""
    fmt = esxi_registry.get_format("esxi-logs.esxi_log")
    for fname, text in LOGS.items():
        p = tmp_path / fname
        p.write_text(text)
        parsed = fmt.parse(str(p), fmt.resolve_options({}))
        store.ingest_rows(parsed["columns"], parsed["rows"], name=fname,
                          column_types=parsed["column_types"], build_fts=False)
    return store


def _widgets():
    prof = next(p for p in defaults.profiles() if p["name"] == "ESXi / UAC triage")
    return prof["dashboard"]


def _run(store, title):
    w = next(x for x in _widgets() if x["title"] == title)
    return store.dashboard_widget_preview(w["source"], w["query"])


def test_esxi_plugin_loads_without_error(esxi_registry):
    rec = next(d for d in esxi_registry.describe() if d["fs_name"] == "esxi_logs")
    assert rec["error"] is None, rec["error"]
    assert "esxi-logs.esxi_log" in rec["formats"]


def test_plugin_parses_each_log_type_into_the_shared_schema(esxi_store):
    got = {s["name"]: s for s in esxi_store.list_sources()}
    assert set(got) == set(LOGS)
    cols = [c["name"] for c in got["hostd.log"]["columns"]]
    assert cols == ["Timestamp", "Log", "Severity", "Component", "PID", "CPU",
                    "User", "SourceIP", "Message"]


def test_union_placeholder_spans_every_log(esxi_store):
    total = _run(esxi_store, "Log lines")["rows"][0][0]
    assert total == sum(len(v.strip().splitlines()) for v in LOGS.values())
    types = {r[0]: r[1] for r in _run(esxi_store, "Log types")["rows"]}
    assert types["hostd"] == 3 and types["auth"] == 3 and types["shell"] == 3


def test_ssh_and_source_ip_widgets(esxi_store):
    assert _run(esxi_store, "SSH logins accepted")["rows"][0][0] == 1
    assert _run(esxi_store, "Failed SSH logins")["rows"][0][0] == 1
    ips = {r[0]: r[1] for r in _run(esxi_store, "Top source IPs")["rows"]}
    assert ips["10.0.0.9"] == 3          # 1 failed ssh + 2 rhttpproxy
    assert ips["10.0.0.5"] == 1


def test_shell_and_vm_operation_widgets(esxi_store):
    assert _run(esxi_store, "Shell commands")["rows"][0][0] == 3
    top = {r[0]: r[-1] for r in _run(esxi_store, "Top shell commands")["rows"]}
    assert top["esxcli network firewall set --enabled false"] == 2
    assert _run(esxi_store, "VM power operations")["rows"][0][0] == 2
    assert _run(esxi_store, "Snapshot operations")["rows"][0][0] == 1


def test_vobd_and_vib_widgets(esxi_store):
    assert _run(esxi_store, "Account / lockdown / firewall changes")["rows"][0][0] == 2
    assert _run(esxi_store, "VIB install / remove")["rows"][0][0] == 1


def test_sudo_widget(esxi_store):
    assert _run(esxi_store, "sudo / su use")["rows"][0][0] >= 1


def test_every_widget_resolves(esxi_store):
    for w in _widgets():
        res = esxi_store.dashboard_widget_preview(w["source"], w["query"])
        assert "rows" in res


def test_union_placeholder_absent_is_a_friendly_error(store):
    prof = next(p for p in defaults.profiles() if p["name"] == "ESXi / UAC triage")
    w = prof["dashboard"][0]
    with pytest.raises(ValueError, match="table in this case yet"):
        store.dashboard_widget_preview(w["source"], w["query"])
