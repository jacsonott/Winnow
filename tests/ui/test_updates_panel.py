"""Settings → Updates, in a real browser.

What only a browser can prove here: the panel shows the installed version,
the check is wired to the button (and NOT to opening Settings — Winnow
must never reach out on its own), and an offline box gets the sentence
telling it how to update by hand rather than a broken-looking panel."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open_updates(page):
    page.evaluate("() => __winnow.openSettings()")
    page.wait_for_selector("#modal:not([hidden])")
    page.locator("#modalBody .settings-nav-item", has_text="Updates").click()
    page.locator(".btn", has_text="Check for updates").wait_for(state="visible")
    # The panel renders "Version: …" and fills it in when /api/version
    # resolves, so waiting on the button alone races that fetch. It lost
    # once under full-suite load and read the placeholder back.
    page.wait_for_function(
        """() => [...document.querySelectorAll('#modal .fb-help')].some(
             (n) => n.textContent.startsWith('Winnow ')
                 || n.textContent === 'Version: unknown')""",
        timeout=10_000)


def test_panel_shows_the_version_and_checks_only_when_asked(page):
    # Count every call the page makes to the update endpoints.
    page.evaluate("""() => {
      window.__updateCalls = [];
      const orig = window.fetch;
      window.fetch = (u, ...rest) => {
        if (String(u).includes('/api/updates/')) window.__updateCalls.push(String(u));
        return orig(u, ...rest);
      };
    }""")
    _open_updates(page)

    # The installed version is shown...
    panel = page.locator(".settings-pane-body").filter(has=page.locator(".btn", has_text="Check for updates"))
    assert panel.inner_text().strip().startswith("Winnow ")
    # ...and merely opening Settings contacted nothing.
    assert page.evaluate("() => window.__updateCalls") == []


def test_an_offline_check_explains_the_manual_path(page):
    _open_updates(page)
    # Stand in for a box with no route to GitHub: the server's own 400.
    page.evaluate("""() => {
      const orig = window.fetch;
      window.fetch = (u, ...rest) => {
        if (String(u).includes('/api/updates/check')) {
          return Promise.resolve(new Response(
            JSON.stringify({ detail: 'Could not reach GitHub to check for updates '
              + '(no route to host). If this machine has no network, download the '
              + 'release on one that does and apply it with: python update.py --from <file>.zip' }),
            { status: 400, headers: { 'Content-Type': 'application/json' } }));
        }
        return orig(u, ...rest);
      };
    }""")
    page.locator(".btn", has_text="Check for updates").click()
    page.wait_for_selector("text=python update.py --from")
    # No install button offered when we never learned of a release.
    assert page.locator(".btn:visible", has_text="Install update").count() == 0


# ----------------------------------------------------- the develop sync
#
# Alt+right-click on the update buttons syncs to develop's tip, with
# nothing on screen saying so (buildUpdatesPanel). Every /api/updates/*
# call is answered here in the browser: the real routes would reach
# GitHub, and the dev apply would overwrite the checkout under test.

_TIP = {"branch": "develop", "sha": "abc1234def5678", "short": "abc1234",
        "message": "Resizable sidebar", "committed_at": "2026-08-29T10:00:00Z", "url": "u"}


class _Stub:
    """Every /api/updates/* call answered in the browser, and the restart
    they lead to: the shared server must never restart under the suite,
    and /api/version answers as this stub's `boot` (flip it to stand in
    for the restarted server) and `can_restart` say. `calls` is
    [(route, body)] in order."""

    def __init__(self, page, *, available=False, can_restart=True, restart=None):
        self.calls = []
        self.boot = "run-1"
        self.can_restart = can_restart
        self.restart = restart   # (status, json) for /api/restart; None = it goes ahead
        self.available = available
        page.route("**/api/updates/**", self._updates)
        page.route("**/api/restart", self._restart)
        page.route("**/api/version", self._version)

    def _updates(self, route):
        req = route.request
        name = req.url.split("/api/updates/", 1)[1]
        body = req.post_data_json or {}
        self.calls.append((name, body))
        if name == "check" and body.get("dev"):
            route.fulfill(json=_TIP)
        elif name == "check":
            latest = "9.9.9" if self.available else "1.0.0"
            route.fulfill(json={"current": "1.0.0", "latest": latest, "available": self.available,
                                "notes": "", "url": "", "published_at": "", "html_url": ""})
        elif name == "apply" and body.get("dev"):
            route.fulfill(json={"version": "1.0.0", "previous_version": "1.0.0",
                                "branch": "develop", "short": "abc1234",
                                "backup": "b", "restart_required": True})
        elif name == "apply" and self.available:
            route.fulfill(json={"version": "9.9.9", "previous_version": "1.0.0",
                                "backup": "b", "restart_required": True})
        else:
            route.fulfill(status=500, json={"detail": f"unexpected {name} {body}"})

    def _restart(self, route):
        self.calls.append(("restart", {}))
        if self.restart:
            status, body = self.restart
            route.fulfill(status=status, json=body)
        else:
            route.fulfill(json={"ok": True, "boot": self.boot})

    def _version(self, route):
        route.fulfill(json={"version": "1.0.0", "source": "release", "is_release": True,
                            "pid": 1, "boot": self.boot, "can_restart": self.can_restart})


def _stub_updates(page, **kw):
    return _Stub(page, **kw).calls


def _check_btn(page):
    return page.locator(".btn", has_text="Check for updates")


def test_alt_right_click_syncs_to_develop_after_naming_the_commit(page):
    calls = _stub_updates(page)
    _open_updates(page)
    # Semi-hidden: nothing on the button gives it away.
    assert _check_btn(page).get_attribute("title") is None

    _check_btn(page).click(button="right", modifiers=["Alt"])
    page.wait_for_selector(".confirm-message >> text=Sync to develop build abc1234?")
    assert "Resizable sidebar (2026-08-29)" in page.locator(".confirm-message").inner_text()
    assert calls == [("check", {"dev": True})]

    # Kept to the sync itself: the restart it offers has tests of its own.
    page.locator(".confirm-check input").uncheck()
    page.locator(".confirm-actions .btn", has_text="Sync to abc1234").click()
    page.wait_for_selector("#modal >> text=Synced to develop build abc1234. Restart Winnow to run it.")
    assert calls[-1] == ("apply", {"confirm": True, "dev": True})


def test_cancelling_the_develop_sync_installs_nothing(page):
    calls = _stub_updates(page)
    _open_updates(page)
    _check_btn(page).click(button="right", modifiers=["Alt"])
    page.locator(".confirm-actions .btn", has_text="Cancel").click()
    # The status goes back in the same step that would otherwise apply.
    page.wait_for_function(
        """() => ![...document.querySelectorAll('#modal .fb-help')].some(
             (n) => n.textContent === 'Checking develop…')""")
    assert calls == [("check", {"dev": True})]
    assert _check_btn(page).is_enabled()


def test_plain_clicks_are_unchanged(page):
    """A right-click without Alt is the browser's, and a left click is the
    release check, still with no `dev` in it."""
    calls = _stub_updates(page)
    _open_updates(page)
    _check_btn(page).click(button="right")
    _check_btn(page).click()
    page.wait_for_selector("#modal >> text=is up to date")
    # A right-click handler that posted would have done so before the
    # left click did, so the release check being the only call proves it.
    assert calls == [("check", {})]


# ----------------------------------------------------- restarting into it
#
# An install offers to restart Winnow when the server can start itself
# again (/api/version's can_restart), ticked; Restart now stays on offer
# when it didn't. The real restart, windows reloading and all, is
# tests/ui/test_restart_ui.py's; here every call is answered in the
# browser, so the shared server never restarts.


def _install(page, *, keep_ticked=True):
    """Check, then Install 9.9.9 through its confirm. Returns the confirm's text."""
    _check_btn(page).click()
    page.locator(".btn", has_text="Install update").click()
    page.wait_for_selector(".confirm-overlay")
    text = page.locator(".confirm-message").inner_text()
    box = page.locator(".confirm-check input")
    if box.count() and not keep_ticked:
        box.uncheck()
    page.locator(".confirm-actions .btn", has_text="Install 9.9.9").click()
    return text


def test_installing_restarts_when_the_box_stays_ticked(page):
    stub = _Stub(page, available=True)
    _open_updates(page)
    text = _install(page)
    assert "With the box below ticked, Winnow restarts as soon as it’s installed" in text
    page.wait_for_selector("#restarting:not([hidden])")
    assert "Restarting Winnow" in page.locator("#restarting").inner_text()
    assert [c[0] for c in stub.calls] == ["check", "apply", "restart"]
    # The window reloads once the server answering is another run.
    with page.expect_navigation(timeout=15_000):
        stub.boot = "run-2"
    page.wait_for_selector("#restarting", state="hidden")


def test_the_box_is_ticked_by_default_and_unticking_leaves_restart_now(page):
    stub = _Stub(page, available=True)
    _open_updates(page)
    _check_btn(page).click()
    page.locator(".btn", has_text="Install update").click()
    assert page.locator(".confirm-check input").is_checked()
    assert page.locator(".confirm-check").inner_text() == "Restart Winnow when it’s installed"
    page.locator(".confirm-check input").uncheck()
    page.locator(".confirm-actions .btn", has_text="Install 9.9.9").click()
    page.wait_for_selector("#modal >> text=Updated 1.0.0 → 9.9.9. Restart Winnow to run it.")
    assert ("restart", {}) not in stub.calls

    restart = page.locator(".btn", has_text="Restart now")
    assert restart.is_visible()
    restart.click()
    page.wait_for_selector("#restarting:not([hidden])")
    assert stub.calls[-1] == ("restart", {})


def test_nothing_is_offered_when_the_server_cannot_restart(page):
    """Under anything but `python server.py` there is nothing to restart
    into: the confirm and the status say to restart Winnow, as before."""
    stub = _Stub(page, available=True, can_restart=False)
    _open_updates(page)
    text = _install(page)
    assert "Winnow has to be restarted afterwards to run the new version." in text
    page.wait_for_selector("#modal >> text=Updated 1.0.0 → 9.9.9. Restart Winnow to run it.")
    assert page.locator(".btn:visible", has_text="Restart now").count() == 0
    assert ("restart", {}) not in stub.calls


def test_a_refused_restart_says_why_and_leaves_restart_now(page):
    """The installed files don't start (a package the new version needs):
    the server refuses rather than stop for good, and the panel says what
    to do, with Restart now still there for after it's done."""
    why = ("The installed version of Winnow doesn't start (ModuleNotFoundError: No module "
           "named 'duckdb'). Run \"pip install -r requirements.txt\" in the Winnow folder, "
           "then restart Winnow.")
    _Stub(page, available=True, restart=(400, {"detail": why}))
    _open_updates(page)
    _install(page)
    page.wait_for_selector("#modal >> text=No module named 'duckdb'")
    assert page.locator(".btn", has_text="Restart now").is_enabled()
    assert page.locator("#restarting").is_hidden()
