"""A case group's folder, in the three places it shows (a demo of two ways
for a group to get one; see tests/test_case_group_folders.py for the
registry side):

- + New case: picking a group with a folder puts the case file there, and
  leaving the group puts the cases folder back;
- the folder browser, from Import → Add from this machine…: the open
  case's group folder is pinned on top, with no ✕, and the browser opens
  there;
- the group's own list on the home screen: under first_case it names the
  case that set the folder; under settings it is where the folder is
  chosen and cleared.

Each test files its cases under a group name of its own and takes them,
and any mode it switched, back out in a `finally`.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _close(page):
    """Escape the dialog: twice when the group list is open, since the
    first one only closes the list."""
    if page.locator("#modal .combo-list").count():
        page.keyboard.press("Escape")
        page.wait_for_selector("#modal .combo-list", state="detached")
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")


def _forget(api, rec):
    api(f"/api/cases/{rec['id']}?delete_file=true", "DELETE")


def _new_case(page, name, group):
    page.evaluate("() => __winnow.openNewCaseModal()")
    page.wait_for_selector("#modal:not([hidden]) .nc-case")
    page.locator("#modalBody .nc-name").fill(name)
    page.locator("#modalBody .case-group-input").fill(group)


def test_new_case_goes_in_its_groups_folder(page, api, tmp_path):
    group = "UI Group Folder A"
    folder = tmp_path / "acme engagement"
    first = api("/api/cases", "POST", {"path": str(folder / "ws01.db-winnow"), "name": "UI GF WS01",
                                       "group": group})
    try:
        page.evaluate("() => __winnow.openNewCaseModal()")
        page.wait_for_selector("#modal:not([hidden]) .nc-case")
        cases_dir = page.locator("#modalBody .nc-path").input_value().rsplit("/", 1)[0]
        page.locator("#modalBody .nc-name").fill("UI GF DC01")
        page.locator("#modalBody .case-group-input").fill(group)
        path = page.locator("#modalBody .nc-path")
        assert path.input_value() == f"{folder}/ui-gf-dc01.db-winnow"
        assert "set by its first case, UI GF WS01" in page.locator("#modalBody .nc-group-note").inner_text()
        # Out of the group, back where cases go.
        page.locator("#modalBody .case-group-input").fill("")
        assert path.input_value() == f"{cases_dir}/ui-gf-dc01.db-winnow"
        assert page.locator("#modalBody .nc-group-note").is_hidden()
    finally:
        _close(page)
        _forget(api, first)


def test_a_new_group_says_this_folder_becomes_its_folder(page, api):
    _new_case(page, "UI GF new", "UI Group Folder Nobody Has")
    try:
        note = page.locator("#modalBody .nc-group-note").inner_text()
        assert "is a new group, so this folder becomes its folder" in note, note
    finally:
        _close(page)


def test_importing_has_the_groups_folder_pinned(page, api):
    """The open case put in a group: Add from this machine… lists the
    group's folder first, under Pinned, as the group's (no ✕ to unpin it),
    and opens in it."""
    cur = api("/api/case/current")
    me = next(c for c in api("/api/cases") if c["path"] == cur["path"])
    group = "UI Group Folder B"
    folder = cur["path"].rsplit("/", 1)[0]
    api(f"/api/cases/{me['id']}", "PUT", {"group": group})
    try:
        page.evaluate("() => { __winnow.S.importQueue = []; __winnow.openImportModal(); }")
        page.locator("#modal .import-foot .btn", has_text="Add from this machine").click()
        row = page.locator("#modal .browse-panes .side-rail-row:has(.side-rail-fixed)")
        row.wait_for()
        page.wait_for_function("(p) => document.querySelector('#modal .browse-path')?.value === p", arg=folder)
        first = page.evaluate("""() => [...document.querySelectorAll('#modal .side-rail-list > *')].slice(0, 2)
          .map((n) => n.classList.contains('side-rail-group') ? '# ' + n.textContent
            : n.querySelector('.side-rail-label').textContent + ' | ' + n.querySelector('.side-rail-hint').textContent)""")
        assert first == ["# Pinned", f"{group} | ‎{folder}‎"], first
        assert row.locator(".side-rail-pin").count() == 0, "the group pinned it; there is nothing to unpin"
        assert row.locator(".side-rail-fixed").inner_text() == "group"
    finally:
        page.keyboard.press("Escape")
        api(f"/api/cases/{me['id']}", "PUT", {"group": me.get("group") or ""})


def test_settings_the_folder_is_chosen_in_the_groups_list(page, api, tmp_path):
    group = "UIGroupFolderC"
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    api("/api/prefs", "POST", {"group_folder_mode": "settings"})
    rec = api("/api/cases", "POST", {"path": str(tmp_path / "c" / "c.db-winnow"), "name": "UI GF C",
                                     "group": group})
    try:
        page.evaluate("() => { __winnow.showHome(); return __winnow.refreshCases(); }")
        page.locator(f".home-panes .side-rail-item:has(.side-rail-label:text-is('{group}'))").click()
        strip = page.locator(f".home-group-folder[data-group='{group}']")
        strip.wait_for()
        assert "None" in strip.locator(".hgf-path").inner_text()
        strip.locator(".hgf-choose").click()
        page.wait_for_selector("#modal:not([hidden]) .browse-panes")
        page.locator("#modal .browse-path").fill(str(chosen))
        page.locator("#modal .browse-path").press("Enter")
        page.wait_for_function("(p) => document.querySelector('#modal .browse-path')?.value === p", arg=str(chosen))
        page.locator("#modal .table-menu-foot .btn.primary").click()
        strip.locator(".hgf-clear").wait_for()
        assert strip.locator(".hgf-path").inner_text().strip("‎") == str(chosen)

        _new_case(page, "UI GF C2", group)
        assert page.locator("#modalBody .nc-path").input_value() == f"{chosen}/ui-gf-c2.db-winnow"
        assert "from the group’s settings" in page.locator("#modalBody .nc-group-note").inner_text()
        _close(page)

        strip.locator(".hgf-clear").click()
        page.wait_for_function("(g) => !document.querySelector(`.home-group-folder[data-group='${g}'] .hgf-clear`)",
                               arg=group)
        assert "None" in strip.locator(".hgf-path").inner_text()
    finally:
        api("/api/case_groups/folder", "POST", {"group": group, "folder": None})
        api("/api/prefs", "POST", {"group_folder_mode": "first_case"})
        _forget(api, rec)
