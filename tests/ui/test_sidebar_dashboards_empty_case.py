"""A case with no tables yet still lists its dashboards in the sidebar —
a fresh case with a profile applied already has boards, and an empty one
needs the "New dashboard" row to be reachable. Boots its own server on an
empty case, since the shared fixture's case has tables."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui

ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def empty_case_server(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = {**os.environ,
           "WINNOW_IDLE_EXIT_S": "600", "WINNOW_NEVER_CONNECTED_EXIT_S": "600",
           "WINNOW_WORKSPACE_DIR": str(tmp_path / "ws"),
           "WINNOW_ENV_FILE": str(tmp_path / "userenv"),
           "WINNOW_CASES_DIR": str(tmp_path / "cases")}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py"), "--case", str(tmp_path / "empty.db"),
         "--port", str(port), "--host", "127.0.0.1", "--no-browser", "--no-fts"],
        cwd=str(tmp_path), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail("server died during startup")
        try:
            urllib.request.urlopen(base + "/api/sources", timeout=1)
            break
        except OSError:
            time.sleep(0.15)
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_an_empty_case_still_shows_its_dashboards_section(browser, empty_case_server):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    ctx.add_init_script("localStorage.setItem('winnow.remotePrompt', 'seen');"
                        "localStorage.setItem('winnow.appearance', JSON.stringify({ splash: false }));"
                        "localStorage.setItem('winnow.sidebar', JSON.stringify({ collapsed: false }))")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(empty_case_server, wait_until="networkidle")
        pg.wait_for_selector("#app:not([hidden])")
        assert pg.evaluate("() => __winnow.S.sources.length") == 0
        pg.wait_for_selector("#sidebarList .menu-header:has-text('Dashboards')")
        assert pg.locator("#sidebarList .menu-item", has_text="New dashboard").count() == 1
        # …and a board created in the empty case appears as a row
        pg.evaluate("""() => fetch('/api/dashboards', { method: 'POST',
            headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: 'Empty-case board' }) })""")
        pg.evaluate("() => __winnow.loadDashboards().then(() => __winnow.renderSidebar())")
        pg.wait_for_selector("#sidebarList .sidebar-row:has-text('Empty-case board')")
    finally:
        ctx.close()
    assert not errors, errors
