"""A SQL pane query that never ends can still be stopped.

The pane runs whatever the analyst typed, and some of it never finishes: a
recursive CTE with no stop condition, a cross join of two 2M-row tables.
Nothing could stop one. The busy chip had nothing to cancel, a closed
window left it running, a case switch left it running, and Shut down — or
SIGTERM — left the PROCESS running: a worker thread inside sqlite3_step
keeps the interpreter alive, at 100% CPU, with the case lock held until
someone found the process and killed it.

Each test starts a query that would take minutes and asserts it is gone
within seconds of whatever should have stopped it. "Running" is read from
the interpreter (a thread whose stack is inside Store.run_sql) rather than
from any bookkeeping the fix keeps, so these assert the symptom. The query
is bounded — a billion rows, minutes of CPU — rather than truly endless,
so that a regression is a failure and a slow exit rather than a suite that
never finishes.
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from winnow.store import Store

ROOT = Path(__file__).resolve().parent.parent

RUNAWAY = ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c "
           "WHERE x < 1000000000) SELECT count(*) FROM c")

STOPPED_WITHIN_S = 10


def _running(fn_name: str) -> bool:
    """Whether some thread is inside Store.<fn_name> right now."""
    for frame in sys._current_frames().values():
        while frame is not None:
            code = frame.f_code
            if code.co_name == fn_name and code.co_filename.endswith("store.py"):
                return True
            frame = frame.f_back
    return False


def _wait(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def _in_thread(fn, *args, **kwargs):
    out: dict = {}

    def run():
        try:
            out["result"] = fn(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001 — recorded for the test to assert on
            out["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


# ----------------------------------------------------------------- Store


@pytest.mark.parametrize("method", ["run_sql", "sql_to_table"])
def test_closing_the_case_stops_a_runaway_pane_query(case_path, method):
    """A case switch (and the shutdown hook) closes the Store; the pane's
    connection is its own, so close() used to finish around it and leave the
    query spinning for the life of the process. Save as table runs the
    analyst's SQL too — its exact-count wrap first — so it is the same
    exposure."""
    s = Store(case_path)
    args = (RUNAWAY,) if method == "run_sql" else (RUNAWAY, "runaway")
    t, out = _in_thread(getattr(s, method), *args)
    assert _wait(lambda: _running(method), 10), "the query never started"

    s.close()
    assert _wait(lambda: not _running(method), STOPPED_WITHIN_S), \
        f"{method} still running {STOPPED_WITHIN_S}s after the case closed"
    t.join(5)
    assert "error" in out and "result" not in out


def test_a_pane_query_asked_for_after_close_is_refused(case_path):
    """The other half of close(): a query that reaches the Store a moment
    after it was told to stop must not start one nobody will interrupt."""
    s = Store(case_path)
    s.close()
    t, out = _in_thread(s.run_sql, RUNAWAY)
    t.join(STOPPED_WITHIN_S)
    assert not t.is_alive(), "a pane query started on a closed Store"
    assert "error" in out


# ------------------------------------------------------------------ HTTP


def test_the_busy_chip_cancel_stops_a_runaway_query(client):
    """The Stop control: the pane's run carries an op token like every
    other cancellable request, and /api/cancel_op interrupts it."""
    t, out = _in_thread(client.post, "/api/sql", json={"sql": RUNAWAY, "op_token": "tok-sql"})
    assert _wait(lambda: _running("run_sql"), 10), "the query never started"

    client.post("/api/cancel_op", json={"token": "tok-sql"})
    assert _wait(lambda: not _running("run_sql"), STOPPED_WITHIN_S), \
        f"the query was still running {STOPPED_WITHIN_S}s after Cancel"
    t.join(5)
    assert out["result"].status_code == 499   # cancelled, not "your SQL is wrong"


@pytest.fixture
def live_server(store, monkeypatch):
    """A real uvicorn in this process: a TestClient cannot hang up halfway
    through a request, which is the thing being tested."""
    import uvicorn

    import server

    monkeypatch.setattr(server, "STORE", store)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port,
                                        log_level="warning", lifespan="off",
                                        timeout_graceful_shutdown=2))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    assert _wait(lambda: srv.started, 30), "uvicorn never started"
    yield port
    srv.should_exit = True
    t.join(15)


def test_a_client_that_gives_up_takes_its_query_with_it(live_server):
    """A closed window, a reload, a curl --max-time: nobody is left to read
    the answer, and the query ran on regardless — at 100% CPU, holding the
    idle shutdown off too (it is an in-flight request)."""
    c = http.client.HTTPConnection("127.0.0.1", live_server, timeout=30)
    c.request("POST", "/api/sql", body=json.dumps({"sql": RUNAWAY}),
              headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    assert _wait(lambda: _running("run_sql"), 10), "the query never started"

    c.close()   # hang up without reading the response
    assert _wait(lambda: not _running("run_sql"), STOPPED_WITHIN_S), \
        f"the query was still running {STOPPED_WITHIN_S}s after its client left"


def _spawn(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ,
           "WINNOW_WORKSPACE_DIR": str(tmp_path / "ws"),
           "WINNOW_ENV_FILE": str(tmp_path / "userenv"),
           "WINNOW_CASES_DIR": str(tmp_path / "cases")}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py"), "--case", str(tmp_path / "runaway.db"),
         "--port", str(port), "--host", "127.0.0.1", "--no-browser", "--no-fts",
         "--no-idle-shutdown"],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail("server exited during startup:\n" + proc.stdout.read().decode(errors="replace"))
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            c.request("GET", "/api/version")
            c.getresponse().read()
            c.close()
            return proc, port
        except OSError:
            time.sleep(0.15)
    proc.kill()
    pytest.fail("server did not come up")


@pytest.mark.parametrize("how", [
    "shutdown_button",
    pytest.param("sigterm", marks=pytest.mark.skipif(sys.platform == "win32",
                                                     reason="no SIGTERM to send on Windows")),
])
def test_shut_down_is_not_held_up_by_a_runaway_query(tmp_path, how):
    """The reported symptom, end to end: Settings → Shut down answered
    {"ok": true} and the process was still at 100% CPU 15 seconds later,
    holding the case lock. uvicorn's graceful shutdown waits for in-flight
    requests, the request was waiting on a worker thread, and that thread
    was inside sqlite3_step with nothing to tell it to stop. The client
    here stays connected throughout, so only the shutdown itself can be
    what stops the query."""
    proc, port = _spawn(tmp_path)
    held = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        held.request("POST", "/api/sql", body=json.dumps({"sql": RUNAWAY}),
                     headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
        time.sleep(1.0)   # well under way: the request is in, the query is spinning
        if how == "sigterm":
            proc.send_signal(signal.SIGTERM)
        else:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("POST", "/api/shutdown", body="{}",
                      headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
            assert c.getresponse().status == 200
            c.close()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pytest.fail("the server was still running 20s after being told to shut down")
    finally:
        held.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
