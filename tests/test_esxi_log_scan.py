"""Folder import of an ESXi support bundle with the esxi_logs plugin on.

The bug: the plugin matched fourteen log stems by name and nothing by
extension, so every other .log in a bundle — vmkwarning, clomd,
sdrsinjector, vsanmgmt — was excluded by the folder scan's extension
gate, which no include pattern can reach; the plugin's parser had an
"other" type for exactly those files all along. Now the format claims
.log too, and the scan sees the whole bundle.
"""
from pathlib import Path

import pytest

from winnow.plugin_api import PluginRegistry

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "plugins"
LINE = "2026-03-14T08:00:00.123Z info clomd[2098] [Originator@6876 sub=Foo] hello from clomd\n"
BUNDLE = ["var/log/hostd.log", "var/log/hostd.1", "var/log/vpxa.log", "var/log/vmkwarning.log",
          "var/log/clomd.log", "var/log/sdrsinjector.log", "var/run/log/vsanmgmt.log", "commands/localcli_network-ip.txt"]


@pytest.fixture
def esxi_format():
    reg = PluginRegistry()
    reg.load([EXAMPLES])
    fmt = reg.get_format("esxi-logs.esxi_log")
    assert fmt is not None
    return fmt


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "esx-bundle"
    for rel in BUNDLE:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(LINE)
    return root


def test_the_format_claims_every_log_and_the_known_stems(esxi_format):
    assert ".log" in esxi_format.extensions
    for name in ("hostd.log", "vmkwarning.log", "clomd.log", "VSANMGMT.LOG", "hostd.1", "vmkernel.0"):
        assert esxi_format.matches(name), name
    assert not esxi_format.matches("localcli_network-ip.txt")
    assert not esxi_format.matches("report.csv")


def test_folder_scan_sees_the_whole_bundle(store, esxi_format, bundle):
    """The exact call the folder modal makes: default chips (which now
    carry the plugin's .log), the plugin's name patterns, no include."""
    from winnow.store import DEFAULT_IMPORT_EXTENSIONS

    chips = sorted(DEFAULT_IMPORT_EXTENSIONS) + esxi_format.extensions
    r = store.scan_import_directory(str(bundle), extensions=chips, filename_patterns=esxi_format.filename_patterns)
    kinds = {m["rel_path"]: m["kind"] for m in r["matched"]}
    assert kinds == {
        "var/log/hostd.log": "plugin", "var/log/hostd.1": "plugin", "var/log/vpxa.log": "plugin",
        "var/log/vmkwarning.log": "plugin", "var/log/clomd.log": "plugin", "var/log/sdrsinjector.log": "plugin",
        "var/run/log/vsanmgmt.log": "plugin",
        "commands/localcli_network-ip.txt": "csv",
    }
    assert r["excluded"] == []
    # An include of *.log narrows within that, as before
    r2 = store.scan_import_directory(str(bundle), extensions=chips, filename_patterns=esxi_format.filename_patterns,
                                     include_patterns=["*.log"])
    assert {m["rel_path"].split("/")[-1] for m in r2["matched"]} == {
        "hostd.log", "vpxa.log", "vmkwarning.log", "clomd.log", "sdrsinjector.log", "vsanmgmt.log"}


def test_an_unknown_stem_parses_as_other(esxi_format, bundle):
    out = esxi_format.parse(str(bundle / "var/log/clomd.log"), esxi_format.resolve_options({}))
    (row,) = list(out["rows"])
    by = dict(zip(out["columns"], row))
    assert by["Log"] == "other"
    assert by["Timestamp"] == "2026-03-14T08:00:00.123Z"
    assert by["Severity"] == "info" and by["Component"] == "clomd" and by["PID"] == "2098"
    assert by["Message"] == "hello from clomd"


def test_no_chips_means_no_extensions_not_the_defaults(store, bundle, esxi_format):
    r = store.scan_import_directory(str(bundle), extensions=[], filename_patterns=esxi_format.filename_patterns)
    # only the name patterns get anything past the gate now
    assert {m["rel_path"].split("/")[-1] for m in r["matched"]} == {"hostd.log", "hostd.1", "vpxa.log"}
    assert all(e["reason"] == "extension" for e in r["excluded"])
    r_default = store.scan_import_directory(str(bundle), filename_patterns=esxi_format.filename_patterns)
    assert "commands/localcli_network-ip.txt" in {m["rel_path"] for m in r_default["matched"]}
