"""A board opens from its cached results, not from 26 queries.

The measurement this replaces: opening the shipped KAPE board issued one
`POST /api/dashboard/widget/preview` per widget — on every open, on every
card drag and on every edit of any other card. What is asserted here is
the symptom, in requests and in pixels: on a reopen only the widgets
marked "run every time" go to the server, the rest are already on screen
while that one is still in flight, and every card says how old its number
is.

Counting happens in a `page.route` handler rather than by waiting: the
widgets of one board are all issued inside a single `render()` task, in
card order, and Playwright delivers them to the handler in that order. So
putting the live widget LAST makes "its request arrived and no other did"
a fact rather than a timeout.
"""

from __future__ import annotations

import re
import time

import pytest

pytestmark = pytest.mark.ui

PREVIEW = re.compile(r".*/api/dashboard/widget/preview$")


def _until(page, pred, what, timeout=10):
    """Python-side state (a route handler's list) is invisible to
    wait_for_function; poll it here, bounded."""
    deadline = time.time() + timeout
    while not pred():
        assert time.time() < deadline, what
        page.wait_for_timeout(25)


class _Previews:
    """Every widget preview the page asks for, in order. `hold` keeps them
    in flight so the test can look at a board that has painted but not yet
    re-run."""

    def __init__(self, page):
        self.page = page
        self.bodies = []
        self.held = []
        self.hold = False
        page.route(PREVIEW, self._on)

    def _on(self, route):
        self.bodies.append(route.request.post_data_json or {})
        if self.hold:
            self.held.append(route)
        else:
            route.continue_()

    def release(self):
        self.hold = False
        for r in self.held:
            r.continue_()
        self.held = []


def _make_board(page, name, widgets):
    return page.evaluate("""async ([name, widgets]) => {
      const h = { 'Content-Type': 'application/json', 'X-Timeline-Lite-Client': '1' };
      const d = await fetch('/api/dashboards', { method: 'POST', headers: h,
        body: JSON.stringify({ name, widgets }) }).then(r => r.json());
      await __winnow.loadDashboards();
      return d.id;
    }""", [name, widgets])


def _drop_board(page, did):
    page.evaluate("""(id) => fetch('/api/dashboards/' + id,
      { method: 'DELETE', headers: { 'X-Timeline-Lite-Client': '1' } })""", did)


def _widget(title, sql, live=False):
    w = {"title": title, "source": "sql", "render": "stat", "query": {"sql": sql}}
    if live:
        w["live"] = True
    return w


def _open(page, did):
    # Fired, not awaited: showDashboard resolves only once its widgets have
    # been asked for, and this test holds those requests on purpose.
    page.evaluate("(id) => { __winnow.showDashboard(id); }", did)
    page.wait_for_selector("#dashboardview:not([hidden])", timeout=15_000)


def _leave(page):
    page.evaluate("() => __winnow.showGridTab()")
    page.wait_for_selector("#dashboardview[hidden]", state="attached", timeout=15_000)


def _widget_ids(page, did):
    return page.evaluate("""(id) => fetch('/api/dashboards/' + id).then(r => r.json())
      .then(d => d.widgets.map(w => w.id))""", did)


def test_reopening_a_board_only_runs_the_widgets_marked_live(page):
    did = _make_board(page, "Cache board", [
        _widget("Alpha", "SELECT 42 AS n"),
        _widget("Beta", "SELECT 7 AS n"),
        _widget("Gamma", "SELECT 9 AS n", live=True),
    ])
    prev = _Previews(page)
    try:
        # A first open has nothing to paint from, so it runs everything.
        _open(page, did)
        page.wait_for_function(
            "() => document.querySelectorAll('#dashGrid .dash-stat').length === 3", timeout=15_000)
        _until(page, lambda: len(prev.bodies) == 3, f"first open ran {len(prev.bodies)}/3 widgets")

        ids = _widget_ids(page, did)
        prev.bodies.clear()
        prev.hold = True
        _leave(page)
        _open(page, did)
        # Every card carries its number while the live one is still out.
        page.wait_for_function(
            """() => [...document.querySelectorAll('#dashGrid .dash-stat')]
                 .map(s => s.textContent).join(',') === '42,7,9'""", timeout=15_000)
        _until(page, lambda: len(prev.held) >= 1, "the live widget never re-ran")
        assert [b.get("widget_id") for b in prev.bodies] == [ids[2]]
    finally:
        prev.release()
        _drop_board(page, did)


def test_dragging_a_card_does_not_re_run_the_board(page):
    """Half the cost was here: render() after a persist() re-ran every
    widget, so moving one card re-ran all of them."""
    did = _make_board(page, "Drag cache", [
        _widget("Alpha", "SELECT 42 AS n"),
        _widget("Beta", "SELECT 7 AS n"),
    ])
    prev = _Previews(page)
    try:
        _open(page, did)
        page.wait_for_function(
            "() => document.querySelectorAll('#dashGrid .dash-stat').length === 2", timeout=15_000)
        _until(page, lambda: len(prev.bodies) == 2, "first open did not run both widgets")
        prev.bodies.clear()

        page.evaluate("""() => {
          const cards = [...document.querySelectorAll('#dashGrid .dash-card:not(.dash-add)')];
          const dt = new DataTransfer();
          const grip = cards[0].querySelector('.dash-grip');
          grip.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
          cards[1].dispatchEvent(new DragEvent('dragover', { bubbles: true, dataTransfer: dt }));
          cards[1].dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: dt }));
          grip.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt }));
        }""")
        page.wait_for_function(
            """() => [...document.querySelectorAll('#dashGrid .dash-card h4')]
                 .map(h => h.textContent).join('') === 'BetaAlpha'""", timeout=15_000)
        # The numbers moved with the cards rather than going back to
        # "Loading…" and round-tripping the server.
        assert page.locator("#dashGrid .dash-stat").all_text_contents() == ["7", "42"]
        # ↻ Refresh IS a re-run, and its requests are the barrier: once they
        # have arrived, anything the drag issued would have arrived first.
        page.locator("#dashBar .dash-refresh").click()
        _until(page, lambda: len(prev.bodies) >= 2, "Refresh all ran nothing")
        assert len(prev.bodies) == 2, "the drag re-ran widgets it did not need to"
    finally:
        prev.release()
        _drop_board(page, did)


def test_a_cached_card_says_how_old_it_is_and_a_live_one_says_it_is_live(page):
    """The staleness this introduces is a correctness problem, not a polish
    one: a cached "3 failed logons" reads exactly like a live 3."""
    did = _make_board(page, "Stamp board", [
        _widget("Alpha", "SELECT 42 AS n"),
        _widget("Gamma", "SELECT 9 AS n", live=True),
    ])
    try:
        _open(page, did)
        page.wait_for_function(
            "() => document.querySelectorAll('#dashGrid .dash-stat').length === 2", timeout=15_000)
        _leave(page)
        _open(page, did)
        page.wait_for_selector("#dashBar .dash-stamp", timeout=15_000)
        assert page.locator("#dashBar .dash-stamp").inner_text().startswith("As of ")
        assert "1 widget runs every time" in page.locator("#dashBar .dash-live-note").inner_text()
        cards = page.locator("#dashGrid .dash-card:not(.dash-add)")
        assert cards.nth(0).locator(".dash-mark.dash-age").count() == 1
        assert cards.nth(0).locator(".dash-mark").inner_text() == "now"
        assert cards.nth(1).locator(".dash-mark.dash-live-dot").count() == 1
        assert cards.nth(1).locator(".dash-mark").inner_text() == ""
    finally:
        _drop_board(page, did)


def test_a_tag_write_makes_the_board_say_so_rather_than_hiding_it(page):
    """A number that was true an hour ago beats no number, but only if the
    board says which it is. Tagging a row and undoing it leaves the shared
    case as it was found."""
    did = _make_board(page, "Stale board", [_widget("Alpha", "SELECT 42 AS n")])
    tagged = False
    try:
        _open(page, did)
        page.wait_for_function(
            "() => document.querySelectorAll('#dashGrid .dash-stat').length === 1", timeout=15_000)
        page.evaluate("""() => {
          const t = __winnow.S.tags[0];
          const sid = __winnow.S.sources.find((s) => !s.is_merge).id;
          return fetch('/api/row_tags', { method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Timeline-Lite-Client': '1' },
            body: JSON.stringify({ source_id: sid, rids: [1], tag_id: t.id, on: true }) })
            .then(r => r.json());
        }""")
        tagged = True
        _leave(page)
        _open(page, did)
        page.wait_for_selector("#dashBar .dash-stamp.stale", timeout=15_000)
        assert "the case has changed since" in page.locator("#dashBar .dash-stamp").inner_text()
        assert page.locator("#dashGrid .dash-card .dash-mark.stale").count() == 1
        # The number it worked out is still on the card, dated — not blank.
        assert page.locator("#dashGrid .dash-stat").inner_text() == "42"
    finally:
        if tagged:
            page.evaluate("""() => fetch('/api/row_tags/undo', { method: 'POST',
              headers: { 'X-Timeline-Lite-Client': '1' } }).then(r => r.json())""")
        _drop_board(page, did)


def test_the_editor_turns_run_every_time_on_and_back_off(page):
    """Off again matters on its own: the save path assigns the draft over
    the stored widget, and a key the draft leaves out survives the
    assignment — so "live" has to be cleared explicitly or it can never be
    turned off."""
    did = _make_board(page, "Editor board", [_widget("Alpha", "SELECT 42 AS n")])
    try:
        _open(page, did)
        page.wait_for_function(
            "() => document.querySelectorAll('#dashGrid .dash-stat').length === 1", timeout=15_000)
        page.locator("#dashGrid .dash-card .dash-edit").first.click()
        page.wait_for_selector("#modal:not([hidden])", timeout=10_000)
        assert "1 widget" in page.locator("#modal .dash-cost").inner_text()
        page.locator("#modal .dash-live-box").check()
        page.locator("#modal button", has_text="Save widget").click()
        page.wait_for_selector("#modal[hidden]", state="attached", timeout=10_000)
        page.wait_for_function(
            "() => document.querySelectorAll('#dashGrid .dash-card .dash-live-dot').length === 1",
            timeout=15_000)
        assert page.evaluate("""(id) => fetch('/api/dashboards/' + id).then(r => r.json())
          .then(d => d.widgets[0].live)""", did) is True

        page.locator("#dashGrid .dash-card .dash-edit").first.click()
        page.wait_for_selector("#modal:not([hidden])", timeout=10_000)
        assert page.locator("#modal .dash-live-box").is_checked()
        page.locator("#modal .dash-live-box").uncheck()
        page.locator("#modal button", has_text="Save widget").click()
        page.wait_for_selector("#modal[hidden]", state="attached", timeout=10_000)
        page.wait_for_function(
            "() => document.querySelectorAll('#dashGrid .dash-card .dash-live-dot').length === 0",
            timeout=15_000)
        assert page.evaluate("""(id) => fetch('/api/dashboards/' + id).then(r => r.json())
          .then(d => 'live' in d.widgets[0])""", did) is False
    finally:
        _drop_board(page, did)


def test_run_now_re_runs_one_widget_and_restamps_it(page):
    did = _make_board(page, "Run now board", [
        _widget("Alpha", "SELECT 42 AS n"),
        _widget("Beta", "SELECT 7 AS n"),
    ])
    prev = _Previews(page)
    try:
        _open(page, did)
        page.wait_for_function(
            "() => document.querySelectorAll('#dashGrid .dash-stat').length === 2", timeout=15_000)
        _until(page, lambda: len(prev.bodies) == 2, "first open did not run both widgets")
        prev.bodies.clear()
        page.locator("#dashGrid .dash-card .dash-edit").first.click()
        page.wait_for_selector("#modal:not([hidden])", timeout=10_000)
        with page.expect_response(re.compile(r".*/refresh$"), timeout=15_000) as got:
            page.locator("#modal button", has_text="Run now").click()
        # One widget, named — not the whole board, and not through the
        # per-widget preview route either.
        assert list(got.value.json()["results"]) == [_widget_ids(page, did)[0]]
        assert prev.bodies == []
    finally:
        prev.release()
        _drop_board(page, did)
