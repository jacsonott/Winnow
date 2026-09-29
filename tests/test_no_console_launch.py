"""Starting with no console at all.

Windows' `pythonw.exe` is what a file association launches once the
background setting is on (`assoc.launch_command`), and it is the default.
It differs from `python.exe` in one way that reaches Python code: there is
no console, so `sys.stdout` and `sys.stderr` are **None** rather than
streams that discard.

`print()` tolerates that — CPython returns early when the file is None —
which is why nothing in this codebase noticed. `uvicorn` does not:
`uvicorn.run` configures logging *before* it binds, and its default
formatter asks `sys.stdout.isatty()`. The server therefore died on the
line before it would have started listening, with no console for the
traceback to land in, while the browser thread waited out its fifteen
seconds on a port that was never going to open.

Reported as "launching by file association never opens the browser",
which is that crash seen from outside.
"""

from __future__ import annotations

import io
import os
import sys

import pytest

import server


def _no_console(monkeypatch):
    """A process with no console, as CPython presents one.

    Applied inside each test rather than from a fixture: pytest's own
    capture reassigns sys.stdout between the setup and call phases, so a
    fixture that nulls it in setup has been overwritten by the time the
    test body runs — and every assertion below would then be about
    pytest's capture object instead of the condition under test."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)


def test_uvicorn_can_configure_logging_without_a_console(monkeypatch):
    """The exact line that broke. `uvicorn.Config` runs
    `configure_logging()` in its constructor, so this reaches the failure
    without binding a port or starting a server."""
    _no_console(monkeypatch)
    import uvicorn

    server._ensure_console_streams()
    uvicorn.Config(server.app, host="127.0.0.1", port=0, log_level="warning")


def test_without_the_fix_that_call_really_does_raise(monkeypatch):
    """Guards the test above from becoming a tautology: if a future
    uvicorn stops asking stdout whether it is a tty, the test above would
    pass with the fix removed and stop being evidence of anything."""
    _no_console(monkeypatch)
    import uvicorn

    with pytest.raises(Exception) as caught:      # noqa: PT011 — the chain is the assertion
        uvicorn.Config(server.app, host="127.0.0.1", port=0, log_level="warning")
    # logging.config wraps whatever a formatter raises in a ValueError, so
    # the AttributeError is the cause rather than the exception. Walking
    # the chain is what keeps this pinned to the real reason instead of to
    # the wrapper's wording.
    chain, e = [], caught.value
    while e is not None:
        chain.append(f"{type(e).__name__}: {e}")
        e = e.__cause__ or e.__context__
    assert any("isatty" in link for link in chain), chain


def test_it_binds_both_streams(monkeypatch):
    _no_console(monkeypatch)
    server._ensure_console_streams()
    assert sys.stdout is not None and sys.stderr is not None
    # One handle, not two: they are the same destination and closing is
    # nobody's job here.
    assert sys.stdout is sys.stderr
    sys.stdout.write("this goes nowhere and must not raise\n")
    sys.stdout.flush()


def test_printing_still_works_afterwards(monkeypatch):
    _no_console(monkeypatch)
    server._ensure_console_streams()
    print("the startup banner, with nowhere to be")   # must not raise


def test_a_real_console_is_left_alone(monkeypatch):
    """A normal `python server.py` keeps its console — the whole point of
    the background setting being optional is that the server log is
    reachable when an association will not open."""
    mine_out, mine_err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", mine_out)
    monkeypatch.setattr(sys, "stderr", mine_err)
    server._ensure_console_streams()
    assert sys.stdout is mine_out
    assert sys.stderr is mine_err


def test_one_missing_stream_does_not_take_the_other(monkeypatch):
    """Not a shape Windows produces, but the loop should be per-stream
    rather than all-or-nothing: a caller that redirected only stdout must
    keep its own handle."""
    mine = io.StringIO()
    monkeypatch.setattr(sys, "stdout", mine)
    monkeypatch.setattr(sys, "stderr", None)
    server._ensure_console_streams()
    assert sys.stdout is mine
    assert sys.stderr is not None
    assert sys.stderr is not mine


def test_the_stream_points_at_the_null_device(monkeypatch):
    """Discarding, not buffering: a StringIO would grow for the life of
    the process, and this one is written to by every request uvicorn
    logs."""
    _no_console(monkeypatch)
    server._ensure_console_streams()
    assert os.path.samefile(sys.stdout.name, os.devnull)
