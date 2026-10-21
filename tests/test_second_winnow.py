"""A second Winnow on the same machine is a Winnow of its own.

Reported as tags and filters from one case showing up in another, with two
Winnows open on two case files in the same folder. The second one never
ran: it opened its case, started the thread that opens the window, and only
then had uvicorn bind 8777, which the first one held. That thread waited
for "the port answers", which the FIRST Winnow already did, so it opened
the window there; the bind failed and the second process exited, with no
console to say so. The analyst then worked in the first Winnow's case
through a window they believed was the second one's.

Now the port is bound before anything else happens, the next free one is
taken when the default is in use, and the window waits for the server on
its port to be its own process.
"""

from __future__ import annotations

import http.server
import json
import os
import shlex
import socket
import sys
import threading
import time

import pytest

import server
from spawn import _popen, spawn_launcher
from winnow import browser


def _held_port() -> socket.socket:
    """A port something else is listening on."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    return s


def test_the_default_port_taken_means_the_next_free_one(monkeypatch):
    held = _held_port()
    try:
        monkeypatch.setattr(server, "DEFAULT_PORT", held.getsockname()[1])
        sock = server._claim_port("127.0.0.1", None)
        try:
            assert sock.getsockname()[1] != held.getsockname()[1]
        finally:
            sock.close()
    finally:
        held.close()


def test_a_port_asked_for_by_name_is_that_port_or_nothing():
    """A script that said --port must not be answered on another one."""
    held = _held_port()
    try:
        with pytest.raises(OSError):
            server._claim_port("127.0.0.1", held.getsockname()[1])
    finally:
        held.close()


class _Version(http.server.BaseHTTPRequestHandler):
    pid = None

    def do_GET(self):  # noqa: N802 — http.server's name
        body = json.dumps({"version": "x", "pid": self.pid}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def answering():
    """A server on a free port whose /api/version names pid 4242."""
    handler = type("H", (_Version,), {"pid": 4242})
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _opened(monkeypatch):
    opened = []
    monkeypatch.setattr(browser, "app_command", lambda url, **k: ["app", url])
    monkeypatch.setattr(browser, "launch_detached", lambda argv: opened.append(argv[1]))
    monkeypatch.setattr(browser.webbrowser, "open", lambda u: opened.append(u))
    return opened


def test_a_window_is_not_opened_on_another_winnow(monkeypatch, answering):
    opened = _opened(monkeypatch)
    url = f"http://127.0.0.1:{answering}"
    browser.open_when_ready(url, "127.0.0.1", answering, wait_s=5, expect_pid=1111).join(timeout=10)
    assert opened == []


def test_a_window_is_opened_on_its_own_winnow(monkeypatch, answering):
    opened = _opened(monkeypatch)
    url = f"http://127.0.0.1:{answering}"
    browser.open_when_ready(url, "127.0.0.1", answering, wait_s=5, expect_pid=4242).join(timeout=10)
    assert opened == [url]


# ------------------------------------------------------------ end to end

@pytest.fixture
def stand_in_browser(tmp_path):
    """`google-chrome` on PATH that only writes down what it was asked to
    open. Linux only: elsewhere Winnow looks for a browser by install path,
    not on PATH, and would open a real one."""
    if not sys.platform.startswith("linux"):
        pytest.skip("finds the stand-in browser through PATH, which only Linux searches")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "opened.log"
    fake = bin_dir / "google-chrome"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {shlex.quote(str(log))}\n")
    fake.chmod(0o755)
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}, log


def _opened_urls(log, n, timeout=20.0) -> list[str]:
    deadline = time.monotonic() + timeout
    while True:
        lines = log.read_text().split() if log.exists() else []
        if len(lines) >= n or time.monotonic() > deadline:
            return [ln.removeprefix("--app=") for ln in lines]
        time.sleep(0.1)


def test_two_winnows_on_two_cases_in_one_folder_each_get_their_own(tmp_path, stand_in_browser):
    """The report, end to end: one install, one cases folder, two Winnows.
    Each answers on its own port, as its own process, with its own case,
    and each window opened on its own Winnow."""
    env, log = stand_in_browser
    folder = tmp_path / "cases"
    folder.mkdir()
    a, b = folder / "alpha.db-winnow", folder / "bravo.db-winnow"
    first = spawn_launcher(tmp_path / "install", "--case", a, env=env, browser=True)
    try:
        second = spawn_launcher(tmp_path / "install", "--case", b, env=env, browser=True)
        try:
            assert first.port != second.port
            for srv, case in ((first, a), (second, b)):
                assert srv.get("/api/version")["pid"] == srv.proc.pid
                assert srv.get("/api/case/current")["path"] == str(case)
            assert sorted(_opened_urls(log, 2)) == sorted([first.base, second.base])
        finally:
            second.stop()
    finally:
        first.stop()


def test_a_winnow_that_cannot_have_its_port_opens_nothing(tmp_path, stand_in_browser):
    """--port names one port; when it is taken, the Winnow stops before it
    opens the case, imports anything or opens a window."""
    env, log = stand_in_browser
    held = _held_port()
    try:
        csv = tmp_path / "late.csv"
        csv.write_text("a\n1\n", encoding="utf-8")
        case = tmp_path / "bravo.db-winnow"
        proc = _popen(tmp_path / "install", ["--port", held.getsockname()[1], "--case", case,
                                             "--open", csv], env, None)
        out = proc.communicate(timeout=60)[0].decode(errors="replace")
        assert proc.returncode == 3, out
        assert "can't listen" in out
        assert not case.exists(), "the case was opened (and imported into) before the port was"
        # The window opens only after the case is open, so with no case
        # file there was never a window either; the log proves it too.
        assert not log.exists(), log.read_text()
    finally:
        held.close()
