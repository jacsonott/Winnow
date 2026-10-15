"""Idle shutdown: a server whose browsers are all gone exits on its own.

The decision matrix is pure-function tested; the two integration tests
run a REAL server with the graces shrunk via environment and watch the
process — one proving it exits alone, one proving a held presence
connection keeps it alive and its closure releases it. Nothing less
would demonstrate the feature, since the whole point is process exit."""

from __future__ import annotations

import asyncio
import http.client
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import server as srv

ROOT = Path(__file__).resolve().parent.parent


def _p(streams=0, ever=True, enabled=True, started=0.0, last_zero=0.0, goodbye=True):
    p = srv._Presence()
    p.streams = streams
    p.ever_connected = ever
    p.enabled = enabled
    p.started = started
    p.last_zero = last_zero
    # Most existing cases describe a window that closed; the ones about a
    # window that merely went quiet pass goodbye=False.
    p.said_goodbye = goodbye
    return p


def test_exit_reasons_cover_the_matrix():
    idle, never = srv.IDLE_EXIT_S, srv.NEVER_CONNECTED_EXIT_S

    # The exits.
    assert srv._idle_exit_reason(idle + 1, _p(), busy=False)
    assert srv._idle_exit_reason(never + 1, _p(ever=False), busy=False)

    # The holds — each one alone must prevent it.
    assert srv._idle_exit_reason(idle + 1, _p(streams=1), busy=False) is None
    assert srv._idle_exit_reason(idle + 1, _p(), busy=True) is None
    assert srv._idle_exit_reason(idle + 1, _p(enabled=False), busy=False) is None

    # Not yet.
    assert srv._idle_exit_reason(idle - 1, _p(), busy=False) is None
    assert srv._idle_exit_reason(never - 1, _p(ever=False), busy=False) is None
    # A connected-then-closed server uses the SHORT fuse, a never-connected
    # one the long fuse — mixing those up either reaps a --no-browser user
    # typing the URL, or leaves association-spawned orphans for 15 minutes.
    assert srv._idle_exit_reason(idle + 1, _p(ever=False), busy=False) is None


def _spawn(tmp_path, extra_env=None, args=()):
    env = {**os.environ,
           "WINNOW_IDLE_EXIT_S": "2",
           "WINNOW_NEVER_CONNECTED_EXIT_S": "3",
           "WINNOW_IDLE_TICK_S": "0.3",
           # Keep the spawned server out of the developer's real
           # workspace/ (registry, instances.json).
           "WINNOW_WORKSPACE_DIR": str(tmp_path / "ws"),
           "WINNOW_ENV_FILE": str(tmp_path / "userenv"),
           **(extra_env or {})}
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py"), "--case", str(tmp_path / "idle.db"),
         "--port", str(port), "--no-browser", "--no-fts", *args],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            c.request("GET", "/api/version")
            c.getresponse().read()
            c.close()
            return proc, port
        except OSError:
            if proc.poll() is not None:
                out = proc.stdout.read().decode(errors="replace")
                pytest.fail(f"server exited during startup:\n{out}")
            time.sleep(0.15)
    proc.kill()
    pytest.fail("server never came up")


def _wait_exit(proc, timeout):
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return None


def test_a_server_nobody_connects_to_exits_by_itself(tmp_path):
    proc, port = _spawn(tmp_path)
    code = _wait_exit(proc, 30)
    out = proc.stdout.read().decode(errors="replace")
    assert code == 0, f"expected a clean self-exit, got {code}:\n{out}"
    assert "no browser ever connected" in out


def _goodbye(port):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/api/goodbye", headers={"X-Timeline-Lite-Client": "1"})
    assert conn.getresponse().status == 200
    conn.close()


def test_a_held_presence_connection_keeps_it_alive(tmp_path):
    proc, port = _spawn(tmp_path, extra_env={"WINNOW_SUSPENDED_EXIT_S": "3"})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", "/api/presence")
    resp = conn.getresponse()
    resp.read(3)  # the first ping — the stream is genuinely established

    # Well past every shrunken grace: with a stream open it must stay up.
    time.sleep(6)
    assert proc.poll() is None, "exited while a browser was still connected"

    conn.close()
    _goodbye(port)          # the window says it is closing, as a page does
    code = _wait_exit(proc, 30)
    out = proc.stdout.read().decode(errors="replace")
    assert code == 0, f"expected a clean exit after the window closed, got {code}:\n{out}"
    assert "the last window closed" in out


def test_a_stream_that_drops_without_a_goodbye_waits_for_the_window(tmp_path):
    """The reported bug: the analyst still had the window open, but its
    presence stream had dropped — a suspended background tab, a sleeping
    laptop — and the server exited out from under it. Losing the stream is
    not the same as losing the window, so the short fuse no longer applies."""
    proc, port = _spawn(tmp_path, extra_env={"WINNOW_SUSPENDED_EXIT_S": "60"})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", "/api/presence")
    conn.getresponse().read(3)
    conn.close()            # dropped, with no goodbye — the window is still there

    time.sleep(6)           # three times the closed-window fuse
    assert proc.poll() is None, "exited while the window was still open"
    proc.terminate()
    proc.wait(timeout=15)


def test_a_stream_that_drops_without_a_goodbye_still_exits_eventually(tmp_path):
    """A machine asleep overnight should not leave a server holding the
    case file forever — the long fuse is long, not absent."""
    proc, port = _spawn(tmp_path, extra_env={"WINNOW_SUSPENDED_EXIT_S": "2"})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", "/api/presence")
    conn.getresponse().read(3)
    conn.close()
    code = _wait_exit(proc, 30)
    out = proc.stdout.read().decode(errors="replace")
    assert code == 0, f"expected a clean self-exit, got {code}:\n{out}"
    assert "no browser has been connected for" in out


def test_the_flag_disables_it(tmp_path):
    proc, port = _spawn(tmp_path, args=("--no-idle-shutdown",))
    time.sleep(6)   # far past both shrunken graces
    try:
        assert proc.poll() is None, "exited despite --no-idle-shutdown"
    finally:
        proc.terminate()
        _wait_exit(proc, 15)


def test_a_window_that_never_said_goodbye_gets_the_long_fuse():
    """The reported failure: the analyst still had the window open, but its
    presence stream had dropped (a suspended background tab, a sleeping
    laptop), and the server exited out from under it after two minutes."""
    idle, suspended = srv.IDLE_EXIT_S, srv.SUSPENDED_EXIT_S
    assert suspended > idle
    # Silent for longer than the closed-window fuse: not enough any more.
    assert srv._idle_exit_reason(idle + 1, _p(goodbye=False), busy=False) is None
    # It still exits eventually — a machine asleep overnight should not
    # leave a server holding the case file forever.
    reason = srv._idle_exit_reason(suspended + 1, _p(goodbye=False), busy=False)
    assert reason and "no browser has been connected" in reason


def test_a_window_that_said_goodbye_still_exits_promptly():
    reason = srv._idle_exit_reason(srv.IDLE_EXIT_S + 1, _p(goodbye=True), busy=False)
    assert reason and "the last window closed" in reason


def test_a_new_stream_cancels_an_earlier_goodbye(client):
    """Two windows, one closed: the survivor's stream must clear the flag,
    or closing one window puts the other on the short fuse."""
    before = (srv.PRESENCE.streams, srv.PRESENCE.said_goodbye)
    try:
        assert client.post("/api/goodbye").json() == {"ok": True}
        assert srv.PRESENCE.said_goodbye is True
        srv._presence_open()              # what the SSE route does on connect
        assert srv.PRESENCE.said_goodbye is False
        srv._presence_close()
        assert srv.PRESENCE.streams == before[0]
    finally:
        srv.PRESENCE.streams, srv.PRESENCE.said_goodbye = before


def test_goodbye_needs_the_csrf_header():
    from fastapi.testclient import TestClient
    bare = TestClient(srv.app)
    assert bare.post("/api/goodbye").status_code == 403


def test_a_streaming_download_counts_as_in_flight_until_its_body_ends(client, ingested):
    """The in-flight hold is what keeps auto-exit from killing a CSV export
    that outlives its tab. It was counted in a BaseHTTPMiddleware, whose
    call_next returns when the HEADERS exist — so the count dropped back
    the moment the download started and every body chunk went out with the
    hold already released.

    Driven as raw ASGI so each body chunk can be looked at as it is sent:
    TestClient collects the whole response before handing it over, which
    would hide exactly the window this is about."""
    import asyncio

    store, sid = ingested
    vid = client.post("/api/view", json={"source_id": sid}).json()["view_id"]

    async def run():
        chunks: list[int] = []
        request = [{"type": "http.request", "body": b"", "more_body": False}]

        async def receive():
            if request:
                return request.pop()
            await asyncio.sleep(3600)   # the client never goes away mid-test

        async def send(message):
            if message["type"] == "http.response.body":
                chunks.append(srv.PRESENCE.inflight)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "GET", "scheme": "http", "path": "/api/export",
                 "raw_path": b"/api/export", "root_path": "",
                 "query_string": f"view_id={vid}".encode(),
                 "headers": [(b"host", b"127.0.0.1")],
                 "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8000)}
        before = srv.PRESENCE.inflight
        await srv.app(scope, receive, send)
        return before, chunks, srv.PRESENCE.inflight

    before, chunks, after = asyncio.run(run())
    assert len(chunks) >= 2, "expected the export to stream in more than one chunk"
    assert all(n == before + 1 for n in chunks), (before, chunks)
    assert after == before


# ------------------------------------------------ several windows, one closes

@pytest.fixture
def presence(monkeypatch):
    """A fresh presence record driven through the real hooks — the SSE
    route's open/close and the goodbye route — rather than one built by
    hand, since what broke was the bookkeeping between them."""
    p = srv._Presence()
    monkeypatch.setattr(srv, "PRESENCE", p)
    return p


def _fuse(p, at):
    return srv._idle_exit_reason(at, p, busy=False)


def _goodbye_now(page=""):
    """The goodbye route, called as the event loop would (it is async so
    that it shares a thread with the stream hooks)."""
    return asyncio.run(srv.api_goodbye(page=page))


def test_closing_one_of_two_windows_leaves_the_survivor_on_the_long_fuse(presence):
    """Two windows; A is closed (its goodbye arrives, then its stream is
    noticed gone), B stays open and is later suspended. said_goodbye was
    only ever cleared by a NEW stream, so A's goodbye was still standing
    when B went quiet, and the server exited two minutes later under a
    window that was still there."""
    srv._presence_open()             # window A
    srv._presence_open()             # window B
    _goodbye_now()                   # A's pagehide
    srv._presence_close()            # A's stream noticed gone
    srv._presence_close()            # later: B suspended, no goodbye
    now = presence.last_zero
    assert _fuse(presence, now + srv.IDLE_EXIT_S + 1) is None
    assert "no browser has been connected" in _fuse(presence, now + srv.SUSPENDED_EXIT_S + 1)


def test_a_goodbye_heard_after_its_own_stream_closed_is_still_not_the_survivor_s(presence):
    """The same two windows with the other arrival order, which is the
    likelier one: a stream's close is noticed on the event loop the moment
    the socket drops, while the goodbye POST queues for a worker thread. By
    the time A's goodbye lands, A's stream is gone and only B's is open —
    clearing on close alone cannot tell that goodbye from B's own."""
    srv._presence_open("A")
    srv._presence_open("B")
    srv._presence_close("A")
    _goodbye_now(page="A")
    srv._presence_close("B")         # B suspended, no goodbye
    now = presence.last_zero
    assert _fuse(presence, now + srv.IDLE_EXIT_S + 1) is None


def test_the_last_window_to_close_still_gets_the_short_fuse_in_either_order(presence):
    srv._presence_open("A")
    srv._presence_open("B")
    srv._presence_close("B")         # B suspended first — no goodbye
    _goodbye_now(page="A")        # then A is closed, goodbye first
    srv._presence_close("A")
    assert "the last window closed" in _fuse(presence, presence.last_zero + srv.IDLE_EXIT_S + 1)

    srv._presence_open("C")          # a new window, closed the other way round
    srv._presence_close("C")
    _goodbye_now(page="C")
    assert "the last window closed" in _fuse(presence, presence.last_zero + srv.IDLE_EXIT_S + 1)


def test_a_page_s_goodbye_does_not_outlive_its_page(presence):
    """Bookkeeping per page is bounded by the pages that are OPEN: a closed
    page leaves nothing behind for a later one to inherit."""
    srv._presence_open("A")
    _goodbye_now(page="A")
    srv._presence_close("A")
    srv._presence_open("B")
    srv._presence_close("B")         # B never said goodbye
    assert _fuse(presence, presence.last_zero + srv.IDLE_EXIT_S + 1) is None
    assert not presence.pages and not presence.farewells


def test_the_page_id_rides_on_both_presence_requests():
    """connection.js names its page on the stream and on the goodbye; the
    server can only match the two up if both carry the same id."""
    js = (ROOT / "static" / "js" / "connection.js").read_text(encoding="utf-8")
    assert "/api/presence?page=" in js
    assert "/api/goodbye?page=" in js
