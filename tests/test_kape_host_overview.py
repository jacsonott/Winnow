"""The shape of the KAPE triage board: one board, ten cards, every fact
on it exactly once.

This board has been consolidated twice. The first time, two boards became
one of 26 cards — and this file exists because the second board kept
coming back. The second time, those 26 cards became 10: five host-fact kv
cards into **Host**, two chips cards and the two tampering counts into
**Logging posture**, coverage and the activity window into **Coverage**,
and ten single-number stats into **Triage signals** and **Findings**.

So the assertions here are about facts, not titles. Every fact the board
carries appears exactly once — as a card, as a cell of a signals card, or
as a row of the Host card — and nothing that was merged away survives as
a card of its own. Titles are checked too, but as the names of facts
rather than as a list to be updated whenever one moves.
"""

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
    assert not kape.get("dashboards"), "the host overview was merged into the triage board"
    return {"name": kape["name"], "widgets": kape["dashboard"]}


def _widget(title):
    return next(w for w in _board()["widgets"] if w["title"] == title)


def _facts():
    """Every fact the board names, whether it is a card or a cell of one."""
    out = []
    for w in _board()["widgets"]:
        out.append(w["title"])
        out.extend(c["label"] for c in (w.get("cells") or []))
    return out


def _run(store, w):
    return store.dashboard_widget_preview(w["source"], w.get("query") or {}, cells=w.get("cells"))["rows"]


def _rows(store, title):
    return _run(store, _widget(title))


def _kv(store, title):
    return {r[0]: r[1] for r in _rows(store, title)}


@pytest.fixture
def host(store, write_csv):
    store.ingest_csv(write_csv([REG_COLS] + REG_ROWS, "recmd.csv"), name="recmd", build_fts=False)
    store.ingest_csv(write_csv([EVTX_COLS] + EVTX_ROWS, "evtx.csv"), name="evtx", build_fts=False)
    return store


# ------------------------------------------------------------ board shape

def test_the_board_covers_the_asked_for_facts_once_each():
    facts = _facts()
    for want in ["Host", "Logging posture", "Coverage", "Triage signals", "Findings",
                 "Sysmon service", "Sysmon driver", "Script block", "Module", "Transcription",
                 "Logs cleared (1102)", "Audit policy changed (4719)",
                 "Computers seen", "Distinct accounts", "Remote logon peers",
                 "Failed logons (4625)", "Explicit creds (4648)", "RDP sessions (21/24/25)",
                 "Defender alerts", "Run / service entries",
                 "Watchlist hits", "Tagged findings",
                 "Logon volume over time (4624/4625/4648)", "Top remote hosts (4624)",
                 "Rarest logon accounts (long tail)", "Registry entries by category",
                 "Most recent Defender alerts"]:
        assert facts.count(want) == 1, want
    assert len(facts) == len(set(facts)), "a fact is named twice on the board"


def test_a_merged_card_is_gone_as_a_card_and_present_as_a_fact():
    """The five host-fact cards, the two chips cards and the ten stats do
    not survive as cards of their own — their facts moved INTO Host,
    Logging posture, Triage signals and Findings. A merge that left the
    old card behind would double every number on the board."""
    titles = [w["title"] for w in _board()["widgets"]]
    for merged in ["Hostname", "IP addresses", "Domain", "OS version", "System function",
                   "Sysmon enabled", "PowerShell logging enabled", "Security log coverage",
                   "Activity window", "Watchlist hits", "Tagged findings", "Computers seen",
                   "Distinct accounts", "Remote logon peers", "Failed logons (4625)",
                   "Explicit credentials (4648)", "RDP sessions (21/24/25)", "Defender detections",
                   "Event logs cleared (1102)", "Audit policy changed (4719)",
                   "Run / service registry entries"]:
        assert merged not in titles, merged
    # And the three retired long before that stay retired.
    for retired in ["System role", "Sysmon", "PowerShell logging"]:
        assert retired not in _facts(), retired


def test_the_board_is_ten_cards_that_fill_their_rows():
    """`.dash-grid` is four fixed columns and `grid-auto-flow` is left at
    its default, so a card that does not fit the columns left in a row
    drops to the next one and leaves a hole. Each intended row has to sum
    to four."""
    widgets = _board()["widgets"]
    assert len(widgets) == 10
    spans = [w.get("span", 1) for w in widgets]
    assert spans == [2, 1, 1, 3, 1, 2, 2, 2, 2, 2]
    row, rows = 0, []
    for s in spans:
        row += s
        if row == 4:
            rows.append(4)
            row = 0
        assert row <= 4, spans
    assert rows == [4, 4, 4, 4] and row == 2   # four full rows, then the last card beside ＋ Add


def test_a_card_only_carries_copy_its_render_kind_can_show():
    """`sub` is drawn by `stat` and by `signals`, and by nothing else. A
    kv or chart card carrying one is a sentence written for a screen that
    never shows it — which is how the two chips cards it replaced came to
    carry subs nobody had ever read."""
    for w in _board()["widgets"]:
        if w.get("sub"):
            assert w["render"] in ("stat", "signals"), (w["title"], w["render"])


def test_only_the_cheap_card_runs_on_every_open():
    """A board paints from its cached results and re-runs only the widgets
    marked live, so `live` is a claim that the query is cheap enough to pay
    for on every open. The watchlist and tag counts are reads of two small
    case tables; everything else on this board scans a log."""
    live = [w["title"] for w in _board()["widgets"] if w.get("live")]
    assert live == ["Findings"]
    findings = _widget("Findings")
    assert [c["source"] for c in findings["cells"]] == ["watchlist", "tags"]


# ----------------------------------------------------------- the answers

def test_the_host_card_reads_the_batch(host):
    kv = _kv(host, "Host")
    assert kv["Computer name"] == "WKSTN-014" and kv["Hostname (TCP/IP)"] == "wkstn-014"
    assert kv["Domain"] == "corp.example.com"
    assert kv["IP (DHCP)"] == "10.0.0.5" and "0.0.0.0" not in kv.values()
    assert kv["ProductName"] == "Windows 11 Enterprise" and kv["CurrentBuild"] == "22631"
    assert kv["Function"] == "Domain controller"


def test_the_posture_card_reads_both_tables_at_once(host):
    """Its chips come from the registry and its two counts from the event
    log — one card, two tables, because each cell is asked separately."""
    kv = _kv(host, "Logging posture")
    assert kv["Sysmon service"] == 1 and kv["Sysmon driver"] == 0
    assert kv["Script block"] == 1 and kv["Module"] == 0 and kv["Transcription"] == 0
    assert kv["Logs cleared (1102)"] == 0 and kv["Audit policy changed (4719)"] == 0


def test_coverage_is_the_security_log_plus_the_window_any_channel_covers(host):
    kv = _kv(host, "Coverage")
    assert kv["Security: oldest"] == "2024-01-05 13:22:01"   # the System-channel 2023 event does not count
    assert kv["Security: newest"] == "2024-01-15 01:00:00"
    assert kv["Security: span (days)"] == 9.5
    assert kv["Security: events"] == 2
    # The activity window the second card used to hold: every channel.
    assert kv["All channels: window"] == "2023-12-31 23:59:59 → 2024-01-15 01:00:00"


def test_every_coverage_row_says_which_population_it_counted(host):
    """Merging "Security log coverage" into "Coverage" put four
    Security-only rows next to an all-channel one and dropped the only
    word that said which was which: "Oldest event" read 2024-01-05 on a
    case whose oldest event is 2023-12-31, and nothing on the card could
    reconcile the two numbers. Each row carries its own scope now.

    The fixture keeps the two populations genuinely different — a
    System-channel event older than every Security one — so a row that
    lost its scope again would be labelled wrongly, not just vaguely."""
    rows = _rows(host, "Coverage")
    scoped = [k for k, _ in rows if k.startswith("Security: ")]
    everything = [k for k, _ in rows if k.startswith("All channels: ")]
    assert len(scoped) == 4 and len(everything) == 1
    assert len(rows) == len(scoped) + len(everything)
    kv = dict(rows)
    assert kv[everything[0]].startswith("2023-12-31")        # the System-channel event
    assert kv["Security: oldest"].startswith("2024-01-05")   # and not in the Security rows


def test_coverage_says_when_there_is_no_security_log(store, write_csv):
    store.ingest_csv(write_csv([EVTX_COLS, _ev(TimeCreated="2024-01-01 00:00:00", EventId="1", Channel="System")], "e.csv"),
                     name="e", build_fts=False)
    kv = _kv(store, "Coverage")
    assert kv["Security: oldest"] == "(no Security channel events)" and kv["Security: events"] == 0
    # One day: the second timestamp drops its date, or the row is wider
    # than the card and wraps mid-arrow.
    assert kv["All channels: window"] == "2024-01-01 00:00:00 → 00:00:00"


def test_defender_alerts_newest_first_with_description_fallback(host):
    rows = _rows(host, "Most recent Defender alerts")
    assert rows == [["2024-01-12 09:00:00", "1117 · Action taken: Quarantine"],
                    ["2024-01-11 09:00:00", "5001 · Real-time protection disabled"],
                    ["2024-01-10 09:00:00", "1116 · Malware detected"]]
    # and the signal beside it counts exactly those, not a narrower set
    assert dict(_rows(host, "Triage signals"))["Defender alerts"] == 3


def test_defender_alerts_say_when_there_are_none(store, write_csv):
    store.ingest_csv(write_csv([EVTX_COLS, _ev(TimeCreated="2024-01-01 00:00:00", EventId="4624", Channel="Security")], "e.csv"),
                     name="e", build_fts=False)
    assert _rows(store, "Most recent Defender alerts") == [["—", "(no Defender alert events in the logs)"]]


def test_the_signals_card_answers_every_cell_in_one_go(host):
    rows = _rows(host, "Triage signals")
    assert [r[0] for r in rows] == [c["label"] for c in _widget("Triage signals")["cells"]]
    by = dict(rows)
    assert by["Failed logons (4625)"] == 1 and by["Computers seen"] == 0
    # The registry cell rides in the same card and the same request as the
    # nine evtx ones. Its predicate is the one it had as a card of its own,
    # `%\\Services%` included — which catches every service key in the batch,
    # not just the autostart ones. Carried over unchanged on purpose: this
    # change moved cards, it did not rewrite what they count.
    assert by["Run / service entries"] == 5


# ------------------------------------------------------------ applying it

def test_applying_the_profile_lands_one_board(client, host):
    kape = next(b for b in client.get("/api/plugin_bundles").json() if b["name"] == "KAPE triage")
    assert kape.get("dashboards", []) == []
    body = client.post(f"/api/plugin_bundles/{kape['id']}/apply").json()
    assert body["dashboard_applied"] is True and body["dashboards_applied"] == []
    boards = {b["name"]: b for b in client.get("/api/dashboards").json()}
    assert [n for n in boards if n.startswith("KAPE")] == ["KAPE triage"]
    widgets = client.get(f"/api/dashboards/{boards['KAPE triage']['id']}").json()["widgets"]
    assert len(widgets) == len(_board()["widgets"])
    # a second apply refreshes rather than duplicates
    client.post(f"/api/plugin_bundles/{kape['id']}/apply")
    assert sum(1 for b in client.get("/api/dashboards").json() if b["name"] == "KAPE triage") == 1
    # every widget previews without error against the fixture
    for w in widgets:
        pv = client.post("/api/dashboard/widget/preview", json={
            "source": w["source"], "query": w.get("query", {}), "cells": w.get("cells")})
        assert pv.status_code == 200, (w["title"], pv.text)


def test_an_existing_case_keeps_the_board_it_was_given(client, host, case_path):
    """Consolidation changes the SHIPPED profile, and nothing migrates a
    case that already carries a board under that name: the shipped widgets
    are written by `upsert_dashboard_by_name`, which runs on apply and
    nowhere else. What that buys the analyst is the edit they made to the
    board surviving — so that is what is asserted, by making one and
    reopening the case file around it.

    And the other half of "only on apply": re-applying the profile really
    does put the shipped board back. Between those two, the sentence has
    a test."""
    kape = next(b for b in client.get("/api/plugin_bundles").json() if b["name"] == "KAPE triage")
    client.post(f"/api/plugin_bundles/{kape['id']}/apply")
    did = next(b["id"] for b in client.get("/api/dashboards").json() if b["name"] == "KAPE triage")
    shipped = client.get(f"/api/dashboards/{did}").json()["widgets"]

    # The analyst makes the board theirs: one card renamed, one dropped.
    mine = [dict(w) for w in shipped if w["title"] != "Findings"]
    mine[0]["title"] = "Host — WKSTN-014"
    assert client.post(f"/api/dashboards/{did}", json={"widgets": mine}).status_code == 200

    # Reopening the case file is not an apply.
    host.close()
    assert client.post("/api/case/open", json={"path": case_path}).status_code == 200
    after = [w["title"] for w in client.get(f"/api/dashboards/{did}").json()["widgets"]]
    assert after == [w["title"] for w in mine]
    assert "Findings" not in after and len(after) == len(shipped) - 1

    # Re-applying is, and it is the only thing that is.
    client.post(f"/api/plugin_bundles/{kape['id']}/apply")
    back = [w["title"] for w in client.get(f"/api/dashboards/{did}").json()["widgets"]]
    assert back == [w["title"] for w in shipped]


def test_saved_bundles_keep_extra_boards(client):
    r = client.post("/api/plugin_bundles", json={
        "name": "Two boards", "plugins": [], "dashboard": [{"title": "a", "source": "tags", "render": "stat"}],
        "dashboards": [{"name": "Second", "widgets": [{"title": "b", "source": "tags", "render": "stat"}]}]})
    assert r.status_code == 200 and [b["name"] for b in r.json()["dashboards"]] == ["Second"]
