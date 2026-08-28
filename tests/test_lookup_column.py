"""The lookup derived column: join a value in from another table, keyed
by the input column — VLOOKUP, materialized."""

from __future__ import annotations

import pytest

HOSTS = [["IP", "Hostname"],
         ["10.0.0.5", "DC01"],
         ["10.0.0.9", "WKS-ALICE"],
         ["10.0.0.5", "DC01-ALIAS"]]  # duplicate key — 'multi' decides
EVENTS = [["When", "Src"],
          ["2024-01-01 10:00", "10.0.0.5"],
          ["2024-01-01 11:00", "10.0.0.9"],
          ["2024-01-01 12:00", "172.16.0.1"]]  # no match


def _mk(store, write_csv):
    lk = store.ingest_csv(write_csv(HOSTS, "hosts.csv"), name="hosts", build_fts=False)["id"]
    ev = store.ingest_csv(write_csv(EVENTS, "events.csv"), name="events", build_fts=False)["id"]
    return lk, ev


def _add(store, sid, params, name="Host"):
    res = store.add_derived_column(sid, name, "Src", "lookup", params)
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    return res


def _values(store, sid, column):
    src = store.get_source(sid)
    idx = [c["name"] for c in src["columns"]].index(column)
    v = store.build_view(sid, {"source_id": sid, "filters": [], "sort": []})
    return [r["cells"][idx] for r in store.fetch_rows(v["view_id"], 0, 10)["rows"]]


def test_lookup_first_match(store, write_csv):
    lk, ev = _mk(store, write_csv)
    _add(store, ev, {"other_source_id": lk, "match_column": "IP",
                     "value_column": "Hostname", "multi": "first"})
    assert _values(store, ev, "Host") == ["DC01", "WKS-ALICE", None]


def test_lookup_all_matches_comma_joined(store, write_csv):
    lk, ev = _mk(store, write_csv)
    _add(store, ev, {"other_source_id": lk, "match_column": "IP",
                     "value_column": "Hostname", "multi": "all"})
    assert _values(store, ev, "Host") == ["DC01, DC01-ALIAS", "WKS-ALICE", None]


def test_lookup_filters_groups_and_chains(store, write_csv):
    lk, ev = _mk(store, write_csv)
    _add(store, ev, {"other_source_id": lk, "match_column": "IP",
                     "value_column": "Hostname", "multi": "first"})
    v = store.build_view(ev, {"source_id": ev, "sort": [],
                              "filters": [{"column": "Host", "op": "equals", "value": "DC01"}]})
    assert v["row_count"] == 1
    # chains: regex on the looked-up value
    res = store.add_derived_column(ev, "HostKind", "Host", "regex_extract", {"pattern": r"^([A-Z]+)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert _values(store, ev, "HostKind") == ["DC", "WKS", None]


def test_lookup_validates_at_creation(store, write_csv):
    lk, ev = _mk(store, write_csv)
    with pytest.raises(ValueError, match="no key column"):
        store.add_derived_column(ev, "X", "Src", "lookup",
                                 {"other_source_id": lk, "match_column": "Nope", "value_column": "Hostname"})
    with pytest.raises(ValueError, match="No table"):
        store.add_derived_column(ev, "X", "Src", "lookup",
                                 {"other_source_id": 999, "match_column": "IP", "value_column": "Hostname"})
    with pytest.raises(ValueError, match="real table"):
        store.add_derived_column(ev, "X", "Src", "lookup",
                                 {"other_source_id": -1, "match_column": "IP", "value_column": "Hostname"})


def test_lookup_on_a_merge_fans_out(store, write_csv):
    lk, ev = _mk(store, write_csv)
    ev2 = store.ingest_csv(write_csv(EVENTS, "events2.csv"), name="events2", build_fts=False)["id"]
    mid = store.create_merge("evm", [ev, ev2])["id"]
    res = store.add_derived_column(mid, "Host", "Src", "lookup",
                                   {"other_source_id": lk, "match_column": "IP",
                                    "value_column": "Hostname", "multi": "first"})
    for j in res["job_ids"]:
        store.wait_for_ingest_job(j, timeout=30)
    vals = _values(store, mid, "Host")
    assert vals.count("DC01") == 2 and vals.count("WKS-ALICE") == 2


def test_rederive_repoints_the_lookup(store, write_csv):
    lk, ev = _mk(store, write_csv)
    res = _add(store, ev, {"other_source_id": lk, "match_column": "IP",
                           "value_column": "Hostname", "multi": "first"})
    did = res["definition"]["id"]
    out = store.rederive_column(did, {"other_source_id": lk, "match_column": "IP",
                                      "value_column": "IP", "multi": "first"})
    store.wait_for_ingest_job(out["job_id"], timeout=30)
    assert _values(store, ev, "Host") == ["10.0.0.5", "10.0.0.9", None]


def test_preview_loads_the_mapping(store, write_csv):
    lk, ev = _mk(store, write_csv)
    prev = store.preview_derived(ev, "Src", "lookup",
                                 {"other_source_id": lk, "match_column": "IP",
                                  "value_column": "Hostname", "multi": "first"})
    outs = {p["input"]: p["output"] for p in prev["preview"]}
    assert outs["10.0.0.5"] == "DC01"
    assert prev["failures"] == 1  # the unmatched 172.16.0.1
