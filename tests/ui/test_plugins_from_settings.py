"""The way back out of the plugins manager.

The modal is a singleton (`#modalBody`), so Settings → Plugins → "Manage
plugins…" REPLACES Settings rather than stacking on it — and the ✕ then
hid the whole thing, dropping the analyst on the grid. One level up, not
back where they came from, which for a dialog reached through two clicks
of another dialog is a dead end.

Profiles and Saved filters already hand a `returnTo` in for exactly this
(bundles.js, timeframe.js); these pin that the plugins manager does the
same — and that an opening from anywhere else does NOT inherit it, since
the listener behind it is armed per opening.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open_settings_plugins(page):
    page.evaluate("() => __winnow.openSettings()")
    page.wait_for_selector("#modal:not([hidden])")
    page.evaluate("""() => {
      const heads = [...document.querySelectorAll('#modal .settings-section-head')];
      const h = heads.find((x) => x.querySelector('.settings-section-title').textContent.trim() === 'Plugins');
      if (h && h.getAttribute('aria-expanded') !== 'true') h.click();
    }""")
    page.wait_for_selector("#modalBody .settings-line", state="attached")


def _click_manage(page):
    page.evaluate("""() => {
      const btn = [...document.querySelectorAll('#modalBody .settings-line button')]
        .find((b) => b.textContent.startsWith('Manage plugins'));
      btn.click();
    }""")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Plugins'")
    # The detail pane — and the way back in its foot — paint with the body,
    # but the profiles fetch behind "Used by" repaints it; wait for the foot
    # itself rather than for either race to settle.
    page.wait_for_selector("#modalBody .pm-foot", state="attached")


def _title(page):
    return page.evaluate("() => document.getElementById('modalTitle').textContent")


def _has_way_back(page):
    return page.evaluate("() => !!document.querySelector('#modalBody .pm-foot .pm-mini')")


@pytest.fixture(autouse=True)
def _close(page):
    yield
    page.keyboard.press("Escape")
    page.evaluate("() => { document.getElementById('modal').hidden = true; }")


def test_settings_opens_the_manager_with_a_way_back(page):
    _open_settings_plugins(page)
    _click_manage(page)
    assert _has_way_back(page), "no back button in the manager's foot"


def test_the_close_button_goes_back_to_settings(page):
    """The ✕, specifically: it is the control the analyst reaches for, and
    the one that used to hide the dialog stack whole."""
    _open_settings_plugins(page)
    _click_manage(page)
    page.evaluate("() => document.getElementById('modalClose').click()")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Settings'",
                           timeout=5_000)
    assert page.evaluate("() => document.getElementById('modal').hidden") is False


def test_escape_goes_back_to_settings_too(page):
    _open_settings_plugins(page)
    _click_manage(page)
    page.keyboard.press("Escape")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Settings'",
                           timeout=5_000)
    assert page.evaluate("() => document.getElementById('modal').hidden") is False


def test_the_back_button_goes_back_too(page):
    _open_settings_plugins(page)
    _click_manage(page)
    page.evaluate("() => document.querySelector('#modalBody .pm-foot .pm-mini').click()")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Settings'",
                           timeout=5_000)


def test_opened_on_its_own_it_has_no_way_back(page):
    """The one-shot listener is armed per opening. A manager opened any
    other way must not inherit a return armed by an earlier one — it would
    reopen Settings over a dialog nobody came from there."""
    _open_settings_plugins(page)
    _click_manage(page)
    page.keyboard.press("Escape")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Settings'")
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")

    page.evaluate("() => __winnow.openPluginManager()")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Plugins'")
    page.wait_for_selector("#modalBody .pm-foot", state="attached")
    assert _has_way_back(page) is False
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached", timeout=5_000)
