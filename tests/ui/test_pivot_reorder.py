"""Reordering the fields in the Pivot's areas.

The order of an area is part of the question a pivot asks: which field in
Rows nests inside which, which value column comes first, how Columns
stacks its headers. Analysts reported that reordering was difficult. It
was not possible: a chip dropped back into its own area was ignored
(`if (from === zone) return`), a field dropped into an area always went
to the end, and nothing but a drag could move a chip at all — so the only
way to swap two Rows fields was to remove them and add them back, and
for a value that also lost its Sum or Average.

What these pin, in the terms an analyst would notice:
- a chip dragged within its area lands where it was let go, and the
  cross-tab follows;
- the slot is shown as a line while dragging, and not for a drop that
  would change nothing;
- a field from the list lands between the chips it was dropped between,
  and one an area already has moves rather than doubling;
- moving a value keeps its aggregation, and a header sort follows it;
- the handle's menu moves a chip up, down, to the ends or to another
  area, greys what it can't do with why, and Alt+Up / Alt+Down move the
  focused chip without a menu, keeping the focus on it;
- the new order is saved with the pivot.
"""

from __future__ import annotations

import re
import time

import pytest

pytestmark = pytest.mark.ui

KEY = "tab:pivot-table.pivot"
MENU = ".menu"
ITEMS = ".menu .menu-item"


@pytest.fixture(scope="module", autouse=True)
def plugin_on(server_post):
    server_post("/api/plugins/toggle", {"fs_name": "pivot", "scope": "on_all"})
    yield
    server_post("/api/plugin_state", {"key": KEY, "payload": None})
    server_post("/api/plugins/toggle", {"fs_name": "pivot", "scope": "off_all"})


@pytest.fixture
def pv(own_page, server_post):
    """A fresh page on a fresh, empty pivot of ui.csv. The pivots live in
    the shared case file, so they are cleared before each load."""
    server_post("/api/plugin_state", {"key": KEY, "payload": None})
    with own_page() as pg:
        pg.evaluate("() => __winnow.loadPlugins()")
        pg.wait_for_function("() => __winnow.S.pluginTabs.some((p) => p.label === 'Pivot')", timeout=10_000)
        pg.locator(".tab-plugin", has_text="Pivot").click()
        pg.wait_for_selector(".pluginview [data-zone='rows']", timeout=10_000)
        want = str(pg.evaluate("() => __winnow.S.sources.find((s) => s.name === 'ui.csv').id"))
        pg.wait_for_function("() => document.querySelector('.pluginview select')?.options.length > 0",
                             timeout=10_000)
        if pg.eval_on_selector(".pluginview select", "(s) => s.value") != want:
            pg.select_option(".pluginview select", want)
        pg.wait_for_selector(".pluginview [data-field='Host']", timeout=10_000)
        yield pg


def _zone(pg, zone):
    return pg.evaluate(
        "(z) => [...document.querySelectorAll(`.pluginview [data-zone='${z}'] [data-field]`)]"
        ".map((c) => c.dataset.field)", zone)


def _field(pg, name):
    """The field in the LIST — not a chip of the same name in an area."""
    return pg.locator(f".pluginview [data-field='{name}']").first


def _chip(pg, zone, nth):
    return pg.locator(f".pluginview [data-zone='{zone}'] [data-field]").nth(nth)


def _grip(pg, zone, nth):
    return _chip(pg, zone, nth).locator(".pv-grip")


def _item(pg, label):
    """A menu entry by its label exactly — not by a reason line under
    another entry that happens to name it ("Moves it out of Rows")."""
    return pg.locator(ITEMS, has=pg.locator(".menu-item-text", has_text=re.compile(f"^{re.escape(label)}$")))


def _place(pg, name, area):
    """Through the Place in menu, which appends — the order a test builds
    up is then the order it named the fields in."""
    zone = {"Rows": "rows", "Columns": "cols", "Values": "values", "Filters": "filters"}[area]
    before = len(_zone(pg, zone))
    _field(pg, name).click()
    pg.wait_for_selector(MENU, timeout=10_000)
    _item(pg, area).click()
    pg.wait_for_function(
        "([z, n]) => document.querySelectorAll(`.pluginview [data-zone='${z}'] [data-field]`).length === n",
        arg=[zone, before + 1], timeout=10_000)


def _settled(pg):
    """The cross-tab for the areas as they are now: the status line has
    stopped saying Aggregating… and a table is drawn."""
    pg.wait_for_function(
        "() => !document.querySelector('.pluginview')?.textContent.includes('Aggregating')"
        " && document.querySelector('.pluginview table')", timeout=15_000)
    pg.wait_for_timeout(350)   # past the 250 ms refresh debounce, then its answer
    pg.wait_for_function(
        "() => !document.querySelector('.pluginview')?.textContent.includes('Aggregating')", timeout=15_000)


def _headers(pg):
    return pg.evaluate("() => [...document.querySelectorAll('.pluginview table thead th')]"
                       ".map((t) => t.textContent.trim())")


def _press(pg, locator):
    b = locator.bounding_box()
    pg.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
    pg.mouse.down()
    # A few pixels first: Chromium starts a native drag once the pointer
    # has moved past its threshold, not on the press.
    pg.mouse.move(b["x"] + b["width"] / 2 + 4, b["y"] + b["height"] / 2 + 4, steps=3)


def _hover(pg, x, y):
    """Move a drag in progress to (x, y). The emulated drag delivers each
    dragover a move or two behind the pointer (a real one also fires on a
    timer, so it catches up by itself), so it ends on two small moves at
    the spot: the last dragover then has the spot's own coordinates."""
    pg.mouse.move(x, y, steps=8)
    pg.mouse.move(x + 1, y)
    pg.mouse.move(x, y)


def _drag(pg, locator, x, y):
    """A real pointer drag — the HTML5 drag the browser starts itself, so
    the drop arrives with the coordinates the slot is read from."""
    _press(pg, locator)
    _hover(pg, x, y)
    pg.mouse.up()


def _line(pg, zone):
    """The slot line on an area: None when hidden, else its middle as a
    page y."""
    return pg.evaluate("""(z) => {
      const l = document.querySelector(`.pluginview [data-zone='${z}'] .pv-drop-line`);
      if (!l || l.hidden) return null;
      const r = l.getBoundingClientRect();
      return r.top + r.height / 2;
    }""", zone)


def _line_when(pg, zone, ok, timeout=3.0):
    """The slot line once `ok(line)` holds, or as it is at the timeout.
    Chromium sends dragover on a timer as well as on movement, so the line
    can trail the pointer by one beat; poll rather than read once."""
    deadline = time.monotonic() + timeout
    y = _line(pg, zone)
    while not ok(y) and time.monotonic() < deadline:
        pg.wait_for_timeout(50)
        y = _line(pg, zone)
    return y


def _menu(pg):
    return pg.evaluate("""() => {
      const m = document.querySelector('.menu');
      if (!m) return null;
      return {
        header: [...m.querySelectorAll('.menu-header')].map((h) => h.textContent),
        items: [...m.querySelectorAll('.menu-item')].map((b) => ({
          label: b.querySelector('.menu-item-text').textContent,
          desc: b.querySelector('.menu-item-desc')?.textContent || null,
          disabled: b.disabled,
        })),
      };
    }""")


def _pick(pg, label):
    _item(pg, label).click()
    pg.wait_for_selector(MENU, state="detached", timeout=10_000)


def _focused_label(pg):
    return pg.evaluate("() => document.activeElement?.getAttribute('aria-label')")


# ---------------------------------------------------------------- dragging


def test_a_chip_dragged_within_its_area_lands_where_it_is_dropped(pv):
    _place(pv, "Host", "Rows")
    _place(pv, "EventId", "Rows")
    _settled(pv)
    assert _headers(pv)[:2] == ["Host", "EventId"]

    host = _chip(pv, "rows", 0).bounding_box()
    _drag(pv, _chip(pv, "rows", 1), host["x"] + 40, host["y"] + 3)   # the top half of Host
    pv.wait_for_function("() => document.querySelector(\".pluginview [data-zone='rows'] [data-field]\")"
                         "?.dataset.field === 'EventId'", timeout=10_000)
    assert _zone(pv, "rows") == ["EventId", "Host"]
    _settled(pv)
    # The cross-tab asks the new question: EventId is the outer level now.
    assert _headers(pv)[:2] == ["EventId", "Host"]


def test_the_slot_is_shown_before_the_release_and_not_for_a_drop_that_changes_nothing(pv):
    for name in ("Host", "EventId", "Timestamp"):
        _place(pv, name, "Rows")
    host, event, stamp = (_chip(pv, "rows", i).bounding_box() for i in range(3))
    above_host = lambda y: y is not None and y <= host["y"] + 1                                 # noqa: E731
    between = lambda y: y is not None and host["y"] + host["height"] - 1 <= y <= event["y"] + 1  # noqa: E731
    hidden = lambda y: y is None                                                                # noqa: E731

    _press(pv, _chip(pv, "rows", 2))
    # Over the top half of Host: the line sits above Host.
    _hover(pv, host["x"] + 40, host["y"] + 3)
    assert above_host(y := _line_when(pv, "rows", above_host)), (y, host)
    # Between Host and EventId: the line is in the gap between them.
    _hover(pv, host["x"] + 40, event["y"] + 2)
    assert between(y := _line_when(pv, "rows", between)), (y, host, event)
    # On Timestamp itself, or just above it: letting go there moves
    # nothing, so no line promises a move.
    _hover(pv, stamp["x"] + 40, stamp["y"] + stamp["height"] - 3)
    assert hidden(_line_when(pv, "rows", hidden))
    _hover(pv, stamp["x"] + 40, event["y"] + event["height"] - 2)
    pv.wait_for_timeout(250)   # several dragovers' worth: it stays hidden
    assert _line(pv, "rows") is None
    # Out over the cross-tab, which takes no drop: released there, nothing
    # moves and the cue goes with the drag.
    _hover(pv, host["x"] + 40, host["y"] + 3)
    assert above_host(_line_when(pv, "rows", above_host))
    pv.mouse.move(900, 400, steps=8)
    pv.mouse.up()
    assert hidden(_line_when(pv, "rows", hidden))
    assert _zone(pv, "rows") == ["Host", "EventId", "Timestamp"]


def test_a_field_from_the_list_lands_between_the_chips_it_is_dropped_between(pv):
    _place(pv, "Host", "Rows")
    _place(pv, "EventId", "Rows")
    host, event = _chip(pv, "rows", 0).bounding_box(), _chip(pv, "rows", 1).bounding_box()
    _drag(pv, _field(pv, "Timestamp"), host["x"] + 40, (host["y"] + host["height"] + event["y"]) / 2)
    pv.wait_for_function("() => document.querySelectorAll(\".pluginview [data-zone='rows'] [data-field]\").length === 3",
                         timeout=10_000)
    assert _zone(pv, "rows") == ["Host", "Timestamp", "EventId"]

    # A field Rows already has, dropped from the list at the top, moves
    # there — one Rows field can't be in Rows twice.
    top = _chip(pv, "rows", 0).bounding_box()
    _drag(pv, _field(pv, "EventId"), top["x"] + 40, top["y"] + 3)
    pv.wait_for_function("() => document.querySelector(\".pluginview [data-zone='rows'] [data-field]\")"
                         "?.dataset.field === 'EventId'", timeout=10_000)
    assert _zone(pv, "rows") == ["EventId", "Host", "Timestamp"]

    # And one from another area lands at the slot too, not at the end.
    _place(pv, "CommandLine", "Columns")
    first = _chip(pv, "rows", 0).bounding_box()
    _drag(pv, _chip(pv, "cols", 0), first["x"] + 40, first["y"] + 3)
    pv.wait_for_function("() => document.querySelectorAll(\".pluginview [data-zone='rows'] [data-field]\").length === 4",
                         timeout=10_000)
    assert _zone(pv, "rows") == ["CommandLine", "EventId", "Host", "Timestamp"]
    assert _zone(pv, "cols") == []


def test_moving_a_value_keeps_its_aggregation_and_the_sort_follows_it(pv):
    _place(pv, "Host", "Rows")
    _place(pv, "EventId", "Values")   # a number column: Sum
    _place(pv, "Host", "Values")      # text: Count
    _chip(pv, "values", 1).locator("select").select_option("count_distinct")
    _settled(pv)
    assert _headers(pv) == ["Host", "Sum of EventId", "Distinct count of Host"]
    pv.locator(".pluginview table thead th", has_text="Sum of EventId").click()
    pv.wait_for_function("() => [...document.querySelectorAll('.pluginview table thead th')]"
                         ".some((t) => t.textContent.includes('▾'))", timeout=10_000)

    first = _chip(pv, "values", 0).bounding_box()
    _drag(pv, _grip(pv, "values", 1), first["x"] + 40, first["y"] + 3)
    pv.wait_for_function("() => document.querySelector(\".pluginview [data-zone='values'] select\")?.value === 'count_distinct'",
                         timeout=10_000)
    assert pv.evaluate("() => [...document.querySelectorAll(\".pluginview [data-zone='values'] select\")]"
                       ".map((s) => s.value)") == ["count_distinct", "sum"]
    _settled(pv)
    # The sort was on Sum of EventId, and it still is, in its new place.
    assert _headers(pv) == ["Host", "Distinct count of Host", "Sum of EventId ▾"]


# ---------------------------------------------------------- menu and keys


def test_the_handle_menu_moves_a_chip_and_says_why_a_move_is_greyed(pv):
    for name in ("Host", "EventId", "Timestamp"):
        _place(pv, name, "Rows")

    _grip(pv, "rows", 0).click()
    pv.wait_for_selector(MENU, timeout=10_000)
    m = _menu(pv)
    assert m["header"] == ["Host"]
    assert [i["label"] for i in m["items"]] == [
        "Move up", "Move down", "Move to the bottom",
        "Move to Filters", "Move to Columns", "Move to Values", "Remove"]
    assert m["items"][0] == {"label": "Move up", "desc": "Already first", "disabled": True}
    _pick(pv, "Move to the bottom")
    assert _zone(pv, "rows") == ["EventId", "Timestamp", "Host"]

    _grip(pv, "rows", 2).click()
    pv.wait_for_selector(MENU, timeout=10_000)
    m = _menu(pv)
    labels = [i["label"] for i in m["items"]]
    assert labels[:3] == ["Move up", "Move down", "Move to the top"]
    assert m["items"][1] == {"label": "Move down", "desc": "Already last", "disabled": True}
    _pick(pv, "Move up")
    assert _zone(pv, "rows") == ["EventId", "Host", "Timestamp"]

    # Another area, from the same menu: what a drop there does.
    _grip(pv, "rows", 1).click()
    pv.wait_for_selector(MENU, timeout=10_000)
    _pick(pv, "Move to Columns")
    assert _zone(pv, "rows") == ["EventId", "Timestamp"]
    assert _zone(pv, "cols") == ["Host"]

    # A value chip whose field is in Columns: Columns is greyed with why,
    # Rows says it takes it out of Columns. One chip in an area has
    # nothing to reorder against, so no Move up / Move down.
    _place(pv, "Host", "Values")
    _grip(pv, "values", 0).click()
    pv.wait_for_selector(MENU, timeout=10_000)
    m = _menu(pv)
    assert m["header"] == ["Count of Host"]
    items = {i["label"]: i for i in m["items"]}
    assert "Move up" not in items
    assert items["Move to Columns"] == {"label": "Move to Columns", "desc": "Already there", "disabled": True}
    assert items["Move to Rows"] == {"label": "Move to Rows", "desc": "Moves it out of Columns", "disabled": False}
    _pick(pv, "Remove")
    assert _zone(pv, "values") == []

    # A right-click on the chip opens the same menu.
    _chip(pv, "rows", 0).click(button="right")
    pv.wait_for_selector(MENU, timeout=10_000)
    assert _menu(pv)["header"] == ["EventId"]
    pv.keyboard.press("Escape")


def test_alt_arrows_move_the_focused_chip_and_the_focus_stays_on_it(pv):
    for name in ("Host", "EventId", "Timestamp"):
        _place(pv, name, "Rows")
    _grip(pv, "rows", 2).focus()
    assert _focused_label(pv) == "Timestamp, 3 of 3 in Rows"

    pv.keyboard.press("Alt+ArrowUp")
    assert _zone(pv, "rows") == ["Host", "Timestamp", "EventId"]
    assert _focused_label(pv) == "Timestamp, 2 of 3 in Rows"
    pv.keyboard.press("Alt+ArrowUp")
    assert _zone(pv, "rows") == ["Timestamp", "Host", "EventId"]
    assert _focused_label(pv) == "Timestamp, 1 of 3 in Rows"
    pv.keyboard.press("Alt+ArrowUp")   # already first: nothing moves
    assert _zone(pv, "rows") == ["Timestamp", "Host", "EventId"]
    pv.keyboard.press("Alt+ArrowDown")
    assert _zone(pv, "rows") == ["Host", "Timestamp", "EventId"]
    assert _focused_label(pv) == "Timestamp, 2 of 3 in Rows"

    # Enter opens the handle's menu; the arrows walk it and Enter picks,
    # and the focus comes back to the chip where it now is.
    pv.keyboard.press("Enter")
    pv.wait_for_selector(MENU, timeout=10_000)
    pv.keyboard.press("ArrowDown")   # Move up
    pv.keyboard.press("ArrowDown")   # Move down
    pv.keyboard.press("Enter")
    pv.wait_for_selector(MENU, state="detached", timeout=10_000)
    assert _zone(pv, "rows") == ["Host", "EventId", "Timestamp"]
    assert _focused_label(pv) == "Timestamp, 3 of 3 in Rows"


def test_alt_arrows_on_the_aggregation_picker_are_left_to_the_picker(pv):
    _place(pv, "EventId", "Values")
    _place(pv, "Host", "Values")
    _chip(pv, "values", 1).locator("select").focus()
    pv.keyboard.press("Alt+ArrowUp")
    assert _zone(pv, "values") == ["EventId", "Host"]


def test_a_reorder_is_saved_with_the_pivot(pv):
    _place(pv, "Host", "Rows")
    _place(pv, "EventId", "Rows")
    _settled(pv)
    _grip(pv, "rows", 0).focus()
    with pv.expect_response(lambda r: "/api/plugin_state" in r.url and r.request.method == "POST",
                            timeout=15_000) as saved:
        pv.keyboard.press("Alt+ArrowDown")
    assert saved.value.json()["payload"]["pivots"][0]["rows"] == ["EventId", "Host"]
