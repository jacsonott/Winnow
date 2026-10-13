"""Requests that lose their view to a concurrent rebuild.

A view is evicted when the same source rebuilds one (`_evict_root_views`).
Anything holding a handle it read *before* taking the writer lock can find
the table gone by the time it queries — and the contract for that is a
KeyError, which server.py turns into the 409 the frontend rebuilds on.
An OperationalError instead becomes a 500, and the operation is lost
rather than retried.

Driven by evicting between the handle read and the call, which is what a
concurrent rebuild does, rather than by racing threads — a race that
usually loses is not a test.
"""

from __future__ import annotations

import threading

import pytest


def _case(store, write_csv, rows=8):
    body = [["When", "Host"]]
    for i in range(rows):
        body.append([f"2026-03-14 08:0{i}:00", f"H{i % 2}"])
    return store.ingest_csv(write_csv(body, "e.csv"), name="e", build_fts=False)["id"]


def test_tagging_a_whole_view_whose_table_vanished_reports_it_expired(store, write_csv):
    sid = _case(store, write_csv)
    tag = store.upsert_tag(None, "TA", "#ff0000", None)["id"]
    v = store.build_view(sid, {"source_id": sid, "filters": [],
                               "sort": [{"column": "Host", "dir": "asc"}]})
    # What a concurrent rebuild of this source does to it.
    with store.lock:
        store.db.execute(f'DROP TABLE IF EXISTS v."{v["view_id"]}"')
    with pytest.raises(KeyError):
        store.tag_view(v["view_id"], tag, True)


def test_expanding_a_group_whose_table_vanished_reports_it_expired(store, write_csv):
    sid = _case(store, write_csv)
    root = store.build_view(sid, {"source_id": sid, "filters": [],
                                  "sort": [{"column": "Host", "dir": "asc"}]})
    with store.lock:
        store.db.execute(f'DROP TABLE IF EXISTS v."{root["view_id"]}"')
    with pytest.raises(KeyError):
        store.expand_group(root["view_id"], "Host", "H0")


def test_view_ids_are_unique_under_concurrent_builds(store, write_csv):
    """Six call sites used to do `self._view_seq += 1` themselves, outside
    any lock — load/add/store, so two builds arriving together could mint
    the same id, and then one CREATE TABLE raises "already exists" (a 500)
    or the loser wins the handle dict and its row_count describes a table
    it does not own. This pins the one allocator they now share: it is the
    only minting path, and calling it concurrently cannot repeat."""
    _case(store, write_csv)
    ids: list[str] = []
    lock = threading.Lock()

    def mint():
        got = [store._next_view_id() for _ in range(200)]
        with lock:
            ids.extend(got)

    threads = [threading.Thread(target=mint) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ids) == len(set(ids)) == 1600


# ------------------------------------------- a source that isn't readable yet

def test_a_source_still_importing_says_so_instead_of_500ing(store, write_csv):
    """Every ingest path writes the `sources` row with `columns='[]'` and
    fills it in at the end, while `open_tabs` is written straight away — so
    a directory import gives the tab strip tabs for files still importing,
    and a kill -9 leaves one that way permanently. Reading it built
    `SELECT rid,  FROM "src_5"`, whose OperationalError is not "no such
    table" and so reached the route as a 500."""
    sid = _case(store, write_csv)
    v = store.build_view(sid, {"source_id": sid, "filters": [], "sort": []})
    with store.lock, store.db:
        store.db.execute("UPDATE sources SET columns='[]' WHERE id=?", (sid,))

    with pytest.raises(ValueError, match="still importing"):
        store.fetch_rows(v["view_id"], 0, 10)
    with pytest.raises(ValueError, match="still importing"):
        store.column_max_lengths(sid)
    # Eagerly, not from inside the streaming generator — a route's
    # try/except cannot catch what the response body raises later.
    with pytest.raises(ValueError, match="still importing"):
        store.export_view_csv(v["view_id"])


# ------------------------------- a handle registered after the lock is gone
#
# build_view and expand_group used to put the new handle into `_views`
# AFTER releasing the writer lock. A rebuild of the same source that took
# the lock in that gap could not see the new view, so could not evict it:
# two live root views for one source, a `v.view_N` nothing would ever drop,
# a group whose parent was gone, and an older held search that adopt_view
# would install over the newer build instead of answering 409. Driven
# deterministically: the lock below runs the competing rebuild on another
# thread at the exact moment the build under test lets go of the lock.


class _RunOnRelease:
    """Store.lock, plus: after `arm(fn)`, the next time the thread that
    armed it fully releases the lock, run `fn` on another thread and wait
    for it — a competing build taking the writer lock in the gap after
    release. Only that thread's release counts: a background index or FTS
    build passes through this lock too, and firing on its release ran
    `fn` concurrently with the assertions instead of before them."""

    def __init__(self, inner):
        self.inner = inner
        self.fn = None
        self.owner = None

    def arm(self, fn):
        self.owner = threading.get_ident()
        self.fn = fn

    def acquire(self, *a, **k):
        return self.inner.acquire(*a, **k)

    def release(self):
        self.inner.release()
        if (self.fn is not None and threading.get_ident() == self.owner
                and not self.inner._is_owned()):
            fn, self.fn = self.fn, None
            t = threading.Thread(target=fn)
            t.start()
            t.join()

    def __enter__(self):
        self.inner.acquire()
        return self

    def __exit__(self, *exc):
        self.release()

    def _is_owned(self):
        return self.inner._is_owned()


def _v_tables(store):
    with store._reader() as ro:
        return {r[0] for r in ro.execute(
            "SELECT name FROM v.sqlite_master WHERE type='table' AND name LIKE 'view_%'")}


def _arm_after_next_mint(store, monkeypatch, fn):
    """Arm the hook the moment the build under test mints its view id, so
    it fires at the end of that build's locked block."""
    mint = store._next_view_id

    def minting():
        vid = mint()
        monkeypatch.setattr(store, "_next_view_id", mint)
        store.lock.arm(fn)
        return vid

    monkeypatch.setattr(store, "_next_view_id", minting)


def test_a_held_search_overtaken_by_a_rebuild_cannot_be_adopted(store, write_csv, monkeypatch):
    sid = _case(store, write_csv, rows=8)
    store.build_view(sid, {})
    store.lock = _RunOnRelease(store.lock)
    newer = {}
    _arm_after_next_mint(store, monkeypatch, lambda: newer.setdefault(
        "v", store.build_view(sid, {"filters": [{"column": "Host", "op": "equals", "value": "H1"}]})))

    held = store.build_view(sid, {"search": "H0"}, hold=True)

    assert newer["v"]["view_id"] in store._views
    assert held["view_id"] not in store._views          # the newer intent evicted it
    with pytest.raises(KeyError):                       # → the 409 "expired" the client re-runs on
        store.adopt_view(held["view_id"])
    assert _v_tables(store) == {newer["v"]["view_id"]}  # and its table is gone, not orphaned


def test_a_materialised_group_is_dropped_with_a_parent_rebuilt_under_it(store, write_csv, monkeypatch):
    sid = _case(store, write_csv, rows=8)
    root = store.build_view(sid, {"sort": [{"column": "When", "dir": "desc"}]})
    monkeypatch.setattr(store, "GROUP_MATERIALIZE_THRESHOLD", 1)
    store.lock = _RunOnRelease(store.lock)
    rebuilt = {}
    _arm_after_next_mint(store, monkeypatch, lambda: rebuilt.setdefault(
        "v", store.build_view(sid, {"sort": [{"column": "When", "dir": "asc"}]})))

    group = store.expand_group(root["view_id"], "Host", "H0")

    assert root["view_id"] not in store._views
    assert group["view_id"] not in store._views         # cascaded with its parent
    assert _v_tables(store) == {rebuilt["v"]["view_id"]}


def test_a_virtual_group_is_not_registered_under_a_parent_already_gone(store, write_csv, monkeypatch):
    sid = _case(store, write_csv, rows=8)
    root = store.build_view(sid, {})                    # root_virtual: the virtual group path
    assert root["kind"] == "root_virtual"
    store.lock = _RunOnRelease(store.lock)
    resolve = store._resolve_members

    def resolving(source_id):
        # armed at the top of expand_group: fires when its count block
        # lets go of the lock, before the group is registered
        store.lock.arm(lambda: store.build_view(sid, {"filters": [
            {"column": "Host", "op": "equals", "value": "H1"}]}))
        monkeypatch.setattr(store, "_resolve_members", resolve)
        return resolve(source_id)

    monkeypatch.setattr(store, "_resolve_members", resolving)
    with pytest.raises(KeyError):
        store.expand_group(root["view_id"], "Host", "H0")
    assert not [h for h in store._views.values() if h.get("parent_view_id") == root["view_id"]]
