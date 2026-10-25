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


def _stub_updates(page):
    """[(route, body)] for every update call the page makes, in order."""
    calls = []

    def answer(route):
        req = route.request
        name = req.url.split("/api/updates/", 1)[1]
        body = req.post_data_json or {}
        calls.append((name, body))
        if name == "check" and body.get("dev"):
            route.fulfill(json=_TIP)
        elif name == "check":
            route.fulfill(json={"current": "1.0.0", "latest": "1.0.0", "available": False,
                                "notes": "", "url": "", "published_at": "", "html_url": ""})
        elif name == "apply" and body.get("dev"):
            route.fulfill(json={"version": "1.0.0", "previous_version": "1.0.0",
                                "branch": "develop", "short": "abc1234",
                                "backup": "b", "restart_required": True})
        else:
            route.fulfill(status=500, json={"detail": f"unexpected {name} {body}"})

    page.route("**/api/updates/**", answer)
    return calls


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
