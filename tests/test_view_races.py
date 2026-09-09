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
