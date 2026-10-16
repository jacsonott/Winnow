"""A real `python server.py` in a subprocess, for tests that need one of
their own (tests/ has no __init__, so `from spawn import ...` works from
tests/ and tests/ui alike).

One copy because isolating the process is one decision. A subprocess is out
of reach of conftest's monkeypatches, so everything it could write outside
the test's tmp dir has to be pointed into it through the environment
(`isolated_env`). The nine copies this replaced each made that decision
again, and five of them left the file-association dirs pointing at the
developer's real ones.
"""

from __future__ import annotations

import collections
import contextlib
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    """A port the OS just told us was free. Racy in principle; in practice the
    window between closing this socket and uvicorn binding it is microseconds,
    and the alternative (a fixed port) collides with the analyst's own running
    Winnow, or with a second session on the same box — which is the far more
    likely failure here."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def isolated_env(tmp: Path, **extra: str) -> dict[str, str]:
    """os.environ, with every place a server writes outside its case file
    pointed under `tmp`."""
    return {
        **os.environ,
        # The case registry and instances.json: without this every spawned
        # server registers its throwaway case in the developer's real one.
        "WINNOW_WORKSPACE_DIR": str(tmp / "ws"),
        # The WINNOW_* token store: without this, Windows falls through to
        # the developer's real HKCU\Environment.
        "WINNOW_ENV_FILE": str(tmp / "userenv"),
        # Where a quick-look or a new case lands by default.
        "WINNOW_CASES_DIR": str(tmp / "cases"),
        # The association adapters' target dirs: a File associations test
        # must never write the developer's real ~/.local/share or ~/.config.
        "XDG_DATA_HOME": str(tmp / "xdg-data"),
        "XDG_CONFIG_HOME": str(tmp / "xdg-config"),
        # A print() to a pipe is block-buffered; the log should arrive while
        # the test that caused it is still the one running.
        "PYTHONUNBUFFERED": "1",
        **extra,
    }


class ServerLog:
    """What a server writes, read as it writes it. A pipe nobody reads
    blocks the server once it holds 64 KB (on Linux), mid-request, which
    hangs the test; and a traceback that stays in the pipe never reaches
    the report. Keeps the last MAX_LINES; `mark()` and `since()` take just
    the lines written during one test."""

    MAX_LINES = 5000

    def __init__(self, stream):
        self._lines: collections.deque[str] = collections.deque(maxlen=self.MAX_LINES)
        self._total = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream):
        for raw in iter(stream.readline, b""):
            with self._lock:
                self._lines.append(raw.decode(errors="replace").rstrip("\r\n"))
                self._total += 1

    def mark(self) -> int:
        with self._lock:
            return self._total

    def since(self, mark: int) -> list[str]:
        with self._lock:
            kept_from = self._total - len(self._lines)
            return list(self._lines)[max(mark - kept_from, 0):]

    def text(self) -> str:
        return "\n".join(self.since(0))

    def finish(self, timeout: float = 5) -> str:
        """Everything still held, once the process has exited."""
        self._thread.join(timeout)
        return self.text()


class Server:
    """A spawned server, answering on `base` once `spawn_server` returns."""

    def __init__(self, proc: subprocess.Popen, port: int, log: ServerLog | None = None):
        self.proc, self.port = proc, port
        self.base = f"http://127.0.0.1:{port}"
        self.log = log or ServerLog(proc.stdout)

    def get(self, route, timeout=15):
        with urllib.request.urlopen(self.base + route, timeout=timeout) as r:
            return json.loads(r.read())

    def post(self, route, body, timeout=15):
        req = urllib.request.Request(
            self.base + route, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def wait_exit(self, timeout):
        """The exit code, or None (and the process killed) if it is still
        running after `timeout` seconds."""
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            return None

    def stop(self) -> str:
        """Terminate it, and return everything it wrote."""
        if self.proc.poll() is None:
            self.proc.terminate()
            self.wait_exit(10)
        return self.log.finish()


def _answers(port, route) -> bool:
    """Whether anything answers `route`, by plain HTTP: no client header and
    no cookie, so the probe never counts as a browser that connected."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            c.request("GET", route)
            return c.getresponse().status == 200
        finally:
            c.close()
    except OSError:
        return False


def _popen(tmp, args, env, cwd):
    tmp.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [sys.executable, str(ROOT / "server.py"), *map(str, args)],
        cwd=str(cwd or ROOT), env=isolated_env(tmp, **(env or {})),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _wait_ready(srv, ready, timeout):
    deadline = time.monotonic() + timeout
    while not _answers(srv.port, ready):
        if srv.proc.poll() is not None:
            pytest.fail(f"server exited during startup:\n{srv.log.finish()}")
        if time.monotonic() > deadline:
            pytest.fail(f"server did not come up within {timeout:.0f}s:\n{srv.stop()}")
        time.sleep(0.15)
    return srv


def spawn_server(tmp: Path, *args, env: dict | None = None, cwd: Path | None = None,
                 ready: str = "/api/version", timeout: float = 45) -> Server:
    """`python server.py <args>` on a free port, isolated under `tmp`, once
    `ready` answers 200. The port, host and --no-browser are added here;
    pass --case, --open, --no-fts and the rest yourself. Fails the test with
    whatever the server wrote if it exits or never answers. Stop it with
    `Server.stop()`, or use `running_server`."""
    port = free_port()
    proc = _popen(tmp, ["--port", port, "--host", "127.0.0.1", "--no-browser", *args], env, cwd)
    return _wait_ready(Server(proc, port), ready, timeout)


ANNOUNCED = re.compile(r"Winnow on http://127\.0\.0\.1:(\d+)")


def spawn_launcher(tmp: Path, *args, env: dict | None = None, cwd: Path | None = None,
                   timeout: float = 45) -> Server:
    """`python server.py <args>` choosing its own port, as a file
    association's `--assoc` launch does: the port is read from the line it
    prints ("Winnow on http://127.0.0.1:<port>/"), which goes out before
    uvicorn binds, so it then waits for /api/version too. --no-browser is
    added here."""
    proc = _popen(tmp, ["--no-browser", *args], env, cwd)
    log = ServerLog(proc.stdout)
    deadline = time.monotonic() + timeout
    while not (m := ANNOUNCED.search(log.text())):
        if proc.poll() is not None:
            pytest.fail(f"launcher exited before announcing a port:\n{log.finish()}")
        if time.monotonic() > deadline:
            proc.kill()
            pytest.fail(f"launcher never announced a port:\n{log.finish()}")
        time.sleep(0.1)
    return _wait_ready(Server(proc, int(m[1]), log), "/api/version", timeout)


@contextlib.contextmanager
def running_server(tmp: Path, *args, launcher: bool = False, **kw):
    """spawn_server (or spawn_launcher), stopped on the way out."""
    srv = (spawn_launcher if launcher else spawn_server)(tmp, *args, **kw)
    try:
        yield srv
    finally:
        srv.stop()
