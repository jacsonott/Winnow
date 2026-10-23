"""Copying another table's column layout onto the table on screen, for the
columns the two share.

The default layout for a header set reaches only tables with exactly the
same columns, and saving one changes what every later import of them
opens to. What was asked for was one table's order put on another, and
two exports of the same log from two tool versions differ by a column or
two. Taking the layout from another table in the table menu's Layout
section does that: the shared columns take the other table's order,
visibility and widths, and a column only this table has stays beside the
column it follows."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _ingest(api, tmp_path, name, headers):
    f = tmp_path / name
    f.write_text(",".join(headers) + "\n" + ",".join(f"{h}-1" for h in headers) + "\n", encoding="utf-8")
    return api("/api/ingest/path", "POST", {"path": str(f), "name": name, "build_fts": False})["id"]


def _open(page, sid):
    page.evaluate("(id) => __winnow.loadSources().then(() => __winnow.openSource(id))", sid)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.busyCount === 0", arg=sid)
    page.wait_for_selector(".row")


def _headers(page):
    return page.evaluate("() => [...document.querySelectorAll('.hcell[data-col]')].map((h) => h.dataset.col)")


def test_copying_a_layout_between_tables_whose_columns_differ(page, api, tmp_path, layout_saved):
    home = page.evaluate("() => __winnow.S.sourceId")
    a = _ingest(api, tmp_path, "cl_a.csv", ["CpWhen", "CpHost", "CpUser", "CpCmd"])
    # One more column than A, after CpWhen: a newer export of the same log.
    b = _ingest(api, tmp_path, "cl_b.csv", ["CpWhen", "CpExtra", "CpHost", "CpUser", "CpCmd"])
    unrelated = _ingest(api, tmp_path, "cl_unrelated.csv", ["CpNothingShared"])
    try:
        _open(page, a)
        page.evaluate("""() => {
          __winnow.S.order = ['CpCmd', 'CpUser', 'CpWhen', 'CpHost'];
          __winnow.S.layout.CpHost = { hidden: true };
          __winnow.S.layout.CpCmd = { w: 333 };
          __winnow.renderHead(); __winnow.saveLayout();
        }""")
        layout_saved()

        _open(page, b)
        page.evaluate("() => { __winnow.S.layout.CpExtra = { w: 211 }; __winnow.renderHead(); __winnow.saveLayout(); }")
        layout_saved()
        own = page.evaluate("() => [...__winnow.S.order]")
        assert own == ["CpWhen", "CpExtra", "CpHost", "CpUser", "CpCmd"]

        page.evaluate("() => __winnow.openTableMenu(undefined, 'layout')")
        page.wait_for_selector(".tm-layout-from")
        listed = page.locator(".tm-layout-from option").all_inner_texts()
        assert any("cl_a.csv" in t and "4 of 5 columns" in t for t in listed), listed
        assert not any("cl_unrelated.csv" in t for t in listed), "a table with no column in common is not offered"
        key = page.locator(".tm-layout-from option", has_text="cl_a.csv").get_attribute("value")
        page.locator(".tm-layout-from").select_option(key)
        page.wait_for_function("() => !document.querySelector('.tm-layout-preview.stale')"
                               " && document.querySelector('.copy-layout-col')?.dataset.col === 'CpCmd'")
        want = ["CpCmd", "CpUser", "CpWhen", "CpExtra", "CpHost"]
        assert page.locator(".copy-layout-col").evaluate_all("(rs) => rs.map((r) => r.dataset.col)") == want
        # Only what applying would change is marked: CpExtra keeps its
        # place after CpWhen, and the moves are the shared columns'.
        changed = page.locator(".copy-layout-col.tm-changed").evaluate_all("(rs) => rs.map((r) => r.dataset.col)")
        assert "CpExtra" not in changed and "CpCmd" in changed and "CpHost" in changed, changed
        page.locator("#modalBody .btn", has_text="Apply layout").click()
        page.wait_for_function("(w) => __winnow.S.order.join() === w.join()", arg=want)

        # A's order, A's hidden column hidden, A's width; B's extra column
        # beside the column it followed, with its own width.
        assert _headers(page) == ["CpCmd", "CpUser", "CpWhen", "CpExtra"]
        assert page.evaluate("() => __winnow.S.layout.CpCmd.w") == 333
        assert page.evaluate("() => __winnow.S.layout.CpExtra.w") == 211
        layout_saved()

        page.locator("#toast .toast-action", has_text="Undo").click()
        page.wait_for_function("(o) => __winnow.S.order.join() === o.join()", arg=own)
        assert _headers(page) == own
        layout_saved()
    finally:
        for sid in (a, b, unrelated):
            api(f"/api/source/{sid}", "DELETE")
        page.evaluate("""(home) => (async () => {
          __winnow.S.sourceId = null;
          await __winnow.loadSources();
          await __winnow.openSource(home);
        })()""", home)
        page.wait_for_selector(".row")
