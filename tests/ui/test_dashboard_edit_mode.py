"""Widget cards carry ONE button (✎ opens the editor, which now owns
removal) plus a drag grip — the grip is the only drag source, so widget
text stays selectable. And the SQL pane's "To dashboard…" turns the
current query into a widget on a chosen board."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _new_board(page, name):
    did = page.evaluate("""async (name) => {
      const h = { 'Content-Type': 'application/json', 'X-Timeline-Lite-Client': '1' };
      const d = await fetch('/api/dashboards', { method: 'POST', headers: h,
        body: JSON.stringify({ name }) }).then(r => r.json());
      await __winnow.loadDashboards();
      await __winnow.showDashboard(d.id);
      return d.id;
    }""", name)
    page.wait_for_selector("#dashboardview:not([hidden])")
    return did


def _delete_board(page, did):
    page.evaluate("""(id) => fetch('/api/dashboards/' + id,
      { method: 'DELETE', headers: { 'X-Timeline-Lite-Client': '1' } })""", did)


def _seed_widgets(page, did, titles):
    page.evaluate("""async ([id, titles]) => {
      const h = { 'Content-Type': 'application/json', 'X-Timeline-Lite-Client': '1' };
      const widgets = titles.map((t) => ({ title: t, source: 'sql', render: 'stat',
        query: { sql: 'SELECT 1 AS n' } }));
      await fetch('/api/dashboards/' + id, { method: 'POST', headers: h,
        body: JSON.stringify({ widgets }) });
      await __winnow.showDashboard(id);
    }""", [did, titles])
    page.wait_for_function(
        "(n) => document.querySelectorAll('#dashGrid .dash-card:not(.dash-add)').length === n", arg=len(titles))


def test_single_edit_button_and_remove_via_editor(page):
    did = _new_board(page, "Edit board")
    _seed_widgets(page, did, ["W1", "W2"])
    card = page.locator("#dashGrid .dash-card", has_text="W1").first
    # One action button on the card: edit. No per-card remove.
    assert card.locator(".dash-edit").count() == 1
    assert card.locator(".dash-rm").count() == 0
    assert card.locator(".dash-grip").count() == 1
    card.locator(".dash-edit").click()
    page.wait_for_selector("#modal:not([hidden])")
    page.locator("#modal button", has_text="Remove widget…").click()
    page.locator(".confirm-card button", has_text="Remove").click()
    page.wait_for_selector("#modal[hidden]", state="attached")
    page.wait_for_function(
        "() => ![...document.querySelectorAll('#dashGrid .dash-card h4')].some(h => h.textContent === 'W1')")
    _delete_board(page, did)


def test_grip_drag_reorders_and_persists(page):
    did = _new_board(page, "Drag board")
    _seed_widgets(page, did, ["A", "B", "C"])
    # HTML5 dnd, synthesized (Playwright's drag_and_drop sends mouse events,
    # which never fire dragstart): grip of A → card C.
    page.evaluate("""() => {
      const cards = [...document.querySelectorAll('#dashGrid .dash-card:not(.dash-add)')];
      const dt = new DataTransfer();
      const grip = cards[0].querySelector('.dash-grip');
      grip.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
      cards[2].dispatchEvent(new DragEvent('dragover', { bubbles: true, dataTransfer: dt }));
      cards[2].dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: dt }));
      grip.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt }));
    }""")
    page.wait_for_function(
        """() => [...document.querySelectorAll('#dashGrid .dash-card h4')].map(h => h.textContent).join('') === 'BCA'""")
    # Persisted server-side, not just repainted.
    order = page.evaluate("""(id) => fetch('/api/dashboards/' + id).then(r => r.json())
      .then(d => d.widgets.map(w => w.title).join(''))""", did)
    assert order == "BCA"
    _delete_board(page, did)


def test_sql_query_becomes_a_widget(page):
    did = _new_board(page, "SQL board")
    page.locator("#tabSql").click()
    page.wait_for_selector("#sqlview:not([hidden])")
    src = page.evaluate("() => __winnow.S.sources.find((s) => !s.is_merge).id")
    page.locator("#sqlText").fill(f"SELECT COUNT(*) AS n FROM src_{src}")
    page.locator("#btnSqlWidget").click()
    # Board picker lists our board; choosing it opens the prefilled editor.
    page.locator(".menu .menu-item", has_text="SQL board").click()
    page.wait_for_selector("#modal:not([hidden])")
    assert f"src_{src}" in page.locator("#modal .dash-sql").input_value()
    page.locator("#modal .dash-title-input").fill("Rows via SQL")
    page.locator("#modal button", has_text="Save widget").click()
    page.wait_for_selector("#modal[hidden]", state="attached")
    page.wait_for_function(
        "() => [...document.querySelectorAll('#dashGrid .dash-card h4')].some(h => h.textContent === 'Rows via SQL')")
    _delete_board(page, did)


def _titles_on_server(page, did):
    return page.evaluate("""(id) => fetch('/api/dashboards/' + id).then(r => r.json())
      .then(d => d.widgets.map(w => w.title))""", did)


def _titles_on_screen(page):
    return page.evaluate("() => [...document.querySelectorAll('#dashGrid .dash-card h4')].map(h => h.textContent)")


def _hold_board_get(page, did, held):
    """Hold this board's GET (its widgets) until the test lets it go; let
    anything else to the same URL — a save — straight through."""
    page.route(f"**/api/dashboards/{did}",
               lambda r: held.append(r) if r.request.method == "GET" else r.continue_())


def _wait_held(page, held):
    for _ in range(100):
        if held:
            return
        page.wait_for_timeout(50)
    raise AssertionError("the board's widgets were never asked for")


def test_a_board_that_loads_late_does_not_land_on_the_one_switched_to(page):
    """Click board A, then board B before A's widgets arrive. loadWidgets
    bumped the board generation but never compared it after its await, so
    A's late answer replaced B's widgets on screen — under B's name."""
    a = _new_board(page, "Late board")
    _seed_widgets(page, a, ["L1", "L2"])
    b = _new_board(page, "Switched-to board")
    _seed_widgets(page, b, ["S1", "S2", "S3"])
    page.evaluate("() => __winnow.showGridTab()")
    held = []
    _hold_board_get(page, a, held)
    try:
        page.evaluate("([a, b]) => { window.__openA = __winnow.showDashboard(a);"
                      " return __winnow.showDashboard(b); }", [a, b])
        _wait_held(page, held)
        assert _titles_on_screen(page) == ["S1", "S2", "S3"]
        for r in held:
            r.continue_()
    finally:
        page.unroute(f"**/api/dashboards/{a}")
    page.evaluate("() => window.__openA")      # A's open has fully run its course
    assert page.evaluate("() => __winnow.S.dashboardId") == b
    assert _titles_on_screen(page) == ["S1", "S2", "S3"]
    assert page.locator("#dashBar .dash-title").inner_text() == "Switched-to board"
    _delete_board(page, a)
    _delete_board(page, b)


def test_a_drag_while_the_next_board_loads_saves_to_the_board_it_was_made_on(page):
    """The other half of the same window: board B has been clicked, its
    widgets are still on their way, and board A's cards are what is on
    screen. persist() saved `widgets` to S.dashboardId — B — so one drag
    of A's cards wrote them over B's own, and B's widgets were gone."""
    a = _new_board(page, "Dragged board")
    _seed_widgets(page, a, ["D1", "D2", "D3"])
    b = _new_board(page, "Waiting board")
    _seed_widgets(page, b, ["W1", "W2"])
    page.evaluate("(a) => __winnow.showDashboard(a)", a)
    page.wait_for_function("() => document.querySelectorAll('#dashGrid .dash-card:not(.dash-add)').length === 3")
    held = []
    _hold_board_get(page, b, held)
    try:
        page.evaluate("(b) => { window.__openB = __winnow.showDashboard(b); }", b)
        _wait_held(page, held)
        with page.expect_response(lambda r: r.request.method == "POST" and "/api/dashboards/" in r.url):
            page.evaluate("""() => {
              const cards = [...document.querySelectorAll('#dashGrid .dash-card:not(.dash-add)')];
              const dt = new DataTransfer();
              const grip = cards[0].querySelector('.dash-grip');
              grip.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
              cards[2].dispatchEvent(new DragEvent('dragover', { bubbles: true, dataTransfer: dt }));
              cards[2].dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: dt }));
              grip.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt }));
            }""")
        for r in held:
            r.continue_()
    finally:
        page.unroute(f"**/api/dashboards/{b}")
    page.evaluate("() => window.__openB")
    assert _titles_on_server(page, b) == ["W1", "W2"]
    assert _titles_on_server(page, a) == ["D2", "D3", "D1"]
    page.wait_for_function("() => [...document.querySelectorAll('#dashGrid .dash-card h4')]"
                           ".map(h => h.textContent).join() === 'W1,W2'")
    _delete_board(page, a)
    _delete_board(page, b)
