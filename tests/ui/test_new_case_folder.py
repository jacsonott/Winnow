"""Creating a folder from the case-creation flow, and the CSV control
being gone from it."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _open_new_case(page):
    page.evaluate("() => { __winnow.showHome(); return __winnow.refreshCases(); }")
    page.wait_for_selector("#home:not([hidden]) .home-head")
    page.locator("#home .btn", has_text="New case").click()
    page.wait_for_selector("#modal:not([hidden])")


def _back_to_app(page):
    page.keyboard.press("Escape")
    page.evaluate("() => __winnow.showApp()")
    page.evaluate("(id) => __winnow.openSource(id)", page.evaluate("() => __winnow.S.sources[0].id"))
    page.wait_for_function("() => __winnow.S.view")


def test_creating_a_case_no_longer_offers_a_csv(page):
    """Importing is its own flow with its own preview and options; offering
    a one-shot CSV here was a second, weaker door into it."""
    try:
        _open_new_case(page)
        body = page.locator("#modalBody").inner_text().lower()
        assert "csv" not in body, body
        assert page.locator("#modalBody input[type=file]").count() == 0
    finally:
        _back_to_app(page)


def test_the_folder_picker_can_make_a_folder(page, tmp_path):
    try:
        _open_new_case(page)
        page.locator("#modalBody .btn", has_text="Browse").click()
        page.wait_for_selector("#modalBody .btn:has-text('New folder')")

        # Point the browser at a directory this test owns.
        path_input = page.locator("#modalBody input").first
        path_input.fill(str(tmp_path))
        path_input.press("Enter")
        page.wait_for_function("(p) => document.querySelector('#modalBody input').value === p",
                               arg=str(tmp_path))

        page.locator("#modalBody .btn", has_text="New folder").click()
        page.wait_for_selector(".confirm-overlay input")
        page.locator(".confirm-overlay input").fill("Intrusion 2026")
        page.locator(".confirm-card .btn", has_text="OK").first.click()

        # Created on disk, and the picker stepped into it — which is where
        # the analyst was going.
        made = tmp_path / "Intrusion 2026"
        page.wait_for_function("(p) => document.querySelector('#modalBody input').value === p",
                               arg=str(made), timeout=10_000)
        assert made.is_dir()
    finally:
        _back_to_app(page)


def test_a_duplicate_folder_name_is_reported_not_swallowed(page, tmp_path):
    (tmp_path / "already").mkdir()
    try:
        _open_new_case(page)
        page.locator("#modalBody .btn", has_text="Browse").click()
        page.wait_for_selector("#modalBody .btn:has-text('New folder')")
        path_input = page.locator("#modalBody input").first
        path_input.fill(str(tmp_path))
        path_input.press("Enter")
        page.wait_for_function("(p) => document.querySelector('#modalBody input').value === p",
                               arg=str(tmp_path))

        page.locator("#modalBody .btn", has_text="New folder").click()
        page.wait_for_selector(".confirm-overlay input")
        page.locator(".confirm-overlay input").fill("already")
        page.locator(".confirm-card .btn", has_text="OK").first.click()

        page.wait_for_selector("#toast:not([hidden])", timeout=8_000)
        assert "already exists" in page.locator("#toast").inner_text()
    finally:
        _back_to_app(page)
