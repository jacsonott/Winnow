"""The "Hide duplicate rows" toggle in the browser: the table menu and
Filters ▾ turn it on and off, the toolbar count follows and says so, hiding
or showing a column changes what is compared (and so the view), and the
toggle survives a tab switch and a reload (it lives in the layout) while
staying off for other tables.
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui


@pytest.fixture
def dupes(page, server_post, ui_csv, until):
    p = Path(ui_csv).parent / "dupes.csv"
    # Rows 2 and 4 repeat row 1 (row 2 from another file); row 5 repeats
    # row 3 from another file. Every column compared: 4 rows. File hidden: 2.
    p.write_text("Event,Host,File\n4624,H1,a.evtx\n4624,H1,b.evtx\n4625,H2,a.evtx\n4624,H1,a.evtx\n4625,H2,b.evtx\n")
    server_post("/api/ingest/jobs/path", {"path": str(p), "build_fts": False})
    until(page, lambda: page.evaluate("""() => __winnow.loadSources(undefined, { navigate: false }).then(() =>
            __winnow.S.sources.some((s) => s.name === 'dupes.csv' && s.row_count === 5 && s.columns.length))"""),
          "dupes.csv to finish importing", timeout=20)
    sid = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'dupes.csv').id")
    yield sid
    page.evaluate("(id) => __winnow.api(`/api/source/${id}`, { method: 'DELETE' }).then(() => __winnow.loadSources())", sid)


def _open(page, sid):
    page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && !!__winnow.S.view", arg=sid)


def _rows(page, n, on=True):
    page.wait_for_function(
        "([n, on]) => __winnow.S.hideDuplicateRows === on && __winnow.S.view.row_count === n"
        " && document.getElementById('viewStats').textContent.startsWith(`${n} of 5 rows`)",
        arg=[n, on])


def test_toggle_hides_repeats_and_follows_the_visible_columns(page, dupes, layout_saved):
    _open(page, dupes)
    assert page.evaluate("() => __winnow.S.view.row_count") == 5
    page.evaluate("() => __winnow.openTableMenu()")
    btn = page.locator("#modalBody button", has_text="Hide duplicate rows")
    btn.wait_for(state="visible")
    assert btn.get_attribute("aria-pressed") == "false"
    btn.click()
    _rows(page, 4)
    assert btn.get_attribute("aria-pressed") == "true"
    assert "duplicates hidden" in page.locator("#viewStats").inner_text()

    # Unticking File stops it counting: the copies from b.evtx collapse.
    row = page.locator("#modalBody .collist-row", has=page.locator(".collist-name", has_text="File"))
    row.locator("input[type=checkbox]").uncheck()
    _rows(page, 2)
    assert page.evaluate("() => __winnow.currentSpec().dedupe_columns") == ["Event", "Host"]
    # The rows kept are the first by Line.
    rids = page.evaluate("""() => __winnow.api(`/api/rows?view_id=${__winnow.S.view.view_id}&start=0&count=10`)
                            .then((r) => r.rows.map((x) => x.rid))""")
    assert rids == [1, 3]
    row.locator("input[type=checkbox]").check()
    _rows(page, 4)
    page.evaluate("() => __winnow.closeModal()")

    # Off for another table; back on when we return.
    other = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'ui.csv').id")
    _open(page, other)
    assert page.evaluate("() => __winnow.S.hideDuplicateRows") is False
    assert "duplicates hidden" not in page.locator("#viewStats").inner_text()
    _open(page, dupes)
    _rows(page, 4)

    # Survives a reload: the layout carries it.
    layout_saved()
    page.reload(wait_until="networkidle")
    page.wait_for_selector(".row")
    _open(page, dupes)
    _rows(page, 4)

    # Filters ▾ has it too, ticked while it is on; it turns it off.
    page.click("#btnFilters")
    item = page.locator(".menu .menu-item", has_text="Hide duplicate rows")
    item.wait_for(state="visible")
    assert item.locator(".menu-check").inner_text() == "✓"
    item.click()
    _rows(page, 5, on=False)
    assert "duplicates hidden" not in page.locator("#viewStats").inner_text()
    layout_saved()


def test_no_tags_leaves_the_copies_of_a_tagged_row_hidden(page, dupes, api, layout_saved):
    """The bug: tag the row kept from a set of copies, press No tags on the
    ribbon, and its next copy came back as an untagged row. The tag filter
    looks at the rows kept now, so what is left is the one set nobody has
    tagged — and pressing Exclude on the tag leaves the same."""
    _open(page, dupes)
    page.evaluate("() => __winnow.openTableMenu()")
    page.locator("#modalBody button", has_text="Hide duplicate rows").click()
    _rows(page, 4)
    # File hidden: rows 2 and 4 are copies of 1, and 5 of 3.
    row = page.locator("#modalBody .collist-row", has=page.locator(".collist-name", has_text="File"))
    row.locator("input[type=checkbox]").uncheck()
    _rows(page, 2)
    page.evaluate("() => __winnow.closeModal()")
    tag = page.evaluate("() => __winnow.S.tags[0]")
    api("/api/row_tags", "POST", {"source_id": dupes, "rids": [1], "tag_id": tag["id"], "on": True})
    page.evaluate("() => __winnow.loadTags()")

    def kept():
        page.wait_for_function("() => __winnow.busyCount === 0 && !!__winnow.S.view")
        return page.evaluate("""() => __winnow.api(`/api/rows?view_id=${__winnow.S.view.view_id}&start=0&count=10`)
                                .then((r) => r.rows.map((x) => x.rid))""")
    try:
        page.locator("#tagRibbon .tag-chip", has_text="No tags").click()
        page.wait_for_function("() => __winnow.S.tagFilter.includes('__none__') && __winnow.S.view.row_count === 1")
        assert kept() == [3]
        page.locator("#tagRibbon .tag-chip", has_text="No tags").click()
        page.evaluate("(id) => __winnow.toggleTagExclusion(id)", tag["id"])
        page.wait_for_function("(k) => __winnow.S.tagFilter.includes(k) && __winnow.S.view.row_count === 1",
                               arg=f"!{tag['id']}")
        assert kept() == [3]
    finally:
        page.evaluate("""() => { __winnow.S.tagFilter = []; __winnow.renderTagRibbon();
          return __winnow.rebuildView({ keepScroll: false, keepRow: false }); }""")
        api("/api/row_tags", "POST", {"source_id": dupes, "rids": [1], "tag_id": tag["id"], "on": False})
        page.evaluate("() => __winnow.loadTags()")
        layout_saved()
