"""Deleting a merge: the merge goes, everything it owned goes with it,
and everything its MEMBERS own survives — a merge has no tags or notes
of its own (invariant #9: they live on the member rows), so member-side
triage state is exactly what deletion must never touch."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

HEADERS = {"X-Timeline-Lite-Client": "1"}

ROWS_A = [["When", "Who"], ["2024-01-01 10:00", "alice"], ["2024-01-01 11:00", "bob"]]
ROWS_B = [["When", "Who"], ["2024-01-02 10:00", "carol"]]


@pytest.fixture
def merged(store, write_csv):
    a = store.ingest_csv(write_csv(ROWS_A, "a.csv"), build_fts=False)["id"]
    b = store.ingest_csv(write_csv(ROWS_B, "b.csv"), build_fts=False)["id"]
    merge = store.create_merge("both", [a, b])
    assert merge["id"] < 0    # the signed-id trap — see docs/notes/store.md
    return a, b, merge


def test_delete_merge_removes_it_and_all_its_keyed_state(store, merged):
    a, b, merge = merged
    store.save_layout(merge["id"], {"widths": {"When": 140}})
    store.save_view(merge["id"], "sorted", {"sort": [{"column": "When", "dir": "asc"}]})

    store.delete_merge(-merge["id"])

    assert store.list_merges() == []
    assert all(s["id"] != merge["id"] for s in store.list_sources())
    for table in ("open_tabs", "layouts", "saved_views"):
        n = store.db.execute(
            f"SELECT COUNT(*) c FROM {table} WHERE source_id=?", (merge["id"],)).fetchone()["c"]
        assert n == 0, f"{table} row leaked for the deleted merge"


def test_members_and_their_tags_survive_merge_deletion(store, merged):
    a, b, merge = merged
    tag = store.list_tags()[0]["id"]
    store.set_tags(a, [1], tag, True)

    store.delete_merge(-merge["id"])

    ids = {s["id"] for s in store.list_sources()}
    assert a in ids and b in ids
    n = store.db.execute(
        "SELECT COUNT(*) c FROM row_tags WHERE source_id=?", (a,)).fetchone()["c"]
    assert n == 1


def test_deleting_a_nonexistent_merge_is_a_quiet_no_op(store, merged):
    store.delete_merge(999)   # nothing raised, nothing else harmed
    assert len(store.list_merges()) == 1


def test_merge_delete_route(store, monkeypatch, merged):
    import server
    a, b, merge = merged
    monkeypatch.setattr(server, "STORE", store)
    c = TestClient(server.app)
    res = c.delete(f"/api/merges/{-merge['id']}", headers=HEADERS)
    assert res.status_code == 200
    assert store.list_merges() == []
