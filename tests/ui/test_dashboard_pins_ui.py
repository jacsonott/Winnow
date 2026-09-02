"""Pinned dashboards ride the page strip: pin from the sidebar action or
by dragging a board onto the Pages header, the tab shows and highlights
the board, the Pages row's ✕ unpins; and the library round-trips from the
bar's Save to library… to a Library row that adds the board to a case."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _new_board(page, name):
    did = page.evaluate("""async (name) => {
      const h = { 'Content-Type': 'application/json', 'X-Timeline-Lite-Client': '1' };
      const d = await fetch('/api/dashboards', { method: 'POST', headers: h,
        body: JSON.stringify({ name, widgets: [{ title: 'W', source: 'tags', render: 'stat' }] }) }).then(r => r.json());
      await __winnow.loadDashboards();
      __winnow.renderSidebar();
      return d.id;
    }""", name)
    return did


def _cleanup(page):
    page.evaluate("""async () => {
      const h = { 'X-Timeline-Lite-Client': '1' };
      for (const d of await fetch('/api/dashboards', { headers: h }).then(r => r.json()))
        await fetch('/api/dashboards/' + d.id, { method: 'DELETE', headers: h });
      for (const b of await fetch('/api/dashboard_library', { headers: h }).then(r => r.json()))
        await fetch('/api/dashboard_library/' + b.id, { method: 'DELETE', headers: h });
      __winnow.S.dashboardId = null;
      if (__winnow.S.activeTab === 'dashboard') __winnow.showGridTab();
      await __winnow.loadDashboards();
      __winnow.renderSidebar();
    }""")


def test_pin_via_action_and_unpin_via_pages_row(page):
    did = _new_board(page, "Pinboard")
    try:
        row = page.locator("#sidebarList .sidebar-row", has_text="Pinboard").first
        row.hover()
        row.locator(".menu-item-action[title^='Pin as a page tab']").click()
        tab = page.locator("#pageTabs .tab-dashboard", has_text="Pinboard")
        tab.wait_for(state="visible")
        tab.click()
        page.wait_for_selector("#dashboardview:not([hidden])")
        page.wait_for_function("(id) => __winnow.S.dashboardId === id", arg=did)
        assert tab.get_attribute("aria-selected") == "true"
        # The Pages section lists it; its ✕ unpins (the board stays in the case).
        prow = page.locator("#sidebarList .sidebar-row", has_text="Pinboard").first
        prow.hover()
        prow.locator(".menu-item-action[title='Close this page tab']").click()
        page.wait_for_selector("#pageTabs .tab-dashboard", state="detached")
        assert page.evaluate("() => __winnow.S.dashboards.some((d) => d.name === 'Pinboard' && !d.pinned)")
    finally:
        _cleanup(page)


def test_drag_onto_pages_header_pins(page):
    _new_board(page, "Dragboard")
    try:
        page.evaluate("""() => {
          const row = [...document.querySelectorAll('#sidebarList .sidebar-row')].find(r => r.textContent.includes('Dragboard') && r.draggable);
          const hdr = document.querySelector('#sidebarList .sidebar-pages-header');
          const dt = new DataTransfer();
          row.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
          hdr.dispatchEvent(new DragEvent('dragover', { bubbles: true, dataTransfer: dt, cancelable: true }));
          hdr.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: dt, cancelable: true }));
          row.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt }));
        }""")
        page.locator("#pageTabs .tab-dashboard", has_text="Dragboard").wait_for(state="visible", timeout=10_000)
    finally:
        _cleanup(page)


def test_save_to_library_and_add_to_case(page):
    did = _new_board(page, "Libboard")
    try:
        page.evaluate("(id) => __winnow.showDashboard(id)", did)
        page.wait_for_selector("#dashboardview:not([hidden])")
        page.locator("#dashBar button", has_text="Save to library…").click()
        page.wait_for_selector(".confirm-overlay .confirm-input")
        page.locator(".confirm-overlay .confirm-input").fill("Libboard (shared)")
        page.locator(".confirm-actions .btn", has_text="Save").click()
        page.wait_for_selector("#sidebarList .sidebar-dash-library:has-text('Libboard (shared)')")
        # Delete the case copy, then bring the library board back in.
        page.evaluate("""async (id) => {
          const h = { 'X-Timeline-Lite-Client': '1' };
          await fetch('/api/dashboards/' + id, { method: 'DELETE', headers: h });
          __winnow.S.dashboardId = null; __winnow.showGridTab();
          await __winnow.loadDashboards(); __winnow.renderSidebar();
        }""", did)
        lrow = page.locator("#sidebarList .sidebar-dash-library", has_text="Libboard (shared)")
        lrow.hover()
        lrow.locator(".menu-item-action[title^='Add this board']").click()
        page.wait_for_selector("#dashboardview:not([hidden])")
        page.wait_for_function("() => __winnow.S.dashboards.some((d) => d.name === 'Libboard (shared)')")
    finally:
        _cleanup(page)
