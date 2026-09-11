"""Import UX: the queue modal's Import-all button sits on the right and
says so when nothing is queued; the folder modal's Save-as-profile lives
on the bottom row; the file picker can filter and sort (closes #211)."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.ui


def test_import_all_is_right_aligned_and_complains_about_an_empty_queue(page):
    page.evaluate("() => { __winnow.S.importQueue = []; __winnow.openImportModal(); }")
    page.wait_for_selector("#modal:not([hidden])")
    btn = page.locator("#modalBody .btn", has_text="Import all queued")
    folder = page.locator("#modalBody .btn", has_text="Import a whole folder…")
    bb, fb = btn.bounding_box(), folder.bounding_box()
    row = btn.evaluate("(n) => n.parentElement.getBoundingClientRect().right")
    assert bb["x"] > fb["x"] + fb["width"] + 40, "pushed away from the folder button"
    assert row - (bb["x"] + bb["width"]) < 4, "flush with the row's right edge"
    btn.click()
    page.wait_for_selector(".toast", state="visible")
    assert "No files queued" in page.locator(".toast").last.inner_text()
    assert page.locator("#modal").is_visible(), "an empty click keeps the modal open"
    page.keyboard.press("Escape")


def test_save_as_profile_sits_on_the_bottom_row(page):
    page.evaluate("() => __winnow.openDirectoryImportModal()")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Import a folder'")
    page.wait_for_selector("#modalBody .btn:has-text('Save as profile…')")
    rows = page.locator("#modalBody .row-actions")
    last = rows.last
    labels = last.locator(".btn").all_inner_texts()
    assert labels[0] == "Save as profile…" and labels[1].startswith("Import checked") and labels[2] == "Cancel", labels
    # Only one Save button, and it's in that row — not in a row of its own above the results.
    assert page.locator("#modalBody .btn", has_text="Save as profile…").count() == 1
    save = last.locator(".btn", has_text="Save as profile…").bounding_box()
    imp = last.locator(".btn", has_text="Import checked").bounding_box()
    assert save["x"] < imp["x"], "profile on the left, import on the right"
    page.keyboard.press("Escape")


def test_the_file_picker_filters_and_sorts(page, tmp_path):
    big = tmp_path / "b_big.csv"
    small = tmp_path / "a_small.csv"
    old = tmp_path / "c_old.txt"
    big.write_text("x" * 5000)
    small.write_text("x" * 10)
    old.write_text("x" * 100)
    os.utime(old, (1_600_000_000, 1_600_000_000))
    os.utime(small, (1_700_000_000, 1_700_000_000))
    os.utime(big, (1_650_000_000, 1_650_000_000))
    page.evaluate("() => localStorage.removeItem('winnow.browse.sort')")
    page.evaluate("(p) => __winnow.openFolderBrowser(p, () => {}, () => { document.getElementById('modal').hidden = true; }, { mode: 'files' })", str(tmp_path))
    page.wait_for_selector("#modalBody .browse-row .session-name")
    names = lambda: page.locator("#modalBody .browse-row:visible .session-name").all_inner_texts()  # noqa: E731
    assert names() == ["a_small.csv", "b_big.csv", "c_old.txt"]
    # A modified column, per file.
    assert page.locator("#modalBody .browse-row .browse-mtime").first.inner_text().startswith("20")

    page.locator("#modalBody .browse-sort").select_option("size")
    assert names() == ["a_small.csv", "c_old.txt", "b_big.csv"]
    page.locator("#modalBody .browse-sort-dir").click()
    assert names() == ["b_big.csv", "c_old.txt", "a_small.csv"]
    page.locator("#modalBody .browse-sort").select_option("mtime")
    assert names() == ["a_small.csv", "b_big.csv", "c_old.txt"], "newest first, descending"

    # A selection survives re-sorting and filtering.
    page.locator("#modalBody .browse-row", has_text="c_old.txt").locator("input").check()
    page.locator("#modalBody .browse-filter").fill("csv")
    assert names() == ["a_small.csv", "b_big.csv"]
    assert "Add 1 file" in page.locator("#modalBody .btn", has_text="Add").first.inner_text()
    page.locator("#modalBody .browse-filter").fill("zzz")
    assert page.locator("#modalBody .note-status", has_text="No files here match").count() == 1
    page.locator("#modalBody .browse-filter").fill("")
    assert len(names()) == 3
    # The sort is remembered for the next opening.
    assert page.evaluate("() => JSON.parse(localStorage.getItem('winnow.browse.sort'))") == {"by": "mtime", "dir": "desc"}
    page.locator("#modalBody .btn", has_text="Cancel").click()
    page.wait_for_selector("#modal", state="hidden")
    page.evaluate("() => localStorage.removeItem('winnow.browse.sort')")
