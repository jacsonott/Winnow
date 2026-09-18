"""Held views: build_view(hold=True) builds a view that leaves the
source's live view alone, and adopt_view is the step that makes it the
live one. The background search (start_view_job) is built on this — the
grid keeps paging the rows it has while a long search runs, and the
result waits for Apply.

The rules pinned here: a held build evicts nothing the grid is using
(materialised AND virtual-root paths — a cleared search box lands in the
latter, which evicts before it has a handle); adopt evicts the rest and
clears `pending`; a normal build evicts pending views too (the newer
intent wins); a second held build for the same source evicts the first
(one pending per source); a cancelled held build rolls back with every
view intact; merges take the same path; and close() with a job running
cancels it, joins it, and returns."""

from __future__ import annotations

import threading
import time

import pytest

from winnow.store import DEFAULT_TAGS, OpCancelled, Store

# The reliably-slow build test_cancel_op.py uses: catastrophic
# backtracking inside the REGEXP UDF, per row.
SLOW_REGEX = "(a+)+$"


@pytest.fixture
def slow_source(store, write_csv):
    rows = [["Name", "Payload"]]
    rows += [[f"n{i}", "a" * 24 + "X"] for i in range(5000)]
    rec = store.ingest_csv(write_csv(rows), name="slow.csv", build_fts=False)
    return store, rec["id"]


def _slow_spec(token):
    return {
        "filters": [{"column": "Payload", "op": "regex", "value": SLOW_REGEX}],
        "op_token": token,
    }


def _contains(col, value):
    return {"filters": [{"column": col, "op": "contains", "value": value}]}


def _pages(store, view):
    return store.fetch_rows(view["view_id"], 0, 10)["rows"]


def _gone(store, view):
    with pytest.raises(KeyError):
        store.fetch_rows(view["view_id"], 0, 1)


# ------------------------------------------------------------ hold / adopt


def test_held_build_leaves_the_live_view_pageable(ingested):
    store, sid = ingested
    live = store.build_view(sid, _contains("Process", "svchost"))
    held = store.build_view(sid, _contains("Process", "exe"), hold=True)
    assert held["pending"] is True
    assert held["row_count"] == 4
    # The grid's view is untouched — and the held one pages too, so a
    # client that adopts it later finds it exactly as built.
    assert len(_pages(store, live)) == 1
    assert len(_pages(store, held)) == 4
    assert "pending" not in live


def test_held_virtual_root_build_leaves_the_live_view_pageable(ingested):
    """A spec with no filter and no sort takes the virtual-root path,
    which evicts under the lock before minting its handle — the one place
    a hold that only reached the materialised block would still evict."""
    store, sid = ingested
    live = store.build_view(sid, _contains("Process", "svchost"))
    held = store.build_view(sid, {}, hold=True)
    assert held["kind"] == "root_virtual"
    assert held["pending"] is True
    assert len(_pages(store, live)) == 1
    assert len(_pages(store, held)) == 4


def test_adopt_evicts_the_others_and_clears_pending(ingested):
    store, sid = ingested
    live = store.build_view(sid, _contains("Process", "svchost"))
    held = store.build_view(sid, _contains("Process", "exe"), hold=True)
    adopted = store.adopt_view(held["view_id"])
    # The same payload /api/view answers with, for the same view.
    assert adopted["view_id"] == held["view_id"]
    assert adopted["pending"] is False
    assert set(adopted) >= {"view_id", "source_id", "row_count", "kind", "elapsed_ms"}
    _gone(store, live)
    assert len(_pages(store, held)) == 4
    # Adopting a view that is no longer there is the KeyError server.py
    # turns into 409 "expired".
    with pytest.raises(KeyError, match="expired"):
        store.adopt_view(live["view_id"])


def test_adopting_a_virtual_root_held_view(ingested):
    store, sid = ingested
    live = store.build_view(sid, _contains("Process", "svchost"))
    held = store.build_view(sid, {}, hold=True)
    store.adopt_view(held["view_id"])
    _gone(store, live)
    assert store.tag_positions(held["view_id"]) is not None


def test_a_normal_build_evicts_a_pending_view(ingested):
    """Newer intent wins: a filter change while a search is pending makes
    that search's view moot, and the client's adopt then 409s into a
    fresh build rather than installing a view built against filters the
    analyst has since moved on from."""
    store, sid = ingested
    live = store.build_view(sid, _contains("Process", "svchost"))
    held = store.build_view(sid, _contains("Process", "exe"), hold=True)
    newer = store.build_view(sid, _contains("Process", "cmd"))
    _gone(store, live)
    _gone(store, held)
    with pytest.raises(KeyError, match="expired"):
        store.adopt_view(held["view_id"])
    assert len(_pages(store, newer)) == 1


def test_a_second_held_build_evicts_the_first_pending_one(ingested):
    """One pending per source, or every abandoned search would leave a
    table behind in the views db. The live view is still not touched."""
    store, sid = ingested
    live = store.build_view(sid, _contains("Process", "svchost"))
    first = store.build_view(sid, _contains("Process", "exe"), hold=True)
    second = store.build_view(sid, _contains("User", "ACME"), hold=True)
    _gone(store, first)
    assert len(_pages(store, live)) == 1
    assert len(_pages(store, second)) == 3
    assert second["pending"] is True


def test_held_views_are_per_source(store, write_csv):
    """A held build for one table never touches another table's views,
    pending or live."""
    a = store.ingest_csv(write_csv([["X"], ["1"], ["2"]], "a.csv"), name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv([["X"], ["3"], ["4"]], "b.csv"), name="b", build_fts=False)["id"]
    live_a = store.build_view(a, _contains("X", "1"))
    held_a = store.build_view(a, _contains("X", "2"), hold=True)
    held_b = store.build_view(b, _contains("X", "3"), hold=True)
    assert len(_pages(store, live_a)) == 1
    assert len(_pages(store, held_a)) == 1
    assert len(_pages(store, held_b)) == 1
    store.adopt_view(held_b["view_id"])
    assert len(_pages(store, live_a)) == 1
    assert len(_pages(store, held_a)) == 1


def test_cancel_of_a_held_build_evicts_nothing(slow_source):
    """The evict-after-build ordering test_cancel_op.py pins, for the
    held path: the interrupted transaction rolls back, and both the live
    view and an earlier pending view are exactly where they were."""
    store, sid = slow_source
    live = store.build_view(sid, _contains("Name", "n1"))
    pending = store.build_view(sid, _contains("Name", "n2"), hold=True)
    result = {}

    def build():
        try:
            result["view"] = store.build_view(sid, _slow_spec("tok-hold"), hold=True)
        except BaseException as e:  # noqa: BLE001 — recorded for the main thread to assert on
            result["error"] = e

    t = threading.Thread(target=build)
    t.start()
    time.sleep(0.4)  # let the INSERT get going
    assert store.cancel_op("tok-hold") is True
    t.join(30)
    assert not t.is_alive(), "interrupt never landed — build still running"
    assert isinstance(result.get("error"), OpCancelled)
    assert _pages(store, live)
    assert _pages(store, pending)
    assert store.adopt_view(pending["view_id"])["pending"] is False


# ------------------------------------------------------------------ merges


A_ROWS = [["When", "Msg"], ["2024-01-01 10:00", "alpha beacon"], ["2024-01-01 11:00", "beta"]]
B_ROWS = [["When", "Msg"], ["2024-01-02 10:00", "gamma beacon"], ["2024-01-02 11:00", "delta"]]


def test_hold_and_adopt_on_a_merge(store, write_csv):
    """Invariant #9: the held path is the same code for a merge (a
    negative source_id), whose views always materialise."""
    a = store.ingest_csv(write_csv(A_ROWS, "a.csv"), name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv(B_ROWS, "b.csv"), name="b", build_fts=False)["id"]
    mid = store.create_merge("both", [a, b])["id"]
    live = store.build_view(mid, {})
    assert live["row_count"] == 4
    held = store.build_view(mid, {"search": "beacon"}, hold=True)
    assert held["pending"] is True
    assert held["row_count"] == 2
    rows = _pages(store, live)
    assert len(rows) == 4
    assert {r["source_id"] for r in _pages(store, held)} == {a, b}
    adopted = store.adopt_view(held["view_id"])
    assert adopted["pending"] is False
    _gone(store, live)
    assert len(_pages(store, held)) == 2
    # A normal merge build still evicts a pending one.
    held2 = store.build_view(mid, {"search": "delta"}, hold=True)
    store.build_view(mid, {})
    _gone(store, held2)


# ---------------------------------------------------------------- view jobs


def test_start_view_job_answers_inline_when_the_build_is_fast(ingested):
    store, sid = ingested
    job = store.start_view_job(sid, _contains("Process", "svchost"))
    assert job["status"] == "done"
    assert job["view"]["pending"] is True
    assert job["view"]["row_count"] == 1
    assert job["error"] is None
    assert isinstance(job["elapsed_ms"], int)
    assert store.get_view_job(job["job_id"])["status"] == "done"
    assert store.running_view_jobs() == 0


def test_a_running_view_job_is_cancellable_and_evicts_nothing(slow_source):
    store, sid = slow_source
    live = store.build_view(sid, _contains("Name", "n1"))
    job = store.start_view_job(sid, _slow_spec(None), wait_ms=50)
    assert job["status"] == "running"
    assert store.running_view_jobs() == 1
    assert store.cancel_view_job(job["job_id"]) is True
    done = store.wait_for_view_job(job["job_id"], timeout=30)
    assert done["status"] == "cancelled"
    assert done["view"] is None
    assert store.running_view_jobs() == 0
    assert _pages(store, live)
    # Cancelling it again, or an id that never existed, is a no-op.
    assert store.cancel_view_job(job["job_id"]) is False
    assert store.cancel_view_job(job["job_id"] + 999) is False


def test_starting_a_second_job_for_the_source_supersedes_the_first(slow_source):
    store, sid = slow_source
    first = store.start_view_job(sid, _slow_spec("tok-first"), wait_ms=50)
    assert first["status"] == "running"
    second = store.start_view_job(sid, _contains("Name", "n2"))
    assert second["job_id"] != first["job_id"]
    assert second["status"] == "done"
    # Only the newest job is addressable — a poller on the old id gets
    # None (404 over HTTP) — and the old build was cancelled with it.
    assert store.get_view_job(first["job_id"]) is None
    assert store.wait_for_view_job(first["job_id"], timeout=1) is None
    assert store.cancel_op("tok-first") is False   # nothing left running under it
    assert store.running_view_jobs() == 0


def test_discarding_a_finished_job_drops_its_pending_view(ingested):
    store, sid = ingested
    live = store.build_view(sid, _contains("Process", "svchost"))
    job = store.start_view_job(sid, _contains("Process", "exe"))
    assert job["status"] == "done"
    assert store.cancel_view_job(job["job_id"]) is True
    _gone(store, job["view"])
    assert store.get_view_job(job["job_id"])["status"] == "cancelled"
    assert _pages(store, live)
    with pytest.raises(KeyError, match="expired"):
        store.adopt_view(job["view"]["view_id"])


def test_a_bad_filter_lands_in_the_job_record_as_a_400(ingested):
    """A tree condition on a column the table lacks is the strict path
    (UnknownFilterColumn, a ValueError — api_view's 400); a quick filter
    on one is dropped by design."""
    store, sid = ingested
    job = store.start_view_job(sid, {"filter_tree": {"type": "group", "op": "AND", "children": [
        {"type": "cond", "column": "NoSuchColumn", "op": "contains", "value": "x"}]}})
    job = store.wait_for_view_job(job["job_id"], timeout=10)
    assert job["status"] == "error"
    assert job["error_status"] == 400
    assert "NoSuchColumn" in job["error"]
    assert job["view"] is None


def test_close_with_a_running_view_job_cancels_it_and_returns(case_path, write_csv):
    """A worker queued on the writer lock at close time must not take it
    after the connection is gone: close() cancels the build through its
    token, joins the thread, and only then closes the database."""
    s = Store(case_path, default_tags=DEFAULT_TAGS)
    rows = [["Name", "Payload"]] + [[f"n{i}", "a" * 24 + "X"] for i in range(5000)]
    sid = s.ingest_csv(write_csv(rows), name="slow.csv", build_fts=False)["id"]
    job = s.start_view_job(sid, _slow_spec(None), wait_ms=50)
    assert job["status"] == "running"
    t0 = time.time()
    s.close()   # must cancel the job and return, not hang or crash
    assert time.time() - t0 < 30
    assert s.closed is True
    assert s.get_view_job(job["job_id"])["status"] == "cancelled"
    assert s.running_view_jobs() == 0
