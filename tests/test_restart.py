"""Restarting from Settings → Updates (POST /api/restart, server._relaunch).

What it has to get right: the server that comes back is the same address
and the same case, so the windows already open only have to reload (on
Linux and macOS it is the same process, execve'd; on Windows a new one the
old one waits on); the port is handed over rather than bound again, so a
request in between is answered by the new server instead of refused; and
nothing is restarted into that cannot start, since that would leave the
analyst with no server at all."""

from __future__ import annotations

import argparse
import contextlib
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error

import pytest

import server as srv
from spawn import Server, free_port, isolated_env, spawn_server
from test_shutdown import _Window


class _Client(Server):
    """Server's requests, for a process this test did not spawn."""

    def __init__(self, port):
        self.port, self.base = port, f"http://127.0.0.1:{port}"


@pytest.fixture
def restart_state(monkeypatch):
    """A fresh restart record, and the shutdown it triggers recorded
    rather than raised (the real one would SIGINT pytest)."""
    state = {"requested": False, "listener": None}
    triggered = []
    monkeypatch.setattr(srv, "_RESTART", state)
    monkeypatch.setattr(srv, "_SHUTDOWN_ONCE", threading.Lock())
    monkeypatch.setattr(srv, "_trigger_shutdown", lambda: triggered.append(True))
    yield state, triggered
    if state["listener"] is not None:
        state["listener"].close()


@pytest.fixture
def launched(monkeypatch, tmp_path):
    """As if main() had started this server: what a restart needs exists."""
    args = argparse.Namespace(host="127.0.0.1", port=8777, allow_host=[], plugins_dir=[],
                              no_idle_shutdown=False)
    launch = {"args": args, "env": dict(os.environ), "cwd": os.getcwd()}
    monkeypatch.setattr(srv, "_LAUNCH", launch)
    return launch


def test_a_server_main_did_not_start_cannot_restart(client, restart_state, monkeypatch):
    """Under the test client, as under a bare `uvicorn server:app`, there is
    no command line to start again with: say so rather than stop for good."""
    _, triggered = restart_state
    # Set outright: the tests that drive main() in this process up to the
    # bind leave theirs behind.
    monkeypatch.setattr(srv, "_LAUNCH", None)
    v = client.get("/api/version").json()
    assert v["can_restart"] is False and v["boot"] == srv.BOOT_ID
    r = client.post("/api/restart")
    assert r.status_code == 400
    assert "python server.py" in r.json()["detail"]
    assert triggered == []


def test_restart_answers_with_this_run_and_hands_the_listener_on(
        client, restart_state, launched, monkeypatch):
    state, triggered = restart_state
    monkeypatch.setattr(srv, "_new_version_problem", lambda: None)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    monkeypatch.setattr(srv, "_LISTENER", listener)
    try:
        assert client.get("/api/version").json()["can_restart"] is True
        assert client.post("/api/restart").json() == {"ok": True, "boot": srv.BOOT_ID}
        assert state["requested"] and triggered == [True]
        # A second handle on the same socket, which outlives uvicorn
        # closing its own.
        handed = state["listener"]
        assert handed.fileno() != listener.fileno()
        assert handed.getsockname() == listener.getsockname()
        # A second press (another window) is not a second shutdown.
        assert client.post("/api/restart").status_code == 409
        assert triggered == [True]
    finally:
        listener.close()


def test_a_version_that_does_not_start_is_not_restarted_into(
        client, restart_state, launched, monkeypatch):
    """A new version needing a package this Python lacks: the restart would
    stop the server and never bring one back. Refused, saying what to run."""
    state, triggered = restart_state
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(
            argv, 1, "", "Traceback (most recent call last):\n  ...\n"
                         "ModuleNotFoundError: No module named 'duckdb'\n")
    monkeypatch.setattr(srv.subprocess, "run", fake_run)
    r = client.post("/api/restart")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "No module named 'duckdb'" in detail and "pip install -r requirements.txt" in detail
    assert seen and seen[0][-1] == "--help"
    assert not state["requested"] and triggered == []


def test_the_startup_check_passes_on_a_version_that_starts(launched):
    """The real check, on this checkout: `server.py --help` imports what a
    start imports and exits 0 without binding or opening anything."""
    assert srv._new_version_problem() is None


def test_the_command_line_is_what_the_server_is_not_what_it_did(tmp_path):
    args = argparse.Namespace(
        host="127.0.0.1", port=8790, allow_host=["winnow.lan"], plugins_dir=["/opt/p"],
        no_idle_shutdown=True, open_files=["a.csv"], assoc=["b.evtx"], force=True,
        no_fts=True, case="started-with.db-winnow", browser_tab=True)
    argv = srv._relaunch_argv(args, str(tmp_path / "open-now.db-winnow"))
    assert argv[:2] == [sys.executable, str(srv.Path(srv.__file__).resolve())]
    assert argv[2:] == [
        "--host", "127.0.0.1", "--port", "8790", "--no-browser",
        "--allow-host", "winnow.lan", "--plugins-dir", "/opt/p", "--no-idle-shutdown",
        "--case", str(tmp_path / "open-now.db-winnow")]
    # On the home screen: no case at all, not the one it started with.
    assert "--case" not in srv._relaunch_argv(args, None)


def test_a_handed_down_listener_is_adopted():
    sock = srv._claim_port("127.0.0.1", 0)
    port = sock.getsockname()[1]
    handed = sock.dup()
    handed.set_inheritable(True)
    try:
        got = srv._adopt_listener(str(handed.detach()), "127.0.0.1", port)
        try:
            assert got.getsockname()[1] == port
            # Not on to whatever this process starts next.
            assert got.get_inheritable() is False
        finally:
            got.close()
    finally:
        sock.close()


@pytest.mark.parametrize("handle", ["not-a-handle", "999999"])
def test_a_listener_that_cannot_be_adopted_is_bound_by_number(handle):
    port = free_port()
    got = srv._adopt_listener(handle, "127.0.0.1", port)
    try:
        assert got.getsockname()[1] == port
    finally:
        got.close()


def test_restart_comes_back_on_the_same_port_and_case(tmp_path):
    """End to end against a real server: the windows are told it is a
    restart, the next run answers on the same port with the same case open,
    and nothing asked of it in between is refused."""
    case = tmp_path / "keep.db-winnow"
    server = spawn_server(tmp_path, "--case", case, "--no-idle-shutdown")
    lock = os.path.realpath(case) + ".winnow-lock"
    windows = [_Window(server.port, "A"), _Window(server.port, "B")]
    try:
        before = server.get("/api/version")
        assert before["can_restart"] is True
        assert server.post("/api/restart", {}) == {"ok": True, "boot": before["boot"]}
        refused, deadline = [], time.monotonic() + 30
        while True:
            try:
                now = server.get("/api/version", timeout=10)
            except (urllib.error.URLError, ConnectionError) as e:
                refused.append(e)
                now = before
            if now["boot"] != before["boot"]:
                break
            assert time.monotonic() < deadline, f"never came back:\n{server.log.text()}"
            time.sleep(0.1)
        assert refused == [], "the port closed during the restart — the listener wasn't handed over"
        assert server.proc.poll() is None
        if os.name != "nt":
            assert now["pid"] == before["pid"]   # execve: the same process
        assert server.get("/api/case/current")["path"] == str(case)
        assert os.path.exists(lock), "the restarted server doesn't hold the case lock"
        for w in windows:
            w.thread.join(5)
            assert f"event: restart\ndata: {before['boot']}".encode() in w.rest
        log = server.log.text()
        assert log.count("Winnow on http://127.0.0.1:%d" % server.port) == 2, log
    finally:
        for w in windows:
            w.close()
        server.stop()


def test_the_windows_way_starts_a_new_process_and_waits_for_it(tmp_path, monkeypatch):
    """Windows has no execve, so _relaunch starts the next Winnow and waits
    on it, leaving with its exit code. Run here the same way: the socket is
    inherited, the new process serves on it, and when that one shuts down
    the old one's wait ends with its code."""
    case = tmp_path / "win.db-winnow"
    listener = srv._claim_port("127.0.0.1", 0)
    port = listener.getsockname()[1]
    args = argparse.Namespace(host="127.0.0.1", port=port, allow_host=[], plugins_dir=[],
                              no_idle_shutdown=True)
    monkeypatch.setattr(srv, "_LAUNCH", {"args": args, "env": isolated_env(tmp_path),
                                         "cwd": os.getcwd()})
    monkeypatch.setattr(srv, "_LAST_CASE", str(case))
    monkeypatch.setattr(srv, "_RESTART", {"requested": True, "listener": listener})
    result = {}
    waiter = threading.Thread(target=lambda: result.update(code=srv._relaunch(in_place=False)))
    waiter.start()
    new = _Client(port)
    try:
        deadline = time.monotonic() + 45
        while True:
            try:
                v = new.get("/api/version", timeout=2)
                break
            except (urllib.error.URLError, ConnectionError):
                assert "code" not in result, f"the new process exited ({result})"
                assert time.monotonic() < deadline, "the new process never answered"
                time.sleep(0.2)
        assert v["pid"] != os.getpid()
        assert new.get("/api/case/current")["path"] == str(case)
        # The old process let go of its handle once the new one had it.
        assert listener.fileno() == -1
        assert new.post("/api/shutdown", {}) == {"ok": True}
        waiter.join(20)
        assert result.get("code") == 0
    finally:
        if waiter.is_alive():
            with contextlib.suppress(OSError, urllib.error.URLError):
                new.post("/api/shutdown", {})
            waiter.join(20)
