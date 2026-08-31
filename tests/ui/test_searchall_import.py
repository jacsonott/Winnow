"""Search all → Import terms from file: an IOC list usually exists as a
file already; reading it in beats retyping. One term per line, blanks
and #-comments dropped, duplicates skipped against what's typed."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_term_file_import_fills_the_paste_pane(page, tmp_path):
    f = tmp_path / "iocs.txt"
    f.write_text(
        "# C2 hosts from the 2026-08 feed\n"
        "evil.example.com\n"
        "10.66.6.6\n"
        "\n"
        "deadbeefcafe\n"
        "10.66.6.6\n",           # duplicate inside the file
        encoding="utf-8")
    page.evaluate("() => __winnow.openSearchAllModal()")
    page.wait_for_selector("#modal:not([hidden])")
    page.locator("#modal .search-all-paste").fill("already-here")
    page.locator("#modal input[type=file]").set_input_files(str(f))
    page.wait_for_function(
        "() => document.querySelector('#modal .search-all-paste').value.includes('deadbeefcafe')")
    lines = page.locator("#modal .search-all-paste").input_value().split("\n")
    assert lines == ["already-here", "evil.example.com", "10.66.6.6", "deadbeefcafe"]

    # Re-importing the same file adds nothing (dedupe against the pane).
    page.locator("#modal input[type=file]").set_input_files(str(f))
    page.wait_for_timeout(300)
    assert page.locator("#modal .search-all-paste").input_value().split("\n") == lines

    # And the terms feed the search exactly as if they had been typed.
    terms = page.evaluate("() => __winnow.searchAllTerms(__winnow.searchAllState())")
    assert [t["term"] for t in terms] == lines
    page.keyboard.press("Escape")
    page.evaluate("() => { __winnow.S.searchAll = null; }")   # leave no paste text behind
