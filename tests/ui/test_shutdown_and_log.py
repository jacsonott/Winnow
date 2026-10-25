"""Shutdown warns when work is in flight, and stops the server with its
windows still open; the log opens from the Case menu (errors used to go
only to the terminal)."""

from __future__ import annotations

import os

import pytest

from spawn import spawn_server

pytestmark = pytest.mark.ui


def test_shutdown_warns_about_in_flight_work(page):
    # pretend a search-all sweep is running, then hit shutdown
    page.evaluate("() => { __winnow.S.searchAll = { running: true }; }")
    page.evaluate("() => { __winnow.shutdownWinnow(); }")   # opens the confirm (not awaited)
    page.wait_for_selector(".confirm-overlay")
    msg = page.locator(".confirm-overlay .confirm-message").inner_text()
    assert "Still running" in msg and "Search-all" in msg
    # keep the shared server alive
    page.locator(".confirm-actions .btn", has_text="Keep running").click()
    page.evaluate("() => { __winnow.S.searchAll = null; }")


def test_log_opens_from_case_menu(page):
    page.locator("#btnCase").click()
    page.wait_for_selector(".menu")
    page.locator(".menu >> text=Log…").click()
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator("#modalTitle").inner_text().lower() == "log"
    # Errors | All, and a filter box.
    assert page.locator("#modalBody .vp-seg .btn").all_inner_texts() == ["Errors", "All"]
    assert page.locator("#modalBody .errlog-search").count() == 1
    page.keyboard.press("Escape")


def test_shut_down_stops_the_server_with_its_windows_still_open(own_page, until, tmp_path):
    """The reported bug, end to end: Case ▾ → Shut down Winnow… said
    "Winnow is off" and the server ran on, case lock held, until every
    window was closed — each window's presence stream is a response that
    never ends, and uvicorn's graceful shutdown waits for it. The other
    window, meanwhile, said it had lost the connection and that Winnow
    shuts down a couple of minutes after the last window closes."""
    csv = tmp_path / "a.csv"
    csv.write_text("When,Host\n" + "".join(f"2020-01-01 08:{i:02d}:00,H{i}\n" for i in range(5)),
                   encoding="utf-8")
    case = tmp_path / "off.db"
    lock = os.path.realpath(case) + ".winnow-lock"
    server = spawn_server(tmp_path, "--case", case, "--open", csv, "--no-fts", ready="/api/sources")
    try:
        windows = own_page(base=server.base)
        with windows as page:
            other = windows.extra_page()
            other.goto(server.base)
            other.wait_for_selector(".row")
            until(page, lambda: server.get("/api/presence/others")["others"] == 2, "both windows connected")

            page.locator("#btnCase").click()
            page.locator(".menu >> text=Shut down Winnow…").click()
            page.wait_for_selector(".confirm-overlay")
            msg = page.locator(".confirm-overlay .confirm-message").inner_text()
            assert "1 other Winnow window is open on this server" in msg
            page.locator(".confirm-actions .btn", has_text="Shut down").click()

            # Both windows stay open throughout.
            code = server.wait_exit(15)
            assert code is not None, f"still running 15s after Shut down:\n{server.log.finish()}"
            assert not os.path.exists(lock), "the exit skipped Store.close() — case lock left behind"
            page.wait_for_selector(".shutdown-note-title")
            other.wait_for_selector("#connBanner:not([hidden])")
            assert "server has shut down" in other.locator("#connBanner").inner_text()
    finally:
        server.stop()
