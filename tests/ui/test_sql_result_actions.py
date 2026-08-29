"""The SQL result bar's three actions: Copy, CSV…, Save as table.

Copy and the CSV name prompt are new; the bar itself was rebuilt because
the buttons used to be appended straight after the status text, with no
grouping, so they collided with it and read as part of the sentence."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _run(page, sql):
    page.click("#tabSql")
    page.wait_for_selector("#sqlview:not([hidden])")
    page.wait_for_function(
        "() => __winnow.S.sqlTabs.length > 0 && !document.getElementById('sqlText').disabled")
    page.locator("#sqlText").fill(sql)
    page.click("#btnRunSql")
    page.wait_for_selector("#sqlResult table")


def test_actions_are_grouped_to_the_right_of_the_status(page):
    _run(page, "SELECT rid, EventId FROM src_1 ORDER BY rid LIMIT 5")
    bar = page.locator(".sql-result-bar")
    status = bar.locator(".note-status").bounding_box()
    acts = bar.locator(".sql-result-actions").bounding_box()
    # The regression this replaces: buttons butted up against the text.
    assert acts["x"] > status["x"] + status["width"], "actions must not overlap the status text"
    assert [b.strip() for b in page.locator(".sql-result-actions .btn").all_inner_texts()] \
        == ["Copy", "CSV…", "Save as table…"]


def test_copy_takes_the_whole_result_with_headers(page):
    _run(page, "SELECT rid, EventId FROM src_1 ORDER BY rid LIMIT 5")
    page.locator(".sql-result-actions .btn", has_text="Copy").click()
    page.wait_for_timeout(200)
    clip = page.evaluate("() => navigator.clipboard.readText()")
    lines = clip.split("\n")
    assert lines[0] == "rid\tEventId", "a header row is the point of the feature"
    assert len(lines) == 6  # header + 5 rows


def test_copy_prefers_the_selection_when_there_is_one(page):
    _run(page, "SELECT rid, EventId FROM src_1 ORDER BY rid LIMIT 10")
    rows = page.locator("#sqlResult tr")
    rows.nth(1).click()
    rows.nth(3).click(modifiers=["Control"])
    page.wait_for_timeout(150)
    page.locator(".sql-result-actions .btn", has_text="Copy").click()
    page.wait_for_timeout(200)
    lines = page.evaluate("() => navigator.clipboard.readText()").split("\n")
    assert lines[0] == "rid\tEventId"
    assert len(lines) == 3, "header + the two selected rows only"
    page.keyboard.press("Escape")


def test_csv_prompts_with_the_tab_name_as_the_default(page):
    _run(page, "SELECT rid FROM src_1 LIMIT 3")
    page.locator(".sql-result-actions .btn", has_text="CSV").click()
    page.wait_for_selector(".confirm-overlay input")
    # Read the tab's name rather than hard-coding "Query 1": SQL tabs live
    # in the case file and the UI suite shares one, so a rename in another
    # module carries over. The contract is "whatever this tab is called",
    # not one particular string — and definitely not "query-results.csv",
    # which made every tab's export collide in the downloads folder.
    tab_name = page.evaluate("() => __winnow.activeSqlTab().name")
    assert page.locator(".confirm-overlay input").input_value() == tab_name
    page.locator(".confirm-card .btn", has_text="Cancel").click()
    page.wait_for_selector(".confirm-overlay", state="detached")


def test_csv_download_uses_the_name_given(page):
    _run(page, "SELECT rid FROM src_1 LIMIT 3")
    page.locator(".sql-result-actions .btn", has_text="CSV").click()
    page.wait_for_selector(".confirm-overlay input")
    page.locator(".confirm-overlay input").fill("logon sweep")
    with page.expect_download() as dl:
        page.locator(".confirm-card .btn", has_text="Save").first.click()
    assert dl.value.suggested_filename == "logon sweep.csv"
