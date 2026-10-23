"""The Import dialog in two panes: the queued files down the left, the
chosen one's settings and preview on the right. It used to be a column of
rows with a button on each that swapped the whole dialog for that file's
preview, and a setting only stuck once "Use these settings" was pressed;
these pin what replaced that — a setting is kept as it changes, and kept
when another file is chosen, all the way into the import request.

Nothing here imports for real: the import requests are answered by a
route, so the case every module shares is left as it was found."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui

SEMI = "When;User;Note\n2026-03-14 08:00:00;alice;a\n2026-03-14 08:01:00;bob;b\n"
COMMA = "When,User\n2026-03-14 08:00:00,carol\n"
NESTED = "".join(json.dumps({"Pid": i, "Hashes": {"MD5": "d41d", "SHA1": "da39"}}) + "\n" for i in range(3))


@pytest.fixture
def files(tmp_path):
    (tmp_path / "semi.csv").write_text(SEMI)
    (tmp_path / "comma.csv").write_text(COMMA)
    (tmp_path / "procs.jsonl").write_text(NESTED)
    return tmp_path


def _queue(page, base, *names):
    page.evaluate("""async ([base, names]) => {
      __winnow.S.importQueue = [];
      await __winnow.queuePaths(names.map((n) => ({ path: base + '/' + n, name: n })));
      __winnow.openImportModal();
    }""", [str(base), list(names)])
    page.wait_for_selector("#modal:not([hidden]) .import-pane")


def _choose(page, name):
    page.locator("#modalBody .side-rail-item", has_text=name).click()
    page.wait_for_function("(n) => document.querySelector('#modalBody .import-file-name')?.textContent === n", arg=name)


def _hint(page, name):
    return page.locator("#modalBody .side-rail-item", has_text=name).locator(".side-rail-hint").inner_text()


def _heads_are(page, want):
    page.wait_for_function("(want) => JSON.stringify([...document.querySelectorAll('#modalBody .preview-colname')]"
                           ".map((n) => n.textContent)) === JSON.stringify(want)", arg=want, timeout=10_000)


def _posted(name):
    return lambda r: r.url.endswith("/api/ingest/jobs/path") and r.post_data_json["name"] == name


@pytest.fixture
def fake_import(page):
    """Answers the import requests instead of the server, keeping what each
    one asked for."""
    sent = []

    def answer(route):
        sent.append(route.request.post_data_json)
        route.fulfill(status=200, content_type="application/json", body="{}")
    page.route("**/api/ingest/jobs/path", answer)
    yield sent
    page.unroute("**/api/ingest/jobs/path")
    page.evaluate("() => { __winnow.closeModal(); __winnow.S.importQueue = []; }")


def test_a_setting_is_kept_when_another_file_is_chosen_and_reaches_the_import(page, files, fake_import):
    _queue(page, files, "semi.csv", "comma.csv")
    # The first file is chosen, and auto-detect has already split it.
    _heads_are(page, ["When", "User", "Note"])
    assert "Semicolon-delimited (detected)" in page.locator("#modalBody .import-preview .side-preview-foot").inner_text()
    page.locator("#modalBody .import-delims button[data-key=semicolon]").click()
    assert _hint(page, "semi.csv") == "Delimited · semicolon"
    page.locator("#modalBody .preview-tbl th", has_text="User").locator("select").select_option("datetime")

    _choose(page, "comma.csv")
    _heads_are(page, ["When", "User"])
    _choose(page, "semi.csv")
    _heads_are(page, ["When", "User", "Note"])
    assert page.locator("#modalBody .import-delims button[aria-pressed=true]").get_attribute("data-key") == "semicolon"
    assert page.locator("#modalBody .preview-tbl th", has_text="User").locator("select").input_value() == "datetime"

    # The loop posts one file after the other, in queue order.
    with page.expect_request(_posted("semi.csv")) as semi, page.expect_request(_posted("comma.csv")) as comma:
        page.locator("#modalBody .import-foot .btn.primary", has_text="Import 2 files").click()
    semi, comma = semi.value.post_data_json, comma.value.post_data_json
    assert semi["delimiter"] == ";" and semi["column_types"][1] == "datetime"
    assert comma["delimiter"] is None and comma["column_types"] is None
    assert page.evaluate("() => document.getElementById('modal').hidden")


def test_json_flattens_in_its_pane(page, files, fake_import):
    _queue(page, files, "procs.jsonl")
    _heads_are(page, ["Pid", "Hashes"])
    assert _hint(page, "procs.jsonl") == "JSON · nested kept as JSON"
    page.locator("#modalBody .import-flatten button[data-key=full]").click()
    _heads_are(page, ["Pid", "Hashes.MD5", "Hashes.SHA1"])
    assert _hint(page, "procs.jsonl") == "JSON · flattened"
    assert "3 records" in page.locator("#modalBody .import-preview .side-preview-foot").inner_text()
    assert page.locator("#modalBody .import-depth").is_hidden()
    page.locator("#modalBody .import-flatten button[data-key=depth]").click()
    assert page.locator("#modalBody .import-depth").is_visible()
    assert _hint(page, "procs.jsonl") == "JSON · flattened 1 deep"


def test_taking_files_out_of_the_queue(page, files, fake_import):
    _queue(page, files, "semi.csv", "comma.csv")
    rail = page.locator("#modalBody .side-rail-row", has_text="semi.csv")
    rail.locator(".side-rail-pin").click()
    # The next file takes its place in the pane.
    page.wait_for_function("() => document.querySelector('#modalBody .import-file-name')?.textContent === 'comma.csv'")
    assert page.locator("#modalBody .side-rail-item").all_inner_texts()[0].startswith("comma.csv")
    assert page.locator("#modalBody .import-foot .btn.primary").inner_text() == "Import 1 file"
    page.locator("#modalBody .side-rail-row", has_text="comma.csv").locator(".side-rail-pin").click()
    page.wait_for_selector("#modalBody .import-empty")
    assert page.locator("#modalBody .import-foot .btn.primary").is_disabled()


def test_import_goes_to_the_file_that_still_needs_a_choice(page, files, fake_import):
    import sqlite3
    db = sqlite3.connect(files / "History.sqlite")
    db.execute("create table urls(id integer primary key, url text)")
    db.commit()
    db.close()
    _queue(page, files, "comma.csv", "History.sqlite")
    # Opened on the file that needs something before the queue can import.
    page.wait_for_function("() => document.querySelector('#modalBody .import-file-name')?.textContent === 'History.sqlite'")
    _choose(page, "comma.csv")
    page.locator("#modalBody .import-foot .btn.primary").click()
    page.wait_for_function("() => document.querySelector('#modalBody .import-file-name')?.textContent === 'History.sqlite'")
    assert "Pick which tables" in page.locator("#toast").inner_text()
    assert fake_import == []
    page.wait_for_selector("#modalBody .import-unit")
    page.locator("#modalBody .import-unit", has_text="urls").locator("input").check()
    assert _hint(page, "History.sqlite") == "SQLite · 1 table"


def test_an_uploads_preview_sends_only_the_head(page, fake_import):
    """The preview now loads when a file is chosen, not when a button is
    pressed, so a browser-picked CSV must not be sent up whole to show
    its first rows: the server reads a 512 KB sample of it anyway."""
    page.evaluate("""async () => {
      __winnow.S.importQueue = [];
      const row = 'a,b,c\\n' + '1,2,3\\n'.repeat(500000);   // 3 MB
      await __winnow.queueFiles([new File([row], 'big.csv')]);
    }""")
    with page.expect_request("**/api/ingest/preview") as req:
        page.evaluate("() => __winnow.openImportModal()")
    _heads_are(page, ["a", "b", "c"])
    assert len(req.value.post_data_buffer) < 1.1 * (1 << 20)


def test_a_large_upload_asks_before_its_preview_sends_it_whole(page, fake_import):
    """A JSON document can't be cut short, so its preview sends the whole
    file; past 32 MB the pane asks first."""
    asked = []
    page.on("request", lambda r: asked.append(r.url) if "/json/preview" in r.url else None)
    page.evaluate("""async () => {
      __winnow.S.importQueue = [];
      await __winnow.queueFiles([new File([new Uint8Array(33 << 20)], 'huge.json')]);
      __winnow.openImportModal();
    }""")
    page.wait_for_selector("#modalBody .import-preview .btn:has-text('Load preview')")
    assert asked == []
