"""Restarting after an update, end to end: a real server restarts, and
every window on it comes back by itself, on the same case.

The update itself is answered in the browser (the real one would replace
the checkout under test); everything from Restart on is real. The shared
server must never be the one restarted, so this spawns its own."""

from __future__ import annotations

import os

import pytest

from spawn import spawn_server

pytestmark = pytest.mark.ui


def _fake_release(route):
    name = route.request.url.split("/api/updates/", 1)[1]
    if name == "check":
        route.fulfill(json={"current": "1.0.0", "latest": "9.9.9", "available": True,
                            "notes": "", "url": "", "published_at": "", "html_url": ""})
    else:
        route.fulfill(json={"version": "9.9.9", "previous_version": "1.0.0",
                            "backup": "b", "restart_required": True})


def test_every_window_comes_back_on_the_same_case_after_a_restart(own_page, until, tmp_path):
    csv = tmp_path / "a.csv"
    csv.write_text("When,Host\n" + "".join(f"2020-01-01 08:{i:02d}:00,H{i}\n" for i in range(5)),
                   encoding="utf-8")
    case = tmp_path / "restart.db"
    lock = os.path.realpath(case) + ".winnow-lock"
    server = spawn_server(tmp_path, "--case", case, "--open", csv, "--no-fts", ready="/api/sources")
    try:
        before = server.get("/api/version")
        windows = own_page(base=server.base)
        with windows as page:
            other = windows.extra_page()
            other.goto(server.base)
            other.wait_for_selector(".row")
            until(page, lambda: server.get("/api/presence/others")["others"] == 2, "both windows connected")
            page.route("**/api/updates/**", _fake_release)

            page.evaluate("() => __winnow.openSettings()")
            page.locator("#modalBody .settings-nav-item", has_text="Updates").click()
            page.locator(".btn", has_text="Check for updates").click()
            page.locator(".btn", has_text="Install update").click()
            assert page.locator(".confirm-check input").is_checked()
            # Both windows reload by themselves: the one that asked, and the
            # other one, told by its presence stream.
            with page.expect_navigation(timeout=30_000), other.expect_navigation(timeout=30_000):
                page.locator(".confirm-actions .btn", has_text="Install 9.9.9").click()
                page.wait_for_selector("#restarting:not([hidden])")
                other.wait_for_selector("#restarting:not([hidden])")

            after = server.get("/api/version")
            assert after["boot"] != before["boot"]
            assert server.proc.poll() is None
            for w in (page, other):
                w.wait_for_selector(".row")
                assert w.locator("#restarting").is_hidden()
                assert w.locator("#connBanner").is_hidden()
            assert os.path.exists(lock), "the restarted server doesn't hold the case lock"
            until(page, lambda: server.get("/api/presence/others")["others"] == 2,
                  "both windows reconnected to the new server")
    finally:
        server.stop()
