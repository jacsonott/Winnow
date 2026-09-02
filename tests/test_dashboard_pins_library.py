"""Dashboards promoted into the page strip (a pinned flag in the case file,
migrated onto older case files) and the machine-wide dashboard library
(workspace/dashboards.json): save/upsert/delete, and add-to-case."""

from __future__ import annotations

import sqlite3

import pytest

from winnow import workspace as WS
from winnow.store import DEFAULT_TAGS, Store


def test_pin_flag_round_trips(client, store):
    d = store.create_dashboard("Overview")
    assert store.list_dashboards()[0]["pinned"] is False
    r = client.post(f"/api/dashboards/{d['id']}", json={"pinned": True})
    assert r.status_code == 200
    assert client.get("/api/dashboards").json()[0]["pinned"] is True
    client.post(f"/api/dashboards/{d['id']}", json={"pinned": False})
    assert client.get("/api/dashboards").json()[0]["pinned"] is False
    assert client.post("/api/dashboards/99999", json={"pinned": True}).status_code == 404


def test_older_case_files_gain_the_pinned_column(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE dashboards (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                                 widgets TEXT NOT NULL DEFAULT '[]', pos INTEGER NOT NULL DEFAULT 0);
        INSERT INTO dashboards(name, widgets, pos) VALUES ('Legacy', '[]', 0);
    """)
    db.commit(); db.close()
    st = Store(str(path), default_tags=DEFAULT_TAGS)
    try:
        boards = st.list_dashboards()
        assert boards[0]["name"] == "Legacy" and boards[0]["pinned"] is False
        st.set_dashboard_pinned(boards[0]["id"], True)
        assert st.list_dashboards()[0]["pinned"] is True
    finally:
        st.close()


def test_library_save_upsert_delete(client):
    r = client.post("/api/dashboard_library", json={"name": "Host overview", "widgets": [{"title": "a"}]})
    assert r.status_code == 200, r.text
    bid = r.json()["id"]
    assert r.json()["widget_count"] == 1
    # Upsert by name (case-insensitive) — no duplicate, widgets replaced.
    r2 = client.post("/api/dashboard_library", json={"name": "host overview", "widgets": [{"title": "a"}, {"title": "b"}]})
    assert r2.json()["id"] == bid and r2.json()["widget_count"] == 2
    assert len(client.get("/api/dashboard_library").json()) == 1
    assert client.post("/api/dashboard_library", json={"name": "  ", "widgets": []}).status_code == 400
    client.delete(f"/api/dashboard_library/{bid}")
    assert client.get("/api/dashboard_library").json() == []


def test_library_board_adds_into_the_open_case_by_name(client, store):
    bid = client.post("/api/dashboard_library",
                      json={"name": "Triage", "widgets": [{"title": "w", "source": "tags", "render": "stat"}]}).json()["id"]
    r = client.post(f"/api/dashboard_library/{bid}/add", json={})
    assert r.status_code == 200, r.text
    boards = store.list_dashboards()
    assert [b["name"] for b in boards] == ["Triage"] and boards[0]["widget_count"] == 1
    # Adding it again refreshes the same board rather than duplicating.
    client.post(f"/api/dashboard_library/{bid}/add", json={})
    assert len(store.list_dashboards()) == 1
    # A custom name lands a second copy under that name.
    client.post(f"/api/dashboard_library/{bid}/add", json={"name": "Triage (copy)"})
    assert sorted(b["name"] for b in store.list_dashboards()) == ["Triage", "Triage (copy)"]
    assert client.post("/api/dashboard_library/424242/add", json={}).status_code == 404
