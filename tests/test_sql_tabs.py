"""The SQL pane's named sub-tabs — a per-case sidecar table (sql_tabs), so a
worked-out query survives a restart and travels with the case file. Covers
Store CRUD/ordering plus the /api/sql_tabs routes."""

from __future__ import annotations

import pytest

from winnow import store as store_module


def test_sql_tabs_start_empty(store):
    assert store.list_sql_tabs() == []


def test_create_returns_record_and_lists_in_creation_order(store):
    a = store.create_sql_tab("logons", "SELECT 1")
    b = store.create_sql_tab("services")
    assert a["name"] == "logons"
    assert a["sql"] == "SELECT 1"
    assert b["sql"] == ""            # sql defaults to empty, not NULL
    assert b["pos"] > a["pos"]
    assert [t["name"] for t in store.list_sql_tabs()] == ["logons", "services"]


def test_update_is_partial(store):
    t = store.create_sql_tab("scratch", "SELECT 1")

    # sql-only (the editor's debounced autosave) leaves the name alone.
    edited = store.update_sql_tab(t["id"], sql="SELECT 2")
    assert edited["sql"] == "SELECT 2"
    assert edited["name"] == "scratch"

    # name-only (rename) leaves the query alone.
    renamed = store.update_sql_tab(t["id"], name="4624s")
    assert renamed["name"] == "4624s"
    assert renamed["sql"] == "SELECT 2"

    # Both persisted, not just returned.
    stored = store.list_sql_tabs()[0]
    assert (stored["name"], stored["sql"]) == ("4624s", "SELECT 2")


def test_update_unknown_id_raises(store):
    with pytest.raises(KeyError):
        store.update_sql_tab(9999, name="nope")


def test_delete_removes_only_that_tab(store):
    a = store.create_sql_tab("a")
    b = store.create_sql_tab("b")
    store.delete_sql_tab(a["id"])
    assert [t["id"] for t in store.list_sql_tabs()] == [b["id"]]


def test_reorder_renumbers_pos(store):
    a = store.create_sql_tab("a")
    b = store.create_sql_tab("b")
    c = store.create_sql_tab("c")

    out = store.reorder_sql_tabs([c["id"], a["id"], b["id"]])
    assert [t["name"] for t in out] == ["c", "a", "b"]
    # Persisted, not just reflected in the return value.
    assert [t["name"] for t in store.list_sql_tabs()] == ["c", "a", "b"]


def test_reorder_ignores_unknown_ids_and_keeps_unlisted_tabs(store):
    a = store.create_sql_tab("a")
    b = store.create_sql_tab("b")
    c = store.create_sql_tab("c")

    # 9999 doesn't exist and `b` isn't listed — the listed run comes first,
    # anything unlisted keeps its relative order after it.
    out = store.reorder_sql_tabs([9999, c["id"], a["id"]])
    assert [t["name"] for t in out] == ["c", "a", "b"]
    assert b["id"] in {t["id"] for t in out}


def test_tabs_survive_reopening_the_case_file(tmp_path):
    path = str(tmp_path / "case.db")
    s1 = store_module.Store(path)
    s1.create_sql_tab("logons", "SELECT * FROM src_1")
    s1.close()

    s2 = store_module.Store(path)
    try:
        tabs = s2.list_sql_tabs()
        assert [(t["name"], t["sql"]) for t in tabs] == [("logons", "SELECT * FROM src_1")]
    finally:
        s2.close()


# --------------------------------------------------------------- HTTP routes

def test_sql_tab_routes_crud(client):
    assert client.get("/api/sql_tabs").json() == []

    created = client.post("/api/sql_tabs", json={"name": "Query 1", "sql": "SELECT 1"}).json()
    assert created["name"] == "Query 1"
    assert client.get("/api/sql_tabs").json() == [created]

    renamed = client.put(f"/api/sql_tabs/{created['id']}", json={"name": "logons"}).json()
    assert renamed["name"] == "logons"
    assert renamed["sql"] == "SELECT 1"  # untouched by a name-only PUT

    r = client.delete(f"/api/sql_tabs/{created['id']}")
    assert r.status_code == 200
    assert client.get("/api/sql_tabs").json() == []


def test_sql_tab_update_route_404s_unknown_id(client):
    r = client.put("/api/sql_tabs/9999", json={"name": "nope"})
    assert r.status_code == 404


def test_sql_tab_reorder_route(client):
    a = client.post("/api/sql_tabs", json={"name": "a"}).json()
    b = client.post("/api/sql_tabs", json={"name": "b"}).json()
    out = client.post("/api/sql_tabs/reorder", json={"ids": [b["id"], a["id"]]}).json()
    assert [t["name"] for t in out] == ["b", "a"]
