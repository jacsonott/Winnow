"""A case group's folders, in the three places they show (see
tests/test_case_group_folders.py for the registry side):

- + New case: picking a group puts the case file in its first folder,
  leaving the group puts the cases folder back, and the note under the
  path offers the group's other folders, or to add the one typed;
- the folder browser, from Import → Add from this machine…: the open
  case's group folders are pinned on top, with no ✕, and the browser
  opens in the first;
- the group's own list on the home screen: it names the case that set
  the first folder, and is where more are added, put first and removed.

Each test files its cases under a group name of its own and takes them
back out in a `finally`.
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


def _folders(api, group):
    return [f["path"] for f in api("/api/case_groups")["groups"][group]["folders"]]


def test_the_note_offers_the_groups_other_folders_and_adds_a_new_one(page, api, tmp_path):
    group = "UI Group Folder D"
    share, third = tmp_path / "evidence share", tmp_path / "third"
    share.mkdir()
    third.mkdir()
    first = api("/api/cases", "POST", {"path": str(tmp_path / "acme" / "ws01.db-winnow"), "name": "UI GF D1",
                                       "group": group})
    api("/api/case_groups/folders", "POST", {"group": group, "add": str(share)})
    try:
        _new_case(page, "UI GF D2", group)
        path = page.locator("#modalBody .nc-path")
        note = page.locator("#modalBody .nc-group-note")
        assert path.input_value() == f"{tmp_path}/acme/ui-gf-d2.db-winnow"
        assert note.inner_text().startswith(f"One of {group}’s 2 folders, set by its first case, UI GF D1."), note.inner_text()
        note.locator(".nc-use-group", has_text="evidence share").click()
        assert path.input_value() == f"{share}/ui-gf-d2.db-winnow"
        assert note.inner_text().startswith(f"One of {group}’s 2 folders. Its other folder:"), note.inner_text()

        # Somewhere the group doesn't use yet: one click adds it.
        path.fill(f"{third}/ui-gf-d2.db-winnow")
        assert f"Not in any of {group}’s 2 folders" in note.inner_text()
        note.locator(".nc-add-group-folder").click()
        page.wait_for_function("(g) => document.querySelector('#modalBody .nc-group-note')?.textContent"
                               ".startsWith(`One of ${g}’s 3 folders`)", arg=group)
        assert _folders(api, group) == [f"{tmp_path}/acme", str(share), str(third)]
    finally:
        _close(page)
        _forget(api, first)


def test_importing_has_the_groups_folders_pinned(page, api, tmp_path):
    """The open case put in a group: Add from this machine… lists the
    group's folders first, under Pinned, by their own names and marked as
    the group's (no ✕ to unpin them), and opens in the first."""
    cur = api("/api/case/current")
    me = next(c for c in api("/api/cases") if c["path"] == cur["path"])
    group = "UI Group Folder B"
    folder = cur["path"].rsplit("/", 1)[0]
    api(f"/api/cases/{me['id']}", "PUT", {"group": group})
    api("/api/case_groups/folders", "POST", {"group": group, "add": str(tmp_path)})
    try:
        page.evaluate("() => { __winnow.S.importQueue = []; __winnow.openImportModal(); }")
        page.locator("#modal .import-foot .btn", has_text="Add from this machine").click()
        rows = page.locator("#modal .browse-panes .side-rail-row:has(.side-rail-fixed)")
        rows.nth(1).wait_for()
        page.wait_for_function("(p) => document.querySelector('#modal .browse-path')?.value === p", arg=folder)
        first = page.evaluate("""() => [...document.querySelectorAll('#modal .side-rail-list > *')].slice(0, 3)
          .map((n) => n.classList.contains('side-rail-group') ? '# ' + n.textContent
            : n.querySelector('.side-rail-label').textContent + ' | ' + n.querySelector('.side-rail-hint').textContent)""")
        leaf = lambda p: str(p).rstrip("/").rsplit("/", 1)[-1]  # noqa: E731
        assert first == ["# Pinned", f"{leaf(folder)} | \u200e{folder}\u200e",
                         f"{leaf(tmp_path)} | \u200e{tmp_path}\u200e"], first
        assert rows.locator(".side-rail-pin").count() == 0, "the group pinned them; there is nothing to unpin"
        assert rows.locator(".side-rail-fixed").all_inner_texts() == ["group", "group"]
    finally:
        page.keyboard.press("Escape")
        api(f"/api/cases/{me['id']}", "PUT", {"group": me.get("group") or ""})


def test_the_groups_list_adds_reorders_and_removes_folders(page, api, tmp_path):
    group = "UIGroupFolderC"
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    rec = api("/api/cases", "POST", {"path": str(tmp_path / "c" / "c.db-winnow"), "name": "UI GF C",
                                     "group": group})
    try:
        page.evaluate("() => { __winnow.showHome(); return __winnow.refreshCases(); }")
        page.locator(f".home-panes .side-rail-item:has(.side-rail-label:text-is('{group}'))").click()
        strip = page.locator(f".home-group-folder[data-group='{group}']")
        strip.wait_for()
        rows = strip.locator(".hgf-row")
        assert rows.count() == 1
        assert rows.first.locator(".hgf-how").inner_text() == "set by its first case, UI GF C"
        assert rows.first.locator(".hgf-first, .hgf-make-first").count() == 0, "one folder has no order to pick"

        strip.locator(".hgf-add").click()
        page.wait_for_selector("#modal:not([hidden]) .browse-panes")
        page.locator("#modal .browse-path").fill(str(chosen))
        page.locator("#modal .browse-path").press("Enter")
        page.wait_for_function("(p) => document.querySelector('#modal .browse-path')?.value === p", arg=str(chosen))
        page.locator("#modal .table-menu-foot .btn.primary").click()
        rows.nth(1).wait_for()
        assert strip.locator(".hgf-label").inner_text().lower() == "group folders"
        assert rows.first.locator(".hgf-first").inner_text() == "New cases start here"

        rows.nth(1).locator(".hgf-make-first").click()
        page.wait_for_function("(p) => document.querySelector('.home-group-folder .hgf-row')?.dataset.path === p",
                               arg=str(chosen))
        _new_case(page, "UI GF C2", group)
        assert page.locator("#modalBody .nc-path").input_value() == f"{chosen}/ui-gf-c2.db-winnow"
        _close(page)

        for _ in range(2):
            n = rows.count()
            rows.first.locator(".hgf-remove").click()
            page.wait_for_function("([g, n]) => document.querySelectorAll(`.home-group-folder[data-group='${g}'] .hgf-row`)"
                                   ".length === n - 1", arg=[group, n])
        assert strip.locator(".hgf-none").inner_text() == f"None — the next case filed in {group} sets it"
        assert _folders(api, group) == []
    finally:
        _forget(api, rec)
