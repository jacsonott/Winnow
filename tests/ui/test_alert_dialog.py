"""alertDialog — the one-button themed replacement for window.alert()."""
import pytest

pytestmark = pytest.mark.ui


def test_one_button_resolves_on_click(page):
    page.evaluate("() => { window.__done = false; __winnow.alertDialog('Service unreachable.', { okLabel: 'Got it' }).then(() => { window.__done = true; }); }")
    page.wait_for_selector(".confirm-overlay")
    assert page.locator(".confirm-card .btn").count() == 1
    assert page.locator(".confirm-card .confirm-message").inner_text() == "Service unreachable."
    page.locator(".confirm-card .btn", has_text="Got it").click()
    page.wait_for_function("() => window.__done === true")
    assert page.locator(".confirm-overlay").count() == 0


def test_escape_resolves_too(page):
    page.evaluate("() => { window.__done = false; __winnow.alertDialog('x').then(() => { window.__done = true; }); }")
    page.wait_for_selector(".confirm-overlay")
    page.keyboard.press("Escape")
    page.wait_for_function("() => window.__done === true")
    assert page.locator(".confirm-overlay").count() == 0


def test_stacks_above_an_open_modal(page):
    page.evaluate("() => { __winnow.modal('Behind', (b) => { b.textContent = 'modal body'; }); __winnow.alertDialog('on top'); }")
    page.wait_for_selector(".confirm-overlay")
    z_modal = page.evaluate("() => Number(getComputedStyle(document.getElementById('modal')).zIndex)")
    z_alert = page.evaluate("() => Number(getComputedStyle(document.querySelector('.confirm-overlay')).zIndex)")
    assert z_alert > z_modal
    page.locator(".confirm-card .btn").click()
    assert page.locator(".confirm-overlay").count() == 0
    assert page.evaluate("() => document.getElementById('modal').hidden") is False
    page.evaluate("() => __winnow.closeModal()")
