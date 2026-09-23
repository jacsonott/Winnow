"""Scoping the search-all sweep to chosen tables.

The sweep has only ever had one scope — every real table in the case — so
the one-table question ("do these forty hostnames appear in THIS file")
could only be answered by sweeping the whole case and reading one row of
the answer. `source_ids` carries the scope from the modal to the snapshot
`_iter_search_all_sources` filters, the shape `_iter_watchlist_scan`
already uses.

The merge case is the one worth staring at: a merge has no table of its
own (CLAUDE.md invariant #9's exceptions), so "search this table" on a
merged table can only mean searching the tables its rows actually live in.
It expands to its members, and says so rather than quietly answering about
different tables than the one that was named.
"""

from __future__ import annotations

import pytest


def _two_tables(store, write_csv):
    """Two real tables, each with a row only it matches."""
    p1 = write_csv([["Process"], ["svchost.exe"], ["rclone.exe"]], name="one.csv")
    p2 = write_csv([["Process"], ["svchost.exe"], ["lsass.exe"]], name="two.csv")
    a = store.ingest_csv(p1, name="one.csv", build_fts=False)
    b = store.ingest_csv(p2, name="two.csv", build_fts=False)
    return a["id"], b["id"]


def test_unscoped_sweep_still_covers_every_table(store, write_csv):
    a, b = _two_tables(store, write_csv)
    hits = store.search_all_sources(query="svchost")
    assert sorted(h["source_id"] for h in hits) == sorted([a, b])


def test_scoped_sweep_counts_only_the_named_table(store, write_csv):
    a, b = _two_tables(store, write_csv)
    hits = store.search_all_sources(query="svchost", source_ids=[a])
    assert [h["source_id"] for h in hits] == [a]
    # ...and the table left out is genuinely not scanned, not merely not
    # reported: its own unique term comes back with no hits at all.
    assert store.search_all_sources(query="lsass", source_ids=[a]) == []
    assert [h["source_id"] for h in store.search_all_sources(query="lsass", source_ids=[b])] == [b]


def test_scoped_job_reports_the_scope_it_ran_and_scans_only_it(store, write_csv):
    a, _b = _two_tables(store, write_csv)
    started = store.start_search_all_job(query="svchost", source_ids=[a])
    assert started["scope"] == {"requested": [a], "source_ids": [a], "merges": []}
    job = store.wait_for_search_all_job(timeout=10)
    assert job["done"] is True
    # `total` is the progress denominator the modal shows — it has to count
    # the tables this run really covers, not the case's tables.
    assert job["total"] == 1
    assert [h["source_id"] for h in job["hits"]] == [a]
    assert job["scope"]["source_ids"] == [a]


def test_an_unscoped_job_says_so_in_its_scope(store, write_csv):
    _two_tables(store, write_csv)
    store.start_search_all_job(query="svchost")
    job = store.wait_for_search_all_job(timeout=10)
    assert job["scope"] == {"requested": None, "source_ids": None, "merges": []}
    assert job["total"] == 2


def test_scoping_to_a_merge_searches_its_member_tables(store, write_csv):
    """Merge parity (invariant #9). The open table can be a merge, and the
    sweep has never scanned one — `list_sources` returns real sources only,
    and a merge's rows are counted through its members. Scoping to one
    expands to those members rather than scanning nothing."""
    a, b = _two_tables(store, write_csv)
    merge = store.create_merge("Both", [a, b])
    assert merge["id"] < 0

    scope = store.resolve_search_all_scope([merge["id"]])
    assert scope["source_ids"] == [a, b]
    assert scope["merges"] == [
        {"id": merge["id"], "name": "Both", "member_source_ids": [a, b]},
    ]

    hits = store.search_all_sources(query="svchost", source_ids=[merge["id"]])
    # The results name the MEMBERS: there is no per-merge count to report,
    # and a hit keyed by the merge's negative id would name a table the
    # "Open ↦" button could not filter.
    assert sorted(h["source_id"] for h in hits) == sorted([a, b])

    job = store.start_search_all_job(query="rclone", source_ids=[merge["id"]])
    assert job["scope"]["merges"][0]["name"] == "Both"
    assert job["scope"]["source_ids"] == [a, b]
    done = store.wait_for_search_all_job(timeout=10)
    assert done["total"] == 2
    assert [h["source_id"] for h in done["hits"]] == [a]


def test_a_merge_and_one_of_its_members_resolve_to_each_table_once(store, write_csv):
    a, b = _two_tables(store, write_csv)
    merge = store.create_merge("Both", [a, b])
    assert store.resolve_search_all_scope([a, merge["id"]])["source_ids"] == [a, b]


def test_an_unknown_table_in_the_scope_is_an_error_not_an_empty_answer(store, write_csv):
    a, _b = _two_tables(store, write_csv)
    for bad in (a + 999, -999):
        try:
            store.resolve_search_all_scope([bad])
        except KeyError:
            pass
        else:
            raise AssertionError(f"scope {bad} should not resolve")


def test_a_table_dropped_after_the_scope_was_chosen_costs_only_its_own_scan(store, write_csv):
    """The scope is resolved once, where it is accepted. What happens to a
    table that goes away between then and the sweep reaching it is decided
    by the `list_sources()` snapshot the sweep filters: the id is simply
    not in it, so it loses its own scan rather than ending the run — which
    is how the unscoped sweep has always treated a source removed under
    it."""
    a, b = _two_tables(store, write_csv)
    store.drop_source(b)
    hits = [h for _, _, h in store._iter_search_all_sources(query="svchost", source_ids=[a, b]) if h]
    assert [h["source_id"] for h in hits] == [a]


def test_an_empty_scope_scans_nothing(store, write_csv):
    _two_tables(store, write_csv)
    assert store.search_all_sources(query="svchost", source_ids=[]) == []
    store.start_search_all_job(query="svchost", source_ids=[])
    job = store.wait_for_search_all_job(timeout=10)
    assert job["total"] == 0 and job["hits"] == []


def test_the_route_carries_the_scope_and_rejects_an_unknown_table(client, store, write_csv):
    a, b = _two_tables(store, write_csv)
    r = client.post("/api/search_all/start", json={"query": "svchost", "source_ids": [a]})
    assert r.status_code == 200
    assert r.json()["scope"]["source_ids"] == [a]
    store.wait_for_search_all_job(timeout=10)

    r = client.post("/api/search_all", json={"query": "svchost", "source_ids": [b]})
    assert [h["source_id"] for h in r.json()] == [b]

    bad = client.post("/api/search_all/start", json={"query": "svchost", "source_ids": [b + 999]})
    assert bad.status_code == 400
    assert bad.json()["detail"] == f"No table {b + 999}"      # not the repr KeyError carries
    assert client.post("/api/search_all", json={"query": "x", "source_ids": [b + 999]}).status_code == 400


def test_a_keyerror_inside_the_sweep_is_not_reported_as_a_bad_request(
        client, store, write_csv, monkeypatch):
    """Only the scope resolve gets to answer 400 here. A KeyError from the
    scan itself is a defect in Winnow, and returning it as a 400 would
    blame the analyst's request for something they cannot fix while
    swallowing the traceback — the shape api_view was fixed out of (see
    docs/notes/server.md)."""
    a, _b = _two_tables(store, write_csv)

    def boom(*_args, **_kw):
        raise KeyError("a column the scan expected")

    monkeypatch.setattr(store, "search_all_sources", boom)
    with pytest.raises(KeyError):
        client.post("/api/search_all", json={"query": "svchost", "source_ids": [a]})
