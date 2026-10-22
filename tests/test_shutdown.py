"""Shut down stops the server while its windows are still open.

The reported bug: Case ▾ → Shut down Winnow… answered {"ok": true}, the
page said "Winnow is off", and the process ran on with the case lock held
until the analyst closed the last window. Every page holds a presence
stream open (server.py's idle-shutdown block), that stream is a response
that never finishes, and uvicorn's graceful shutdown waits for every open
response before it runs the lifespan's shutdown half. So the window that
pressed the button was itself what kept the server up. Ctrl+C and SIGTERM
waited the same way.

The integration tests hold streams open exactly as two windows would and
never close them, so only the shutdown itself can be what ends them; the
case lock marker being gone afterwards is the proof the exit was the
graceful one (Store.close() removes it; a force-quit leaves it)."""

from __future__ import annotations

import asyncio
import http.client
import os
import signal
import sys
import threading

import pytest

import server as srv
from spawn import spawn_server


class _Window:
    """A page's presence stream, read on a thread until the server ends it."""

    def __init__(self, port, page):
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        self.conn.request("GET", f"/api/presence?page={page}")
        self.resp = self.conn.getresponse()
        self.resp.read(3)   # the first ping — the stream is genuinely established
        self.rest = b""
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self):
        try:
            self.rest = self.resp.read()
        except (OSError, http.client.HTTPException):
            pass

    def close(self):
        self.conn.close()


@pytest.mark.parametrize("how", [
    "shutdown_button",
    pytest.param("ctrl_c", marks=pytest.mark.skipif(sys.platform == "win32",
                                                    reason="no SIGINT to send on Windows")),
    pytest.param("sigterm", marks=pytest.mark.skipif(sys.platform == "win32",
                                                     reason="no SIGTERM to send on Windows")),
])
def test_shut_down_does_not_wait_for_the_windows_to_close(tmp_path, how):
    case = tmp_path / "shut.db"
    server = spawn_server(tmp_path, "--case", case, "--no-fts", "--no-idle-shutdown")
    lock = os.path.realpath(case) + ".winnow-lock"
    windows = [_Window(server.port, "A"), _Window(server.port, "B")]
    try:
        assert os.path.exists(lock), "expected the open case to hold its lock marker"
        if how == "shutdown_button":
            assert server.post("/api/shutdown", {}) == {"ok": True}
        else:
            server.proc.send_signal(signal.SIGINT if how == "ctrl_c" else signal.SIGTERM)
        code = server.wait_exit(15)
        out = server.log.finish()
        assert code is not None, f"still running 15s after being told to shut down:\n{out}"
        assert not os.path.exists(lock), \
            f"the case lock was left behind — the exit skipped Store.close():\n{out}"
        # ...and closed it as the last connection, which is what has SQLite
        # fold the -wal into the case file and remove it and the -shm.
        assert [s for s in ("-wal", "-shm") if os.path.exists(f"{case}{s}")] == [], out
        for w in windows:
            w.thread.join(5)
            assert b"event: shutdown" in w.rest, \
                "a window was not told the server shut down, so it reads as a lost connection"
    finally:
        for w in windows:
            w.close()
        server.stop()


def test_a_second_shut_down_is_not_a_force_quit(monkeypatch):
    """uvicorn reads a second SIGINT as the impatient Ctrl+C and skips the
    lifespan, so Store.close() never runs. Two windows pressing Shut down
    together must still raise only the one."""
    started = []

    class _Timer:
        def __init__(self, delay, fn):
            self.fn = fn

        def start(self):
            started.append(self.fn)

    monkeypatch.setattr(srv, "_SHUTDOWN_ONCE", threading.Lock())
    monkeypatch.setattr(srv.threading, "Timer", _Timer)
    srv._trigger_shutdown()
    srv._trigger_shutdown()
    assert len(started) == 1


@pytest.fixture
def presence(monkeypatch):
    p = srv._Presence()
    monkeypatch.setattr(srv, "PRESENCE", p)
    return p


def test_shut_down_names_the_other_windows_and_not_the_one_asking(presence):
    srv._presence_open("A")
    srv._presence_open("B")
    srv._presence_open("B")       # B's EventSource mid-reconnect: still one window
    srv._presence_open()          # a client too old to send a page id
    assert asyncio.run(srv.api_presence_others(page="A")) == {"others": 2}
    srv._presence_close("B")
    srv._presence_close("B")
    srv._presence_close()
    assert asyncio.run(srv.api_presence_others(page="A")) == {"others": 0}
