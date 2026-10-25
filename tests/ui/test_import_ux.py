"""Import UX: the Import dialog's import button sits on the right and
waits for a file; the folder modal's Save-as-profile lives on the bottom
row; the file picker can filter and sort (closes #211)."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.ui


def test_the_import_button_is_right_aligned_and_waits_for_a_file(page):
    page.evaluate("() => { __winnow.S.importQueue = []; __winnow.openImportModal(); }")
    page.wait_for_selector("#modal:not([hidden])")
    btn = page.locator("#modalBody .import-foot .btn.primary")
    folder = page.locator("#modalBody .btn", has_text="Import a whole folder…")
    bb, fb = btn.bounding_box(), folder.bounding_box()
    row = btn.evaluate("(n) => n.parentElement.getBoundingClientRect().right")
    assert bb["x"] > fb["x"] + fb["width"] + 40, "pushed away from the folder button"
    assert row - (bb["x"] + bb["width"]) < 4, "flush with the row's right edge"
    # Nothing to import yet: the button can't be pressed, and the pane
    # says how to give it something.
    assert btn.is_disabled()
    assert "Drop files here" in page.locator("#modalBody .import-empty").inner_text()
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
    page.wait_for_selector("#modalBody .browse-row-file .tables-name")
    names = lambda: page.locator("#modalBody .browse-row-file:visible .tables-name").all_inner_texts()  # noqa: E731
    assert names() == ["a_small.csv", "b_big.csv", "c_old.txt"]
    # A modified column, per file.
    assert page.locator("#modalBody .browse-row-file .browse-mtime").first.inner_text().startswith("20")

    # A sort change re-asks the server (the cap is cut in that order), so
    # each one waits for the WHOLE order to land — never for the first row
    # alone. a_small.csv leads under name-ascending and under
    # size-ascending both, so "the first row is a_small" was already true
    # of the list being replaced, and that wait returned without waiting.
    # It cost a full-suite run on a 2-CPU box, where the server was slow
    # enough for the assertion under it to read the previous listing, and
    # the module failed for a reason that had nothing to do with sorting.
    # The filter below needs no wait for the mirror-image reason: it is
    # `paint()` over the listing already in hand, not another `load()`.
    def order_is(*expected):
        page.wait_for_function(
            "(want) => [...document.querySelectorAll('#modalBody .browse-row-file .tables-name')]"
            ".map((n) => n.textContent).join() === want",
            arg=",".join(expected), timeout=10_000)

    # The column heads are the sort: a new column starts largest or newest
    # first, and its head again turns it round.
    sort_by = lambda key: page.locator(f"#modalBody .browse-head .tables-sort[data-sort={key}]")  # noqa: E731
    sort_by("size").click()
    order_is("b_big.csv", "c_old.txt", "a_small.csv")
    assert names() == ["b_big.csv", "c_old.txt", "a_small.csv"]
    assert sort_by("size").get_attribute("aria-pressed") == "true"
    sort_by("size").click()
    order_is("a_small.csv", "c_old.txt", "b_big.csv")
    assert names() == ["a_small.csv", "c_old.txt", "b_big.csv"]
    sort_by("mtime").click()
    order_is("a_small.csv", "b_big.csv", "c_old.txt")
    assert names() == ["a_small.csv", "b_big.csv", "c_old.txt"], "newest first, descending"

    # A selection survives re-sorting and filtering.
    page.locator("#modalBody .browse-row-file[data-id='c_old.txt'] input").check()
    filter_box = page.locator("#modalBody input[aria-label='Filter this folder']")
    filter_box.fill("csv")
    assert names() == ["a_small.csv", "b_big.csv"]
    assert page.locator("#modal .table-menu-foot .btn.primary").inner_text() == "Add 1 file"
    filter_box.fill("zzz")
    assert page.locator("#modalBody .tables-empty", has_text="Nothing here matches").count() == 1
    filter_box.fill("")
    assert len(names()) == 3
    # The sort is remembered for the next opening.
    assert page.evaluate("() => JSON.parse(localStorage.getItem('winnow.browse.sort'))") == {"by": "mtime", "dir": "desc"}
    page.locator("#modal .table-menu-foot .btn", has_text="Cancel").click()
    page.wait_for_selector("#modal", state="hidden")
    page.evaluate("() => localStorage.removeItem('winnow.browse.sort')")


# One file name per kind the import queue routes to, and the word an
# analyst holding that file would look for on the screen.
IMPORT_KINDS = {"logons.csv": "CSV", "events.jsonl": "JSON", "History.db": "SQLite",
                "Book1.xlsx": "Excel", "run.plaso": "Plaso", "bundle.tgz": "archive",
                "auth.log.1": "text"}


def test_the_drop_overlay_and_import_intro_name_everything_that_imports(page):
    """The drop overlay said "CSV/TSV/JSON, or a single SQLite file" and
    the Import window left out Plaso, archives and raw text, so an
    analyst holding a .plaso or a support bundle was told it would not
    take one. Both list every kind the queue routes a file to: the
    Import window where its empty queue's pane says what to drop."""
    kinds = page.evaluate("(names) => names.map((n) => __winnow.importKindFor(n))", list(IMPORT_KINDS))
    assert kinds == ["csv", "json", "sqlite", "xlsx", "plaso", "archive", "text"]
    drag = """(type) => {
      const dt = new DataTransfer();
      dt.items.add(new File(['x'], 'run.plaso'));
      window.dispatchEvent(new DragEvent(type, { dataTransfer: dt }));
    }"""
    page.evaluate(drag, "dragenter")
    page.wait_for_selector("#dropOverlay:not([hidden])")
    overlay = page.locator("#dropOverlay").inner_text().lower()
    page.evaluate(drag, "dragleave")
    page.wait_for_selector("#dropOverlay", state="hidden")
    page.evaluate("() => { __winnow.S.importQueue = []; __winnow.openImportModal(); }")
    page.wait_for_selector("#modal:not([hidden])")
    intro = page.locator("#modalBody .import-empty").inner_text().lower()
    page.keyboard.press("Escape")
    for word in IMPORT_KINDS.values():
        assert word.lower() in overlay, (word, overlay)
        assert word.lower() in intro, (word, intro)
