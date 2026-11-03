"""Every default shortcut, pressed.

DEFAULT_KEYMAP is about fifty keys, and until this module a third of the
actions they name had never been pressed by any test: PageDown/PageUp,
`0`, the saved-filter keys (`q` `w` `[` `]`), `f` and `F`, the backtick,
`x`, `L`, `Q` and `.`. Most of those handlers have no other way in, either
— no button, no menu entry — so a dispatcher change that stopped them
firing, or a handler that threw, would have shipped with every check
green, and an analyst reaching for the key would have been the first to
know.

Two halves:

* The first test presses EVERY default binding with the handlers swapped
  for recorders, and checks each one reached the action it is bound to
  and nothing else. That is the dispatcher's half — keySpecFromEvent,
  matchAction's Shift fallback, the gates in front of it — and it is
  driven from DEFAULT_KEYMAP itself, so an action given a default key
  later is pressed here without anyone remembering to add it. One page,
  no side effects: nothing the keys would do actually happens.
* The rest press the keys whose handlers nothing else reaches, for real,
  and assert what the analyst would see change.

Everything here runs against the shared ui.csv table and puts it back:
saved filters and the SQL tab are deleted by id, the layout is restored
and saved, and the default layout `L` would write to the shared workspace
is answered by a route instead of reaching it.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

# 200 rows; Host cycles H0..H4 and EventId through four values, so a
# Host value has 40 rows, an EventId value 50, and one of each 10 —
# whatever order the table happens to be sorted in.
ROWS, PER_HOST, PER_EVENT, PER_HOST_AND_EVENT = 200, 40, 50, 10

MODIFIERS = {"Ctrl": "Control", "Meta": "Meta", "Alt": "Alt", "Shift": "Shift"}


def _playwright_key(spec):
    """'Ctrl+ArrowUp' → 'Control+ArrowUp'. A printable key keeps its case:
    Shift lives in the capital (keySpecFromEvent), so 'G' is pressed as G."""
    *mods, key = spec.split("+")
    return "+".join([MODIFIERS[m] for m in mods] + [key])


@pytest.fixture(autouse=True)
def flat_unfiltered(page):
    """Each test starts on the shared table's whole, flat view with the
    grid focused, and leaves it that way."""
    page.wait_for_selector("#body .row")
    page.locator("#body").focus()
    yield
    page.evaluate("""async () => {
      if (!document.getElementById('modal').hidden) __winnow.closeModal();
      if (__winnow.S.activeTab !== 'grid') __winnow.showGridTab();
      if (__winnow.S.groupByCols.length) await __winnow.dropGrouping();
      __winnow.selClear();
      __winnow.clearCellSelection();
      await __winnow.clearAllFilters();
    }""")
    page.wait_for_function("() => __winnow.busyCount === 0")


def _view_rows(page):
    return page.evaluate("() => __winnow.S.view.row_count")


def _cell(page, pos, column):
    ci = page.evaluate("(c) => __winnow.visibleCols().indexOf(c)", column)
    assert ci >= 0, f"{column} is not on screen"
    return page.locator(f"#body .row[data-pos='{pos}'] .cell").nth(ci)


def _value(page, pos, column):
    return page.evaluate("""([pos, column]) => __winnow.rowAt(pos)
        .cells[__winnow.S.columns.findIndex((c) => c.name === column)]""", [pos, column])


def _filter_box(page, column):
    return page.locator(f".fcell input[data-col='{column}']").input_value()


def _toast(page, text):
    page.wait_for_function("(t) => (document.getElementById('toast').textContent || '').includes(t)", arg=text)


# --- the dispatcher, for every binding --------------------------------------

def test_every_default_key_reaches_its_action(page):
    keymap = page.evaluate("() => __winnow.DEFAULT_KEYMAP")
    bindings = [(action, key) for action, keys in keymap.items() for key in keys]
    assert len(bindings) > 40, "DEFAULT_KEYMAP did not come back from the page"
    page.evaluate("""() => {
      window.__fired = [];
      const handlers = __winnow.ACTION_HANDLERS;
      for (const name of Object.keys(handlers)) handlers[name] = () => { window.__fired.push(name); };
    }""")
    wrong = []
    for action, key in bindings:
        page.locator("#body").focus()
        page.evaluate("() => { window.__fired = []; }")
        page.keyboard.press(_playwright_key(key))
        fired = page.evaluate("() => window.__fired")
        if action == "focusSearch" and key == "Ctrl+f":
            # Taken ahead of the keymap (find-in-page can't see a virtual
            # grid), so it opens the search box itself, not via a handler.
            focused = page.evaluate("() => document.activeElement && document.activeElement.id")
            if fired or focused != "search":
                wrong.append(f"{key!r}: fired {fired}, focus on #{focused}, wanted the search box")
            page.keyboard.press("Escape")
            continue
        if fired != [action]:
            wrong.append(f"{key!r}: fired {fired}, wanted [{action!r}]")
    assert not wrong, "default keys that don't reach their action:\n  " + "\n  ".join(wrong)


# --- the keys whose handlers nothing else reaches ---------------------------

def test_page_down_and_page_up_move_the_cell_cursor_a_screen_at_a_time(page):
    _cell(page, 0, "Host").click()
    stride = page.evaluate("""() => Math.floor((document.getElementById('body').clientHeight
        - __winnow.headH()) / __winnow.ROW_H) - 1""")
    assert 1 < stride < ROWS // 2, f"a {stride}-row screen can't tell a page from a step"

    page.keyboard.press("PageDown")
    page.wait_for_function("(n) => __winnow.S.cellFocus.pos === n", arg=stride)
    assert page.evaluate("() => __winnow.S.cursor") == stride
    # The row it landed on was scrolled to and wears the cursor ring.
    ring = page.locator(f"#body .row[data-pos='{stride}'] .cell.cell-active")
    ring.wait_for()
    body, cell = page.locator("#body").bounding_box(), ring.bounding_box()
    assert body["y"] <= cell["y"] and cell["y"] + cell["height"] <= body["y"] + body["height"]

    page.keyboard.press("PageDown")
    page.wait_for_function("(n) => __winnow.S.cellFocus.pos === n", arg=2 * stride)
    page.keyboard.press("PageUp")
    page.wait_for_function("(n) => __winnow.S.cellFocus.pos === n", arg=stride)
    page.keyboard.press("PageUp")
    page.wait_for_function("() => __winnow.S.cellFocus.pos === 0")

    # Shift grows the rectangle a screen at a time, from where it started.
    page.keyboard.press("Shift+PageDown")
    page.wait_for_function("(n) => __winnow.S.cellFocus.pos === n", arg=stride)
    rng = page.evaluate("() => __winnow.S.cellRange")
    assert (rng["r0"], rng["r1"]) == (0, stride)


def test_zero_puts_every_column_back_to_its_default_width(page, layout_saved):
    saved = page.evaluate("() => JSON.stringify(__winnow.S.layout)")
    width = "(c) => document.querySelector(`.hcell[data-col='${c}']`).getBoundingClientRect().width"
    try:
        defaults = page.evaluate("""() => {
          for (const c of ['Host', 'EventId']) if (__winnow.S.layout[c]) delete __winnow.S.layout[c].w;
          __winnow.renderHead(); __winnow.render();
          return ['Host', 'EventId'].map((c) =>
            document.querySelector(`.hcell[data-col='${c}']`).getBoundingClientRect().width);
        }""")
        page.evaluate("""() => {
          for (const [c, w] of [['Host', 333], ['EventId', 222]])
            __winnow.S.layout[c] = { ...(__winnow.S.layout[c] || {}), w };
          __winnow.renderHead(); __winnow.render();
        }""")
        assert [page.evaluate(width, c) for c in ("Host", "EventId")] == [333, 222]

        page.locator("#body").focus()
        page.keyboard.press("0")
        page.wait_for_function(f"(want) => ['Host', 'EventId'].every((c, i) => ({width})(c) === want[i])",
                               arg=defaults)
        assert not any("w" in (page.evaluate("(c) => __winnow.S.layout[c] || {}", c)) for c in ("Host", "EventId"))
        _toast(page, "Column widths reset to default")
    finally:
        page.evaluate("""(saved) => {
          __winnow.S.layout = JSON.parse(saved);
          __winnow.renderHead(); __winnow.render(); __winnow.saveLayout();
        }""", saved)
        layout_saved()


def test_q_and_w_step_through_the_saved_filters_and_out_the_end(page, api, view_rebuilt):
    page.keyboard.press("w")
    _toast(page, "No saved filters for these columns")

    cols = page.evaluate("() => __winnow.baseColumns().map((c) => c.name)")

    def cond(column, value):
        return {"filter_tree": {"type": "group", "op": "AND", "children": [
            {"type": "cond", "column": column, "op": "in", "value": [value]}]},
            "search": "", "search_mode": "contains", "search_terms": []}

    made = []
    try:
        made.append(api("/api/saved_filters", "POST",
                        {"name": "keys probe event", "col_names": cols, "payload": cond("EventId", "4624")}))
        made.append(api("/api/saved_filters", "POST",
                        {"name": "keys probe host", "col_names": cols, "payload": cond("Host", "H1")}))
        page.evaluate("() => __winnow.loadSavedFilters()")
        order = [f["name"] for f in page.evaluate("() => __winnow.filtersForCurrentSource()")]
        assert order == ["keys probe event", "keys probe host"]
        rows = {"keys probe event": PER_EVENT, "keys probe host": PER_HOST, None: ROWS}

        def press(key, want):
            page.locator("#body").focus()
            with view_rebuilt(page):
                page.keyboard.press(key)
            active = page.evaluate("() => { const f = __winnow.activeSavedFilterRecord(); return f && f.name; }")
            assert (active, _view_rows(page)) == (want, rows[want]), f"after {key!r}"

        press("w", "keys probe event")
        press("w", "keys probe host")
        # Past the last one is "no filter", its own stop, not a wrap.
        press("w", None)
        press("q", "keys probe host")
        press("[", "keys probe event")
        press("]", "keys probe host")
    finally:
        for f in made:
            api(f"/api/saved_filters/{f['id']}", "DELETE")
        page.evaluate("() => __winnow.loadSavedFilters()")


def test_f_filters_to_the_selected_cell_and_F_drops_every_other_filter(page, view_rebuilt):
    _cell(page, 1, "Host").click()
    host = _value(page, 1, "Host")
    with view_rebuilt(page):
        page.keyboard.press("f")
    assert _view_rows(page) == PER_HOST
    assert host in _filter_box(page, "Host")

    # A second f narrows: the filters add up.
    _cell(page, 0, "EventId").click()
    event = _value(page, 0, "EventId")
    with view_rebuilt(page):
        page.keyboard.press("f")
    assert _view_rows(page) == PER_HOST_AND_EVENT
    assert host in _filter_box(page, "Host") and event in _filter_box(page, "EventId")

    # F keeps only the cell's own filter.
    _cell(page, 0, "EventId").click()
    with view_rebuilt(page):
        page.keyboard.press("F")
    assert _view_rows(page) == PER_EVENT
    assert _filter_box(page, "Host") == ""
    assert event in _filter_box(page, "EventId")


def test_backtick_hides_and_shows_the_table_list(page):
    sidebar = page.locator("#sidebar")
    assert sidebar.is_visible()
    remembered = "() => JSON.parse(localStorage.getItem('winnow.sidebar') || '{}').collapsed"

    page.keyboard.press("`")
    sidebar.wait_for(state="hidden")
    assert page.evaluate(remembered) is True

    page.keyboard.press("`")
    sidebar.wait_for(state="visible")
    assert page.evaluate(remembered) is False


def test_x_drops_the_grouping(page):
    flat = page.evaluate("() => __winnow.visibleCols()")
    page.evaluate("() => __winnow.addGroupLevel('Host')")
    page.wait_for_selector("#body .group-header-row")
    assert "Host" not in page.evaluate("() => __winnow.visibleCols()")

    page.locator("#body").focus()
    page.keyboard.press("x")
    page.wait_for_function("() => !__winnow.S.groupByCols.length")
    page.wait_for_selector("#body .group-header-row", state="detached")
    page.wait_for_selector("#body .row[data-pos='0']")
    # The grouped column is back where it was, not at the far end.
    assert page.evaluate("() => __winnow.visibleCols()") == flat

    # Ungrouped, x has nothing to drop and does nothing.
    page.keyboard.press("x")
    assert page.evaluate("() => __winnow.visibleCols()") == flat


def test_L_saves_this_layout_as_the_default_for_its_columns(page, until):
    posted = []

    def answer(route):
        posted.append(route.request.post_data_json)
        route.fulfill(status=200, content_type="application/json", body="{}")

    # Answered here, not by the server: the default is cross-case state in
    # the shared workspace, keyed by ui.csv's columns, and there is no
    # route to take one back out.
    page.route("**/api/column_layouts", answer)
    try:
        page.keyboard.press("L")
        until(page, lambda: posted, "the default layout to be posted")
        want = page.evaluate("""() => ({ col_names: __winnow.baseColumns().map((c) => c.name),
                                        order: __winnow.S.order, columns: __winnow.S.layout })""")
        assert posted == [want]
        _toast(page, "Saved as the default layout for these columns")
    finally:
        page.unroute("**/api/column_layouts")


def test_Q_opens_the_current_filter_as_a_query_in_the_sql_pane(page, api, view_rebuilt):
    _cell(page, 2, "Host").click()
    with view_rebuilt(page):
        page.keyboard.press("f")
    name = page.evaluate("() => __winnow.S.sources.find((s) => s.id === __winnow.S.sourceId).name")
    before = {t["id"] for t in api("/api/sql_tabs")}
    tab = None
    try:
        page.locator("#body").focus()
        page.keyboard.press("Q")
        page.wait_for_function("() => __winnow.S.activeTab === 'sql'")
        made = [t for t in api("/api/sql_tabs") if t["id"] not in before]
        assert len(made) == 1
        tab = made[0]
        assert tab["name"] == f"Filter: {name}"
        page.wait_for_function("(id) => __winnow.S.sqlTabId === id", arg=tab["id"])
        # The query answers what the grid did: the filtered rows, no more.
        assert len(api("/api/sql", "POST", {"sql": tab["sql"], "limit": 1000})["rows"]) == PER_HOST
    finally:
        page.evaluate("() => __winnow.showGridTab()")
        if tab:
            api(f"/api/sql_tabs/{tab['id']}", "DELETE")


def test_dot_jumps_again_to_the_saved_moment(page):
    moment = "2026-03-14 08:02:30"
    # Nothing saved yet: the key asks for a moment rather than doing nothing.
    page.keyboard.press(".")
    box = page.locator("#modalBody input.confirm-input")
    box.wait_for()
    box.fill(moment)
    box.press("Enter")
    page.wait_for_function("() => document.getElementById('modal').hidden")
    page.wait_for_function("""(m) => { const r = __winnow.rowAt(__winnow.S.cursor);
        return !!r && r.cells[__winnow.S.columns.findIndex((c) => c.name === 'Timestamp')] === m; }""", arg=moment)
    jumped = page.evaluate("() => __winnow.S.cursor")
    assert jumped > 0

    page.locator("#body").focus()
    page.keyboard.press("g")
    page.wait_for_function("() => __winnow.S.cursor === 0")
    page.keyboard.press(".")
    page.wait_for_function("(p) => __winnow.S.cursor === p", arg=jumped)
    _toast(page, f"Jumped to {moment}")
    assert page.locator("#modal").is_hidden()
