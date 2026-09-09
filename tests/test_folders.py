"""Sidebar folders — the per-case tree an analyst sorts tables into.

Folders are organizational metadata in the case file (Store's folder
methods + the source_folders / source_folder_map tables). The load-bearing
guarantee is that they never touch evidence: deleting a folder reparents
its tables and drops no source (invariant #1)."""

from __future__ import annotations

import pytest


def _mk(store, write_csv, name):
    return store.ingest_csv(write_csv([["A", "B"], ["1", "2"]], name), name=name, build_fts=False)


def test_create_nest_and_list(store):
    a = store.create_folder("RegistryHives")
    b = store.create_folder("Users", a["id"])
    c = store.create_folder("EventLogs")
    got = {f["name"]: f for f in store.list_folders()}
    assert got["RegistryHives"]["parent_id"] is None
    assert got["Users"]["parent_id"] == a["id"]
    # siblings at the root get increasing pos in creation order
    assert got["EventLogs"]["pos"] > got["RegistryHives"]["pos"]
    assert c["id"] != a["id"] != b["id"]


def test_create_rejects_blank_and_too_long(store):
    with pytest.raises(ValueError):
        store.create_folder("   ")
    with pytest.raises(ValueError):
        store.create_folder("x" * 201)


def test_rename(store):
    a = store.create_folder("old")
    store.rename_folder(a["id"], "new")
    assert store.list_folders()[0]["name"] == "new"
    with pytest.raises(KeyError):
        store.rename_folder(9999, "x")


def test_reorder_siblings(store):
    a = store.create_folder("a")
    b = store.create_folder("b")
    c = store.create_folder("c")
    store.reorder_folders(None, [c["id"], a["id"], b["id"]])
    order = [f["name"] for f in sorted(store.list_folders(), key=lambda f: f["pos"])]
    assert order == ["c", "a", "b"]


def test_move_and_cycle_guard(store):
    a = store.create_folder("a")
    b = store.create_folder("b", a["id"])
    d = store.create_folder("d")
    # d can move under a
    store.move_folder(d["id"], a["id"])
    assert {f["id"]: f["parent_id"] for f in store.list_folders()}[d["id"]] == a["id"]
    # a cannot move under its own descendant b
    with pytest.raises(ValueError):
        store.move_folder(a["id"], b["id"])
    # nor under itself
    with pytest.raises(ValueError):
        store.move_folder(a["id"], a["id"])


def test_set_source_folder_shows_in_list_sources(store, write_csv):
    a = store.create_folder("Logs")
    r = _mk(store, write_csv, "evtx.csv")
    store.set_source_folder(r["id"], a["id"])
    src = next(s for s in store.list_sources() if s["id"] == r["id"])
    assert src["folder_id"] == a["id"]
    # None puts it back at the root (no map row)
    store.set_source_folder(r["id"], None)
    src = next(s for s in store.list_sources() if s["id"] == r["id"])
    assert src["folder_id"] is None


def test_set_source_folder_rejects_missing_folder(store, write_csv):
    r = _mk(store, write_csv, "evtx.csv")
    with pytest.raises(ValueError):
        store.set_source_folder(r["id"], 4242)


def test_merge_can_be_foldered(store, write_csv):
    a = _mk(store, write_csv, "a.csv")
    b = _mk(store, write_csv, "b.csv")
    m = store.create_merge("both", [a["id"], b["id"]])
    fld = store.create_folder("Merged")
    store.set_source_folder(m["id"], fld["id"])   # signed (negative) id
    merged = next(s for s in store.list_merges() if s["id"] == m["id"])
    assert merged["folder_id"] == fld["id"]


def test_ensure_folder_path_is_nested_and_idempotent(store):
    leaf = store.ensure_folder_path("RegistryHives/Users")
    again = store.ensure_folder_path("RegistryHives/Users")
    assert leaf == again                                   # no duplicate branch
    assert sum(f["name"] == "Users" for f in store.list_folders()) == 1
    deeper = store.ensure_folder_path("RegistryHives/Users/NTUSER")
    assert deeper != leaf
    # the chain reads back the way it was written
    by_id = {f["id"]: f for f in store.list_folders()}
    chain = []
    cur = deeper
    while cur is not None:
        chain.append(by_id[cur]["name"])
        cur = by_id[cur]["parent_id"]
    assert list(reversed(chain)) == ["RegistryHives", "Users", "NTUSER"]


def test_ensure_folder_path_empty_leaves_root(store):
    assert store.ensure_folder_path("") is None
    assert store.ensure_folder_path("   ") is None
    assert store.list_folders() == []


def test_delete_reparents_children_and_tables_and_keeps_sources(store, write_csv):
    """The one that matters: deleting a folder moves its subfolders and
    tables up to its parent and deletes NO evidence."""
    parent = store.create_folder("Triage")
    mid = store.create_folder("Registry", parent["id"])
    child = store.create_folder("Users", mid["id"])
    r = _mk(store, write_csv, "system.csv")
    store.set_source_folder(r["id"], mid["id"])

    store.delete_folder(mid["id"])

    folders = {f["id"]: f for f in store.list_folders()}
    assert mid["id"] not in folders                       # the folder is gone
    assert folders[child["id"]]["parent_id"] == parent["id"]   # child reparented up
    src = next(s for s in store.list_sources() if s["id"] == r["id"])
    assert src is not None                                # source survived
    assert src["folder_id"] == parent["id"]               # and moved to the parent


def test_delete_root_folder_sends_tables_to_root(store, write_csv):
    top = store.create_folder("Top")
    r = _mk(store, write_csv, "t.csv")
    store.set_source_folder(r["id"], top["id"])
    store.delete_folder(top["id"])
    src = next(s for s in store.list_sources() if s["id"] == r["id"])
    assert src["folder_id"] is None                       # no parent → back to root
