"""The shipped TLE-converted filters (defaults/filters.json): every tree must
compile against a real table of its header set, and the conversions with
tricky precedence must select exactly the rows the TLE original would.

The compile-all test is the one that earns its keep long-term: a typo'd op
or column in a future addition fails here instead of as a 400 the first
time an analyst clicks the suggestion banner."""

from __future__ import annotations

import pytest

from winnow import defaults
from winnow.defaults import DefaultsError


def _table_for(store, write_csv, cols, rows, name):
    all_rows = [list(cols)] + [[r.get(c, "") for c in cols] for r in rows]
    return store.ingest_csv(write_csv(all_rows, name), name=name, build_fts=False)["id"]


def _apply(store, source_id, payload):
    spec = {
        "source_id": source_id, "filters": [], "sort": [],
        "filter_tree": payload.get("filter_tree"),
        "search": payload.get("search", ""),
        "search_mode": payload.get("search_mode", "contains"),
        "search_terms": payload.get("search_terms", []),
    }
    return store.build_view(source_id, spec)


def test_every_shipped_filter_compiles_against_its_header_set(store, write_csv):
    tables = {}
    for name, cols, payload in defaults.filters()["filters"]:
        key = tuple(cols)
        if key not in tables:
            tables[key] = _table_for(store, write_csv, cols, [{}], f"t{len(tables)}.csv")
        view = _apply(store, tables[key], payload)   # raises on a bad tree
        assert view["row_count"] >= 0


def test_seeded_filters_bind_to_the_shipped_header_sets():
    """Binding is what makes the suggestion banner offer these the moment an
    EvtxECmd/RECmd/MFT table opens — every col_names set must be one of
    headers.json's sets, verbatim.

    In the Python version a filter's column list WAS the header set's list,
    so this could not fail. Naming the set in JSON makes it possible to get
    wrong, which is why it is checked here as well as in the loader."""
    known = {tuple(sorted(c.strip().lower() for c in cols))
             for _, cols in defaults.headers()["nicknames"]}
    for name, cols, _ in defaults.filters()["filters"]:
        assert tuple(sorted(c.strip().lower() for c in cols)) in known, name


def test_a_filter_naming_an_unknown_header_set_is_refused(tmp_path, monkeypatch):
    """A filter bound to nothing would sit in the list and never apply to
    anything — silent, and the sort of thing only noticed months later."""
    import json
    bad = tmp_path / "filters.json"
    bad.write_text(json.dumps({"version": 1, "filters": [
        {"name": "Broken", "header_set": "No such tool", "payload": {"filter_tree": {}}}]}))
    monkeypatch.setattr(defaults, "FILTERS_FILE", bad)
    defaults.filters.cache_clear()
    try:
        with pytest.raises(DefaultsError, match="does not define"):
            defaults.filters()
    finally:
        defaults.filters.cache_clear()


def test_malformed_defaults_name_the_file(tmp_path, monkeypatch):
    bad = tmp_path / "headers.json"
    bad.write_text("{not json")
    monkeypatch.setattr(defaults, "HEADERS_FILE", bad)
    defaults.headers.cache_clear()
    try:
        with pytest.raises(DefaultsError, match="headers.json is not valid JSON"):
            defaults.headers()
    finally:
        defaults.headers.cache_clear()


def _cols(header_set):
    """The shipped column list for a header set. The Python module exposed
    these as EVTX/MFT/RECMD constants; the data has no such notion, so the
    tests look them up by the same name a filter uses."""
    return dict(defaults.headers()["nicknames"])[header_set]


def _payload(name):
    return next(p for n, _, p in defaults.filters()["filters"] if n == name)


def test_defense_evasion_keeps_tle_and_or_nesting(store, write_csv):
    """The TLE original is three parenthesized And-arms Or'd together —
    1102 counts only on the Security channel, 6006 only on System."""
    sid = _table_for(store, write_csv, _cols("Event logs (EvtxECmd)"), [
        {"EventId": "1102", "Channel": "Security"},    # hit
        {"EventId": "1102", "Channel": "System"},      # not: 1102 needs Security
        {"EventId": "6006", "Channel": "System"},      # hit
        {"EventId": "6006", "Channel": "Security"},    # not
        {"EventId": "5001", "Channel": "Whatever",
         "Provider": "Microsoft-Windows-Windows Defender/Operational"},  # hit
        {"EventId": "5001", "Channel": "Whatever", "Provider": "Other"},  # not
    ], "evtx.csv")
    view = _apply(store, sid, _payload("EVTX — Defense evasion: log clearing / Defender off"))
    assert view["row_count"] == 3


def test_logons_excludes_the_font_driver_and_dwm_noise(store, write_csv):
    sid = _table_for(store, write_csv, _cols("Event logs (EvtxECmd)"), [
        {"EventId": "4624", "PayloadData1": "Target: ACME\\jsmith"},   # hit
        {"EventId": "4624", "PayloadData1": "Target: UMFD-2"},          # excluded
        {"EventId": "4624", "PayloadData1": "Target: DWM-1"},           # excluded
        {"EventId": "9999", "PayloadData1": "Target: ACME\\jsmith"},   # wrong EID
    ], "logon.csv")
    view = _apply(store, sid, _payload("EVTX — Logons"))
    assert view["row_count"] == 1


def test_mft_odd_places_precedence_and_ini_exclusion(store, write_csv):
    """(roaming AND exe) OR music OR ... , all AND'd with <> '.ini' — the
    outer exclusion has to bind over the whole OR, not just the last arm."""
    sid = _table_for(store, write_csv, _cols("NTFS $MFT (MFTECmd)"), [
        {"ParentPath": ".\\Users\\x\\AppData\\Roaming\\y", "Extension": ".exe"},   # hit
        {"ParentPath": ".\\Users\\x\\AppData\\Roaming\\y", "Extension": ".dll"},   # not: roaming needs exe
        {"ParentPath": ".\\Users\\x\\Music", "Extension": ".ini"},                  # not: .ini excluded
        {"ParentPath": ".\\Users\\x\\Music", "Extension": ".dat"},                  # hit: music alone
        {"ParentPath": ".\\Users\\x\\Downloads", "Extension": ".ps1"},              # hit
        {"ParentPath": ".\\Users\\x\\Documents", "Extension": ".exe"},              # not: no listed dir
    ], "mft.csv")
    view = _apply(store, sid, _payload("MFT — Executables in odd places"))
    assert view["row_count"] == 3


def test_tool_sweep_is_an_advanced_search_with_the_xdr_exclusion(store, write_csv):
    """The -cyvera exclusion deliberately lives in the filter tree, not the
    term chain: connectors follow SQL precedence, so an AND NOT at the end
    of an OR chain would guard only the last term. Tree and search AND
    together server-side, which is TLE's search-minus-term semantics."""
    payload = _payload("MFT — Suspicious tool sweep (search)")
    assert payload["search_mode"] == "advanced"
    assert not any(t["exclude"] for t in payload["search_terms"])
    assert payload["filter_tree"]["children"][0]["op"] == "not_contains"
    sid = _table_for(store, write_csv, _cols("NTFS $MFT (MFTECmd)"), [
        {"FileName": "AnyDesk.exe", "ParentPath": ".\\ProgramData"},        # hit
        {"FileName": "rclone.conf", "ParentPath": ".\\Users\\x"},           # hit
        {"FileName": "psexec.exe", "ParentPath": ".\\cyvera\\agent"},      # excluded
        {"FileName": "notepad.exe", "ParentPath": ".\\Windows"},            # no term
    ], "tools.csv")
    view = _apply(store, sid, payload)
    assert view["row_count"] == 2


def test_powershell_filter_ships_with_its_grouping():
    p = _payload("EVTX — PowerShell")
    assert p["group_by"] == ["PayloadData1"]


def test_every_shipped_tree_root_is_a_group():
    """A bare-condition root compiles server-side but the client's spec
    gate reads it as "no filter" — the v1 seeds shipped eight of those, so
    'Sysmon present?' et al showed as applied while filtering nothing.
    Roots must be groups, forever."""
    for name, _, payload in defaults.filters()["filters"]:
        tree = payload.get("filter_tree")
        if tree:
            assert tree["type"] == "group", name


def test_network_share_access_actually_filters(store, write_csv):
    """Row-level check for one of the formerly cond-rooted filters — the
    compile-all test above can't catch a filter that silently matches
    everything."""
    sid = _table_for(store, write_csv, _cols("Event logs (EvtxECmd)"), [
        {"EventId": "5140"},   # hit
        {"EventId": "5145"},   # hit
        {"EventId": "4624"},   # must be excluded
    ], "shares.csv")
    view = _apply(store, sid, _payload("EVTX — Network share access (5140/5145)"))
    assert view["row_count"] == 2
