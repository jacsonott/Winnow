"""Console output that cannot stall the server.

Everything Winnow says on its console — the startup lines, every log.record
entry (log.py prints each one to stderr), uvicorn's own warnings — used to
be written straight to the console by whichever thread had something to
say. A write to a console returns only once the console has taken the
text, and some consoles stop taking it for as long as someone leaves them
that way:

* Windows' console window (what PowerShell, cmd and launch/winnow.bat run
  in) stops accepting output while a selection is open, and with QuickEdit
  Mode on — the default — one click anywhere in the window opens one. The
  title bar gains "Select"; Esc, Enter or a right-click ends it. Until then
  every write to the console blocks inside WriteConsole.
* Edit ▸ Mark from the window menu, the Pause key, and Ctrl+S in a Linux
  or macOS terminal do the same thing deliberately.
* A pipe nobody is reading (`python server.py | more`, a wrapper that
  stopped draining) blocks its writer once it holds 64 KB.

The thread that blocked was whichever one spoke next: a request handler
(the browser posting a client log line), an import job saying it had
finished, and worst, the event loop itself, which writes when a route
raises (the catch-all error handler logs it), when the idle monitor shuts
down, and whenever uvicorn warns. A blocked event loop is every request
blocked. Winnow froze, and stayed frozen until someone pressed Esc in a
window they had clicked by accident and long since stopped looking at.

install() puts a queue in front of each stream that can stall. A write
appends to it and returns; one daemon thread does every real write, in the
order things were said, stdout and stderr interleaved as they happened. A
console that stops accepting output now stalls that one thread and nothing
else, and catches up the moment it is released. The queue is bounded
(LIMIT): a console held for hours turns away what it cannot hold and says
how many lines it skipped once it is writing again. Nothing is lost that
matters: every log.record entry is also in the in-app log.

Two consequences worth knowing:

* flush() returns at once. Waiting there for the console to take the text
  is the stall this module exists to remove; the writer flushes after
  every write instead, so the console is as current as it is willing to be.
* Interpreter exit. The writer is a daemon thread, so whatever is still
  queued when the process ends would be lost — including the line a
  refusal prints just before sys.exit(1). _at_exit gives it EXIT_GRACE_S
  to catch up. A console still stalled after that keeps the rest: the
  queued streams stay installed to the end, because Python's exit flushes
  sys.stdout and sys.stderr, and with the real stream there that flush
  waits on the writer stuck inside it, for as long as the console stays
  stalled (tests/test_console_stall.py shows it). Finalization later puts
  sys.__stdout__/sys.__stderr__ back and flushes those as well; _at_exit
  points them at the queued streams too, so no flush on the way out can
  reach a stream the writer is stuck in.

Stdlib only, imports nothing from the app.
"""

from __future__ import annotations

import atexit
import collections
import contextlib
import io
import os
import stat
import sys
import threading

# Characters held for a console that has stopped taking them, ~10,000 log
# lines. Well past what a selection left open over lunch accumulates; small
# enough not to matter if one is left open all weekend.
LIMIT = 1 << 20
# How long interpreter exit waits for the console to take what is queued.
EXIT_GRACE_S = 1.0
# Most a single real write coalesces, so one burst is not one write per print.
_BATCH = 1 << 16


class Writer:
    """The one thread that writes to the real streams, and its queue."""

    def __init__(self, limit: int = LIMIT):
        self.limit = limit
        # An RLock (Condition's default): a signal handler runs on the main
        # thread between two bytecodes, possibly inside put(), and one that
        # prints must not deadlock against the put() it interrupted.
        self._cond = threading.Condition()
        self._queue: collections.deque[tuple[object, str]] = collections.deque()
        self._held = 0        # characters queued or being written
        self._writing = False
        self._dropped = 0     # lines turned away since the queue last had room
        self._dropped_to = None
        self._thread = threading.Thread(target=self._run, name="winnow-console", daemon=True)
        self._thread.start()

    def put(self, stream, text: str) -> None:
        with self._cond:
            if self._held and self._held + len(text) > self.limit:
                self._dropped += text.count("\n") or 1
                self._dropped_to = stream
                return
            self._say_dropped()
            self._enqueue(stream, text)

    def drain(self, timeout: float | None = None) -> bool:
        """Wait until everything queued has been written. False if the
        streams were still not taking it after `timeout` seconds."""
        with self._cond:
            return self._cond.wait_for(lambda: not self._queue and not self._writing, timeout)

    def _enqueue(self, stream, text: str) -> None:
        self._queue.append((stream, text))
        self._held += len(text)
        self._cond.notify_all()

    def _say_dropped(self) -> None:
        if self._dropped:
            n, self._dropped = self._dropped, 0
            self._enqueue(self._dropped_to, (
                f"[winnow] {n:,} line(s) of console output skipped while this console was "
                "not accepting any (a selection in the window, Ctrl+S, or a pipe nobody "
                "reads). Winnow kept running; Case ▾ → Log… has every error and job message.\n"))

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                stream, text = self._queue.popleft()
                parts, size = [text], len(text)
                while self._queue and self._queue[0][0] is stream and size < _BATCH:
                    nxt = self._queue.popleft()[1]
                    parts.append(nxt)
                    size += len(nxt)
                self._writing = True
            try:
                _write(stream, "".join(parts))
            finally:
                with self._cond:
                    self._held -= size
                    self._writing = False
                    if not self._queue:
                        # Nothing more is coming to carry the notice, so it
                        # goes on its own, now that the console takes output.
                        self._say_dropped()
                    self._cond.notify_all()


def _write(stream, text: str) -> None:
    """One real write, which must never raise: there is no caller left to
    hear it, and a writer thread that died would leave every later write
    queued for ever."""
    try:
        stream.write(text)
        stream.flush()
    except UnicodeError:
        # A strict stream reconfigure() couldn't loosen (server.py's
        # _ensure_console_streams tries): say it in escapes rather than not
        # at all.
        with contextlib.suppress(Exception):
            stream.write(text.encode("ascii", "backslashreplace").decode("ascii"))
            stream.flush()
    except Exception:  # noqa: BLE001 — closed or broken (the reader of a pipe went away)
        pass


class QueuedStream(io.TextIOBase):
    """sys.stdout or sys.stderr, with each write handed to the Writer."""

    def __init__(self, stream, writer: Writer):
        self.wrapped = stream
        self._writer = writer
        # Asked once: uvicorn asks at startup to decide on colours, and the
        # answer cannot change.
        try:
            self._isatty = bool(stream.isatty())
        except Exception:  # noqa: BLE001
            self._isatty = False

    def write(self, s: str) -> int:
        if not isinstance(s, str):
            raise TypeError(f"write() argument must be str, not {type(s).__name__}")
        if s:
            self._writer.put(self.wrapped, s)
        return len(s)

    def flush(self) -> None:
        """Nothing to wait for — see the module docstring."""

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return self._isatty

    def fileno(self) -> int:
        return self.wrapped.fileno()

    @property
    def encoding(self):
        return self.wrapped.encoding

    @property
    def errors(self):
        return self.wrapped.errors

    def __getattr__(self, name):
        # Anything else (name, buffer, reconfigure, line_buffering) is the
        # real stream's. "wrapped" is excluded so a half-built instance
        # raises AttributeError rather than recursing.
        if name == "wrapped":
            raise AttributeError(name)
        return getattr(self.wrapped, name)


def can_stall(stream) -> bool:
    """Whether a write to `stream` can wait on something outside Winnow:
    a console, a terminal or a pipe. A regular file takes a write as fast as
    the disk does, and nobody can pause it from outside; a stream with no
    descriptor at all (a StringIO, a test's capture) is in-process."""
    if stream is None or isinstance(stream, QueuedStream):
        return False
    if getattr(stream, "name", None) == os.devnull:
        # _ensure_console_streams' stand-in for no console: discards, never waits.
        return False
    try:
        return not stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
    except (AttributeError, OSError, ValueError):   # io.UnsupportedOperation is both
        return False


_writer: Writer | None = None


def install(writer: Writer | None = None) -> Writer | None:
    """Queue sys.stdout and sys.stderr wherever a write to them can stall.
    Safe to call twice. Returns the Writer, or None if neither stream
    needed one. `writer` is for tests; the process-wide one also drains at
    exit."""
    global _writer
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if not can_stall(stream):
            continue
        if writer is None:
            if _writer is None:
                _writer = Writer()
                atexit.register(_at_exit)
            writer = _writer
        setattr(sys, name, QueuedStream(stream, writer))
    return writer


def _at_exit() -> None:
    w = _writer
    if w is None or w.drain(EXIT_GRACE_S):
        return
    # Still stalled: the writer is inside a write the console has not taken,
    # holding that stream's lock. Finalization restores sys.stdout/stderr
    # from these and flushes them again; pointed at the queued streams, that
    # flush cannot wait.
    for name in ("stdout", "stderr"):
        current = getattr(sys, name, None)
        if isinstance(current, QueuedStream) and current._writer is w:
            setattr(sys, f"__{name}__", current)
