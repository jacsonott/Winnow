"""Saved views: the store CRUD and its three routes. This is currently a
backend-only feature (CLAUDE.md backlog #5 — the UI doesn't call it yet),
which is exactly why it needs pinning: nothing exercises it day-to-day,
so a regression would sit invisible until the UI finally lands on top of
a broken foundation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

HEADERS = {"X-Timeline-Lite-Client": "1"}

ROWS = [["When", "Who"], ["2024-01-01 10:00", "alice"], ["2024-01-01 11:00", "bob"]]

PAYLOAD = {"filters": [{"column": "Who", "op": "equals", "value": "alice"}],
           "sort": [{"column": "When", "dir": "desc"}],
           "search": "svc", "search_mode": "contains"}


@pytest.fixture
def sid(store, write_csv):
    return store.ingest_csv(write_csv(ROWS, "sv.csv"), build_fts=False)["id"]


def test_save_list_delete_round_trip(store, sid):
    saved = store.save_view(sid, "alice only", PAYLOAD)
    assert saved["id"] > 0

    views = store.list_saved_views(sid)
    assert len(views) == 1
    # Payload fidelity is the whole feature — a saved view that comes back
    # subtly different silently changes what the analyst re-applies.
    assert views[0]["payload"] == PAYLOAD
    assert views[0]["name"] == "alice only"

    store.delete_saved_view(saved["id"])
    assert store.list_saved_views(sid) == []


def test_saved_views_are_scoped_to_their_source(store, write_csv, sid):
    other = store.ingest_csv(write_csv(ROWS, "sv2.csv"), build_fts=False)["id"]
    store.save_view(sid, "mine", PAYLOAD)
    store.save_view(other, "theirs", PAYLOAD)
    assert [v["name"] for v in store.list_saved_views(sid)] == ["mine"]
    assert [v["name"] for v in store.list_saved_views(other)] == ["theirs"]


def test_newest_first_ordering(store, sid):
    store.save_view(sid, "first", PAYLOAD)
    store.save_view(sid, "second", PAYLOAD)
    assert [v["name"] for v in store.list_saved_views(sid)] == ["second", "first"]


def test_drop_source_takes_its_saved_views_with_it(store, sid):
    store.save_view(sid, "doomed", PAYLOAD)
    store.drop_source(sid)
    n = store.db.execute(
        "SELECT COUNT(*) c FROM saved_views WHERE source_id=?", (sid,)).fetchone()["c"]
    assert n == 0


def test_routes_round_trip(store, monkeypatch, sid):
    import server
    monkeypatch.setattr(server, "STORE", store)
    c = TestClient(server.app)

    res = c.post("/api/saved_views",
                 json={"source_id": sid, "name": "via api", "payload": PAYLOAD},
                 headers=HEADERS)
    assert res.status_code == 200
    vid = res.json()["id"]

    listed = c.get(f"/api/saved_views?source_id={sid}", headers=HEADERS).json()
    assert [v["name"] for v in listed] == ["via api"]
    assert listed[0]["payload"] == PAYLOAD

    assert c.delete(f"/api/saved_views/{vid}", headers=HEADERS).status_code == 200
    assert c.get(f"/api/saved_views?source_id={sid}", headers=HEADERS).json() == []
