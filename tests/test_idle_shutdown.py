"""Idle shutdown: a server whose browsers are all gone exits on its own.

The decision matrix is pure-function tested; the two integration tests
run a REAL server with the graces shrunk via environment and watch the
process — one proving it exits alone, one proving a held presence
connection keeps it alive and its closure releases it. Nothing less
would demonstrate the feature, since the whole point is process exit."""

from __future__ import annotations

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


def _p(streams=0, ever=True, enabled=True, started=0.0, last_zero=0.0):
    p = srv._Presence()
    p.streams = streams
    p.ever_connected = ever
    p.enabled = enabled
    p.started = started
    p.last_zero = last_zero
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


def test_a_held_presence_connection_keeps_it_alive(tmp_path):
    proc, port = _spawn(tmp_path)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", "/api/presence")
    resp = conn.getresponse()
    resp.read(3)  # the first ping — the stream is genuinely established

    # Well past every shrunken grace: with a stream open it must stay up.
    time.sleep(6)
    assert proc.poll() is None, "exited while a browser was still connected"

    conn.close()
    code = _wait_exit(proc, 30)
    out = proc.stdout.read().decode(errors="replace")
    assert code == 0, f"expected a clean exit after disconnect, got {code}:\n{out}"
    assert "no browser has been connected for" in out


def test_the_flag_disables_it(tmp_path):
    proc, port = _spawn(tmp_path, args=("--no-idle-shutdown",))
    time.sleep(6)   # far past both shrunken graces
    try:
        assert proc.poll() is None, "exited despite --no-idle-shutdown"
    finally:
        proc.terminate()
        _wait_exit(proc, 15)
