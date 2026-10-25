"""The KAPE board's registry-backed cards, against a synthetic RECmd batch
with the traps a real one has.

These facts used to be seven cards — Hostname, IP addresses, Domain, OS
version, System function and two chips cards — and are now two: one
**Host** card that UNIONs the five key-path lookups, and the registry
half of **Logging posture**, whose chips are cells with a drill each. The
traps carried over with them, and they are what this file is for: a
`CurrentVersion\\Winlogon` subkey that must not leak into the OS rows, a
disconnected 0.0.0.0 interface, an empty NV Domain, a Sysmon
`\\Parameters` subkey beside its ImagePath. Every one of them is keyed on
a KeyPath/ValueName suffix so it holds for any batch and any ControlSet.

The one behaviour that CHANGED with the merge, and is asserted here: a
fact the batch does not carry is simply not listed. Four of the five
cards it replaced rendered "(not in this RECmd output)" on a batch that
lacked them, so the top of the board was four empty cards where one Host
card belongs.

A batch carrying none of them at all answers with NO ROWS rather than a
sentinel line — the board reads an empty answer and collapses the card
into its empty-cards strip (static/js/dashboard.js), which is where the
reason and the fix belong.
"""

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
    # A subkey of CurrentVersion must NOT leak into the OS rows.
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


def _board():
    return next(p for p in defaults.profiles() if p["name"] == "KAPE triage")["dashboard"]


def _widget(title):
    return next(w for w in _board() if w["title"] == title)


def _rows(store, title):
    w = _widget(title)
    res = store.dashboard_widget_preview(w["source"], w.get("query") or {}, cells=w.get("cells"))
    return [tuple(r) for r in res["rows"]]


def _kv(store, title):
    return {r[0]: r[1] for r in _rows(store, title)}


def _host(store):
    return _kv(store, "Host")


# ------------------------------------------------------------ the Host card

def test_the_host_card_reads_every_fact_its_five_cards_did(reg_store):
    kv = _host(reg_store)
    assert kv["Hostname (TCP/IP)"] == "WKSTN-014"        # no ComputerName key in this batch
    assert kv["Domain"] == "corp.example.com"
    assert kv["ProductName"] == "Windows 10 Pro"
    assert kv["DisplayVersion"] == "22H2"
    assert kv["CurrentBuild"] == "19045"
    assert kv["IP (DHCP)"] == "10.0.0.5"
    assert kv["Gateway"] == "10.0.0.1"
    assert kv["Function"] == "Workstation"
    assert kv["NV Domain"] == "(none — workgroup)"       # an empty value reads honestly


def test_the_host_card_keeps_the_order_the_five_cards_were_read_in(reg_store):
    """Identity, then network, then OS, then role — the order the cards
    sat in, which is the order an analyst reads them."""
    assert [r[0] for r in _rows(reg_store, "Host")] == [
        "Hostname (TCP/IP)", "IP (DHCP)", "Gateway", "Domain", "NV Domain",
        "ProductName", "DisplayVersion", "CurrentBuild", "Function"]


def test_the_host_card_excludes_what_the_five_cards_excluded(reg_store):
    values = set(_host(reg_store).values())
    assert "decoy" not in values          # the CurrentVersion\Winlogon subkey
    assert "0.0.0.0" not in values        # the disconnected interface
    assert "SHA256" not in values         # Sysmon's Parameters subkey is not a host fact


def test_a_fact_the_batch_lacks_is_absent_rather_than_an_empty_card(store, write_csv):
    """The visible defect the merge fixes: four of the five cards rendered
    "(not in this RECmd output)" as a card of their own, so a batch with
    only an OS version gave four empty cards and one number."""
    store.ingest_csv(write_csv([REG_COLS, ROWS[0]], "bare.csv"), name="bare", build_fts=False)
    assert _rows(store, "Host") == [("ProductName", "Windows 10 Pro")]


def test_a_batch_with_no_host_facts_at_all_answers_with_no_rows(store, write_csv):
    """No sentinel row. The card used to UNION in a literal "(no host facts
    in this RECmd output)" so it would not render as a blank box, and that
    line then held a full-width card on the first screen of the board. The
    query now says what it found — nothing — and the board collapses the
    card into its empty-cards strip, which is the only place that can say
    WHY it is empty and offer to fix it."""
    store.ingest_csv(write_csv([REG_COLS, _row(KeyPath="x", ValueName="y", ValueData="z")], "none.csv"),
                     name="none", build_fts=False)
    assert _rows(store, "Host") == []


def test_a_fact_whose_only_row_is_empty_is_not_a_fact(store, write_csv):
    """The guard each of the five cards carried, and the one the merge has
    to keep: a ValueName RECmd wrote with no ValueData is a row, not a host
    fact. Without it every key path below matches and what lands on screen
    is a full-looking Host card whose values are all blank — which is
    worse than an empty one, because the empty one folds into the board's
    strip and says the batch carried no host facts."""
    rows = [
        _row(HiveType="SYSTEM", KeyPath="ControlSet001\\Control\\ComputerName\\ComputerName",
             ValueName="ComputerName", ValueData=""),
        _row(HiveType="SYSTEM", KeyPath="ControlSet001\\Services\\Tcpip\\Parameters",
             ValueName="Hostname", ValueData=""),
        _row(HiveType="SYSTEM", KeyPath="ControlSet001\\Services\\Tcpip\\Parameters\\Interfaces\\{9ec42dd6}",
             ValueName="DhcpIPAddress", ValueData=""),
        _row(HiveType="SOFTWARE", KeyPath="Microsoft\\Windows NT\\CurrentVersion",
             ValueName="ProductName", ValueData=""),
        _row(HiveType="SYSTEM", KeyPath="ControlSet001\\Control\\ProductOptions",
             ValueName="ProductType", ValueData=""),
    ]
    store.ingest_csv(write_csv([REG_COLS] + rows, "blank.csv"), name="blank", build_fts=False)
    assert _rows(store, "Host") == []


def test_an_empty_domain_is_the_one_empty_value_that_is_an_answer(store, write_csv):
    """Which is why the guard is per fact rather than one WHERE over the
    union: `Domain` with no value means the host is in a workgroup, and
    the card has always said so."""
    row = _row(HiveType="SYSTEM", KeyPath="ControlSet001\\Services\\Tcpip\\Parameters",
               ValueName="Domain", ValueData="")
    store.ingest_csv(write_csv([REG_COLS, row], "workgroup.csv"), name="workgroup", build_fts=False)
    assert _rows(store, "Host") == [("Domain", "(none — workgroup)")]


@pytest.mark.parametrize("product_type, reads", [
    ("WinNT", "Workstation"),
    ("ServerNT", "Member / standalone server"),
    ("LanmanNT", "Domain controller"),
    ("SomethingNew", "SomethingNew"),        # an unmapped value is shown, not hidden
])
def test_system_function_maps_producttype(store, write_csv, product_type, reads):
    row = _row(HiveType="SYSTEM", KeyPath="ControlSet001\\Control\\ProductOptions",
               ValueName="ProductType", ValueData=product_type)
    store.ingest_csv(write_csv([REG_COLS, row], "role.csv"), name="role", build_fts=False)
    assert _host(store)["Function"] == reads


# -------------------------------------------------------- Logging posture

def test_the_posture_chips_need_the_service_and_the_driver(reg_store):
    posture = _kv(reg_store, "Logging posture")
    assert posture["Sysmon service"] == 1
    assert posture["Sysmon driver"] == 1


def test_the_posture_chips_read_the_policy_keys(reg_store):
    posture = _kv(reg_store, "Logging posture")
    assert posture["Script block"] == 1
    assert posture["Module"] == 0           # the key is there and says 0
    assert posture["Transcription"] == 0    # the key is not there at all


def test_an_unconfigured_host_reads_off_on_every_chip(store, write_csv):
    store.ingest_csv(write_csv([REG_COLS, ROWS[0]], "bare2.csv"), name="bare2", build_fts=False)
    posture = _kv(store, "Logging posture")
    assert [posture[k] for k in ("Sysmon service", "Sysmon driver", "Script block", "Module", "Transcription")] \
        == [0, 0, 0, 0, 0]


def test_the_evtx_half_of_the_posture_card_answers_without_a_registry(store, write_csv):
    """The merge's other risk, and why the cells are asked separately: one
    card, two tables. A case with no RECmd batch must still get the
    tampering counts rather than one error where seven answers belong."""
    evtx_cols = dict(defaults.headers()["nicknames"])["Event logs (EvtxECmd)"]
    row = {c: "" for c in evtx_cols}
    row.update({"TimeCreated": "2026-03-14 08:00:00", "EventId": "1102", "Channel": "Security"})
    store.ingest_csv(write_csv([evtx_cols, [row[c] for c in evtx_cols]], "evtx.csv"),
                     name="evtx", build_fts=False)
    w = _widget("Logging posture")
    out = store.dashboard_widget_preview("cells", {}, cells=w["cells"])
    by = dict(out["rows"])
    assert by["Logs cleared (1102)"] == 1 and by["Audit policy changed (4719)"] == 0
    assert by["Sysmon service"] is None
    errs = dict(zip([r[0] for r in out["rows"]], out["cell_errors"]))
    assert "Registry (RECmd batch)" in errs["Sysmon service"]
    assert errs["Logs cleared (1102)"] is None
