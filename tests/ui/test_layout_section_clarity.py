"""The table menu's Save & copy layout section says which way each half
goes, and what applying would do in words.

The report was that the table layout menu confused people. Reading the
section it was, three things did it. It was called "Layout" under "This
table" while the list that lays the columns out by hand is "Show, hide &
order" under "Columns", so the name promised the wrong pane. Its one
button that writes the default, "Save this layout as the default", sat
under a preview of ANOTHER layout, where "this layout" read as the
previewed one rather than the table's. And the preview marked a column
"hidden" in the same word and place where an unchanged hidden column
also said "hidden", so a change and a state were indistinguishable.

So: one section named for both directions, under Columns; the default
in a card of its own with its own button and a line saying whether this
table matches it; and changes said as changes ("gets hidden", "moves to
2nd"), counted on one line above the preview.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

# This module's own header set, so the default it saves in the shared
# workspace can't meet a table another module opens.
HEADERS = ["LcWhen", "LcHost", "LcUser", "LcCmd"]


def _csv(tmp_path, name):
    f = tmp_path / name
    f.write_text(",".join(HEADERS) + "\n"
                 "2026-05-01 09:00:00,H1,alice,whoami\n"
                 "2026-05-01 10:00:00,H2,bob,ipconfig\n", encoding="utf-8")
    return f


def _open(page, sid):
    page.evaluate("(id) => __winnow.loadSources().then(() => __winnow.openSource(id))", sid)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.busyCount === 0", arg=sid)
    page.wait_for_selector(".row")


def _section(page):
    page.evaluate("() => __winnow.openTableMenu(undefined, 'layout')")
    page.wait_for_selector(".tm-layout-from")
    page.wait_for_function("() => !/Looking/.test(document.querySelector('.tm-layout-state').textContent)")


def _go_home(page, home):
    page.evaluate("""(home) => (async () => {
      __winnow.S.sourceId = null;
      await __winnow.loadSources();
      await __winnow.openSource(home);
    })()""", home)
    page.wait_for_selector(".row")


def test_the_two_directions_are_separate_and_each_carries_its_own_button(page, api, tmp_path, layout_saved):
    """Saving the default and copying one onto this table are two blocks
    with headings of their own, and the save button is in the first — not
    under the second's preview, where it read as "save what is previewed"."""
    home = page.evaluate("() => __winnow.S.sourceId")
    a = api("/api/ingest/path", "POST", {"path": str(_csv(tmp_path, "lc_a.csv")),
                                         "name": "lc_a.csv", "build_fts": False})["id"]
    b = api("/api/ingest/path", "POST", {"path": str(_csv(tmp_path, "lc_b.csv")),
                                         "name": "lc_b.csv", "build_fts": False})["id"]
    try:
        _open(page, a)
        _section(page)
        # Under Columns, beside the list that lays the columns out by hand.
        rail = page.locator('.side-rail-item[data-section="layout"]')
        assert rail.locator(".side-rail-label").inner_text() == "Save & copy layout"
        # The rail's group names are upper-cased by the stylesheet.
        groups = [t.lower() for t in page.locator(".table-menu .side-rail-group").all_inner_texts()]
        order = page.locator(".side-rail-item").evaluate_all("(xs) => xs.map((x) => x.dataset.section)")
        assert groups == ["columns", "this table"], groups
        assert order[:3] == ["columns", "layout", "valueFilters"], order

        # The save button is the default card's, the apply button the
        # copy block's, and neither block holds the other's.
        dflt = page.locator(".tm-layout-default")
        copy = page.locator(".tm-layout-copy")
        # Both headings are upper-cased by the stylesheet, like the rail's.
        assert dflt.locator(".tm-layout-head").inner_text().lower().startswith("default for")
        assert copy.locator(".tm-layout-head").inner_text().lower() == "copy a layout onto this table"
        assert dflt.locator("button").count() == 1
        assert "as the default" in dflt.locator("button").inner_text()
        assert copy.locator(".tm-layout-preview").count() == 1
        assert dflt.locator(".tm-layout-preview").count() == 0
        assert copy.locator("button.primary").inner_text() == "Apply to this table"

        # No default saved yet: the card says so, and the side list too.
        assert "No default is saved" in dflt.locator(".tm-layout-state").inner_text()
        page.wait_for_function("() => document.querySelector('.side-rail-item[data-section=\"layout\"]"
                               " .side-rail-hint').textContent === 'No default saved'")

        # Lay A out, save it as the default, and the card flips to saying
        # this table matches it — with nothing left to save.
        page.evaluate("""() => {
          __winnow.S.order = ['LcCmd', 'LcUser', 'LcWhen', 'LcHost'];
          __winnow.S.layout.LcHost = { hidden: true };
          __winnow.renderHead(); __winnow.saveLayout();
        }""")
        layout_saved()
        _section(page)
        assert "No default is saved" in page.locator(".tm-layout-state").inner_text()
        with page.expect_response(lambda r: r.request.method == "POST"
                                  and r.url.endswith("/api/column_layouts")):
            page.locator(".tm-layout-default button").click()
        page.wait_for_function("() => /matches the default/.test(document.querySelector('.tm-layout-state').textContent)")
        assert page.locator(".tm-layout-default button").is_disabled()
        page.wait_for_function("() => document.querySelector('.side-rail-item[data-section=\"layout\"]"
                               " .side-rail-hint').textContent === 'Matches the default'")
    finally:
        for sid in (a, b):
            api(f"/api/source/{sid}", "DELETE")
        _go_home(page, home)


def test_a_table_that_differs_is_told_how_far_and_in_what_words(page, api, tmp_path, layout_saved):
    """The state line counts the columns this table differs by, the
    summary counts what applying would change by kind, and each row says
    the change as a change — never the bare state word an unchanged
    column uses for the same thing."""
    home = page.evaluate("() => __winnow.S.sourceId")
    a = api("/api/ingest/path", "POST", {"path": str(_csv(tmp_path, "lc2_a.csv")),
                                         "name": "lc2_a.csv", "build_fts": False})["id"]
    b = api("/api/ingest/path", "POST", {"path": str(_csv(tmp_path, "lc2_b.csv")),
                                         "name": "lc2_b.csv", "build_fts": False})["id"]
    try:
        # A: LcHost hidden, LcCmd first and pinned — the default.
        _open(page, a)
        page.evaluate("""() => {
          __winnow.S.order = ['LcCmd', 'LcWhen', 'LcUser', 'LcHost'];
          __winnow.S.layout.LcHost = { hidden: true };
          __winnow.S.layout.LcCmd = { pinned: true };
          __winnow.renderHead(); __winnow.saveLayout();
        }""")
        layout_saved()
        _section(page)
        with page.expect_response(lambda r: r.request.method == "POST"
                                  and r.url.endswith("/api/column_layouts")):
            page.locator(".tm-layout-default button").click()
        page.wait_for_function("() => /matches the default/.test(document.querySelector('.tm-layout-state').textContent)")

        # B keeps the file's own order, every column shown.
        _open(page, b)
        page.evaluate("(o) => { __winnow.S.order = o; __winnow.S.layout = __winnow.nameMap();"
                      " __winnow.renderHead(); __winnow.saveLayout(); }", HEADERS)
        layout_saved()
        _section(page)
        page.wait_for_function("() => !document.querySelector('.tm-layout-preview.stale')")
        # The card counts the difference rather than just saying one exists,
        # and offers to replace the default rather than to "save" over it
        # silently.
        state = page.locator(".tm-layout-state").inner_text()
        assert "differs from the default in 3 columns" in state, state
        assert page.locator(".tm-layout-default button").inner_text() == "Replace the default with this table’s layout"

        summary = page.locator(".tm-layout-summary").inner_text()
        assert summary.startswith("Applying it changes 3 of 4 columns:"), summary
        assert "1 hidden" in summary and "1 pinned" in summary, summary

        # Only the changed columns are listed, each saying what happens to
        # it. "gets hidden", never "hidden", which is what an unchanged
        # hidden column says in the All columns view below.
        changes = page.locator(".copy-layout-col .tm-change").all_inner_texts()
        rows = page.locator(".copy-layout-col").evaluate_all("(rs) => rs.map((r) => r.dataset.col)")
        assert rows == ["LcCmd", "LcHost"] or rows == ["LcCmd", "LcUser", "LcHost"], rows
        assert any("gets pinned" in c for c in changes), changes
        assert any("moves to 1st" in c for c in changes), changes
        assert any("gets hidden" in c for c in changes), changes
        assert not any(c.strip() in ("hidden", "pinned") for c in changes), changes

        # All columns lists every one, in the order applying would leave
        # them, and an unchanged state is grey beside the name instead.
        page.locator('.tm-layout-view button[data-key="all"]').click()
        page.wait_for_function("() => document.querySelectorAll('.copy-layout-col').length === 4")
        assert page.locator(".copy-layout-col").evaluate_all("(rs) => rs.map((r) => r.dataset.col)") \
            == ["LcCmd", "LcWhen", "LcUser", "LcHost"]

        page.locator("#modalBody .btn", has_text="Apply to this table").click()
        page.wait_for_function("() => __winnow.S.order[0] === 'LcCmd'")
        layout_saved()
        _section(page)
        page.wait_for_function("() => /matches the default/.test(document.querySelector('.tm-layout-state').textContent)")
        page.wait_for_function("() => !document.querySelector('.tm-layout-preview.stale')")
        assert page.locator(".tm-layout-summary").inner_text() == "Applying it changes nothing."
        assert page.locator("#modalBody .btn", has_text="Apply to this table").is_disabled()
        # Nothing changes, so the box shows the layout rather than nothing.
        assert "already has this layout" in page.locator(".tm-layout-same").inner_text()
        assert page.locator(".copy-layout-col").count() == 4
    finally:
        for sid in (a, b):
            api(f"/api/source/{sid}", "DELETE")
        _go_home(page, home)
