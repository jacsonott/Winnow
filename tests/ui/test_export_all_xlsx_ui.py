"""Export's Every row: every table to one workbook. On a small case there's
nothing to warn about, so the main button is the download."""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.ui


def test_the_button_downloads_a_workbook(page):
    page.evaluate("() => __winnow.openExportModal('all')")
    page.wait_for_selector("#modal:not([hidden]) .export-pane[data-section=all]")
    hint = page.locator("#modalBody .side-rail-item[data-section=all] .side-rail-hint").inner_text()
    assert re.fullmatch(r"[\d,]+ rows · \d+ sheets?", hint), hint
    btn = page.locator("#modal .table-menu-foot .btn.primary")
    assert re.fullmatch(r"Export \d+ tables? \(\.xlsx\)", btn.inner_text()), btn.inner_text()
    with page.expect_download(timeout=15_000) as dl:
        btn.click()
    download = dl.value
    assert download.suggested_filename == "all-tables.xlsx"
    path = download.path()
    assert path is not None
    with open(path, "rb") as fh:
        assert fh.read(2) == b"PK"
    assert page.locator("#modal").is_hidden()
    assert page.locator(".confirm-overlay").count() == 0, "200 rows is nowhere near the cap — no warning"
