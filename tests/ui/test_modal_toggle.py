"""Keybind-opened dialogs toggle: the key that opened it closes it."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


@pytest.mark.parametrize("key", ["C", "e", "?", "t"])
def test_the_opening_keybind_also_closes(page, key):
    page.keyboard.press(key)
    page.wait_for_selector("#modal:not([hidden])")
    page.keyboard.press(key)
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_toggle_defers_to_a_focused_input(page):
    """The jump modal autofocuses its timestamp box — J must TYPE there,
    not close the dialog out from under the caret. Blur first and the
    toggle works."""
    page.keyboard.press("J")
    page.wait_for_selector("#modal:not([hidden])")
    page.keyboard.press("J")
    page.wait_for_timeout(100)
    assert not page.locator("#modal").is_hidden()
    assert "J" in page.evaluate("() => document.activeElement.value || ''")
    page.evaluate("() => document.activeElement.blur()")
    page.keyboard.press("J")
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_the_caret_is_in_the_box_before_the_next_keystroke_can_arrive(page):
    """The flake behind the test above, made deterministic.

    The input used to be focused in a setTimeout(0), while modal() unhides
    synchronously — so there was a window where the dialog was on screen
    with the caret still on <body>. A keystroke landing in it went to the
    global keymap, and since J is what opened the dialog, J closed it again
    out from under someone already typing. On an idle machine the window
    closes before Playwright can send the second key, which is why this only
    ever failed in full-suite runs.

    Opening and typing inside ONE task removes the timing: no macrotask can
    run between them, so a deferred focus has not fired yet. The keystroke
    is dispatched from the focused element, which is where the browser sends
    a real one (keymap.js reads e.target)."""
    out = page.evaluate("""() => {
        __winnow.openJumpTsModal();
        const focused = document.activeElement;
        focused.dispatchEvent(new KeyboardEvent('keydown', { key: 'J', bubbles: true }));
        return { tag: focused.tagName, hidden: document.getElementById('modal').hidden };
    }""")
    assert out["tag"] == "INPUT", "the caret was not in the box when the key arrived"
    assert out["hidden"] is False, "the keystroke closed the dialog instead of typing into it"
    page.keyboard.press("Escape")


def test_a_different_keybind_does_not_close_someone_elses_dialog(page):
    page.keyboard.press("C")  # table menu
    page.wait_for_selector("#modal:not([hidden])")
    page.keyboard.press("e")  # the builder's key — must stay inert, not swap dialogs
    page.wait_for_timeout(150)
    assert not page.locator("#modal").is_hidden()
    assert page.locator("#modalTitle").inner_text().lower().startswith("table")
    page.keyboard.press("Escape")


def test_value_picker_rows_show_what_auto_resolves_to(page):
    page.keyboard.press("C")
    page.wait_for_selector(".collist-pick")
    # the ui fixture is small, so table-level auto means ON
    assert page.locator(".collist-pick").first.inner_text() == "▾ auto·on"
    page.keyboard.press("Escape")
