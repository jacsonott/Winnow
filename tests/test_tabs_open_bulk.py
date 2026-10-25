"""Opening several tabs in one call.

The counterpart of close_all_tabs, and there for the same reason: the
sidebar's "open all" on a directory import of thirty files was thirty
round trips, each taking the writer lock in turn. Which tables belong in
the list is the caller's decision — "every table" and "every table with
tagged rows" differ over merges — so this is about the opening, not the
choosing.
"""

from __future__ import annotations

import pytest

from winnow.store import Store


def _csv(tmp_path, name, rows=3):
    p = tmp_path / name
    p.write_text("A,B\n" + "".join(f"{i},x\n" for i in range(rows)), encoding="utf-8")
    return p


def _open_ids(store):
    with store.lock:
        return sorted(r[0] for r in store.db.execute("SELECT source_id FROM open_tabs"))


def test_opens_several_at_once_and_counts_only_the_ones_it_changed(tmp_path):
    store = Store(str(tmp_path / "case.db"))
    try:
        ids = [store.ingest_csv(str(_csv(tmp_path, f"t{i}.csv")))["id"] for i in range(3)]
        store.close_all_tabs()
        assert _open_ids(store) == []

        assert store.open_tabs(ids) == 3
        assert _open_ids(store) == sorted(ids)
        # Already open is not an error and is not counted — "open all" run
        # twice reports nothing to do the second time.
        assert store.open_tabs(ids) == 0
        assert _open_ids(store) == sorted(ids)
    finally:
        store.close()


def test_a_merge_opens_by_its_negative_id(tmp_path):
    store = Store(str(tmp_path / "case.db"))
    try:
        a = store.ingest_csv(str(_csv(tmp_path, "a.csv")))["id"]
        b = store.ingest_csv(str(_csv(tmp_path, "b.csv")))["id"]
        merge = store.create_merge("both", [a, b])
        store.close_all_tabs()
        assert store.open_tabs([merge["id"]]) == 1
        assert _open_ids(store) == [merge["id"]]
    finally:
        store.close()


def test_an_id_that_names_no_table_opens_nothing_at_all(tmp_path):
    """All-or-nothing: a bad id in the list must not leave half the tabs
    open for the analyst to make sense of."""
    store = Store(str(tmp_path / "case.db"))
    try:
        good = store.ingest_csv(str(_csv(tmp_path, "a.csv")))["id"]
        store.close_all_tabs()
        with pytest.raises(KeyError):
            store.open_tabs([good, 9999])
        assert _open_ids(store) == []
    finally:
        store.close()


def test_an_empty_list_is_a_no_op(tmp_path):
    store = Store(str(tmp_path / "case.db"))
    try:
        store.ingest_csv(str(_csv(tmp_path, "a.csv")))
        before = _open_ids(store)
        assert store.open_tabs([]) == 0
        assert _open_ids(store) == before
    finally:
        store.close()


def test_the_route_answers_400_for_an_id_that_names_no_table(client, store, tmp_path):
    """A typo'd id is the analyst's problem to see, not a tab for nothing
    — and the store raises KeyError, which without the mapping would be a
    500 (server.md's 400-vs-500 rule)."""
    store.ingest_csv(str(_csv(tmp_path, "route.csv")))
    r = client.post("/api/tabs/open", json={"source_ids": [9999]})
    assert r.status_code == 400
    assert "9999" in r.json()["detail"]
