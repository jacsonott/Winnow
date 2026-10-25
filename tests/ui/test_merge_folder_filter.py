"""The merge builder's folder picker: once tables are filed in sidebar
folders, the builder's table list can be narrowed to one folder, which
takes its subfolders along, and each table says which folder it is in.

The case it exists for is a directory import of several hosts' output:
every host has the same files, the builder groups them all together by
header set, and the analyst wants one host's tables out of that group."""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.ui

HEADER = "TimeCreated,EventId,Computer,Payload"
NAMES = ("fold_ws01_sec.csv", "fold_ws01_sys.csv", "fold_dc01_sec.csv")


def _wait_for_sources(page, names, timeout=25.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        have = page.evaluate(
            "() => __winnow.loadSources().then(() => __winnow.S.sources.map((s) => s.name))")
        if all(n in have for n in names):
            return
        page.wait_for_timeout(250)
    pytest.fail(f"{names} never all appeared in S.sources")


def _listed(page):
    return page.eval_on_selector_all(
        "#modal .mb-trow .mb-tname", "els => els.map((e) => e.textContent)")


def _open_builder(page):
    page.evaluate("() => __winnow.openMergeBuilder()")
    page.wait_for_selector("#modal .mb")


def _close_builder(page):
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_folder_picker_narrows_the_list_to_one_folder_and_its_subfolders(page, api, tmp_path):
    for n in NAMES:
        f = tmp_path / n
        f.write_text(HEADER + f"\n2026-03-15 09:00:00,4624,{n[5:9].upper()},x\n", encoding="utf-8")
        api("/api/ingest/jobs/path", "POST", {"path": str(f), "name": n, "kind": "csv"})
    page.evaluate("() => __winnow.startJobsPoll()")
    _wait_for_sources(page, NAMES)
    ids = page.evaluate(
        "(ns) => Object.fromEntries(__winnow.S.sources.filter((s) => ns.includes(s.name)).map((s) => [s.name, s.id]))",
        list(NAMES))
    folders = []
    try:
        # With no folders, every table is in one place: no picker to show.
        _open_builder(page)
        assert page.locator("#modal .mb-folder").count() == 0
        _close_builder(page)

        ws01 = api("/api/folders", "POST", {"name": "WS01"})
        logs = api("/api/folders", "POST", {"name": "EventLogs", "parent_id": ws01["id"]})
        dc01 = api("/api/folders", "POST", {"name": "DC01"})
        folders = [logs["id"], ws01["id"], dc01["id"]]
        api(f"/api/source/{ids['fold_ws01_sec.csv']}/folder", "POST", {"folder_id": logs["id"]})
        api(f"/api/source/{ids['fold_ws01_sys.csv']}/folder", "POST", {"folder_id": ws01["id"]})
        api(f"/api/source/{ids['fold_dc01_sec.csv']}/folder", "POST", {"folder_id": dc01["id"]})
        page.evaluate("() => __winnow.loadSources()")

        _open_builder(page)
        pick = page.locator("#modal select.mb-folder")
        options = pick.locator("option").all_inner_texts()
        assert options[:4] == ["All folders", "WS01 (2)", "WS01 / EventLogs (1)", "DC01 (1)"], options
        assert options[4].startswith("Not in a folder ("), options
        for n in NAMES:
            assert n in _listed(page), f"{n} missing under All folders"

        # Each table names its folder, by its full path.
        sub = page.locator("#modal label.mb-trow", has_text="fold_ws01_sec.csv").locator(".mb-tsub")
        assert sub.text_content().endswith("WS01 / EventLogs"), sub.text_content()

        # WS01 takes EventLogs along with it, and leaves DC01 and the
        # unfiled tables out.
        pick.select_option(label="WS01 (2)")
        page.wait_for_function(
            "() => document.querySelectorAll('#modal .mb-trow').length === 2")
        assert sorted(_listed(page)) == ["fold_ws01_sec.csv", "fold_ws01_sys.csv"]

        # Select all ticks only what the folder shows: the point of the
        # picker is getting one host's tables out of a shared group.
        page.locator("#modal .mb-group", has_text="fold_ws01_sec.csv").locator(
            "button", has_text="Select all").click()
        pick.select_option(label="All folders")
        page.wait_for_function(
            "() => document.querySelectorAll('#modal .mb-trow').length > 3")
        ticked = page.eval_on_selector_all(
            "#modal .mb-trow", """rows => rows.filter((r) => r.querySelector('input').checked)
                                     .map((r) => r.querySelector('.mb-tname').textContent)""")
        assert sorted(ticked) == ["fold_ws01_sec.csv", "fold_ws01_sys.csv"]

        # Not in a folder: none of the filed tables.
        pick.select_option(index=4)
        page.wait_for_function(
            "() => !document.querySelector('#modal .mb-trow .mb-tname')?.textContent.startsWith('fold_')")
        listed = _listed(page)
        assert listed and not [n for n in listed if n in NAMES], listed
        _close_builder(page)
    finally:
        for fid in folders:
            api(f"/api/folders/{fid}", "DELETE")
        for n in NAMES:
            api(f"/api/source/{ids[n]}", "DELETE")
        page.evaluate("() => { __winnow.S.sourceId = null; return __winnow.loadSources(); }")
