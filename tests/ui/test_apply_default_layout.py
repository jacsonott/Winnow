"""Putting a header set's saved default layout on a table that already has
a layout of its own.

The report: a column order saved as the default for a header set, then
Reset view on another table with the same headers, and that table kept
its own order. Opening a table seeds the default only into one that has
never saved a layout, and Reset view keeps the layout on purpose, so
nothing in the app could put the default on a table opened before it was
saved. The table menu's Layout section, taking the layout from the
default saved for these columns, is the way to do that now."""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.ui

# A header set of this module's own, so the default it saves in the
# shared workspace can't meet a table any other module opens.
HEADERS = ["AdlWhen", "AdlHost", "AdlUser", "AdlCmd"]


def _csv(tmp_path, name):
    f = tmp_path / name
    f.write_text(",".join(HEADERS) + "\n"
                 "2026-05-01 09:00:00,H1,u=alice,whoami\n"
                 "2026-05-01 10:00:00,H2,u=bob,ipconfig\n", encoding="utf-8")
    return f


def _open(page, sid):
    page.evaluate("(id) => __winnow.loadSources().then(() => __winnow.openSource(id))", sid)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.busyCount === 0", arg=sid)
    page.wait_for_selector(".row")


def _headers(page):
    return page.evaluate("() => [...document.querySelectorAll('.hcell[data-col]')].map((h) => h.dataset.col)")


def _table_menu_button(page, label):
    page.evaluate("() => __winnow.openTableMenu()")
    page.wait_for_selector(".collist-row")
    page.locator("#modalBody .btn", has_text=label).click()


def _layout_section(page):
    page.evaluate("() => __winnow.openTableMenu(undefined, 'layout')")
    page.wait_for_selector(".tm-layout-from")


def _save_default(page):
    _layout_section(page)
    page.locator("#modalBody .btn", has_text="Save this layout as the default").click()


def _apply_default(page):
    """The Layout section opens on the saved default once it has been
    found; Apply layout puts it on the table."""
    _layout_section(page)
    page.wait_for_function("""() => {
      const s = document.querySelector('.tm-layout-from');
      const b = [...document.querySelectorAll('#modalBody .btn')].find((x) => x.textContent === 'Apply layout');
      return s && s.value === 'default' && b && !b.disabled;
    }""")
    page.locator("#modalBody .btn", has_text="Apply layout").click()


def _wait_derived(api, sid, timeout=25.0):
    """Polled from Python: page.wait_for_function does not await a
    promise predicate (see test_merge_flow.py)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(d["status"] == "ready" for d in api(f"/api/derived?source_id={sid}")):
            return
        time.sleep(0.2)
    pytest.fail(f"derived columns on {sid} never finished")


def _go_home(page, home):
    page.evaluate("""(home) => (async () => {
      __winnow.S.sourceId = null;
      await __winnow.loadSources();
      await __winnow.openSource(home);
    })()""", home)
    page.wait_for_selector(".row")


def test_the_default_reaches_a_table_that_already_had_a_layout(page, api, tmp_path, layout_saved):
    home = page.evaluate("() => __winnow.S.sourceId")
    a = api("/api/ingest/path", "POST", {"path": str(_csv(tmp_path, "adl_a.csv")),
                                         "name": "adl_a.csv", "build_fts": False})["id"]
    b = api("/api/ingest/path", "POST", {"path": str(_csv(tmp_path, "adl_b.csv")),
                                         "name": "adl_b.csv", "build_fts": False})["id"]
    try:
        # B gets a derived column, and a layout of its own the way the
        # report's did: Reset view saves the layout it leaves alone.
        api("/api/derived", "POST", {"source_id": b, "name": "AdlName", "input_column": "AdlUser",
                                     "op_id": "regex_extract", "params": {"pattern": r"u=(\w+)"}})
        _wait_derived(api, b)
        _open(page, b)
        page.evaluate("() => { __winnow.S.layout.AdlName = { w: 222 }; __winnow.renderHead(); }")
        _table_menu_button(page, "Reset view")
        layout_saved()
        b_before = page.evaluate("() => [...__winnow.S.order]")
        assert b_before == HEADERS[:3] + ["AdlName", "AdlCmd"], b_before

        # A: reorder, hide one, widen one, and save that as the default.
        _open(page, a)
        page.evaluate("""() => {
          __winnow.S.order = ['AdlCmd', 'AdlUser', 'AdlWhen', 'AdlHost'];
          __winnow.S.layout.AdlHost = { hidden: true };
          __winnow.S.layout.AdlCmd = { w: 333 };
          __winnow.renderHead(); __winnow.saveLayout();
        }""")
        layout_saved()
        with page.expect_response(lambda r: r.request.method == "POST"
                                  and r.url.endswith("/api/column_layouts")):
            _save_default(page)
        # The toast goes up once the save's answer is read, a beat after
        # the response itself.
        page.wait_for_function("() => /keeps it until/.test(document.querySelector('#toast').textContent)")

        # Back on B, which opens to its own layout: that part is unchanged.
        _open(page, b)
        assert page.evaluate("() => [...__winnow.S.order]") == b_before

        _apply_default(page)
        page.wait_for_function("() => __winnow.S.order[0] === 'AdlCmd'")
        # A's order, with B's derived column back beside its input and
        # keeping the width B gave it; A's hidden column hidden here too.
        assert page.evaluate("() => [...__winnow.S.order]") == \
            ["AdlCmd", "AdlUser", "AdlName", "AdlWhen", "AdlHost"]
        assert _headers(page) == ["AdlCmd", "AdlUser", "AdlName", "AdlWhen"]
        assert page.evaluate("() => __winnow.S.layout.AdlCmd.w") == 333
        assert page.evaluate("() => __winnow.S.layout.AdlName.w") == 222
        layout_saved()
        # Saved, so it is what B opens to from now on.
        _go_home(page, home)
        _open(page, b)
        assert _headers(page) == ["AdlCmd", "AdlUser", "AdlName", "AdlWhen"]

        # Undo puts B's own layout back.
        page.evaluate("""() => { __winnow.S.order = [...__winnow.S.order].reverse();
          __winnow.renderHead(); __winnow.saveLayout(); }""")
        layout_saved()
        reversed_order = page.evaluate("() => [...__winnow.S.order]")
        _apply_default(page)
        page.wait_for_function("() => __winnow.S.order[0] === 'AdlCmd'")
        page.locator("#toast .toast-action", has_text="Undo").click()
        page.wait_for_function("(o) => __winnow.S.order.join() === o.join()", arg=reversed_order)
        layout_saved()
    finally:
        for sid in (a, b):
            api(f"/api/source/{sid}", "DELETE")
        _go_home(page, home)


def test_with_no_default_saved_it_says_so_and_leaves_the_layout(page, api, tmp_path):
    home = page.evaluate("() => __winnow.S.sourceId")
    f = tmp_path / "adl_none.csv"
    f.write_text("AdlNoneOne,AdlNoneTwo\nx,y\n", encoding="utf-8")
    sid = api("/api/ingest/path", "POST", {"path": str(f), "name": "adl_none.csv", "build_fts": False})["id"]
    try:
        _open(page, sid)
        before = page.evaluate("() => [...__winnow.S.order]")
        _layout_section(page)
        page.wait_for_function("() => /none saved yet/.test(document.querySelector('.tm-layout-from').textContent)")
        assert page.locator(".tm-layout-from option[value=default]").is_disabled()
        assert "no default is saved" in page.locator("#modalBody .mb-notes").inner_text()
        assert page.locator("#modalBody .btn", has_text="Apply layout").is_disabled()
        assert page.evaluate("() => [...__winnow.S.order]") == before
    finally:
        api(f"/api/source/{sid}", "DELETE")
        _go_home(page, home)


def test_applying_while_grouped_keeps_the_grouped_column_out_until_ungrouped(page, api, tmp_path, layout_saved):
    """Grouping keeps its column in S.order and hides it from the grid
    (#423); a layout applied meanwhile must not put it back on screen,
    and Ungroup shows it where the default puts it."""
    home = page.evaluate("() => __winnow.S.sourceId")
    a = api("/api/ingest/path", "POST", {"path": str(_csv(tmp_path, "adl_ga.csv")),
                                         "name": "adl_ga.csv", "build_fts": False})["id"]
    b = api("/api/ingest/path", "POST", {"path": str(_csv(tmp_path, "adl_gb.csv")),
                                         "name": "adl_gb.csv", "build_fts": False})["id"]
    try:
        # A opens to the default the first test saved for these headers,
        # which hides AdlHost; this one wants every column showing.
        _open(page, a)
        page.evaluate("""() => { __winnow.S.order = ['AdlCmd', 'AdlUser', 'AdlWhen', 'AdlHost'];
          __winnow.S.layout = __winnow.nameMap(); __winnow.renderHead(); __winnow.saveLayout(); }""")
        layout_saved()
        with page.expect_response(lambda r: r.request.method == "POST"
                                  and r.url.endswith("/api/column_layouts")):
            _save_default(page)

        # B opens to that default (it never saved a layout), so give it an
        # order of its own for the default to replace.
        _open(page, b)
        page.evaluate("(o) => { __winnow.S.order = o; __winnow.renderHead(); __winnow.saveLayout(); }", HEADERS)
        layout_saved()
        page.evaluate("() => __winnow.addGroupLevel('AdlHost')")
        page.wait_for_function("() => !document.querySelector('.hcell[data-col=\"AdlHost\"]')")
        _layout_section(page)
        page.wait_for_function("() => !document.querySelector('.tm-layout-preview.stale')"
                               " && document.querySelector('.copy-layout-col')?.dataset.col === 'AdlCmd'")
        listed = page.locator(".copy-layout-col").evaluate_all("(rs) => rs.map((r) => r.dataset.col)")
        assert listed == ["AdlCmd", "AdlUser", "AdlWhen"], "the grouped column is left out of the preview"
        _apply_default(page)
        page.wait_for_function("() => __winnow.S.order[0] === 'AdlCmd'")
        assert _headers(page) == ["AdlCmd", "AdlUser", "AdlWhen"]
        page.evaluate("() => __winnow.dropGrouping()")
        page.wait_for_function("() => document.querySelector('.hcell[data-col=\"AdlHost\"]')")
        assert _headers(page) == ["AdlCmd", "AdlUser", "AdlWhen", "AdlHost"]
        layout_saved()
    finally:
        for sid in (a, b):
            api(f"/api/source/{sid}", "DELETE")
        _go_home(page, home)
