"""Markdown notes can link INTO the case: [label](winnow:table/N) renders
as an in-app link that opens the table from Preview, and the Link ▾ menu
writes those links so nobody hand-authors ids."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_winnow_link_renders_and_navigates(page):
    sid = page.evaluate("() => __winnow.S.sources.find((s) => !s.is_merge).id")
    page.locator("#tabNotes").click()
    page.wait_for_selector("#notesview:not([hidden])")
    page.locator("#notesEditor").fill(f"See [the logons](winnow:table/{sid}) for the sweep.\n"
                                      "External: [docs](https://example.com/x)")
    page.locator("#btnNotesPreview").click()
    link = page.locator("#notesPreview a.notes-link")
    assert link.count() == 1
    assert link.inner_text() == "the logons"
    # External links stay external (target=_blank), in-app ones don't.
    ext = page.locator("#notesPreview a[target=_blank]")
    assert ext.count() == 1
    link.click()
    page.wait_for_selector("#notesview", state="hidden")
    assert page.evaluate("() => __winnow.S.activeTab") == "grid"
    assert page.evaluate("() => __winnow.S.sourceId") == sid


def test_link_menu_inserts_at_cursor(page):
    page.locator("#tabNotes").click()
    page.wait_for_selector("#notesview:not([hidden])")
    page.locator("#notesEditor").fill("")
    page.locator("#btnNotesLink").click()
    item = page.locator(".menu .menu-item", has_text="Table:").first
    item.click()
    val = page.locator("#notesEditor").input_value()
    assert "](winnow:table/" in val and val.startswith("[")
    # A dead link degrades to a toast, not a crash.
    page.locator("#notesEditor").fill("[gone](winnow:table/99999)")
    page.locator("#btnNotesPreview").click()
    page.locator("#notesPreview a.notes-link").click()
    page.wait_for_selector(".toast, #toast", state="attached")
    page.locator("#btnNotesEdit").click()
    page.locator("#notesEditor").fill("")
