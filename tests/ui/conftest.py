"""Fixtures for the browser-driven UI tests.

These drive the *real* app — a real uvicorn serving a real case file, a real
Chromium rendering `static/app.js` — because the bugs they exist to catch
are ones no backend test can see. Every one of the regressions in
`test_regressions.py` shipped past a green backend suite: a CSS change that
silently unpinned the filter row from its columns, an autofit that measured
the wrong thing, a Reset button that had never worked. They're only
observable as geometry and state in a live document.

Cost and dependencies are deliberately kept off the analyst's machine.
Playwright and its Chromium live in `requirements-dev.txt` only — the
airgapped target still needs nothing but the standard library plus FastAPI —
and the whole module skips, rather than fails, when either is missing, so
`pytest` on a box without a browser stays green and honest about what it
covered.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed (pip install -r requirements-dev.txt)"
).sync_playwright


def _free_port() -> int:
    """A port the OS just told us was free. Racy in principle; in practice the
    window between closing this socket and uvicorn binding it is microseconds,
    and the alternative (a fixed port) collides with the analyst's own running
    Winnow, or with a second session on the same box — which is the far more
    likely failure here."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # no browser binary installed
            pytest.skip(f"chromium unavailable ({e}); run `playwright install chromium`")
        yield b
        b.close()


@pytest.fixture(scope="session")
def ui_csv(tmp_path_factory) -> Path:
    """Small, but shaped for the things the UI tests measure: a column whose
    *header* is far longer than its values (autofit), one whose values are far
    longer than its header (the autofit cap), and enough repetition for the
    value picker to have something to list."""
    path = tmp_path_factory.mktemp("ui") / "ui.csv"
    rows = ["Timestamp,EventId,Host,ExtremelyLongColumnHeaderName,CommandLine"]
    for i in range(200):
        rows.append(
            f"2026-03-14 08:{i // 60:02d}:{i % 60:02d},"
            f"{[4624, 4625, 4688, 1][i % 4]},"
            f"H{i % 5},"
            f"v,"
            f"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe -Enc {'A' * 300}"
        )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


@pytest.fixture(scope="session")
def server(tmp_path_factory, ui_csv):
    """One server for the whole UI session — startup is ~1s and none of these
    tests mutate the case in a way another test reads. Uses its own tmp case
    file, like every other test in the suite."""
    port = _free_port()
    case = tmp_path_factory.mktemp("case") / "ui.db"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py"), "--case", str(case),
         "--open", str(ui_csv), "--port", str(port), "--host", "127.0.0.1",
         "--no-browser", "--no-fts"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 45
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
            pytest.fail(f"server exited during startup:\n{out}")
        try:
            with urllib.request.urlopen(base + "/api/sources", timeout=1) as r:
                if r.status == 200:
                    break
        except (urllib.error.URLError, OSError):
            time.sleep(0.15)
    else:
        proc.kill()
        pytest.fail("server did not come up within 45s")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def page(browser, server):
    """A fresh browser context per test: localStorage is where the keymap,
    appearance and sidebar state live, so sharing one would let a test that
    rebinds a key or changes the autofit cap decide the next test's outcome."""
    ctx = browser.new_context(viewport={"width": 1500, "height": 900},
                              permissions=["clipboard-read", "clipboard-write"])
    # Every context is a "first run on this machine" — pre-answer the
    # one-time remote-mode prompt so it can't overlay the app mid-test, and
    # turn the launch animation off. The splash covers the whole viewport
    # for several seconds, which every click in every test would otherwise
    # wait out. test_first_run_prompt.py and test_splash.py build their own
    # contexts without this.
    ctx.add_init_script("localStorage.setItem('winnow.remotePrompt', 'seen');"
                        "localStorage.setItem('winnow.appearance',"
                        " JSON.stringify({ splash: false }))")
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(server, wait_until="networkidle")
    pg.wait_for_selector(".row", timeout=30_000)
    yield pg
    # An uncaught exception in app.js doesn't fail a test on its own — the
    # assertion usually still passes, because the grid was already painted
    # before the handler threw. Surfacing it here is what makes these tests
    # catch a broken menu handler rather than just a broken layout.
    ctx.close()
    assert not errors, "uncaught JS errors: " + " | ".join(errors)
