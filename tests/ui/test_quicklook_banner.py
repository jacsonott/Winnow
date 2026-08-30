"""The quick-look banner: visible in a temp case, absent in a real one,
and Save-as promotes the case while the analyst watches."""

from __future__ import annotations

import json
import subprocess
import sys
import socket
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui

ROOT = Path(__file__).resolve().parent.parent.parent


def test_no_banner_on_a_real_case(page):
    assert page.locator("#tempBanner").get_attribute("hidden") is not None


@pytest.fixture
def quicklook_server(browser, tmp_path):
    """A real --assoc launch, since temp-ness is a fact about the path and
    the shared fixture's case is real."""
    dropped = tmp_path / "dropped.csv"
    dropped.write_text("Host,User\nh1,alice\nh2,bob\n", encoding="utf-8")
    # Same isolation as the backend launcher test: a real subprocess sees
    # the real INSTALL_ROOT, so point its workspace and cases dir at
    # tmp_path or "Promoted case" registers in the developer's install and
    # collides with itself on every rerun.
    env = dict(**__import__("os").environ,
               WINNOW_IDLE_EXIT_S="600", WINNOW_NEVER_CONNECTED_EXIT_S="600",
               WINNOW_WORKSPACE_DIR=str(tmp_path / "ws"),
               WINNOW_CASES_DIR=str(tmp_path / "cases"))
    proc = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "server.py"), "--assoc", str(dropped),
         "--no-browser", "--no-fts"],
        cwd=str(tmp_path), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    port = None
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline and port is None:
        line = proc.stdout.readline().decode(errors="replace")
        if "Winnow on http://127.0.0.1:" in line:
            port = int(line.split(":")[2].split()[0])
        if proc.poll() is not None:
            pytest.fail("assoc server died during startup")
    assert port
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=1)
            break
        except OSError:
            time.sleep(0.2)
    yield port
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_banner_shows_and_save_as_promotes(browser, quicklook_server, tmp_path):
    ctx = browser.new_context(viewport={"width": 1200, "height": 700})
    ctx.add_init_script("localStorage.setItem('winnow.remotePrompt', 'seen');"
                        "localStorage.setItem('winnow.appearance', JSON.stringify({ splash: false }))")
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(f"http://127.0.0.1:{quicklook_server}/")
        pg.wait_for_selector("#tempBanner:not([hidden])", timeout=20_000)
        assert "temporary" in pg.locator("#tempBanner").inner_text()

        pg.locator("#tempSaveBtn").click()
        pg.wait_for_selector(".confirm-overlay input")
        pg.locator(".confirm-overlay input").fill("Promoted case")
        pg.locator(".confirm-card .btn", has_text="OK").first.click()

        pg.wait_for_selector("#tempBanner[hidden]", state="attached", timeout=15_000)
        cur = pg.evaluate("""() => fetch('/api/case/current',
          { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())""")
        assert cur["temp"] is False
        assert cur["name"] == "Promoted case"
        assert not errors, errors
    finally:
        ctx.close()


def test_home_navigation_is_guarded_in_a_quicklook(browser, quicklook_server, tmp_path):
    """A quick look never appears in the case list, so the home screen
    has no way back to it — clicking the brand used to strand the case
    (reported). The guard offers the three real exits, and Stay works."""
    ctx = browser.new_context(viewport={"width": 1200, "height": 700})
    ctx.add_init_script("localStorage.setItem('winnow.remotePrompt', 'seen');"
                        "localStorage.setItem('winnow.appearance', JSON.stringify({ splash: false }))")
    pg = ctx.new_page()
    try:
        pg.goto(f"http://127.0.0.1:{quicklook_server}/")
        pg.wait_for_selector("#tempBanner:not([hidden])", timeout=20_000)

        pg.locator("#btnHome").click()
        pg.wait_for_selector("#modal:not([hidden])")
        assert "isn't saved" in pg.locator("#modalTitle").inner_text().lower().replace("’", "'")
        assert pg.evaluate("() => document.getElementById('home').hidden"), \
            "the guard must hold navigation, not race it"

        pg.locator("#modal button", has_text="Stay here").click()
        pg.wait_for_selector("#modal[hidden]", state="attached")
        assert pg.evaluate("() => document.getElementById('home').hidden")
        assert not pg.evaluate("() => document.getElementById('tempBanner').hidden")

        # Discard from the guard lands on the home screen with the
        # quick look gone for good.
        pg.locator("#btnHome").click()
        pg.wait_for_selector("#modal:not([hidden])")
        pg.locator("#modal button", has_text="Discard").click()
        pg.wait_for_selector(".confirm-overlay")
        pg.locator(".confirm-card .btn", has_text="Discard").click()
        pg.wait_for_selector("#home:not([hidden])", timeout=15_000)
        cur = pg.evaluate("""() => fetch('/api/case/current',
          { headers: { 'X-Timeline-Lite-Client': '1' } }).then((r) => r.json())""")
        assert cur["open"] is False
    finally:
        ctx.close()
