"""Configure tables… (the Timeline's per-header-set settings), driven
through the real dialog.

What it replaced was one stacked block per TABLE, each with its own Save,
over a config that is stored per HEADER SET: in a case with two tables of
the same shape, saving one silently changed the other, and nothing on the
screen said so. These pin the symptoms of that and of the rebuild:

- tables sharing a header set are ONE entry that names them all, and its
  Save reaches every one of them on the Timeline;
- the preview follows the fields as they change, with no button to press,
  and shows what the Timeline really falls back to when no body column is
  picked;
- an edit survives switching to another header set and back, and the list
  says which sets hold unsaved edits;
- Back to default removes the saved setting;
- the Timeline behind the dialog shows a save without being reopened.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui

# Headers no other module ingests, so each header set here holds exactly
# the tables this module imports, and the templates it saves match nothing
# else in the shared session's workspace.
PAIR_HEADER = ["TcWhen", "TcHost", "TcUser", "TcWhat"]
SOLO_HEADER = ["TsLogged", "TsProc"]


def _call(server, route, body=None, method="POST"):
    req = urllib.request.Request(
        server.rstrip("/") + route, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def _templates_here(server):
    keys = {tuple(sorted(c.lower() for c in h)) for h in (PAIR_HEADER, SOLO_HEADER)}
    return [t for t in _call(server, "/api/timeline_templates", method="GET")
            if tuple(t["col_names"]) in keys]


@pytest.fixture(scope="module")
def tables(server, tmp_path_factory):
    """Two tables with one header set (Alpha, Bravo), one with another
    (Solo). One row of each pair table is tagged, so both are on the
    Timeline; Solo has none tagged."""
    d = tmp_path_factory.mktemp("tlc")
    ids = {}
    for name, host in (("tlc_alpha.csv", "WKS-A"), ("tlc_bravo.csv", "WKS-B")):
        p = d / name
        p.write_text(",".join(PAIR_HEADER) + "\n"
                     f"2026-02-01 10:00:00,{host},alice,logon\n"
                     f"2026-02-01 11:00:00,{host},bob,logoff\n", encoding="utf-8")
        ids[name] = _call(server, "/api/ingest/path", {"path": str(p), "name": name, "build_fts": False})["id"]
    p = d / "tlc_solo.csv"
    p.write_text(",".join(SOLO_HEADER) + "\n2026-02-02 09:00:00,x.exe\n", encoding="utf-8")
    ids["tlc_solo.csv"] = _call(server, "/api/ingest/path",
                                {"path": str(p), "name": "tlc_solo.csv", "build_fts": False})["id"]
    tag = _call(server, "/api/tags", method="GET")["tags"][0]["id"]
    for name in ("tlc_alpha.csv", "tlc_bravo.csv"):
        _call(server, "/api/row_tags", {"source_id": ids[name], "rids": [1], "tag_id": tag, "on": True})
    yield ids
    for t in _templates_here(server):
        _call(server, f"/api/timeline_templates/{t['id']}", method="DELETE")
    for sid in ids.values():
        _call(server, f"/api/source/{sid}", method="DELETE")


@pytest.fixture
def clean_templates(server):
    for t in _templates_here(server):
        _call(server, f"/api/timeline_templates/{t['id']}", method="DELETE")
    yield
    for t in _templates_here(server):
        _call(server, f"/api/timeline_templates/{t['id']}", method="DELETE")


def _open(page):
    page.evaluate("() => __winnow.loadTimelineTemplates().then(() => __winnow.openTimelineSourceConfig())")
    page.wait_for_selector("#modal:not([hidden]) .tlc-sets")


def _pick_set(page, table):
    page.locator(".tlc-set", has_text=table).click()
    page.wait_for_function("(t) => document.querySelector('.tlc-scope')?.textContent.includes(t)", arg=table)


def _wait_preview(page, text, where=".tl-col-body"):
    """Until a preview line's cell holds `text` — the preview is fetched
    after a short debounce, so the assertion waits for it."""
    page.wait_for_function(
        """([sel, t]) => [...document.querySelectorAll('.tlc-preview .timeline-row ' + sel)]
             .some((n) => n.textContent.includes(t))""",
        arg=[where, text], timeout=10_000)


def _preview_text(page, where=".tl-col-body"):
    return page.locator(f".tlc-preview .timeline-row {where}").all_text_contents()


def _close(page):
    page.keyboard.press("Escape")
    if page.locator("#modal").is_visible():
        page.locator("#modalClose").click()


def test_one_header_set_is_one_entry_naming_every_table(page, tables, clean_templates):
    _open(page)
    try:
        entries = page.locator(".tlc-set", has_text="tlc_alpha.csv")
        assert entries.count() == 1
        # The same entry lists the other table of the same shape: the
        # dialog says, before anything is saved, that one setting covers both.
        assert "tlc_bravo.csv" in entries.first.text_content()
        assert page.locator(".tlc-set", has_text="tlc_solo.csv").count() == 1
        assert "tlc_bravo.csv" not in page.locator(".tlc-set", has_text="tlc_solo.csv").text_content()
        _pick_set(page, "tlc_alpha.csv")
        scope = page.locator(".tlc-scope").text_content()
        assert "tlc_alpha.csv" in scope and "tlc_bravo.csv" in scope
    finally:
        _close(page)


def test_saving_the_entry_changes_every_table_of_that_shape(page, api, tables, clean_templates):
    _open(page)
    try:
        _pick_set(page, "tlc_alpha.csv")
        page.locator("#modal").get_by_label("Type", exact=True).fill("Logon log")
        page.locator(".tlc-foot .btn.primary", has_text="Save").click()
        page.wait_for_function("() => document.querySelector('.tlc-set[aria-pressed=\"true\"] .tlc-mark.saved')")
    finally:
        _close(page)
    view = api("/api/timeline", "POST", {"tag_ids": []})
    rows = api(f"/api/timeline_rows?view_id={view['view_id']}&start=0&count=500")["rows"]
    mine = {r["source_name"]: r["type_label"] for r in rows if r["source_name"].startswith("tlc_")}
    assert mine == {"tlc_alpha.csv": "Logon log", "tlc_bravo.csv": "Logon log"}


def test_save_waits_for_a_change(page, tables, clean_templates):
    _open(page)
    try:
        _pick_set(page, "tlc_solo.csv")
        save = page.locator(".tlc-foot .btn.primary", has_text="Save")
        assert save.is_disabled()
        page.locator(".tlc-chip", has_text="TsProc").click()
        assert save.is_enabled()
        page.locator(".tlc-chip", has_text="TsProc").click()   # and back: nothing to save again
        assert save.is_disabled()
    finally:
        _close(page)


def test_the_preview_follows_the_fields_without_a_button(page, tables, clean_templates):
    _open(page)
    try:
        _pick_set(page, "tlc_alpha.csv")
        # Nothing picked, a shape Winnow ships no summary for: the Body is
        # every column joined, which is what the Timeline falls back to.
        _wait_preview(page, "2026-02-01 10:00:00 | WKS-A | alice | logon")
        assert "every column" in page.locator(".tlc .dlg-field small").last.text_content()

        page.locator(".tlc-chip", has_text="TcUser").click()
        page.locator(".tlc-chip", has_text="TcHost").click()
        _wait_preview(page, "alice | WKS-A")
        # The chips say their order in the Body.
        assert page.locator(".tlc-chip[aria-pressed='true']").all_text_contents() == ["2TcHost", "1TcUser"]

        page.locator("#modal").get_by_label("Type", exact=True).fill("Logons")
        _wait_preview(page, "Logons", where=".type-badge")
        assert set(_preview_text(page, ".type-badge")) == {"Logons"}

        # Labelled, like every other field here; and there is no Preview
        # button to press — the lines above changed on their own.
        assert page.locator("#modal").get_by_label("Timestamp column").input_value() == ""
        assert not page.locator("#modal button", has_text="Preview").count()
    finally:
        _close(page)


def test_a_preview_reads_from_any_table_of_the_set(page, tables, clean_templates):
    _open(page)
    try:
        _pick_set(page, "tlc_alpha.csv")
        _wait_preview(page, "WKS-A")
        page.locator(".tlc-prev-from select").select_option(label="tlc_bravo.csv")
        _wait_preview(page, "WKS-B")
        assert not any("WKS-A" in t for t in _preview_text(page))
    finally:
        _close(page)


def test_an_edit_survives_switching_header_sets(page, tables, clean_templates):
    _open(page)
    try:
        _pick_set(page, "tlc_alpha.csv")
        page.locator("#modal").get_by_label("Type", exact=True).fill("Kept edit")
        alpha = page.locator(".tlc-set", has_text="tlc_alpha.csv")
        assert alpha.locator(".tlc-mark.unsaved").count() == 1

        _pick_set(page, "tlc_solo.csv")
        # The other set's edit is named in the footer, so Save here isn't
        # mistaken for saving that one too.
        hint = page.locator(".tlc-hint").text_content()
        assert "Not saved yet" in hint and "tlc_alpha.csv" in hint
        assert page.locator("#modal").get_by_label("Type", exact=True).input_value() == ""

        _pick_set(page, "tlc_alpha.csv")
        assert page.locator("#modal").get_by_label("Type", exact=True).input_value() == "Kept edit"
        assert page.locator(".tlc-foot .btn.primary", has_text="Save").is_enabled()
    finally:
        _close(page)


def test_back_to_default_removes_the_saved_setting(page, server, tables, clean_templates):
    _open(page)
    try:
        _pick_set(page, "tlc_solo.csv")
        reset = page.locator(".tlc-foot .btn", has_text="Back to default")
        assert reset.is_hidden()   # nothing saved, nothing to go back from
        page.locator(".tlc-chip", has_text="TsProc").click()
        page.locator(".tlc-foot .btn.primary", has_text="Save").click()
        page.wait_for_function("() => document.querySelector('.tlc-title .tlc-mark.saved')")
        assert len(_templates_here(server)) == 1

        reset.click()
        page.locator(".confirm-card .btn", has_text="Back to default").click()
        page.wait_for_function("() => document.querySelector('.tlc-title .tlc-mark.default')")
        assert _templates_here(server) == []
        assert page.locator(".tlc-chip[aria-pressed='true']").count() == 0
        assert reset.is_hidden()
    finally:
        _close(page)


def test_the_timeline_shows_a_save_without_being_reopened(page, tables, clean_templates):
    page.evaluate("() => __winnow.showTimelineTab()")
    page.wait_for_selector("#timelineRows .timeline-row .type-badge[title='tlc_alpha.csv']")
    page.locator("#btnTimelineConfig").click()
    page.wait_for_selector("#modal:not([hidden]) .tlc-sets")
    _pick_set(page, "tlc_alpha.csv")
    page.locator("#modal").get_by_label("Type", exact=True).fill("Rebuilt behind")
    page.locator(".tlc-foot .btn.primary", has_text="Save").click()
    page.wait_for_function("() => document.querySelector('.tlc-title .tlc-mark.saved')")
    _close(page)
    page.wait_for_function(
        """() => [...document.querySelectorAll("#timelineRows .type-badge[title='tlc_alpha.csv']")]
             .every((n) => n.textContent === 'Rebuilt behind')
           && document.querySelectorAll("#timelineRows .type-badge[title='tlc_alpha.csv']").length > 0""",
        timeout=10_000)
    page.evaluate("() => __winnow.showGridTab()")
