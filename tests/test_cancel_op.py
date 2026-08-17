"""Cancellable operations (Store.cancel_op / _interruptible): a slow view
build interrupted mid-INSERT surfaces as OpCancelled, the *previous* view
survives the cancelled rebuild (the evict-after-build ordering — the whole
point of moving eviction), pre-cancelled tokens fail fast, and the HTTP
layer maps it all to 499."""

from __future__ import annotations

import threading
import time

import pytest

from store import OpCancelled

# A catastrophic-backtracking regex against strings of a's: each row costs
# real CPU inside the REGEXP UDF, so the build is reliably still running
# when the cancel lands — and SQLite checks its interrupt flag between VM
# ops, so the abort is prompt. 5k rows at ~ms each is minutes of runway;
# the test only needs the first few hundred ms of it.
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


def test_cancel_interrupts_build_and_previous_view_survives(slow_source):
    store, sid = slow_source
    # A plain view first — the one that must still be alive after the
    # cancelled rebuild rolls back.
    old = store.build_view(sid, {"filters": [{"column": "Name", "op": "contains", "value": "n1"}]})
    assert store.fetch_rows(old["view_id"], 0, 1)["rows"]

    result = {}

    def build():
        try:
            result["view"] = store.build_view(sid, _slow_spec("tok-1"))
        except BaseException as e:  # noqa: BLE001 — recorded for the main thread to assert on
            result["error"] = e

    t = threading.Thread(target=build)
    t.start()
    time.sleep(0.4)  # let the INSERT get going
    assert store.cancel_op("tok-1") is True
    t.join(30)
    assert not t.is_alive(), "interrupt never landed — build still running"
    assert isinstance(result.get("error"), OpCancelled)

    # The rollback restored the world: the old view still pages.
    assert store.fetch_rows(old["view_id"], 0, 1)["rows"]
    # And the store is healthy — a fresh build works.
    v = store.build_view(sid, {"filters": [{"column": "Name", "op": "contains", "value": "n2"}]})
    assert store.fetch_rows(v["view_id"], 0, 1)["rows"]


def test_precancelled_token_fails_fast(slow_source):
    store, sid = slow_source
    store.cancel_op("tok-early")
    t0 = time.time()
    with pytest.raises(OpCancelled):
        store.build_view(sid, _slow_spec("tok-early"))
    assert time.time() - t0 < 1.0  # never started the slow INSERT


def test_cancel_unknown_token_reports_false(store):
    assert store.cancel_op("never-registered") is False


def test_group_summary_honours_token(ingested):
    store, sid = ingested
    v = store.build_view(sid, {})
    store.cancel_op("tok-group")
    with pytest.raises(OpCancelled):
        store.group_summary(v["view_id"], "Process", op_token="tok-group")
    # An uncancelled call on the same view still works (the reader that
    # carried the cancelled op was closed, not repooled).
    assert store.group_summary(v["view_id"], "Process")["groups"]


def test_build_without_token_unaffected(ingested):
    store, sid = ingested
    v = store.build_view(sid, {"filters": [{"column": "Process", "op": "contains", "value": "svchost"}]})
    assert v["row_count"] == 1


# ------------------------------------------------------------------ HTTP


def test_api_view_pre_cancelled_token_is_499(client, ingested):
    store, sid = ingested
    r = client.post("/api/cancel_op", json={"token": "tok-http"})
    assert r.status_code == 200
    assert r.json() == {"cancelled": False}  # nothing in flight yet
    r = client.post("/api/view", json={"source_id": sid,
                                       "filters": [{"column": "Process", "op": "contains", "value": "x"}],
                                       "op_token": "tok-http"})
    assert r.status_code == 499
    assert r.json()["detail"] == "Cancelled"


def test_api_cancel_lands_mid_build(client, slow_source):
    store, sid = slow_source
    result = {}

    def build():
        result["resp"] = client.post("/api/view", json={"source_id": sid, **_slow_spec("tok-http-2")})

    t = threading.Thread(target=build)
    t.start()
    time.sleep(0.4)
    r = client.post("/api/cancel_op", json={"token": "tok-http-2"})
    assert r.status_code == 200
    t.join(30)
    assert not t.is_alive()
    assert result["resp"].status_code == 499
