"""Managing a dashboard by hand: renaming it (double-click its sidebar row or
its title), deleting it from the sidebar's ✕, and "Save as profile…" on its
bar. Every existing test made and removed boards through the API, so none
of these gestures had ever run.

What they owe the analyst: a rename lands on both places the name shows; a
delete asks first, and the board that was on screen gives way to another
board, or to the grid when it was the last; a profile carries the board's
widgets and the case's variable DEFINITIONS, never the values (a profile
is a template for the next case, and a value is this case's evidence).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

WIDGET = {"title": "Tagged rows", "source": "tags", "render": "stat"}


@pytest.fixture
def boards(page, api):
    """Make boards with `boards(name)`; whatever is left is deleted after,
    and the page goes back to the grid."""
    made = []

    def make(name, widgets=()):
        d = api("/api/dashboards", "POST", {"name": name, "widgets": list(widgets)})
        made.append(d["id"])
        page.evaluate("async () => { await __winnow.loadDashboards(); __winnow.renderSidebar(); }")
        return d["id"]

    try:
        yield make
    finally:
        left = {d["id"] for d in api("/api/dashboards")}
        for did in made:
            if did in left:
                api(f"/api/dashboards/{did}", "DELETE")
        page.evaluate("""async () => {
          for (const o of document.querySelectorAll('.confirm-overlay')) o.remove();
          await __winnow.loadDashboards(); __winnow.renderSidebar(); __winnow.showGridTab();
        }""")


def _show(page, did):
    page.evaluate("(id) => __winnow.showDashboard(id)", did)
    page.wait_for_selector("#dashboardview:not([hidden])")
    page.wait_for_function("(id) => __winnow.S.dashboardId === id", arg=did)


def _sidebar_row(page, name):
    return page.locator("#sidebarList .sidebar-row", has=page.locator(".menu-item", has_text=name))


def _cross(page, name):
    """The row's ✕, which shows only while the pointer is on the row."""
    row = _sidebar_row(page, name)
    row.hover()
    return row.locator(".menu-item-action[title='Delete this dashboard']")


def _names(api):
    return [d["name"] for d in api("/api/dashboards")]


def _answer(page, text=None, button="OK"):
    """Fill the open prompt (or leave it) and press `button`."""
    box = page.locator(".confirm-input")
    if text is not None:
        box.fill(text)
    page.locator(".confirm-card .btn", has_text=button).click()
    page.wait_for_selector(".confirm-overlay", state="detached")


def test_double_clicking_a_sidebar_row_renames_the_board(page, api, boards):
    did = boards("Board to rename")
    _show(page, did)
    _sidebar_row(page, "Board to rename").locator(".menu-item").dblclick()
    box = page.locator(".confirm-input")
    box.wait_for()
    assert page.locator(".confirm-message").inner_text() == "Rename dashboard:"
    assert box.input_value() == "Board to rename"

    _answer(page, "  Renamed board  ", button="Rename")
    page.wait_for_function("""() => [...document.querySelectorAll('#sidebarList .sidebar-row .menu-item')]
                              .some((b) => b.textContent === 'Renamed board')""")
    assert "Renamed board" in _names(api) and "Board to rename" not in _names(api)
    # The board on screen says its new name too.
    assert page.locator("#dashBar .dash-title").inner_text() == "Renamed board"


def test_double_clicking_the_title_renames_and_a_blank_or_same_name_sends_nothing(page, api, boards):
    did = boards("Title board")
    _show(page, did)
    sent = []
    page.on("request", lambda r: sent.append(r.post_data) if r.method == "POST"
            and r.url.endswith(f"/api/dashboards/{did}") else None)

    for typed, button in (("   ", "Rename"), ("Title board", "Rename"), ("Never saved", "Cancel")):
        page.locator("#dashBar .dash-title").dblclick()
        page.locator(".confirm-input").wait_for()
        _answer(page, typed, button=button)
    # Nothing to wait for: the check is that none of the three made a request.
    page.wait_for_timeout(300)
    assert sent == []
    assert "Title board" in _names(api)

    page.locator("#dashBar .dash-title").dblclick()
    page.locator(".confirm-input").wait_for()
    _answer(page, "Title board 2", button="Rename")
    page.wait_for_function("() => document.querySelector('#dashBar .dash-title').textContent === 'Title board 2'")
    assert "Title board 2" in _names(api)


def test_the_sidebar_cross_asks_first_and_cancel_keeps_the_board(page, api, boards):
    did = boards("Kept board", [WIDGET])
    _show(page, did)
    _cross(page, "Kept board").click()
    msg = page.locator(".confirm-message")
    msg.wait_for()
    assert msg.inner_text() == ("Delete dashboard “Kept board”? Its widgets go with it; "
                                "the case data is untouched.")
    _answer(page, button="Cancel")
    assert "Kept board" in _names(api)
    assert page.evaluate("() => __winnow.S.dashboardId") == did
    assert _sidebar_row(page, "Kept board").count() == 1


def test_deleting_the_board_on_screen_shows_the_next_one(page, api, boards):
    keep = boards("Board that stays")
    gone = boards("Board that goes", [WIDGET])
    _show(page, gone)
    _cross(page, "Board that goes").click()
    page.locator(".confirm-message").wait_for()
    _answer(page, button="Delete dashboard")

    page.wait_for_function("(gone) => __winnow.S.dashboardId !== gone", arg=gone)
    assert "Board that goes" not in _names(api)
    assert _sidebar_row(page, "Board that goes").count() == 0
    # The first board left in the list, on screen, not an empty dashboard view.
    first = api("/api/dashboards")[0]["id"]
    assert page.evaluate("() => __winnow.S.dashboardId") == first
    assert page.evaluate("() => __winnow.S.activeTab") == "dashboard"
    assert keep in {d["id"] for d in api("/api/dashboards")}


def test_deleting_a_board_not_on_screen_leaves_the_one_that_is(page, api, boards):
    shown = boards("Board on screen")
    other = boards("Board elsewhere")
    _show(page, shown)
    _cross(page, "Board elsewhere").click()
    page.locator(".confirm-message").wait_for()
    _answer(page, button="Delete dashboard")
    page.wait_for_function("() => ![...document.querySelectorAll('#sidebarList .sidebar-row')]"
                           ".some((r) => r.textContent.includes('Board elsewhere'))")
    assert other not in {d["id"] for d in api("/api/dashboards")}
    assert page.evaluate("() => __winnow.S.dashboardId") == shown
    assert page.locator("#dashBar .dash-title").inner_text() == "Board on screen"


def test_deleting_the_last_board_goes_back_to_the_grid(page, api, boards):
    assert api("/api/dashboards") == [], "this needs a case with no other dashboards"
    only = boards("The only board", [WIDGET])
    _show(page, only)
    _cross(page, "The only board").click()
    page.locator(".confirm-message").wait_for()
    _answer(page, button="Delete dashboard")

    page.wait_for_function("() => __winnow.S.activeTab === 'grid' && __winnow.S.dashboardId === null")
    page.wait_for_selector("#dashboardview", state="hidden")
    page.wait_for_selector("#body .row")
    assert api("/api/dashboards") == []


@pytest.fixture
def profile_cleanup(page, api):
    """Profiles are machine-wide: whatever a test saves under these names
    goes, and so do the case variables it set."""
    names = set()
    try:
        yield names
    finally:
        for b in api("/api/plugin_bundles"):
            if b["name"] in names:
                api(f"/api/plugin_bundles/{b['id']}", "DELETE")
        for v in api("/api/case/variables"):
            if v["name"].startswith("dm_"):
                api(f"/api/case/variables/{v['name']}", "DELETE")
        page.evaluate("() => __winnow.loadCaseVariables()")


def test_save_as_profile_carries_the_widgets_and_variable_definitions(page, api, boards, profile_cleanup):
    profile_cleanup.add("Board profile")
    api("/api/case/variables", "POST", {"name": "dm_engagement", "value": "ACME-2026-10",
                                        "description": "the engagement code", "required": True})
    api("/api/case/variables", "POST", {"name": "dm_analyst", "value": "jo"})
    page.evaluate("() => __winnow.loadCaseVariables()")
    did = boards("Profile board", [WIDGET, {"title": "Sources", "source": "sql",
                                            "query": {"sql": "SELECT 1 AS n"}, "render": "stat"}])
    _show(page, did)
    page.wait_for_selector("#dashGrid .dash-card h4")

    page.locator("#dashBar button", has_text="Save as profile…").click()
    page.locator(".confirm-input").wait_for()
    assert page.locator(".confirm-message").inner_text() == \
        "Save the current plugins + this dashboard as a profile named:"
    _answer(page, "  Board profile  ")
    page.wait_for_function("""() => (document.getElementById('toast').textContent || '').includes(
        'Profile "Board profile" saved — apply it from Settings → Profiles on a new case')""")

    [rec] = [b for b in api("/api/plugin_bundles") if b["name"] == "Board profile"]
    assert [w["title"] for w in rec["dashboard"]] == ["Tagged rows", "Sources"]
    assert rec["dashboard"][1]["query"] == {"sql": "SELECT 1 AS n"}
    # The plugins it names are the ones on for this case now.
    on = sorted(p["fs_name"] for p in api("/api/plugins")["plugins"] if p["enabled"])
    assert rec["plugins"] == on
    # Definitions, never values.
    by_name = {v["name"]: v for v in rec["variables"]}
    assert by_name["dm_engagement"] == {"name": "dm_engagement", "description": "the engagement code",
                                        "required": True}
    assert by_name["dm_analyst"] == {"name": "dm_analyst", "description": "", "required": False}
    assert "ACME-2026-10" not in repr(rec)


def test_save_as_profile_with_no_name_saves_nothing(page, api, boards, profile_cleanup):
    did = boards("Unnamed profile board", [WIDGET])
    _show(page, did)
    before = [b["name"] for b in api("/api/plugin_bundles")]
    sent = []
    page.on("request", lambda r: sent.append(r.url) if "/api/plugin_bundles" in r.url
            and r.method == "POST" else None)
    for typed, button in (("   ", "OK"), ("Cancelled profile", "Cancel")):
        page.locator("#dashBar button", has_text="Save as profile…").click()
        page.locator(".confirm-input").wait_for()
        _answer(page, typed, button=button)
    # Nothing to wait for: the check is that no request was made.
    page.wait_for_timeout(300)
    assert sent == []
    assert [b["name"] for b in api("/api/plugin_bundles")] == before
