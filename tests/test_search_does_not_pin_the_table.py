"""Starting a search on one table must not pin the analyst to it.

`build_view` holds the writer lock for the whole INSERT..SELECT — right
for materialising 1.2M rows, and the reason a four-minute search used to
take the rest of the app with it. Clicking another tab ran into it twice:

- Every read the switch makes — the source list, the merges, the folders,
  the layout, the tags and their counts, the derived definitions — was
  still on the writer connection under `self.lock`, so the click blocked
  on the FIRST await, before a view was even asked for. Invariant #4 says
  pure reads belong on the pool; these are the ones that never moved.
- Then the new table's own build queued behind the search, so the grid
  said "Filtering…" for whatever was left of it.

The first half is asserted structurally, the way `test_concurrency.py`
does it: hold the writer lock for the entire duration of the read and
assert the read completes. That is a deterministic deadlock against the
old implementation rather than a race that might win.
"""

from __future__ import annotations

import threading
import time

import pytest

from winnow.store import OpPreempted, Store


@pytest.fixture
def two_tables(tmp_path):
    store = Store(str(tmp_path / "case.db"))
    try:
        srcs = []
        for name in ("a.csv", "b.csv"):
            p = tmp_path / name
            rows = ["Host,EventId,Payload"]
            rows += [f"H{i % 4},{4624 + (i % 3)},payload-{i}" for i in range(200)]
            p.write_text("\n".join(rows) + "\n", encoding="utf-8")
            srcs.append(store.ingest_csv(str(p), build_fts=False))
        store.upsert_tag(None, "Reviewed", "#ff0000", "1")
        yield store, srcs
    finally:
        store.close()


def test_every_read_a_table_switch_makes_runs_while_a_build_holds_the_lock(two_tables):
    """The lock is held for the whole of this. Anything still on the
    writer connection deadlocks here rather than being slow — which is
    the point: "slow" is what the analyst reported, and a test that races
    a real build would pass against the old code whenever it won."""
    store, (a, b) = two_tables
    store.set_tags(a["id"], [1, 2], store.list_tags()[0]["id"], True)
    merge = store.create_merge("both", [a["id"], b["id"]])

    done = []
    with store.lock:
        def switch():
            # openSource's own sequence, minus the view build: the source
            # list and the tab strip, the folder tree, the table's layout,
            # its derived columns, the tag defs and their counts.
            store.list_sources()
            store.list_merges()
            store.list_folders()
            store.get_source(b["id"])
            store.get_layout(b["id"])
            store.list_derived_columns(b["id"])
            store.list_tags()
            store.tag_counts(b["id"])
            store.tag_counts(merge["id"])      # a merge resolves its members too
            store.get_case_settings()
            store.list_sql_tabs()
            done.append(True)

        t = threading.Thread(target=switch, daemon=True)
        t.start()
        t.join(10)
    assert done, "a read the table switch makes is still waiting on the writer lock"


def test_the_counts_are_the_same_answers_off_the_writer(two_tables):
    """Moving a read to the pool must not change what it says — a pooled
    reader sees committed data, and every one of these is called outside a
    transaction."""
    store, (a, b) = two_tables
    tag = store.list_tags()[0]["id"]
    store.set_tags(a["id"], [1, 2, 3], tag, True)
    assert store.tag_counts(a["id"])["counts"] == {str(tag): 3}
    assert store.tag_counts(b["id"])["counts"] == {}
    assert [s["id"] for s in store.list_sources()] == [a["id"], b["id"]]
    assert store.get_source(a["id"])["tagged_row_count"] == 3
    # …including a write's own effect, immediately afterwards.
    store.set_tags(a["id"], [4], tag, True)
    assert store.tag_counts(a["id"])["counts"] == {str(tag): 4}


# ----------------------------------------------------------- the build queue

# "A build holding the writer lock" is a state these tests control rather
# than a slow query they hope is still running: the REGEXP function is
# replaced on the store's own connection with one that blocks on an event
# for its first row. The build is then provably holding the lock, and is
# released exactly when the test says so — which is also how the real
# thing behaves, since SQLite only notices an interrupt once the Python
# function it is inside returns. (A genuinely slow regex works too, and
# was tried first: it leaves a thread burning a core for minutes after the
# assertions are done, which on a 2-CPU box makes every test after it
# flaky.)


def _gate_the_build(store):
    gate = threading.Event()
    seen = []

    def gated(pattern, value):
        seen.append(1)
        if len(seen) == 1:
            gate.wait(10)
        return False

    store.db.create_function("REGEXP", 2, gated, deterministic=True)
    return gate, seen


def _until(pred, what, timeout=10):
    deadline = time.time() + timeout
    while not pred():
        assert time.time() < deadline, what
        time.sleep(0.01)


GATED = {"filters": [{"column": "Payload", "op": "regex", "value": "x"}], "sort": []}


def _gated_spec(token):
    return {**GATED, "op_token": token}


def test_a_build_for_another_table_takes_the_lock_off_a_long_one(two_tables):
    """The analyst clicked another tab. That table's build goes first; the
    long one rolls back whole (a held build evicts nothing, so nothing on
    screen depended on it) and starts again afterwards."""
    store, (a, b) = two_tables
    gate, seen = _gate_the_build(store)
    slow = store.start_view_job(a["id"], _gated_spec("slow-1"), wait_ms=0)
    _until(lambda: seen, "the first build never reached the table")

    quick = store.start_view_job(b["id"], {"filters": [], "sort": [
        {"column": "EventId", "dir": "asc"}]}, wait_ms=0)
    # It is queued on the lock and has asked the holder to stand aside.
    _until(lambda: store._build_running is None
           or "slow-1" in store._op_preempted
           or store._build_yields.get("slow-1", 0) > 0,
           "the waiting build never asked the holder to stand aside")
    gate.set()   # the interrupt lands the moment the function returns

    _until(lambda: (store.get_view_job(quick["job_id"]) or {}).get("status") == "done",
           "the second table never got the writer lock")
    # …and the first search was not reported as cancelled to anybody: it is
    # running again, or has since finished.
    again = store.get_view_job(slow["job_id"])
    assert again and again["status"] in ("running", "done"), again
    store.cancel_view_job(slow["job_id"])


def test_a_build_only_yields_to_another_table(two_tables):
    """Two builds for the SAME table are the ordinary supersede — the
    newer spec replaces the older one and the older one is cancelled, not
    restarted. Restarting it would run a spec nobody is waiting for."""
    store, (a, b) = two_tables
    gate, seen = _gate_the_build(store)
    first = store.start_view_job(a["id"], _gated_spec("same-1"), wait_ms=0)
    _until(lambda: seen, "the first build never reached the table")
    second = store.start_view_job(a["id"], {"filters": [], "sort": [
        {"column": "EventId", "dir": "asc"}]}, wait_ms=0)
    gate.set()
    _until(lambda: (store.get_view_job(second["job_id"]) or {}).get("status") == "done",
           "the superseding build never landed")
    # One live job per source: the first is gone, not queued to restart.
    assert store.get_view_job(first["job_id"]) is None
    with store._op_lock:
        assert store._build_yields.get("same-1", 0) == 0, \
            "a build for the SAME table was treated as another table's"


def test_a_search_cannot_be_restarted_forever(two_tables):
    """The cap exists so that flicking between tabs cannot starve a
    search: past it, the build keeps the lock and finishes."""
    store, (a, b) = two_tables
    assert store.VIEW_BUILD_YIELD_MAX >= 1
    token = "capped-1"
    with store._op_lock:
        store._build_running = {"source_id": a["id"], "token": token}
        store._build_yields[token] = store.VIEW_BUILD_YIELD_MAX
    store._yield_the_writer(b["id"], "other")
    with store._op_lock:
        assert token not in store._op_preempted, \
            "a build past the yield cap was asked to stand aside again"
        store._build_running = None
        store._build_yields.clear()


def test_asking_twice_while_waiting_is_not_two_yields(two_tables):
    """The waiting build re-asks every 50ms until it has the lock (the
    holder may have taken it a moment before the first ask). Those asks
    must not spend the yield budget, or a wait of a few hundred
    milliseconds would use it up on its own."""
    store, (a, b) = two_tables
    token = "asked-twice"
    with store._op_lock:
        store._build_running = {"source_id": a["id"], "token": token}
    try:
        store._yield_the_writer(b["id"], "other")
        for _ in range(5):
            store._yield_the_writer(b["id"], "other")
        with store._op_lock:
            assert store._build_yields[token] == 1
    finally:
        with store._op_lock:
            store._build_running = None
            store._build_yields.clear()
            store._op_preempted.clear()


def test_preemption_is_a_cancellation_to_anything_that_does_not_know_better():
    """OpPreempted subclasses OpCancelled deliberately: every existing
    handler unwinds a preempted build correctly without being taught
    about it — the only code that looks for the difference is the worker
    that restarts it."""
    from winnow.store import OpCancelled
    assert issubclass(OpPreempted, OpCancelled)
