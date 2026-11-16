"""The case Group box's list of groups, in + New case and Edit case.

It was a <datalist>, and a datalist's list is not part of the page:
Chromium draws it as its autofill bubble, white with the OS font on every
skin. color-scheme does not reach it either, so on a dark skin the one
white thing on screen was the list of groups. These pin that the list is
the page's own, painted in the skin's colors on every skin in both
themes, and that it still works as the datalist did from the keyboard:
typing narrows it, Down and Enter take one, and Escape closes the list
without closing the dialog under it.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

CASES = [
    {"id": 901, "path": "/srv/cases/a.db-winnow", "name": "ACME intrusion", "group": "Incident response", "notes": ""},
    {"id": 902, "path": "/srv/cases/b.db-winnow", "name": "Globex BEC", "group": "Incident response", "notes": ""},
    {"id": 903, "path": "/srv/cases/c.db-winnow", "name": "Initech insider", "group": "Insider threat", "notes": ""},
    {"id": 904, "path": "/srv/cases/d.db-winnow", "name": "Umbrella", "group": "Ransomware 2026", "notes": ""},
    {"id": 905, "path": "/srv/cases/e.db-winnow", "name": "Hooli phishing", "group": "", "notes": ""},
]
GROUPS = ["Incident response", "Insider threat", "Ransomware 2026"]


def _edit(page, case=0):
    """Edit case on one of CASES, the groups offered being CASES' own.
    S.cases is what the list reads; set here rather than routing
    /api/cases, so nothing else on the page repaints under the test."""
    page.evaluate("([cases, i]) => { __winnow.S.cases = cases; __winnow.openEditCaseModal(cases[i]); }", [CASES, case])
    # Found by its caption, not by anything the fix added, so the first
    # test fails on the old datalist for the reason it says.
    box = page.locator("#modal .field-label:text-is('Group') + .row-actions input")
    box.wait_for()
    return box


def _shown(page):
    return page.locator("#modal .combo-list .combo-option").all_inner_texts()


def _close(page):
    """Escape the dialog: twice when the list is open, since the first
    one only closes the list (test_escape_closes_the_list_and_leaves_the_dialog_open)."""
    if page.locator("#modal .combo-list").count():
        page.keyboard.press("Escape")
        page.wait_for_selector("#modal .combo-list", state="detached")
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_the_group_list_is_painted_in_the_skin_on_every_skin(page):
    """The symptom, on each skin and theme: the list that opens is part of
    the page (no datalist for the browser to draw) and its background and
    text are the skin's menu colors. Compared against a probe painted with
    the same tokens, so a skin that re-hues them keeps passing and a list
    drawn by anything else does not."""
    styles = page.evaluate("() => Object.keys(__winnow.STYLES)")
    assert len(styles) >= 6, styles
    box = _edit(page)
    try:
        assert page.locator("#modal datalist").count() == 0
        assert box.get_attribute("list") is None, "a list= box is drawn by the browser, not the skin"
        box.click()
        page.wait_for_selector("#modal .combo-list")
        assert _shown(page) == GROUPS
        for style in styles:
            for theme in ("dark", "light"):
                got = page.evaluate("""([style, theme]) => {
                    const html = document.documentElement;
                    html.setAttribute('data-style', style);
                    html.setAttribute('data-theme', theme);
                    const probe = document.createElement('div');
                    probe.style.cssText = 'background: var(--panel); color: var(--text)';
                    document.body.append(probe);
                    const want = getComputedStyle(probe);
                    const list = getComputedStyle(document.querySelector('#modal .combo-list'));
                    const opt = getComputedStyle(document.querySelector('#modal .combo-option:not(.current)'));
                    const out = { want: [want.backgroundColor, want.color],
                                  got: [list.backgroundColor, opt.color] };
                    probe.remove();
                    return out;
                }""", [style, theme])
                assert got["got"] == got["want"], (style, theme, got)
    finally:
        _close(page)


def test_typing_narrows_the_list_and_enter_takes_one(page):
    box = _edit(page, case=4)
    try:
        assert box.input_value() == ""
        box.press_sequentially("ins")
        page.wait_for_function("() => document.querySelectorAll('#modal .combo-option').length === 1")
        assert _shown(page) == ["Insider threat"]
        box.press("ArrowDown")
        page.wait_for_selector("#modal .combo-option.active")
        box.press("Enter")
        page.wait_for_selector("#modal .combo-list", state="detached")
        assert box.input_value() == "Insider threat"
        # Free text still: a group nobody has yet is a value too.
        box.fill("Fraud 2026")
        page.wait_for_selector("#modal .combo-list", state="detached")
        assert box.input_value() == "Fraud 2026"
    finally:
        _close(page)


def test_escape_closes_the_list_and_leaves_the_dialog_open(page):
    box = _edit(page)
    try:
        box.press("ArrowDown")
        page.wait_for_selector("#modal .combo-list")
        box.press("Escape")
        page.wait_for_selector("#modal .combo-list", state="detached")
        assert page.locator("#modal").is_visible(), "Escape on the list closed the dialog too"
        assert box.input_value() == "Incident response"
    finally:
        _close(page)


def test_the_caret_opens_every_group_with_the_current_one_marked(page):
    """The ▾ opens the whole list even when the box already holds a
    group, which a typed filter would narrow to that one group; a click
    on another takes it."""
    box = _edit(page)
    try:
        page.locator("#modal .combo-caret").click()
        page.wait_for_selector("#modal .combo-list")
        assert _shown(page) == GROUPS
        assert page.locator("#modal .combo-option.current").inner_text() == "Incident response"
        page.locator("#modal .combo-option", has_text="Ransomware 2026").click()
        page.wait_for_selector("#modal .combo-list", state="detached")
        assert box.input_value() == "Ransomware 2026"
        page.locator("#modal .combo-caret").click()
        page.wait_for_selector("#modal .combo-list")
        page.locator("#modal .combo-caret").click()
        page.wait_for_selector("#modal .combo-list", state="detached")
    finally:
        _close(page)


def test_new_case_offers_the_same_groups(page):
    page.evaluate("(cases) => { __winnow.S.cases = cases; return __winnow.openNewCaseModal(); }", CASES)
    page.wait_for_selector("#modal:not([hidden]) .nc-case .case-group-input")
    try:
        page.locator("#modal .nc-case .case-group-input").click()
        page.wait_for_selector("#modal .combo-list")
        assert _shown(page) == GROUPS
    finally:
        _close(page)
