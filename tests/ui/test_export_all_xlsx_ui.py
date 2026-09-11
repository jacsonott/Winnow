"""The Export modal's fourth button: every table to one workbook. On a
small case there's nothing to warn about, so the click is the download."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_the_button_downloads_a_workbook(page):
    page.evaluate("() => __winnow.openExportModal()")
    page.wait_for_selector("#modal:not([hidden])")
    btn = page.locator("#modalBody .btn", has_text="Export all tables (.xlsx)")
    assert btn.count() == 1
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
