"""A Saved filters row is a name and a ⋯, not a name-shaped box and five
buttons.

Each row had Edit, Rename, ▲, ▼ and ✕, and the name was a button styled
like a text box. The name is text now — clicking it still applies the
filter — the row drags to reorder within its header set, and Edit,
Rename, Move up, Move down and Delete are in the row's ⋯ menu. Move up and
Move down are there so the order can still be changed without a mouse,
which is why the focus goes back to the row's ⋯ after a move.

The filters here are real saved filters in the session's workspace, made
for ui.csv's columns and deleted afterwards.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.ui

UI_COLUMNS = ["Timestamp", "EventId", "Host", "ExtremelyLongColumnHeaderName", "CommandLine"]


def _payload(event_id):
    return {"filter_tree": {"type": "group", "op": "AND", "children": [
        {"type": "cond", "column": "EventId", "op": "in", "value": [event_id]}]},
        "search": "", "search_mode": "contains", "search_terms": []}


@pytest.fixture
def two_filters(page, api):
    a = api("/api/saved_filters", "POST", {"name": "rows probe A", "col_names": UI_COLUMNS, "payload": _payload("4624")})
    b = api("/api/saved_filters", "POST", {"name": "rows probe B", "col_names": UI_COLUMNS, "payload": _payload("4625")})
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


def _until(probe, what, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe():
            return
        time.sleep(0.1)
    pytest.fail(f"timed out waiting for {what}")


def test_a_row_is_a_plain_name_and_one_button(page, two_filters):
    a, _ = two_filters
    _open(page)
    row = _row(page, a)
    assert row.locator("button").count() == 1, row.inner_html()
    looks = row.locator(".sf-name").evaluate("""(n) => {
      const cs = getComputedStyle(n);
      return { tag: n.tagName, border: cs.borderTopStyle, bg: cs.backgroundColor };
    }""")
    # Not a box: no border and nothing filled behind the text.
    assert looks["tag"] not in ("BUTTON", "INPUT"), looks
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


def test_move_down_and_up_from_the_keyboard(page, api, two_filters):
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
    _until(lambda: _order(api, a, b) == ["rows probe B", "rows probe A"], "Move down to save")
    # Back on the same filter's ⋯, so a second move is the same keys again.
    page.wait_for_function(
        "(id) => document.activeElement.classList.contains('sf-more')"
        " && document.activeElement.closest('.sf-row').dataset.id === String(id)", arg=a["id"])
    choose("Move up")
    _until(lambda: _order(api, a, b) == ["rows probe A", "rows probe B"], "Move up to save")
    page.keyboard.press("Escape")


def test_dragging_a_row_reorders_its_header_set(page, api, two_filters):
    a, b = two_filters
    _open(page)
    target = _row(page, b)
    box = target.bounding_box()
    # Onto B's lower half: A lands after it.
    _row(page, a).drag_to(target, target_position={"x": 60, "y": box["height"] - 3})
    _until(lambda: _order(api, a, b) == ["rows probe B", "rows probe A"], "the drag to save")
    page.wait_for_function(
        "([a, b]) => { const ids = [...document.querySelectorAll('.sf-row')].map((r) => Number(r.dataset.id));"
        " return ids.indexOf(b) < ids.indexOf(a); }", arg=[a["id"], b["id"]])
    page.keyboard.press("Escape")


def test_delete_from_the_menu_still_asks(page, api, two_filters):
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
    _until(lambda: all(f["id"] != b["id"] for f in api("/api/saved_filters")), "the delete to land")
    assert _row(page, b).count() == 0
    assert _row(page, a).count() == 1
    page.keyboard.press("Escape")
