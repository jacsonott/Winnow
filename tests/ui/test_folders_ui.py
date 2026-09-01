"""Sidebar folders, driven through the real UI: create from the sidebar,
file a table into a folder, reorder folders, and delete non-destructively.

The backend behaviour is pinned in tests/test_folders.py; this exists for
the class of bug those can't see — a folder header whose actions never
un-hide, a move menu that doesn't wire, a delete that loses the table."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _new_folder(page, name):
    page.locator("#btnNewFolder").click()
    page.wait_for_selector(".confirm-overlay .confirm-input")
    page.locator(".confirm-overlay .confirm-input").fill(name)
    page.locator(".confirm-actions .btn", has_text="Create").click()
    page.wait_for_selector(".confirm-overlay", state="detached")


def test_create_file_reorder_and_delete(page):
    # a fresh case has one ungrouped table (ui.csv) — remember it
    table = page.locator("#sidebarList .sidebar-row .menu-item").first.inner_text()
    assert table

    # create two folders from the sidebar header
    _new_folder(page, "Registry")
    _new_folder(page, "Logs")
    page.wait_for_function(
        "() => document.querySelectorAll('#sidebarList .sidebar-folder').length === 2")
    names = page.eval_on_selector_all(
        "#sidebarList .sidebar-folder .folder-name", "els => els.map(e => e.textContent)")
    assert names == ["Registry", "Logs"]

    # reorder: push Registry down past Logs with its ▼
    reg = page.locator(".sidebar-folder", has_text="Registry")
    reg.hover()
    reg.locator(".menu-item-action", has_text="▼").click()
    page.wait_for_function(
        "() => [...document.querySelectorAll('#sidebarList .sidebar-folder .folder-name')]"
        ".map(e => e.textContent).join(',') === 'Logs,Registry'")

    # file the table into Registry via the row's move menu
    row = page.locator("#sidebarList .sidebar-row", has_text=table)
    row.hover()
    row.locator('.menu-item-action[title="Move to a folder"]').click()
    page.wait_for_selector(".menu")
    page.locator(".menu .menu-item-text", has_text="Registry").click()
    # the Registry folder now counts one table
    page.wait_for_function(
        """() => { const f=[...document.querySelectorAll('#sidebarList .sidebar-folder')]
             .find(x => /Registry/.test(x.textContent));
             return f && f.querySelector('.sidebar-row-count').textContent === '1'; }""")

    # delete Registry — its table must survive, back at the root
    reg = page.locator(".sidebar-folder", has_text="Registry")
    reg.hover()
    reg.locator(".menu-item-action", has_text="✕").click()
    page.wait_for_selector(".confirm-overlay")
    page.locator(".confirm-actions .btn", has_text="Delete folder").click()
    page.wait_for_function(
        "() => ![...document.querySelectorAll('#sidebarList .sidebar-folder .folder-name')]"
        ".some(e => e.textContent === 'Registry')")
    # the table is still there
    assert page.locator("#sidebarList .sidebar-row", has_text=table).count() == 1
    # and the server agrees it is ungrouped again
    fid = page.evaluate(
        "(name) => (__winnow.S.sources.find(s => (s.nickname||s.name) === name) || {}).folder_id",
        table)
    assert fid is None

    # leave the shared case clean: drop the leftover Logs folder
    page.evaluate("""async () => {
      const h = { 'X-Timeline-Lite-Client': '1' };
      for (const f of await fetch('/api/folders', { headers: h }).then(r => r.json()))
        await fetch('/api/folders/' + f.id, { method: 'DELETE', headers: h });
      await __winnow.loadSources();
    }""")
