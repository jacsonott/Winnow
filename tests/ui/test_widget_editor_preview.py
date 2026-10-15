"""The widget editor answers the draft as it changes, in the builders' shape.

It used to be one long form ending in an empty Preview box that filled only
when a Preview button was pressed, with "Data from" and "Render as" offering
the stored names (`sql`, `cells`, `kv`, `histogram`…). Now the question is on
the left, how it looks on the right above a card that runs the draft by
itself once the typing stops. What is pinned here is what the analyst sees
and what the server is asked:

- the preview fills with no button, and a burst of typing is not a burst of
  queries;
- a slow answer to an OLDER draft that lands after a newer one has painted
  is dropped, not painted over it (the preview route cannot be cancelled,
  so dropping is the whole defence);
- a draft that cannot run yet says what is missing in one line, and asks
  the server nothing;
- changing only how the card looks repaints the answer already there;
- the preview never files anything in the board's cache;
- readable names on screen, the same stored values underneath;
- two columns when there is room, stacked when there is not.
"""

from __future__ import annotations

import json
import re

import pytest

pytestmark = pytest.mark.ui

PREVIEW = re.compile(r".*/api/dashboard/widget/preview$")
H = "{ 'Content-Type': 'application/json', 'X-Timeline-Lite-Client': '1' }"


def _board(page, name, widgets=None):
    did = page.evaluate(f"""async ([name, widgets]) => {{
      const d = await fetch('/api/dashboards', {{ method: 'POST', headers: {H},
        body: JSON.stringify({{ name, widgets }}) }}).then(r => r.json());
      await __winnow.loadDashboards();
      await __winnow.showDashboard(d.id);
      return d.id;
    }}""", [name, widgets or []])
    page.wait_for_selector("#dashboardview:not([hidden])")
    return did


def _drop(page, did):
    page.evaluate("""async (id) => {
      await fetch('/api/dashboards/' + id, { method: 'DELETE', headers: { 'X-Timeline-Lite-Client': '1' } });
      await __winnow.loadDashboards(); __winnow.renderSidebar();
    }""", did)


def _board_json(page, did):
    return page.evaluate("(id) => fetch('/api/dashboards/' + id).then(r => r.json())", did)


def _src(page):
    return page.evaluate("() => __winnow.S.sources.find((s) => !s.is_merge).id")


def _close(page):
    page.keyboard.press("Escape")
    page.wait_for_function("() => document.getElementById('modal').hidden", timeout=10_000)


class _Asks:
    """Every preview the page sends, in order, with an optional hold on the
    ones whose SQL matches `hold`, so a test can decide when (and in which
    order) answers land."""

    def __init__(self, page, hold=None):
        self.page, self.hold = page, hold
        self.bodies, self.held = [], []
        page.route(PREVIEW, self._on)

    def _on(self, route):
        body = route.request.post_data_json or {}
        self.bodies.append(body)
        if self.hold and self.hold in ((body.get("query") or {}).get("sql") or ""):
            self.held.append(route)
        else:
            route.continue_()

    def wait(self, pred, what, timeout_ms=10_000):
        for _ in range(timeout_ms // 25):
            if pred():
                return
            self.page.wait_for_timeout(25)
        raise AssertionError(what)

    def close(self):
        for r in self.held:
            try:
                r.continue_()
            except Exception:
                pass
        self.held = []
        self.page.unroute(PREVIEW)


def _preview_stat(page):
    return page.evaluate("() => { const s = document.querySelector('#modal .dash-preview .dash-stat');"
                         " return s ? s.textContent : null; }")


def test_the_preview_answers_by_itself_and_typing_is_not_a_query_per_key(page):
    did = _board(page, "Live preview")
    asks = _Asks(page)
    try:
        page.locator("#dashBar button", has_text="Add widget").click()
        page.wait_for_selector("#modal:not([hidden]) .dash-ed")
        assert page.locator("#modal button", has_text=re.compile(r"^Preview$")).count() == 0
        page.locator("#modal .dash-sql").click()
        page.keyboard.type("SELECT 6 * 7 AS n")
        page.wait_for_function(
            "() => { const s = document.querySelector('#modal .dash-preview .dash-stat');"
            "        return s && s.textContent === '42'; }", timeout=10_000)
        # Seventeen keystrokes. Without the pause it was one query each,
        # every one of them run to the end on the server.
        sqls = [(b.get("query") or {}).get("sql") for b in asks.bodies]
        assert len(sqls) <= 2, sqls
        assert sqls[-1] == "SELECT 6 * 7 AS n"
        # It says how long the draft took — the number the "run every
        # time" checkbox is decided on.
        assert re.search(r"ran in \d", page.locator("#modal .dash-preview-meta").inner_text())
        _close(page)
    finally:
        asks.close()
        _drop(page, did)


def test_a_late_answer_to_an_older_draft_does_not_paint_over_the_newer_one(page):
    did = _board(page, "Stale preview")
    asks = _Asks(page, hold="SELECT 1 AS n")
    try:
        page.locator("#dashBar button", has_text="Add widget").click()
        page.wait_for_selector("#modal:not([hidden]) .dash-ed")
        page.locator("#modal .dash-sql").fill("SELECT 1 AS n")
        asks.wait(lambda: asks.held, "the first draft was never asked")
        page.locator("#modal .dash-sql").fill("SELECT 2 AS n")
        page.wait_for_function(
            "() => { const s = document.querySelector('#modal .dash-preview .dash-stat');"
            "        return s && s.textContent === '2'; }", timeout=10_000)
        # Now the slow one answers.
        with page.expect_response(lambda r: PREVIEW.match(r.url)
                                  and "SELECT 1 AS n" in (r.request.post_data or "")):
            asks.held.pop().continue_()
        page.evaluate("() => new Promise((r) => setTimeout(r, 200))")
        assert _preview_stat(page) == "2"
        _close(page)
    finally:
        asks.close()
        _drop(page, did)


def test_a_draft_that_cannot_run_says_what_is_missing_and_asks_nothing(page):
    did = _board(page, "Missing pick")
    asks = _Asks(page)
    try:
        # A portable table whose header set has no columns to offer —
        # stubbed at the route AND in state, for the reason
        # test_a_missing_pick_leaves_no_query_behind gives.
        page.route("**/api/header_sets", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"shorthands": {}, "sets": []}'))
        page.evaluate("() => { __winnow.S.headerSets = { shorthands: {}, sets: [] }; }")
        page.locator("#dashBar button", has_text="Add widget").click()
        page.wait_for_selector("#modal:not([hidden]) .dash-ed")
        note = page.locator("#modal .dash-preview .dash-empty-note")
        note.wait_for()
        assert "Write a query" in note.inner_text()
        page.locator("#modal .dash-table").select_option("{{evtx}}")
        page.locator("#modal .dash-template").select_option("top")
        page.wait_for_function(
            "() => /no column to pick/.test(document.querySelector('#modal .dash-preview').textContent)",
            timeout=10_000)
        text = page.locator("#modal .dash-preview .dash-widget-body").inner_text().strip()
        assert "Event logs (EvtxECmd) has no column to pick" in text
        assert "\n" not in text   # one line
        assert page.locator("#modal .dash-preview .dash-empty-note.bad").count() == 0
        assert asks.bodies == []
        _close(page)
    finally:
        page.unroute("**/api/header_sets")
        page.evaluate("() => { __winnow.S.headerSets = null; }")
        asks.close()
        _drop(page, did)


def test_changing_only_the_look_repaints_without_asking_again(page):
    did = _board(page, "Look only", [{
        "title": "Pairs", "source": "sql", "render": "kv",
        "query": {"sql": "SELECT 'alpha' AS k, 3 AS v UNION ALL SELECT 'beta', 5"}}])
    page.wait_for_selector("#dashGrid .dash-kv")
    asks = _Asks(page)
    try:
        page.locator("#dashGrid .dash-edit").first.click()
        page.wait_for_selector("#modal .dash-preview .dash-kv")
        assert len(asks.bodies) == 1
        page.locator("#modal .dash-kinds button[data-render='list']").click()
        page.wait_for_selector("#modal .dash-preview .dash-list-row")
        page.locator("#modal .dash-title-input").fill("Renamed pairs")
        page.wait_for_function(
            "() => document.querySelector('#modal .dash-preview .dash-head h4').textContent === 'Renamed pairs'")
        page.locator("#modal .dash-width button[data-span='3']").click()
        page.evaluate("() => new Promise((r) => setTimeout(r, 700))")   # past the pause
        assert len(asks.bodies) == 1, asks.bodies
        _close(page)
    finally:
        asks.close()
        _drop(page, did)


def test_the_preview_files_nothing_in_the_board_cache(page):
    """A draft is not the widget until it is saved: a preview of edited SQL
    that wrote its answer into the case file would put that number on the
    board's card after Escape, under the old query."""
    src = _src(page)
    did = _board(page, "Cache untouched", [{
        "title": "Rows", "source": "sql", "render": "stat",
        "query": {"sql": f"SELECT COUNT(*) AS n FROM src_{src}"}}])
    try:
        page.wait_for_selector("#dashGrid .dash-stat")
        page.wait_for_function(f"""(id) => fetch('/api/dashboards/' + id).then(r => r.json())
          .then(d => Object.values(d.cache || {{}}).some(c => c.ran_at))""", arg=did)
        before = _board_json(page, did)["cache"]
        asks = _Asks(page)
        try:
            page.locator("#dashGrid .dash-edit").first.click()
            page.locator("#modal .dash-sql").fill("SELECT 99 AS n")
            page.wait_for_function(
                "() => { const s = document.querySelector('#modal .dash-preview .dash-stat');"
                "        return s && s.textContent === '99'; }", timeout=10_000)
            assert asks.bodies and all("dashboard_id" not in b and "widget_id" not in b for b in asks.bodies)
        finally:
            asks.close()
        _close(page)
        assert _board_json(page, did)["cache"] == before
        assert page.locator("#dashGrid .dash-stat").first.inner_text() == "200"
    finally:
        _drop(page, did)


def test_names_are_readable_and_the_stored_values_are_not_changed(page):
    did = _board(page, "Names", [{
        "title": "Pairs", "source": "sql", "render": "kv",
        "query": {"sql": "SELECT 'a' AS k, 1 AS v"}}])
    try:
        page.wait_for_selector("#dashGrid .dash-kv")
        page.locator("#dashGrid .dash-edit").first.click()
        page.wait_for_selector("#modal:not([hidden]) .dash-ed")
        opts = page.evaluate("() => [...document.querySelectorAll('#modal .dash-source option')]"
                             ".map(o => [o.value, o.textContent])")
        assert opts == [["sql", "A query"], ["watchlist", "Watchlist hits"],
                        ["tags", "Tag counts"], ["cells", "Signals card"]]
        kinds = page.evaluate("() => [...document.querySelectorAll('#modal .dash-kinds button')]"
                              ".map(b => [b.dataset.render, b.textContent])")
        assert kinds == [["stat", "Number"], ["kv", "Key / value"], ["chips", "Yes / no chips"],
                         ["list", "List"], ["bar", "Bar chart"], ["histogram", "Histogram"],
                         ["signals", "Signals"]]
        assert page.locator("#modal .dash-kinds [aria-pressed='true']").inner_text() == "Key / value"
        # Saved untouched, it is still exactly what was stored.
        page.locator("#modal button", has_text="Save widget").click()
        page.wait_for_function("() => document.getElementById('modal').hidden", timeout=10_000)
        (w,) = _board_json(page, did)["widgets"]
        assert (w["source"], w["render"]) == ("sql", "kv")
        # And a readable pick stores the old value.
        page.locator("#dashGrid .dash-edit").first.click()
        page.wait_for_selector("#modal:not([hidden]) .dash-ed")
        page.locator("#modal .dash-source").select_option(label="Tag counts")
        page.locator("#modal .dash-kinds button", has_text="Number").click()
        page.locator("#modal button", has_text="Save widget").click()
        page.wait_for_function("() => document.getElementById('modal').hidden", timeout=10_000)
        (w,) = _board_json(page, did)["widgets"]
        assert (w["source"], w["render"]) == ("tags", "stat")
    finally:
        _drop(page, did)


def test_the_sql_example_names_the_picked_table_by_its_label(page):
    did = _board(page, "Placeholder")
    try:
        page.locator("#dashBar button", has_text="Add widget").click()
        page.wait_for_selector("#modal:not([hidden]) .dash-ed")
        src = page.evaluate("() => __winnow.S.sources.find((s) => !s.is_merge)")
        page.locator("#modal .dash-table").select_option(f"src_{src['id']}")
        ph = page.locator("#modal .dash-sql").get_attribute("placeholder")
        assert f"src_{src['id']}" in ph and (src.get("nickname") or src["name"]) in ph
        _close(page)
    finally:
        _drop(page, did)


def test_the_editor_is_two_columns_when_wide_and_stacks_when_narrow(page):
    did = _board(page, "Layout")
    geo = """() => {
      const [l, r] = [...document.querySelectorAll('#modal .dash-ed-pane')].map(p => p.getBoundingClientRect());
      const card = document.querySelector('#modal .dash-preview').getBoundingClientRect();
      const save = [...document.querySelectorAll('#modal button')].find(b => b.textContent === 'Save widget')
        .getBoundingClientRect();
      const body = document.getElementById('modalBody');
      return { l: [l.left, l.top, l.right, l.bottom], r: [r.left, r.top, r.right, r.bottom],
               cardIn: card.left >= r.left - 1 && card.right <= r.right + 1,
               saveBottom: save.bottom, vh: innerHeight,
               hOverflow: body.scrollWidth - body.clientWidth,
               slid: document.body.scrollLeft || document.scrollingElement.scrollLeft };
    }"""
    try:
        page.set_viewport_size({"width": 1400, "height": 900})
        page.locator("#dashBar button", has_text="Add widget").click()
        page.wait_for_selector("#modal:not([hidden]) .dash-ed")
        g = page.evaluate(geo)
        assert g["r"][0] >= g["l"][2] - 1 and abs(g["r"][1] - g["l"][1]) < 2, g   # side by side
        assert g["cardIn"], g                                                       # the preview is on the right
        assert g["saveBottom"] <= g["vh"], g                                        # Save on screen, no scrolling
        page.set_viewport_size({"width": 700, "height": 800})
        page.wait_for_function(
            "() => { const p = document.querySelectorAll('#modal .dash-ed-pane');"
            "        return p[1].getBoundingClientRect().top >= p[0].getBoundingClientRect().bottom - 1; }")
        g = page.evaluate(geo)
        assert g["hOverflow"] <= 1 and g["slid"] == 0, g
        _close(page)
    finally:
        page.set_viewport_size({"width": 1500, "height": 900})
        _drop(page, did)


def test_a_double_clicked_save_adds_the_widget_once(page):
    did = _board(page, "Double save")
    posts, held = [], []

    def on(route):
        if route.request.method != "POST":
            route.continue_()
            return
        posts.append(route)
        held.append(route)

    glob = f"**/api/dashboards/{did}"
    page.route(glob, on)
    try:
        page.locator("#dashBar button", has_text="Add widget").click()
        page.wait_for_selector("#modal:not([hidden]) .dash-ed")
        page.locator("#modal .dash-sql").fill("SELECT 1 AS n")
        page.locator("#modal .dash-title-input").fill("Once")
        page.locator("#modal button", has_text="Save widget").dblclick()
        for _ in range(100):
            if posts:
                break
            page.wait_for_timeout(50)
        page.wait_for_timeout(500)   # room for a second one to show up
        assert len(posts) == 1
    finally:
        for r in held:
            r.fulfill(status=400, content_type="application/json",
                      body=json.dumps({"detail": "refused by the test"}))
        page.unroute(glob)
        page.evaluate("() => { document.getElementById('modal').hidden = true; }")
        _drop(page, did)
