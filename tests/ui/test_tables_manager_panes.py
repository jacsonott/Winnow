"""The Tables manager's sections: Every table (a tick to open or close,
grouped by folder or by kind, sortable), Merges & saved tables, and
Indexes (Build a search index, drop filter indexes) — and the 1.5 s poll
repainting in place without losing a typed filter or the focus.

The module imports one table of its own with ui.csv's columns, so Kinds
has a header set held by two tables, and removes it at the end."""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.ui

NAME = "tm-second.csv"


@pytest.fixture(scope="module")
def second(tmp_path_factory, server_post, server_call):
    path = tmp_path_factory.mktemp("tm") / NAME
    rows = ["Timestamp,EventId,Host,ExtremelyLongColumnHeaderName,CommandLine,Payload"]
    rows += [f"2026-03-15 09:00:0{i},4624,H{i},v,cmd.exe,{{}}" for i in range(5)]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    server_post("/api/ingest/jobs/path", {"path": str(path), "name": NAME, "kind": "csv", "build_fts": False})
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        found = [s for s in server_call("/api/sources") if s["name"] == NAME]
        if found:
            break
        time.sleep(0.25)
    else:
        pytest.fail(f"{NAME} never appeared")
    sid = found[0]["id"]
    yield sid
    server_call(f"/api/source/{sid}", "DELETE")


def _open(page, section="tables"):
    page.evaluate("(s) => __winnow.openTablesManager(s)", section)
    page.wait_for_selector(f'#modal:not([hidden]) .tables-manager-pane[data-section="{section}"]')


def _row(page, name, cls="tables-row"):
    return page.locator(f'#modal .{cls}:has(.tables-name:text-is("{name}"))').first


def _src(page, name):
    return page.evaluate("(n) => __winnow.S.sources.find((s) => s.name === n)", name)


def test_ticking_opens_and_unticking_closes_and_the_manager_stays(page, second):
    page.evaluate("() => __winnow.loadSources()")
    shown = page.evaluate("() => __winnow.S.sourceId")
    was_open = _src(page, NAME)["is_open"]
    _open(page)
    tick = _row(page, NAME).locator("input[type=checkbox]")
    try:
        if was_open:
            tick.uncheck()
            page.wait_for_selector(f'#sourceTabs .tab[data-id="{second}"]', state="detached")
        tick.check()
        # The tab strip, not S.sources: the manager's poll can set is_open
        # from its own fetch before openTables has redrawn the strip.
        page.wait_for_selector(f'#sourceTabs .tab[data-id="{second}"]')
        # Opened as a tab, not gone to: the analyst is still in the manager,
        # on the table they were on.
        assert page.locator("#modal").is_visible()
        assert page.evaluate("() => __winnow.S.sourceId") == shown
        assert _row(page, NAME).locator("input[type=checkbox]").is_checked()

        _row(page, NAME).locator("input[type=checkbox]").uncheck()
        page.wait_for_selector(f'#sourceTabs .tab[data-id="{second}"]', state="detached")
        # The table stays in the case, unticked.
        assert not _row(page, NAME).locator("input[type=checkbox]").is_checked()
    finally:
        page.evaluate("([id, open]) => __winnow.post(`/api/source/${id}/open`, { open })", [second, was_open])
    page.keyboard.press("Escape")


def test_kinds_puts_one_header_set_together_with_a_merge(page, second):
    _open(page)
    page.locator(".tables-groupby button", has_text="Kinds").click()
    heads = page.locator("#modal .tables-list .tables-group")
    head = heads.filter(has=page.locator(".tables-group-meta", has_text="2 tables")).first
    assert head.locator(".tables-group-name").inner_text().startswith("Timestamp, EventId, Host")
    # The two tables are the rows under that head, before the next one.
    names = page.evaluate("""() => {
      const out = []; let inGroup = false;
      for (const n of document.querySelector('#modal .tables-list').children) {
        if (n.classList.contains('tables-group')) { if (inGroup) break; inGroup = n.textContent.includes('2 tables'); continue; }
        if (inGroup) out.push(n.querySelector('.tables-name').textContent);
      }
      return out.sort();
    }""")
    assert names == [NAME, "ui.csv"], names
    head.locator("button", has_text="Merge…").click()
    page.wait_for_selector("#modal:not([hidden]) .mb-trow")
    assert page.locator("#modalTitle").inner_text().lower() == "merge tables"
    ticked = sorted(r.locator(".mb-tname").inner_text() for r in page.locator(".mb-trow").all()
                    if r.locator("input").is_checked())
    assert ticked == [NAME, "ui.csv"], ticked
    page.keyboard.press("Escape")


def test_folders_group_like_the_sidebar(page, second, server_post, server_call):
    folder = server_post("/api/folders", {"name": "TM folder"})
    try:
        server_post(f"/api/source/{second}/folder", {"folder_id": folder["id"]})
        page.evaluate("() => __winnow.loadSources()")
        _open(page)
        heads = [h.locator(".tables-group-name").inner_text() for h in page.locator("#modal .tables-group").all()]
        assert heads[:2] == ["TM folder", "Not in a folder"], heads
        meta = page.locator("#modal .tables-group", has_text="TM folder").locator(".tables-group-meta").inner_text()
        assert meta.startswith("1 table · 5 rows · "), meta
        # The folder's table is the row under its head.
        first = page.locator("#modal .tables-list > *").nth(1)
        assert first.locator(".tables-name").inner_text() == NAME
    finally:
        server_call(f"/api/folders/{folder['id']}", "DELETE")
        page.evaluate("() => __winnow.loadSources()")
    page.keyboard.press("Escape")


def test_a_column_head_sorts(page, second):
    _open(page)
    order = lambda: [n.inner_text() for n in page.locator("#modal .tables-list .tables-name").all()]
    assert order().index(NAME) < order().index("ui.csv")   # by name, the default
    page.locator(".tables-sort[data-sort='row_count']").click()
    assert order().index("ui.csv") < order().index(NAME)   # most rows first
    assert page.locator(".tables-sort[data-sort='row_count']").get_attribute("aria-pressed") == "true"
    page.keyboard.press("Escape")


def test_the_poll_keeps_a_typed_filter_and_the_focus(page, api, second):
    uid = _src(page, "ui.csv")["id"]
    _open(page)
    search = page.locator(".tables-manager-pane .side-search")
    search.fill("second")
    assert page.locator("#modal .tables-list .tables-row").count() == 1
    assert page.locator(".tm-cols-count").inner_text().startswith("1 of ")
    _row(page, NAME).locator(".tables-more").focus()
    page.evaluate("() => { window.__tmOldRow = document.activeElement.closest('.tables-row'); }")
    try:
        # Something the poll repaints for (the row is a new node after it)…
        api(f"/api/source/{uid}/nickname", "POST", {"nickname": "poll repaint"})
        page.wait_for_function("() => !window.__tmOldRow.isConnected", timeout=5000)
        # …and after the repaint, the filter and the focus are where they were.
        assert search.input_value() == "second"
        assert page.locator("#modal .tables-list .tables-row").count() == 1
        assert page.evaluate("""() => document.activeElement.classList.contains('tables-more')
          && document.activeElement.closest('.tables-row').querySelector('.tables-name').textContent""") == NAME
        assert page.evaluate("() => document.activeElement.isConnected")
    finally:
        api(f"/api/source/{uid}/nickname", "POST", {"nickname": ""})
        page.evaluate("() => __winnow.loadSources()")
    page.keyboard.press("Escape")


def test_merges_pane_says_what_each_is_made_of(page, second):
    _open(page, "merges")
    assert "None yet" in page.locator("#modal .tables-made").inner_text()
    uid = _src(page, "ui.csv")["id"]
    merge = page.evaluate("""async (ids) => {
      const m = await __winnow.post('/api/merges', { name: 'TM merge', source_ids: ids });
      return m;
    }""", [uid, second])
    try:
        page.locator(".tables-made-row", has_text="TM merge").wait_for(timeout=5000)   # the poll finds it
        made = page.locator(".tables-made-row", has_text="TM merge")
        assert made.locator(".tables-made-sub").inner_text() == "205 rows · 2 tables"
        members = sorted((m.locator(".tables-member-name").inner_text(), m.locator(".tables-member-rows").inner_text())
                         for m in made.locator(".tables-member").all())
        assert members == sorted([("ui.csv", "200 rows"), (NAME, "5 rows")]), members
        hint = page.locator('.side-rail-item[data-section="merges"] .side-rail-hint').inner_text()
        assert hint == "1 merge", hint
    finally:
        page.evaluate("(mid) => __winnow.api('/api/merges/' + mid, { method: 'DELETE' })", -merge["id"])
        page.evaluate("() => __winnow.loadSources()")
    page.keyboard.press("Escape")


def test_build_a_search_index_and_drop_filter_indexes(page, api, server_call, second):
    # A filter index, the way filtering leaves one: the value picker's query.
    server_call(f"/api/column_values?source_id={second}&column=EventId")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        idx = server_call(f"/api/column_indexes?source_id={second}")
        if idx and not idx[0]["building"]:
            break
        time.sleep(0.2)
    _open(page, "indexes")
    row = _row(page, NAME, "tables-ix-row")
    assert row.locator(".index-status").inner_text() == "not indexed"
    row.locator(".tables-build").click()
    page.wait_for_function("(n) => __winnow.S.sources.find((s) => s.name === n).has_fts", arg=NAME, timeout=20_000)
    page.wait_for_function("""(n) => [...document.querySelectorAll('.tables-ix-row')]
      .find((r) => r.querySelector('.tables-name').textContent === n)
      .querySelector('.index-status').textContent === 'index ready'""", arg=NAME, timeout=5000)
    assert _row(page, NAME, "tables-ix-row").locator(".tables-build").count() == 0

    chip = _row(page, NAME, "tables-ix-row").locator(".index-chip", has_text="EventId")
    assert chip.count() == 1
    assert page.locator(".tables-indexes .side-preview-foot .btn", has_text="Drop all filter indexes").is_enabled()
    chip.locator(".index-chip-drop").click()
    page.wait_for_function("""(n) => ![...document.querySelectorAll('.tables-ix-row')]
      .find((r) => r.querySelector('.tables-name').textContent === n).querySelector('.index-chip')""",
                           arg=NAME, timeout=5000)
    assert server_call(f"/api/column_indexes?source_id={second}") == []
    page.keyboard.press("Escape")


def test_drop_all_asks_then_drops_every_filter_index(page, server_call, second):
    server_call(f"/api/column_values?source_id={second}&column=Host")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not server_call(f"/api/column_indexes?source_id={second}"):
        time.sleep(0.2)
    _open(page, "indexes")
    _row(page, NAME, "tables-ix-row").locator(".index-chip", has_text="Host").wait_for(timeout=10_000)
    page.locator(".tables-indexes .btn", has_text="Drop all filter indexes").click()
    page.locator(".confirm-card .btn", has_text="Drop all").click()
    page.wait_for_function("() => !document.querySelector('.tables-ix-row .index-chip')", timeout=10_000)
    assert server_call(f"/api/column_indexes?source_id={second}") == []
    assert page.locator(".tables-indexes .btn", has_text="Drop all filter indexes").is_disabled()
    page.keyboard.press("Escape")


def test_an_old_sqlite_says_so_instead_of_offering_build(page, second):
    page.route("**/api/search_index", lambda r: r.fulfill(
        status=200, content_type="application/json", body='{"supported": false, "sqlite": "3.40.1"}'))
    _open(page, "indexes")
    page.wait_for_function("() => document.querySelector('.tables-manager-pane .mb-notes').textContent.includes('3.40.1')")
    assert "can't use a search index" in page.locator(".tables-manager-pane .mb-notes").inner_text()
    assert page.locator(".tables-build").count() == 0
    assert page.locator(".tables-manager-pane .btn", has_text="missing search ind").is_hidden()
    page.keyboard.press("Escape")
