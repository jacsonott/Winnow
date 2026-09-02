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
    page.locator("#modal .confirm-input").first.fill("Rows via SQL")
    page.locator("#modal button", has_text="Save widget").click()
    page.wait_for_selector("#modal[hidden]", state="attached")
    page.wait_for_function(
        "() => [...document.querySelectorAll('#dashGrid .dash-card h4')].some(h => h.textContent === 'Rows via SQL')")
    _delete_board(page, did)
