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
    page.locator(".settings-section-head", has_text="Updates").click()
    page.locator(".btn", has_text="Check for updates").wait_for(state="visible")


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
    panel = page.locator(".settings-section-body").filter(has=page.locator(".btn", has_text="Check for updates"))
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
