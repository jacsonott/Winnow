"""A Saved filters row is a name and a ⋯, not a name-shaped box and five
buttons.

Each row had Edit, Rename, ▲, ▼ and ✕, and the name was a button styled
like a text box. The name reads as text now, as a table's does in the
Tables manager — clicking it still applies the filter — the row drags to
reorder within its header set, and Edit, Rename, Move up, Move down and
Delete are in the row's ⋯ menu. Move up and
Move down are there so the order can still be changed without a mouse,
which is why the focus goes back to the row's ⋯ after a move.

The filters here are real saved filters in the session's workspace, made
for ui.csv's columns and deleted afterwards.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

UI_COLUMNS = "() => __winnow.S.sources.find((s) => s.id === __winnow.S.sourceId).columns.map((c) => c.name)"


def _payload(event_id):
    return {"filter_tree": {"type": "group", "op": "AND", "children": [
        {"type": "cond", "column": "EventId", "op": "in", "value": [event_id]}]},
        "search": "", "search_mode": "contains", "search_terms": []}


@pytest.fixture
def two_filters(page, api):
    cols = page.evaluate(UI_COLUMNS)
    a = api("/api/saved_filters", "POST", {"name": "rows probe A", "col_names": cols, "payload": _payload("4624")})
    b = api("/api/saved_filters", "POST", {"name": "rows probe B", "col_names": cols, "payload": _payload("4625")})
    page.evaluate("() => __winnow.loadSavedFilters()")
    yield a, b
    page.evaluate("() => { if (!document.getElementById('modal').hidden) __winnow.closeModal(); }")
    for f in (a, b):
        api(f"/api/saved_filters/{f['id']}", "DELETE")
    page.evaluate("""async () => {
      await __winnow.loadSavedFilters();
      __winnow.S.filterTree = { type: 'group', op: 'AND', children: [] };
      __winnow.updateFiltersButton();
      await __winnow.rebuildView({ keepScroll: false });
    }""")


def _open(page):
    page.evaluate("() => __winnow.openSavedFiltersModal()")
    page.wait_for_selector(".sf-row")


def _row(page, f):
    return page.locator(f".sf-row[data-id='{f['id']}']")


def _order(api, *filters):
    ids = [f["id"] for f in api("/api/saved_filters")]
    return [next(f["name"] for f in filters if f["id"] == i) for i in ids if i in {f["id"] for f in filters}]


def test_a_row_is_a_plain_name_and_one_button(page, two_filters):
    a, _ = two_filters
    _open(page)
    row = _row(page, a)
    # The name (what applies it) and the ⋯; nothing else to press.
    assert row.locator("button").count() == 2, row.inner_html()
    assert row.locator("button.sf-more").count() == 1, row.inner_html()
    looks = row.locator(".sf-name").evaluate("""(n) => {
      const cs = getComputedStyle(n);
      return { tag: n.tagName, border: cs.borderTopStyle, bg: cs.backgroundColor };
    }""")
    # Not a box: no border and nothing filled behind the text.
    assert looks["tag"] != "INPUT", looks
    assert looks["border"] == "none", looks
    assert looks["bg"] == "rgba(0, 0, 0, 0)", looks
    page.keyboard.press("Escape")


def test_clicking_the_name_applies_the_filter(page, two_filters):
    a, _ = two_filters
    _open(page)
    _row(page, a).locator(".sf-name").click()
    page.wait_for_selector("#modal[hidden]", state="attached")
    page.wait_for_function("() => __winnow.S.view.row_count === 50")


def test_enter_on_a_name_applies_it_too(page, two_filters):
    """The name stopped being a <button>; it must not stop being reachable
    and usable from the keyboard."""
    _, b = two_filters
    _open(page)
    name = _row(page, b).locator(".sf-name")
    name.focus()
    assert page.evaluate("() => document.activeElement.classList.contains('sf-name')")
    page.keyboard.press("Enter")
    page.wait_for_selector("#modal[hidden]", state="attached")
    page.wait_for_function("() => __winnow.S.view.row_count === 50")


def test_move_down_and_up_from_the_keyboard(page, api, two_filters, until):
    a, b = two_filters
    assert _order(api, a, b) == ["rows probe A", "rows probe B"]
    _open(page)

    def choose(label):
        page.keyboard.press("Enter")   # the ⋯ has the focus
        page.wait_for_selector(".menu .menu-item")
        for _ in range(8):
            page.keyboard.press("ArrowDown")
            if page.evaluate("() => document.activeElement.textContent.trim()") == label:
                break
        else:
            pytest.fail(f"{label!r} not reachable with the arrow keys")
        page.keyboard.press("Enter")

    _row(page, a).locator(".sf-more").focus()
    choose("Move down")
    until(page, lambda: _order(api, a, b) == ["rows probe B", "rows probe A"], "Move down to save")
    # Back on the same filter's ⋯, so a second move is the same keys again.
    page.wait_for_function(
        "(id) => document.activeElement.classList.contains('sf-more')"
        " && document.activeElement.closest('.sf-row').dataset.id === String(id)", arg=a["id"])
    choose("Move up")
    until(page, lambda: _order(api, a, b) == ["rows probe A", "rows probe B"], "Move up to save")
    page.keyboard.press("Escape")


def test_dragging_a_row_reorders_its_header_set(page, api, two_filters, until):
    a, b = two_filters
    _open(page)
    target = _row(page, b)
    box = target.bounding_box()
    # Onto B's lower half: A lands after it.
    _row(page, a).drag_to(target, target_position={"x": 60, "y": box["height"] - 3})
    until(page, lambda: _order(api, a, b) == ["rows probe B", "rows probe A"], "the drag to save")
    page.wait_for_function(
        "([a, b]) => { const ids = [...document.querySelectorAll('.sf-row')].map((r) => Number(r.dataset.id));"
        " return ids.indexOf(b) < ids.indexOf(a); }", arg=[a["id"], b["id"]])
    page.keyboard.press("Escape")


def test_delete_from_the_menu_still_asks(page, api, two_filters, until):
    a, b = two_filters
    _open(page)
    _row(page, b).locator(".sf-more").click()
    page.locator(".menu .menu-item", has_text="Delete…").click()
    card = page.locator(".confirm-card")
    card.wait_for()
    assert "rows probe B" in card.inner_text()
    card.locator(".btn", has_text="Cancel").click()
    page.wait_for_selector(".confirm-overlay", state="detached")
    assert any(f["id"] == b["id"] for f in api("/api/saved_filters"))

    _row(page, b).locator(".sf-more").click()
    page.locator(".menu .menu-item", has_text="Delete…").click()
    page.locator(".confirm-card .btn", has_text="Delete").click()
    until(page, lambda: all(f["id"] != b["id"] for f in api("/api/saved_filters")), "the delete to land")
    assert _row(page, b).count() == 0
    assert _row(page, a).count() == 1
    page.keyboard.press("Escape")


def _menu(page, f, label):
    _row(page, f).locator(".sf-more").click()
    page.locator(".menu .menu-item", has_text=label).click()


def test_rename_from_the_menu_saves_and_repaints_the_row(page, api, two_filters, until):
    a, _ = two_filters
    _open(page)
    _menu(page, a, "Rename…")
    box = page.locator(".confirm-input")
    box.wait_for()
    assert box.input_value() == "rows probe A"
    box.fill("  rows probe renamed  ")
    page.locator(".confirm-card .btn", has_text="OK").click()

    until(page, lambda: any(f["id"] == a["id"] and f["name"] == "rows probe renamed"
                            for f in api("/api/saved_filters")), "the rename to save")
    page.wait_for_function("(id) => document.querySelector(`.sf-row[data-id='${id}'] .sf-name`)"
                           ".textContent.includes('rows probe renamed')", arg=a["id"])
    # The menu's own button has the focus back, as after a move.
    page.wait_for_function(
        "(id) => document.activeElement.classList.contains('sf-more')"
        " && document.activeElement.closest('.sf-row').dataset.id === String(id)", arg=a["id"])
    assert page.evaluate("(id) => __winnow.S.savedFilters.find((f) => f.id === id).name", a["id"]) \
        == "rows probe renamed"

    # A blank name is not a name: nothing is sent and nothing changes.
    sent = []
    page.on("request", lambda r: sent.append(r.method) if f"/api/saved_filters/{a['id']}" in r.url else None)
    _menu(page, a, "Rename…")
    page.locator(".confirm-input").fill("   ")
    page.locator(".confirm-card .btn", has_text="OK").click()
    page.wait_for_selector(".confirm-overlay", state="detached")
    # Nothing to wait for: the check is that no request was made.
    page.wait_for_timeout(300)
    assert sent == []
    assert next(f["name"] for f in api("/api/saved_filters") if f["id"] == a["id"]) == "rows probe renamed"
    page.keyboard.press("Escape")


def test_edit_applies_the_filter_and_update_saves_the_new_conditions(page, api, two_filters, until):
    """Edit… applies the filter, so the grid behind the builder is live
    feedback, and opens the builder on it; Update writes the conditions
    back to the same record and leaves the header set it was saved for."""
    a, _ = two_filters
    _open(page)
    _menu(page, a, "Edit…")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent"
                           " === 'Edit filter — rows probe A'")
    page.wait_for_function("() => __winnow.S.view.row_count === 50 && __winnow.busyCount === 0")
    sql = page.locator("#modal .fb-sql")
    assert "4624" in sql.input_value()

    sql.fill("\"EventId\" IN ('4625', '4688')")
    page.locator("#modal .btn", has_text='Update "rows probe A"').click()
    page.wait_for_selector("#modal[hidden]", state="attached")
    page.wait_for_function("() => (document.getElementById('toast').textContent || '')"
                           ".includes('Updated filter \"rows probe A\"')")
    saved = next(f for f in api("/api/saved_filters") if f["id"] == a["id"])
    text = str(saved["payload"]["filter_tree"])
    assert "4625" in text and "4688" in text and "4624" not in text, saved["payload"]
    assert saved["col_names"] == a["col_names"] and saved["name"] == "rows probe A"


def test_edit_asks_first_for_a_filter_built_for_other_columns(page, api, two_filters):
    """A filter saved for another header set is edited against the open
    table only after a yes; a no leaves the grid as it was."""
    other = api("/api/saved_filters", "POST", {"name": "rows probe other", "col_names": ["Elsewhere", "Only"],
                                               "payload": _payload("4624")})
    try:
        page.evaluate("() => __winnow.loadSavedFilters()")
        before = page.evaluate("() => __winnow.S.view.row_count")
        page.evaluate("() => __winnow.openSavedFiltersModal({ section: 'all' })")
        row = page.locator(f".sf-row[data-id='{other['id']}']")
        row.wait_for()
        row.locator(".sf-more").click()
        page.locator(".menu .menu-item", has_text="Edit…").click()
        card = page.locator(".confirm-card")
        card.wait_for()
        assert "built for a different column set (Elsewhere, Only)" in card.inner_text()
        card.locator(".btn", has_text="Cancel").click()
        page.wait_for_selector(".confirm-overlay", state="detached")
        assert page.evaluate("() => document.getElementById('modalTitle').textContent") == "Saved filters"
        assert page.evaluate("() => __winnow.S.view.row_count") == before
        assert not page.evaluate("() => __winnow.hasActiveFilterTree()")
    finally:
        api(f"/api/saved_filters/{other['id']}", "DELETE")
        page.evaluate("() => __winnow.loadSavedFilters()")
