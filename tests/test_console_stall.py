"""A console that stops taking output must not stop Winnow.

Reported as "launching Winnow in PowerShell with QuickEdit enabled can
freeze it". With QuickEdit on (the Windows default), one click in the
console window opens a selection, and until it is cleared (Esc, Enter, a
right-click) the console accepts no output: every write to it blocks. Every
log.record entry is printed to stderr, from whichever thread logged it,
including the event loop (the catch-all error handler, the idle monitor,
uvicorn's own warnings), so the next thing Winnow said froze whatever said
it, and a frozen event loop is every request frozen.

A pipe nobody reads is the same condition, and portable: once it is full,
the next write waits. `ServerLog.pause()` is that pipe. The fix is
winnow/console.py: writes go on a queue that one thread drains, so a
stalled console stalls that thread and nothing else.
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error

import pytest

from spawn import ROOT, spawn_server
from winnow import console


class _Console:
    """A console stream that can be stalled: while stalled, write() blocks
    the way WriteConsole does during a QuickEdit selection."""

    encoding = "utf-8"
    errors = "strict"

    def __init__(self, log=None, name="out"):
        self.log = log if log is not None else []
        self.name = name
        self._open = threading.Event()
        self._open.set()
        self.entered = threading.Event()

    def stall(self):
        self._open.clear()

    def release(self):
        self._open.set()

    def write(self, text):
        self.entered.set()
        self._open.wait()
        self.log.append((self.name, text))
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return True

    def text(self):
        return "".join(t for _, t in self.log)


@pytest.fixture
def writer():
    w = console.Writer()
    yield w
    w.drain(5)


# --------------------------------------------------------------- the queue

def test_a_print_returns_while_the_console_is_stalled(writer):
    real = _Console()
    out = console.QueuedStream(real, writer)
    real.stall()
    t0 = time.monotonic()
    print("Import finished: evtx.csv", file=out, flush=True)
    assert time.monotonic() - t0 < 0.5, "print waited for the stalled console"
    assert not writer.drain(0.2), "the console is stalled, so nothing can have been written"
    real.release()
    assert writer.drain(5)
    assert real.text() == "Import finished: evtx.csv\n"


def test_flush_does_not_wait_either(writer):
    """log.record prints with flush=True, and so does uvicorn's handler."""
    real = _Console()
    out = console.QueuedStream(real, writer)
    real.stall()
    out.write("x\n")
    t0 = time.monotonic()
    out.flush()
    assert time.monotonic() - t0 < 0.5
    real.release()


def test_stdout_and_stderr_keep_the_order_they_were_written_in(writer):
    shared = []
    out = console.QueuedStream(_Console(shared, "out"), writer)
    err = console.QueuedStream(_Console(shared, "err"), writer)
    said = []
    for i in range(50):
        which, stream = ("out", out) if i % 3 else ("err", err)
        stream.write(f"{i}\n")
        said.append((which, f"{i}\n"))
    assert writer.drain(5)
    # Consecutive writes to one stream may be coalesced into one; the order
    # of what reached each destination is what must hold.
    got = [(name, line + "\n") for name, chunk in shared for line in chunk.splitlines()]
    assert got == said


def test_the_console_catches_up_with_everything_once_released(writer):
    real = _Console()
    out = console.QueuedStream(real, writer)
    real.stall()
    for i in range(1000):
        print(f"line {i}", file=out)
    real.release()
    assert writer.drain(5)
    assert real.text().splitlines() == [f"line {i}" for i in range(1000)]


def test_a_console_held_for_ever_is_bounded_and_says_what_it_skipped():
    w = console.Writer(limit=100)
    real = _Console()
    out = console.QueuedStream(real, w)
    real.stall()
    out.write("first\n")                       # 6 chars, taken by the writer and stuck
    assert real.entered.wait(5)
    for i in range(20):
        out.write(f"line {i:02d}\n")           # 8 chars each: 11 fit beside "first"
    real.release()
    assert w.drain(5)
    lines = real.text().splitlines()
    assert lines[:12] == ["first"] + [f"line {i:02d}" for i in range(11)]
    assert len(lines) == 13 and lines[12].startswith("[winnow] 9 line(s) of console output skipped")


def test_a_line_after_the_gap_carries_the_notice_ahead_of_it():
    w = console.Writer(limit=20)
    real = _Console()
    out = console.QueuedStream(real, w)
    real.stall()
    out.write("held\n")
    assert real.entered.wait(5)
    out.write("a" * 30 + "\n")                  # does not fit: skipped
    real.release()
    assert w.drain(5)
    out.write("after\n")
    assert w.drain(5)
    lines = real.text().splitlines()
    assert lines[0] == "held"
    assert lines[1].startswith("[winnow] 1 line(s)")
    assert lines[2:] == ["after"]


def test_a_stream_that_cannot_spell_it_gets_escapes_and_the_writer_lives_on(writer):
    """reconfigure() is what normally loosens a strict stream
    (server._ensure_console_streams); one that can't be told must still
    not take the writer thread down with it."""
    buf = io.BytesIO()
    strict = io.TextIOWrapper(buf, encoding="ascii", errors="strict", write_through=True)
    out = console.QueuedStream(strict, writer)
    out.write("Settings → Plugins, café\n")
    out.write("still writing\n")
    assert writer.drain(5)
    nl = os.linesep.encode()   # a text stream writes \r\n on Windows
    assert buf.getvalue() == b"Settings \\u2192 Plugins, caf\\xe9" + nl + b"still writing" + nl


def test_a_broken_console_does_not_stop_the_other_stream(writer):
    class Gone(_Console):
        def write(self, text):
            raise BrokenPipeError(32, "Broken pipe")

    err = console.QueuedStream(Gone(), writer)
    good = _Console()
    out = console.QueuedStream(good, writer)
    err.write("lost\n")
    out.write("kept\n")
    assert writer.drain(5)
    assert good.text() == "kept\n"


def test_what_uvicorn_and_print_ask_of_a_stream_is_the_real_ones_answer(writer):
    r, w = os.pipe()
    try:
        with open(w, "w", encoding="cp1252", errors="backslashreplace", closefd=True) as real:
            out = console.QueuedStream(real, writer)
            assert out.isatty() is False            # uvicorn's colour decision
            assert out.fileno() == real.fileno()
            assert out.encoding == "cp1252" and out.errors == "backslashreplace"
            assert out.line_buffering == real.line_buffering   # anything else: the real stream's
            assert out.writable()
            with pytest.raises(TypeError):
                out.write(b"bytes")
    finally:
        os.close(r)


# ------------------------------------------------------------- install()

def _pipe_stream():
    r, w = os.pipe()
    return r, open(w, "w", encoding="utf-8")


def test_install_wraps_a_pipe_and_leaves_in_process_streams_alone(monkeypatch, writer):
    r, piped = _pipe_stream()
    mine = io.StringIO()
    try:
        monkeypatch.setattr(sys, "stdout", piped)
        monkeypatch.setattr(sys, "stderr", mine)
        assert console.install(writer) is writer
        assert isinstance(sys.stdout, console.QueuedStream) and sys.stdout.wrapped is piped
        assert sys.stderr is mine
        # Twice is once.
        wrapped = sys.stdout
        console.install(writer)
        assert sys.stdout is wrapped
        print("through the queue")
        assert writer.drain(5)
        assert os.read(r, 100) == b"through the queue" + os.linesep.encode()
    finally:
        piped.close()
        os.close(r)


def test_regular_files_and_the_null_stream_cannot_stall(tmp_path):
    with open(tmp_path / "winnow.log", "w", encoding="utf-8") as f:
        assert not console.can_stall(f)           # `python server.py > winnow.log`
    with open(os.devnull, "w", encoding="utf-8") as f:
        assert not console.can_stall(f)           # what pythonw gets (see test_no_console_launch)
    assert not console.can_stall(None)
    assert not console.can_stall(io.StringIO())
    r, piped = _pipe_stream()
    try:
        assert console.can_stall(piped)
    finally:
        piped.close()
        os.close(r)


def test_uvicorn_logs_through_the_queue_when_install_runs_first(monkeypatch, writer):
    """main() installs before it builds uvicorn's Config, whose
    configure_logging() resolves ext://sys.stderr then. Built the other way
    round, uvicorn's warnings would hold the real stream, and they are
    written from the event loop."""
    import uvicorn

    import server

    r, piped = _pipe_stream()
    saved = {n: logging.getLogger(n).handlers[:] for n in ("uvicorn", "uvicorn.access")}
    try:
        monkeypatch.setattr(sys, "stderr", piped)
        console.install(writer)
        uvicorn.Config(server.app, host="127.0.0.1", port=0, log_level="warning")
        streams = [h.stream for h in logging.getLogger("uvicorn").handlers]
        assert streams and all(s is sys.stderr for s in streams)
        assert isinstance(sys.stderr, console.QueuedStream)
    finally:
        for n, handlers in saved.items():
            logging.getLogger(n).handlers = handlers
        writer.drain(5)
        piped.close()
        os.close(r)


# ------------------------------------------------------------------- exit

def _env():
    """Buffered streams, as a Windows console's are. PYTHONUNBUFFERED (set
    for spawned servers) takes away the buffer lock that exit trips over."""
    return {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}


_STALLED = """
import sys
from winnow import console
real = (sys.stdout, sys.stderr)
w = console.install()
for i in range(400):
    print("x" * 500)   # 200 KB: three times what a Linux pipe holds
assert not w.drain(1), "the pipe took it all, so nothing is stalled"
if "--real-streams-back" in sys.argv:
    sys.stdout, sys.stderr = real
"""


def _exit_code_with_a_stalled_console(*args, timeout):
    """Run _STALLED with pipes nobody reads; its exit code, or None if it
    was still running after `timeout` seconds."""
    proc = subprocess.Popen([sys.executable, "-c", _STALLED, *args], cwd=str(ROOT), env=_env(),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    finally:
        proc.kill()
        proc.stdout.close()
        proc.stderr.close()
        proc.wait()


def test_exit_does_not_wait_for_a_stalled_console():
    assert _exit_code_with_a_stalled_console(timeout=15) == 0, \
        "the process could not exit while its console was stalled"


def test_handing_the_real_streams_back_at_exit_would_hang_it():
    """Why _at_exit leaves the queued streams installed: Python's exit
    flushes sys.stdout, and with the real stream there that flush waits on
    the writer stuck inside it. Also keeps the test above honest — if a
    future Python stops waiting here, this fails rather than the other
    passing for a reason that no longer holds."""
    assert _exit_code_with_a_stalled_console("--real-streams-back", timeout=4) is None


def test_what_was_said_just_before_exit_still_arrives():
    """main() refuses a non-case file by printing why and calling
    sys.exit(1) straight after. The writer is a daemon thread, so without
    the drain at exit that line could die in the queue."""
    code = ("import sys\n"
            "from winnow import console\n"
            "console.install()\n"
            "for i in range(2000):\n"
            "    print(f'line {i}')\n"
            "print('ERROR: the reason', file=sys.stderr)\n"
            "sys.exit(1)\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=_env(),
                       capture_output=True, timeout=30)
    assert r.returncode == 1
    assert r.stdout.decode().splitlines() == [f"line {i}" for i in range(2000)]
    assert r.stderr.decode().strip() == "ERROR: the reason"


def test_at_exit_points_the_originals_at_the_queue_only_when_stalled(monkeypatch):
    monkeypatch.setattr(console, "EXIT_GRACE_S", 0.2)
    # Recorded so monkeypatch puts them back whatever _at_exit does.
    monkeypatch.setattr(sys, "__stdout__", sys.__stdout__)
    monkeypatch.setattr(sys, "__stderr__", sys.__stderr__)
    original = sys.__stdout__

    w = console.Writer()
    real = _Console()
    out = console.QueuedStream(real, w)
    monkeypatch.setattr(console, "_writer", w)
    monkeypatch.setattr(sys, "stdout", out)

    out.write("caught up\n")
    console._at_exit()
    assert sys.__stdout__ is original, "a console that caught up keeps its real stream"
    assert real.text() == "caught up\n"

    real.stall()
    out.write("stuck\n")
    console._at_exit()
    assert sys.__stdout__ is out
    assert sys.__stderr__ is not out          # stderr was never queued here
    real.release()
    assert w.drain(5)


# ------------------------------------------------------- a real server

def _flood(srv, n=400):
    """~200 KB of log lines — three times what a Linux pipe holds — through
    the browser's own log route, a plain def route that logs from a worker
    thread, which is exactly what a stalled console used to hang."""
    msg = "x" * 480
    for i in range(n):
        try:
            srv.post("/api/log/client", {"level": "warn", "message": f"{i:04d} {msg}"}, timeout=5)
        except (TimeoutError, urllib.error.URLError) as e:
            pytest.fail(f"request {i} hung behind the stalled console: {e!r}")


def _buffered():
    # As a Windows console's streams are; see _env.
    return {"PYTHONUNBUFFERED": ""}


def test_the_server_keeps_answering_while_its_console_is_stalled(tmp_path):
    srv = spawn_server(tmp_path, "--no-idle-shutdown", env=_buffered())
    try:
        srv.log.pause()
        _flood(srv)
        assert srv.get("/api/version", timeout=5)
        srv.log.resume()
        deadline = time.monotonic() + 15
        while "0399 " not in srv.log.text():
            assert time.monotonic() < deadline, "the console never caught up once released"
            time.sleep(0.1)
        assert "line(s) of console output skipped" not in srv.log.text()
    finally:
        srv.stop()


def test_it_still_shuts_down_while_its_console_is_stalled(tmp_path):
    case = tmp_path / "stalled.db-winnow"
    srv = spawn_server(tmp_path, "--case", case, "--no-fts", "--no-idle-shutdown", env=_buffered())
    lock = os.path.realpath(case) + ".winnow-lock"
    try:
        srv.log.pause()
        _flood(srv)
        assert srv.post("/api/shutdown", {}) == {"ok": True}
        code = srv.wait_exit(15)
    finally:
        out = srv.stop()
    assert code == 0, f"still running 15s after Shut down, with its console stalled:\n{out[-2000:]}"
    assert not os.path.exists(lock), "the exit skipped Store.close()"
