"""A delimiter the analyst types, for a log no reader claims (the backend
half is tests/test_custom_delimiter.py).

A log nothing recognizes queues one line per row. Its pane has a way
straight to columns — "Split each line on a delimiter…" — which reads it
as Delimited on Custom, with the box to type in focused and the header
off. Custom sits beside the presets for any file, keeps what was typed in
it when another preset is tried, and starts from what the file is split
on now. Folder import has the same box for its "other text files", and a
profile keeps it.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

LOG = ("2026-11-02 08:14:03 | INFO | auth | session opened for alice\n"
       '2026-11-02 08:14:09 | WARN | auth | failed password for "admin\n'
       "2026-11-02 08:14:11 | WARN | auth | failed again | attempt 2\n")
FOOT = "#modalBody .import-preview .side-preview-foot"
CUSTOM = "#modalBody .import-delims + input.import-delim-custom"


def _queue(page, *paths):
    page.evaluate("""async (paths) => {
      __winnow.S.importQueue = [];
      await __winnow.queuePaths(paths.map((p) => ({ path: p, name: p.split('/').pop() })));
      __winnow.openImportModal();
    }""", [str(p) for p in paths])
    page.wait_for_selector("#modal:not([hidden]) .import-pane .preview-tbl")


def _hint(page):
    return page.locator('#modalBody .side-rail-item[aria-selected="true"] .side-rail-hint').inner_text()


def _foot_says(page, text):
    page.wait_for_function("([sel, t]) => document.querySelector(sel)?.textContent.includes(t)", arg=[FOOT, text])


def _heads(page):
    return page.evaluate("() => [...document.querySelectorAll('#modalBody .preview-colname')].map((n) => n.textContent)")


@pytest.fixture
def fake_jobs(page):
    """Answers the import requests instead of the server, keeping what each
    one asked for, so nothing is left in the case the modules share."""
    sent = []

    def answer(route):
        sent.append(route.request.post_data_json)
        route.fulfill(status=200, content_type="application/json", body="{}")
    page.route("**/api/ingest/jobs/path", answer)
    yield sent
    page.unroute("**/api/ingest/jobs/path")
    page.evaluate("() => { __winnow.closeModal(); __winnow.S.importQueue = []; }")


def test_a_log_splits_on_what_is_typed_and_imports_that_way(page, tmp_path, api):
    log = tmp_path / "custom-delim.log"
    log.write_text(LOG)
    try:
        _queue(page, log)
        assert _hint(page) == "Text · one line per row"
        page.locator("#modalBody .import-split").click()
        # Delimited, on Custom, with the caret in its box and no header.
        page.wait_for_selector(CUSTOM)
        assert page.locator("#modalBody .import-readas button[aria-pressed=true]").inner_text() == "Delimited"
        assert page.locator("#modalBody .import-delims button[aria-pressed=true]").get_attribute("data-key") == "custom"
        assert page.evaluate("() => document.activeElement.classList.contains('import-delim-custom')")
        assert not page.locator("#modalBody .import-check input").is_checked()
        page.keyboard.type(" | ")
        _foot_says(page, "Split on ␣|␣ as written, quotes not special · 4 columns")
        assert _heads(page) == ["col_1", "col_2", "col_3", "col_4"]
        # The stray quote swallowed nothing, and the extra field stayed.
        cells = page.locator("#modalBody .preview-tbl tr").nth(3).locator("td").all_inner_texts()
        assert cells == ["2026-11-02 08:14:11", "WARN", "auth", "failed again | attempt 2"]
        assert _hint(page) == "Delimited · custom ␣|␣ · no header"

        page.locator("#modalBody .import-foot .btn.primary").click()
        page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'custom-delim.log'"
                               " && s.row_count === 3 && s.columns.length)", timeout=20_000)
        cols = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'custom-delim.log').columns.map((c) => c.name)")
        assert cols == ["col_1", "col_2", "col_3", "col_4"]
    finally:
        page.evaluate("""async () => {
          const ss = await __winnow.api('/api/sources');
          for (const s of ss.filter((s) => s.name === 'custom-delim.log'))
            await __winnow.api('/api/source/' + s.id, { method: 'DELETE' });
          await __winnow.loadSources();
        }""")


def test_custom_starts_from_the_split_in_use_and_keeps_what_was_typed(page, tmp_path, fake_jobs):
    f = tmp_path / "pipes.txt"
    f.write_text("a|b|c\n1|2|3\n")
    _queue(page, f)
    _foot_says(page, "Pipe-delimited (detected)")
    # Hidden until chosen; the row keeps its height either way.
    assert not page.locator(CUSTOM).is_visible()
    page.locator("#modalBody .import-delims button[data-key=custom]").click()
    assert page.locator(CUSTOM).input_value() == "|"
    _foot_says(page, "Split on | · 3 columns")
    page.locator(CUSTOM).fill("^")
    _foot_says(page, "Split on ^ · 1 columns")
    page.locator("#modalBody .import-delims button[data-key=comma]").click()
    assert not page.locator(CUSTOM).is_visible()
    assert _hint(page) == "Delimited · comma"
    page.locator("#modalBody .import-delims button[data-key=custom]").click()
    assert page.locator(CUSTOM).input_value() == "^"
    # Typing a preset's character stays on Custom, under the caret.
    page.locator(CUSTOM).fill("|")
    assert page.locator("#modalBody .import-delims button[aria-pressed=true]").get_attribute("data-key") == "custom"
    _foot_says(page, "Split on | · 3 columns")

    page.locator("#modalBody .import-delims button[data-key=space]").click()
    _foot_says(page, "Space-delimited · 1 columns")
    page.locator("#modalBody .import-delims button[data-key=custom]").click()
    page.locator(CUSTOM).fill('"')
    _foot_says(page, "Preview failed: The double quote")
    page.locator(CUSTOM).fill(" | ")
    with page.expect_request("**/api/ingest/jobs/path") as req:
        page.locator("#modalBody .import-foot .btn.primary").click()
    assert req.value.post_data_json["delimiter"] == " | "


def test_folder_import_splits_other_text_files_and_a_profile_keeps_it(page, tmp_path, api, fake_jobs):
    root = tmp_path / "logs"
    (root / "web").mkdir(parents=True)
    (root / "app.log").write_text(LOG)
    (root / "web" / "app.log.1").write_text(LOG)
    (root / "hosts.csv").write_text("Host,User\nh1,alice\n")
    prof = api("/api/import_profiles", "POST", {"name": "UI split logs", "extensions": [".csv", "*"],
                                                "text_delimiter": " :: ", "text_has_header": True})
    try:
        page.evaluate("(r) => __winnow.openDirectoryImportModal({ root: r })", str(root))
        page.wait_for_selector('#modalBody .side-preview[aria-busy="false"]')
        box = page.locator("#modalBody .import-text-split input.import-delim-custom")
        header = page.locator("#modalBody .import-text-split .import-check input")
        # Off while other text files are: nothing it would apply to.
        assert box.is_disabled() and header.is_disabled()
        page.locator('#modalBody .import-types button[data-ext="*"]').click()
        assert box.is_enabled() and header.is_disabled()   # the header waits for a delimiter
        box.fill(" | ")
        assert header.is_enabled()
        page.wait_for_function("() => document.querySelector('#modalBody .row-actions .btn.primary')"
                               "?.textContent === 'Import checked (3)'")
        page.wait_for_selector('#modalBody .side-preview[aria-busy="false"]')
        page.locator("#modalBody .row-actions .btn.primary").click()
        page.wait_for_function("() => document.getElementById('modal').hidden")
        by_name = {r["name"]: r for r in fake_jobs}
        assert set(by_name) == {"app.log", "app.log.1", "hosts.csv"}
        for name in ("app.log", "app.log.1"):
            assert by_name[name]["kind"] == "csv" and by_name[name]["delimiter"] == " | "
            assert by_name[name]["has_header"] is False
        assert by_name["hosts.csv"]["kind"] == "csv" and "delimiter" not in by_name["hosts.csv"]

        # A profile brings its own.
        page.evaluate("(r) => __winnow.openDirectoryImportModal({ root: r })", str(root))
        page.locator("#modalBody .side-rail-item", has=page.locator(".side-rail-label", has_text="UI split logs")).click()
        page.wait_for_selector('#modalBody .side-rail-item[aria-selected="true"] >> text="UI split logs"')
        assert box.input_value() == " :: " and header.is_checked() and box.is_enabled()
    finally:
        api(f"/api/import_profiles/{prof['id']}", "DELETE")
