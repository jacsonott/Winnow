"""Renaming a derived column from its header menu, driven through the real
dialog: the header takes the new name, and what was set on the column — a
filter, its width — is still set on it afterwards rather than left behind
on a name the table no longer has."""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.ui


def _wait_ready(api, sid, timeout=25.0):
    """Polled from Python: page.wait_for_function does not await a
    promise predicate (see test_merge_flow.py)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(d["status"] == "ready" for d in api(f"/api/derived?source_id={sid}")):
            return
        time.sleep(0.2)
    pytest.fail(f"derived columns on {sid} never finished")


def test_rename_keeps_the_filter_and_width_on_the_column(page, api, tmp_path):
    home = page.evaluate("() => __winnow.S.sourceId")
    f = tmp_path / "dr_rename.csv"
    f.write_text("DrWhen,DrWho\n2026-04-01 09:00:00,u=alice\n2026-04-01 10:00:00,u=bob\n"
                 "2026-04-01 11:00:00,u=carol\n", encoding="utf-8")
    sid = api("/api/ingest/path", "POST", {"path": str(f), "name": "dr_rename.csv", "build_fts": False})["id"]
    try:
        api("/api/derived", "POST", {"source_id": sid, "name": "DrUser", "input_column": "DrWho",
                                     "op_id": "regex_extract", "params": {"pattern": r"u=(\w+)"}})
        _wait_ready(api, sid)
        page.evaluate("(id) => __winnow.loadSources().then(() => __winnow.openSource(id))", sid)
        page.wait_for_function("(id) => __winnow.S.sourceId === id", arg=sid)
        page.wait_for_selector('.hcell[data-col="DrUser"]')
        page.wait_for_selector(".row")

        page.evaluate("""() => { __winnow.S.layout.DrUser = { w: 260 };
          __winnow.renderHead(); __winnow.saveLayout(); }""")
        box = page.locator('.fcell input[data-col="DrUser"]')
        box.fill("alice")
        box.press("Enter")
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 1")

        page.locator('.hcell[data-col="DrUser"]').click(button="right")
        page.locator(".menu .menu-item", has_text="Rename derived column").click()
        page.wait_for_selector(".confirm-input")
        page.locator(".confirm-input").fill("DrAccount")
        page.locator(".confirm-card .btn", has_text="Rename").click()

        page.wait_for_selector('.hcell[data-col="DrAccount"]')
        assert page.locator('.hcell[data-col="DrUser"]').count() == 0
        assert api(f"/api/derived?source_id={sid}")[0]["name"] == "DrAccount"
        # Still filtered to alice, by the column under its new name.
        page.wait_for_function("""() => {
          const i = __winnow.S.columns.findIndex((c) => c.name === 'DrAccount');
          const r = __winnow.rowAt(0);
          return __winnow.S.view.row_count === 1 && r && r.cells[i] === 'alice';
        }""")
        state = page.evaluate("""() => ({
          filters: { ...__winnow.S.filters },
          box: document.querySelector('.fcell input[data-col="DrAccount"]').value,
          width: (__winnow.S.layout.DrAccount || {}).w,
          order: __winnow.S.order,
        })""")
        assert state["filters"] == {"DrAccount": "alice"}, state
        assert state["box"] == "alice"
        assert state["width"] == 260
        assert "DrAccount" in state["order"] and "DrUser" not in state["order"]
    finally:
        api(f"/api/source/{sid}", "DELETE")
        page.evaluate("""(home) => (async () => {
          __winnow.S.sourceId = null;
          await __winnow.loadSources();
          await __winnow.openSource(home);
        })()""", home)
        page.wait_for_selector(".row")
