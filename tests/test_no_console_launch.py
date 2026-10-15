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


# ----------------------------------------------- consoles that can't spell →

def _ansi_open(real_open):
    """open() as it behaves on a Windows box whose ANSI code page is
    cp1252 — the locale encoding a text-mode open() with no encoding=
    picks up there, and what pythonw's replacement streams were opened
    with."""
    def _open(file, mode="r", buffering=-1, encoding=None, *a, **kw):
        if "b" not in mode and encoding is None:
            encoding = "cp1252"
        return real_open(file, mode, buffering, encoding, *a, **kw)
    return _open


class _Served(Exception):
    """Stands in for uvicorn.Server.run: reaching it means startup got as
    far as binding. (main() builds the Server itself rather than calling
    uvicorn.run, so that it can hook the signal handler — see
    server._stop_pane_queries.)"""


def _start(monkeypatch):
    """main() up to the bind. Startup prints a line per bundled example
    plugin, and they ship switched off: "Plugin disabled: … (toggle in
    Settings → Plugins)". That arrow is not in cp1252."""
    import uvicorn
    from winnow import instances

    def _serve(*a, **k):
        raise _Served()

    monkeypatch.setattr(uvicorn.Server, "run", _serve)
    monkeypatch.setattr(instances, "register", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["server.py", "--no-browser"])
    assert any(not p["enabled"] for p in server.PLUGINS.describe()), \
        "no disabled plugin, so startup prints no arrow and this proves nothing"
    with pytest.raises(_Served):
        server.main()


def test_a_cp1252_machine_with_no_console_still_starts(monkeypatch):
    """The association launch on a typical Windows box: pythonw (no
    console) plus a cp1252 locale. The null stream used to be opened in
    the locale encoding, so the first → killed the server before it bound
    and the analyst saw nothing at all."""
    import builtins

    monkeypatch.setattr(server, "open", _ansi_open(builtins.open), raising=False)
    _no_console(monkeypatch)
    _start(monkeypatch)
    print("Settings → Plugins, and a lone surrogate \udc80")   # must not raise


def test_the_log_survives_the_null_stream_too(monkeypatch):
    """log.record prints every entry to stderr — which, with no console,
    is the same null stream."""
    import builtins

    from winnow import log as wlog

    monkeypatch.setattr(server, "open", _ansi_open(builtins.open), raising=False)
    _no_console(monkeypatch)
    server._ensure_console_streams()
    wlog.record("error", "Plugin FAILED: café → 日本")


def test_a_cp1252_console_still_starts_and_keeps_its_stream(monkeypatch):
    """`python server.py > winnow.log` on the same box, or
    PYTHONIOENCODING=cp1252: a real stream, but one in the locale
    encoding with strict errors, so the same print raised there too. It
    keeps its encoding (whatever reads the log expects it) and spells
    what it can't as an escape, which is what Python's own stderr does."""
    buf = io.BytesIO()
    out = io.TextIOWrapper(buf, encoding="cp1252", line_buffering=True)
    monkeypatch.setattr(sys, "stdout", out)
    _start(monkeypatch)
    assert sys.stdout is out
    print("Settings → Plugins")
    out.flush()
    assert b"Settings \\u2192 Plugins" in buf.getvalue()
