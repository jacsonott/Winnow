"""Clicking a field in Pivot or First/Last opens a menu under it, not a dialog.

Both tabs used to answer a click on a field with a full modal titled
"Place <field>" — a note saying drag works too, and one button per zone.
A whole dialog for a one-click choice, and it covered the board the
analyst was arranging. It is the app's own dropdown menu now
(winnow.dropdownMenu), anchored under the field.

What these pin, in the terms an analyst would notice:
- no dialog opens, and the menu sits under the field it was opened from;
- picking a zone places the field exactly as dropping it there does;
- a zone the field can't go into is greyed with the reason, not hidden
  and not silently a no-op — only for rules the tabs already had
  (already there, Total up's number-only rule, First/Last's
  group-column limit);
- Escape and a click outside close it without placing anything;
- the keyboard reaches it: a field takes focus, Enter or Space opens the
  menu, the arrows walk it, Enter picks, and focus comes back to the
  field (or stays in the dialog a pick opened).
"""

from __future__ import annotations

import contextlib

import pytest

pytestmark = pytest.mark.ui

PIVOT_KEY = "tab:pivot-table.pivot"
FL_KEY = "tab:first-last.firstlast"

MENU = ".menu"
ITEMS = ".menu .menu-item"


@pytest.fixture(scope="module", autouse=True)
def plugins_on(server_post):
    server_post("/api/plugins/toggle", {"fs_name": "pivot", "scope": "on_all"})
    server_post("/api/plugins/toggle", {"fs_name": "first_last", "scope": "on_all"})
    yield
    server_post("/api/plugins/toggle", {"fs_name": "pivot", "scope": "off_all"})
    server_post("/api/plugins/toggle", {"fs_name": "first_last", "scope": "off_all"})


@contextlib.contextmanager
def _open(own_page, server_post, *, tab, zone, key, route=None):
    """A fresh page on a fresh, empty sheet of `tab`, pointed at ui.csv.
    The sheet state lives in the case file and is shared with every other
    test here, so it is cleared before each load rather than inherited."""
    server_post("/api/plugin_state", {"key": key, "payload": None})
    with own_page(route=route) as pg:
        pg.evaluate("() => __winnow.loadPlugins()")
        pg.wait_for_function("(t) => __winnow.S.pluginTabs.some((p) => p.label === t)", arg=tab,
                             timeout=10_000)
        pg.locator(".tab-plugin", has_text=tab).click()
        pg.wait_for_selector(f".pluginview [data-zone='{zone}']", timeout=10_000)
        want = str(pg.evaluate("() => __winnow.S.sources.find((s) => s.name === 'ui.csv').id"))
        pg.wait_for_function("() => document.querySelector('.pluginview select')?.options.length > 0",
                             timeout=10_000)
        if pg.eval_on_selector(".pluginview select", "(s) => s.value") != want:
            pg.select_option(".pluginview select", want)
        pg.wait_for_selector(".pluginview [data-field='Host']", timeout=10_000)
        yield pg


@pytest.fixture
def pv(own_page, server_post):
    with _open(own_page, server_post, tab="Pivot", zone="rows", key=PIVOT_KEY) as pg:
        yield pg


@pytest.fixture
def fl(own_page, server_post):
    with _open(own_page, server_post, tab="First/Last", zone="groupBy", key=FL_KEY) as pg:
        yield pg


def _field(pg, name):
    """The field in the LIST — not a chip of the same name in a zone."""
    return pg.locator(f".pluginview [data-field='{name}']").first


def _zone(pg, zone):
    return pg.evaluate(
        "(z) => [...document.querySelectorAll(`.pluginview [data-zone='${z}'] [data-field]`)]"
        ".map((c) => c.dataset.field)", zone)


def _menu(pg):
    """What the open menu says: its header, and per item the label, the
    reason/consequence line and whether it can be picked."""
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


def _item(pg, label):
    return next(i for i in _menu(pg)["items"] if i["label"] == label)


def _click_open(pg, name):
    _field(pg, name).click()
    pg.wait_for_selector(MENU, timeout=10_000)


def _pick(pg, label):
    pg.locator(ITEMS, has_text=label).click()
    pg.wait_for_selector(MENU, state="detached", timeout=10_000)


# ------------------------------------------------------------------ pivot


def test_pivot_a_field_click_opens_a_menu_under_the_field_not_a_dialog(pv):
    field = _field(pv, "Host")
    field.click()
    pv.wait_for_selector(MENU, timeout=10_000)
    assert pv.evaluate("() => document.getElementById('modal').hidden"), "a dialog opened"
    m = _menu(pv)
    assert m["header"] == ["Place in"]
    assert [i["label"] for i in m["items"]] == ["Filters", "Columns", "Rows", "Values"]
    assert not any(i["disabled"] for i in m["items"])
    # Anchored to the field: under it, starting where it starts — and
    # small enough to stay in the rail beside the cross-tab, rather than
    # a dialog over the middle of the board.
    f = field.bounding_box()
    box = pv.locator(MENU).bounding_box()
    assert abs(box["x"] - f["x"]) <= 1, (box, f)
    assert box["y"] >= f["y"] + f["height"] - 1, (box, f)
    rail = pv.evaluate("() => document.querySelector(\".pluginview [data-zone='rows']\")"
                       ".parentElement.parentElement.getBoundingClientRect().right")
    assert box["x"] + box["width"] <= rail + 1, (box, rail)


def test_pivot_picking_an_area_places_the_field_as_a_drop_does(pv):
    _click_open(pv, "Host")
    _pick(pv, "Rows")
    pv.wait_for_function("() => document.querySelectorAll(\".pluginview [data-zone='rows'] [data-field]\").length === 1",
                         timeout=10_000)
    assert _zone(pv, "rows") == ["Host"]
    assert pv.evaluate("() => document.getElementById('modal').hidden")

    # Values takes a field more than once (Count of X and Sum of X are two
    # measures), so it never greys out.
    _click_open(pv, "EventId")
    _pick(pv, "Values")
    pv.wait_for_function("() => document.querySelectorAll(\".pluginview [data-zone='values'] [data-field]\").length === 1",
                         timeout=10_000)
    _click_open(pv, "EventId")
    assert _item(pv, "Values")["disabled"] is False


def test_pivot_an_area_it_is_already_in_is_greyed_with_why(pv):
    _click_open(pv, "Host")
    _pick(pv, "Rows")
    pv.wait_for_function("() => document.querySelectorAll(\".pluginview [data-zone='rows'] [data-field]\").length === 1",
                         timeout=10_000)

    _click_open(pv, "Host")
    assert _item(pv, "Rows") == {"label": "Rows", "desc": "Already there", "disabled": True}
    # The other half of a cross-tab moves it rather than copying it, and
    # says so before the click.
    assert _item(pv, "Columns") == {"label": "Columns", "desc": "Moves it out of Rows", "disabled": False}
    _pick(pv, "Columns")
    pv.wait_for_function("() => document.querySelectorAll(\".pluginview [data-zone='cols'] [data-field]\").length === 1",
                         timeout=10_000)
    assert _zone(pv, "cols") == ["Host"]
    assert _zone(pv, "rows") == []

    _click_open(pv, "Host")
    _pick(pv, "Filters")
    pv.wait_for_function("() => document.querySelectorAll(\".pluginview [data-zone='filters'] [data-field]\").length === 1",
                         timeout=10_000)
    _click_open(pv, "Host")
    assert _item(pv, "Filters") == {"label": "Filters", "desc": "Already there", "disabled": True}


def test_pivot_escape_and_an_outside_click_close_it_and_place_nothing(pv):
    _click_open(pv, "Host")
    pv.keyboard.press("Escape")
    pv.wait_for_selector(MENU, state="detached", timeout=10_000)

    _click_open(pv, "Host")
    # The search box above the list: outside the menu, and clicking it
    # does nothing of its own to the wells.
    pv.locator(".pluginview input[type=search]").first.click()
    pv.wait_for_selector(MENU, state="detached", timeout=10_000)

    for zone in ("filters", "cols", "rows", "values"):
        assert _zone(pv, zone) == [], zone


def test_pivot_the_keyboard_opens_walks_and_picks(pv):
    field = _field(pv, "Host")
    field.focus()
    pv.keyboard.press("Enter")
    pv.wait_for_selector(MENU, timeout=10_000)
    # Down steps in at the first item; the next two walk to Rows.
    pv.keyboard.press("ArrowDown")
    pv.keyboard.press("ArrowDown")
    pv.keyboard.press("ArrowDown")
    assert pv.evaluate("() => document.activeElement.closest('.menu') && "
                       "document.activeElement.querySelector('.menu-item-text').textContent") == "Rows"
    pv.keyboard.press("Enter")
    pv.wait_for_selector(MENU, state="detached", timeout=10_000)
    pv.wait_for_function("() => document.querySelectorAll(\".pluginview [data-zone='rows'] [data-field]\").length === 1",
                         timeout=10_000)
    assert _zone(pv, "rows") == ["Host"]
    # The list was re-rendered under the pick; focus is back on the field
    # in it, not dropped on <body>, so the next Tab carries on from here.
    assert pv.evaluate("() => document.activeElement.dataset.field === 'Host' && "
                       "!document.activeElement.closest('[data-zone]')")

    # Space opens it too; Escape closes it again.
    pv.keyboard.press(" ")
    pv.wait_for_selector(MENU, timeout=10_000)
    pv.keyboard.press("Escape")
    pv.wait_for_selector(MENU, state="detached", timeout=10_000)


# ------------------------------------------------------------- first/last


FL_ZONES = ["Group rows on", "Ordered by", "Include columns", "Total up", "List values", "Filters"]


def test_firstlast_a_field_click_opens_a_menu_under_the_field_not_a_dialog(fl):
    field = _field(fl, "Host")
    field.click()
    fl.wait_for_selector(MENU, timeout=10_000)
    assert fl.evaluate("() => document.getElementById('modal').hidden"), "a dialog opened"
    m = _menu(fl)
    assert m["header"] == ["Place in"]
    assert [i["label"] for i in m["items"]] == FL_ZONES
    f = field.bounding_box()
    box = fl.locator(MENU).bounding_box()
    assert abs(box["x"] - f["x"]) <= 1, (box, f)
    assert box["y"] >= f["y"] + f["height"] - 1, (box, f)


def test_firstlast_total_up_is_greyed_for_a_column_that_is_not_a_number(fl):
    """addField already refused this with a toast after the click; the
    menu says it before."""
    _click_open(fl, "Host")
    assert _item(fl, "Total up") == {"label": "Total up", "desc": "Not a number column", "disabled": True}
    fl.keyboard.press("Escape")
    fl.wait_for_selector(MENU, state="detached", timeout=10_000)

    _click_open(fl, "EventId")
    assert _item(fl, "Total up")["disabled"] is False
    _pick(fl, "Total up")
    fl.wait_for_function("() => document.querySelectorAll(\".pluginview [data-zone='sums'] [data-field]\").length === 1",
                         timeout=10_000)
    assert _zone(fl, "sums") == ["EventId"]
    _click_open(fl, "EventId")
    assert _item(fl, "Total up") == {"label": "Total up", "desc": "Already there", "disabled": True}


def test_firstlast_ordered_by_says_what_it_replaces(fl):
    """One slot: the sheet starts ordered by the first datetime column,
    and a pick replaces it."""
    assert _zone(fl, "sort") == ["Timestamp"]
    _click_open(fl, "Host")
    assert _item(fl, "Ordered by") == {"label": "Ordered by", "desc": "Replaces Timestamp", "disabled": False}
    _pick(fl, "Ordered by")
    fl.wait_for_function("() => document.querySelector(\".pluginview [data-zone='sort'] [data-field]\")?.dataset.field === 'Host'",
                         timeout=10_000)
    _click_open(fl, "Host")
    assert _item(fl, "Ordered by") == {"label": "Ordered by", "desc": "Already there", "disabled": True}


def test_firstlast_grouped_and_included_are_greyed_once_there(fl):
    _click_open(fl, "Host")
    _pick(fl, "Group rows on")
    fl.wait_for_function("() => document.querySelectorAll(\".pluginview [data-zone='carry'] [data-field]\").length === 1",
                         timeout=10_000)
    _click_open(fl, "Host")
    assert _item(fl, "Group rows on") == {"label": "Group rows on", "desc": "Already there", "disabled": True}
    # Grouping includes it (test_firstlast_group_includes.py), so that is
    # already there too.
    assert _item(fl, "Include columns") == {"label": "Include columns", "desc": "Already there", "disabled": True}
    assert _item(fl, "List values")["disabled"] is False


def test_firstlast_escape_and_an_outside_click_close_it_and_place_nothing(fl):
    _click_open(fl, "Host")
    fl.keyboard.press("Escape")
    fl.wait_for_selector(MENU, state="detached", timeout=10_000)
    _click_open(fl, "Host")
    fl.locator(".pluginview input[type=search]").first.click()
    fl.wait_for_selector(MENU, state="detached", timeout=10_000)
    for zone in ("groupBy", "carry", "sums", "values", "filters"):
        assert _zone(fl, zone) == [], zone
    assert _zone(fl, "sort") == ["Timestamp"]


def test_firstlast_the_keyboard_opens_walks_and_picks(fl):
    field = _field(fl, "EventId")
    field.focus()
    fl.keyboard.press("Enter")
    fl.wait_for_selector(MENU, timeout=10_000)
    for _ in range(4):        # Group rows on, Ordered by, Include columns, Total up
        fl.keyboard.press("ArrowDown")
    assert fl.evaluate("() => document.activeElement.closest('.menu') && "
                       "document.activeElement.querySelector('.menu-item-text').textContent") == "Total up"
    fl.keyboard.press("Enter")
    fl.wait_for_selector(MENU, state="detached", timeout=10_000)
    fl.wait_for_function("() => document.querySelectorAll(\".pluginview [data-zone='sums'] [data-field]\").length === 1",
                         timeout=10_000)
    assert fl.evaluate("() => document.activeElement.dataset.field === 'EventId' && "
                       "!document.activeElement.closest('[data-zone]')")


def test_firstlast_a_keyboard_pick_that_opens_a_dialog_leaves_focus_in_it(fl):
    """Filters opens the filter editor on a pick. Putting focus back on the
    field would take it out of the dialog the pick just opened."""
    _field(fl, "Host").focus()
    fl.keyboard.press(" ")
    fl.wait_for_selector(MENU, timeout=10_000)
    fl.keyboard.press("ArrowUp")              # Up steps in at the last item
    assert fl.evaluate("() => document.activeElement.querySelector('.menu-item-text').textContent") == "Filters"
    fl.keyboard.press("Enter")
    fl.wait_for_selector("#modal:not([hidden])", timeout=10_000)
    # The value list paints after a fetch and its search takes focus then.
    fl.wait_for_function("() => document.activeElement === document.querySelector('#modalBody input[type=search]')",
                         timeout=10_000)
    fl.keyboard.press("Escape")
    fl.wait_for_selector("#modal[hidden]", state="attached", timeout=10_000)


def test_firstlast_group_rows_on_greys_out_at_the_group_column_limit(own_page, server_post):
    """The backend refuses more group columns than its limit with a 400 on
    the next preview; the menu reads the same limit from /meta and says so
    first. ui.csv has five columns and the shipped limit is six, so the
    limit is lowered on the way to the page rather than the table widened."""
    def lower_limit(route):
        resp = route.fetch()
        body = resp.json()
        body["limits"]["group_cols"] = 2
        route.fulfill(response=resp, json=body)

    with _open(own_page, server_post, tab="First/Last", zone="groupBy", key=FL_KEY,
               route=("**/api/plugin/first_last/meta", lower_limit)) as pg:
        for name in ("Host", "EventId"):
            _click_open(pg, name)
            _pick(pg, "Group rows on")
        pg.wait_for_function("() => document.querySelectorAll(\".pluginview [data-zone='groupBy'] [data-field]\").length === 2",
                             timeout=10_000)
        _click_open(pg, "CommandLine")
        assert _item(pg, "Group rows on") == {"label": "Group rows on", "desc": "Full — 2 columns at most",
                                              "disabled": True}
        assert _item(pg, "Include columns")["disabled"] is False
