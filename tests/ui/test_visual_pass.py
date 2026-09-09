"""Regressions from the 2026-09 screenshot pass — defects you could only
see. A button folding into a two-line pill, a pressed toggle painted
accent-on-accent, two labels glued into "EnabledColumn", a Settings dialog
that re-centred itself on every click. Each is pinned as geometry or
computed colour in the live document, because a green backend suite said
nothing about any of them."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _rect(page, sel):
    return page.evaluate(
        """(s) => { const r = document.querySelector(s).getBoundingClientRect();
             return { x: r.x, y: r.y, w: r.width, h: r.height, right: r.right, bottom: r.bottom }; }""", sel)


def _token(page, prop, name):
    """The computed value of a CSS custom property, as the browser resolves it."""
    return page.evaluate(
        """([prop, name]) => { const d = document.createElement('div'); d.style[prop] = `var(${name})`;
             document.body.append(d); const v = getComputedStyle(d)[prop]; d.remove(); return v; }""", [prop, name])


def test_toolbar_buttons_never_fold_into_two_lines(page):
    """Tag counts, an open search box and a 1280px window all used to squeeze
    the toolbar's flex items evenly, so every button became a two-line pill
    and the chips stacked vertically. Now the buttons hold and the strip and
    ribbon give way."""
    page.set_viewport_size({"width": 1280, "height": 720})
    rows = page.locator(".row")
    rows.nth(1).locator(".cell").nth(1).click()
    rows.nth(8).locator(".cell").nth(1).click(modifiers=["Shift"])
    page.keyboard.press("1")
    page.wait_for_function("() => (__winnow.rowAt(1) || { tags: [] }).tags.length === 1")
    page.keyboard.press("Escape")
    page.keyboard.press("/")
    page.wait_for_selector("#searchWrap:not([hidden])")
    try:
        heights = page.evaluate(
            "() => [...document.querySelectorAll('#toolbar .btn')].filter(b => b.offsetParent).map(b => b.getBoundingClientRect().height)")
        assert heights and max(heights) < 32, heights
        # The empty group strip is one line: label, hint, + Tag — never "+ Tag" folded under "GROUP BY".
        assert _rect(page, "#groupStrip")["h"] < 40
        # The search box is the one thing that never shrinks.
        assert _rect(page, "#search")["w"] >= 300
    finally:
        page.keyboard.press("Escape")
        rows.nth(1).locator(".cell").nth(1).click()
        rows.nth(8).locator(".cell").nth(1).click(modifiers=["Shift"])
        page.keyboard.press("1")
        page.wait_for_function("() => (__winnow.rowAt(1) || { tags: [] }).tags.length === 0")
        page.keyboard.press("Escape")


def test_sql_run_button_shares_the_row_with_the_others(page):
    page.locator("#tabSql").click()
    page.wait_for_selector("#sqlview:not([hidden])")
    # The query-tab strip renders after its fetch and can add a line above
    # the head; measure both buttons in one call once it has settled.
    page.wait_for_selector("#sqlTabs > *")
    tops = "() => ['btnRunSql', 'btnSqlSchema'].map(id => document.getElementById(id).getBoundingClientRect().top)"
    # Waits for the settled layout rather than a fixed beat: it returns as
    # soon as the two share a row and only spends the timeout on a failure.
    page.wait_for_function(f"() => {{ const t = ({tops})(); return t[0] === t[1]; }}")
    assert page.evaluate(tops)[0] == page.evaluate(tops)[1]


def test_timeframe_enabled_and_column_are_separate_rows(page):
    page.evaluate("() => __winnow.openTimeRangeModal()")
    page.wait_for_selector("#modal:not([hidden])")
    tops = page.evaluate(
        "() => [...document.querySelectorAll('#modalBody label')].slice(0, 2).map(l => [l.textContent.trim(), l.getBoundingClientRect().top])")
    page.keyboard.press("Escape")
    assert [t[0] for t in tops] == ["Enabled", "Column"]
    assert tops[1][1] - tops[0][1] >= 10, tops


def test_regex_hint_sits_outside_the_typed_text(page):
    page.keyboard.press("/")
    page.wait_for_selector("#searchWrap:not([hidden])")
    page.keyboard.type("powershell")
    page.locator("#searchModeToggle button").nth(1).click()   # Regex
    try:
        page.wait_for_function("() => document.getElementById('searchMode').textContent.startsWith('regex')")
        geo = page.evaluate(
            """() => { const i = document.getElementById('search'), h = document.getElementById('searchMode');
                 const ib = i.getBoundingClientRect();
                 return { contentRight: ib.right - parseFloat(getComputedStyle(i).paddingRight),
                          hintLeft: h.getBoundingClientRect().left }; }""")
        assert geo["hintLeft"] >= geo["contentRight"] - 1, geo
    finally:
        page.locator("#searchModeToggle button").nth(0).click()
        page.locator("#search").fill("")
        page.keyboard.press("Escape")


def test_value_picker_pressed_segment_is_the_filled_accent(page):
    """Accent text on the accent-dim fill measured 1.4–2.7:1 across the
    skins. The pressed segment is now the same filled look the Settings
    segments use."""
    page.evaluate("() => __winnow.openValuePickerForColumn('Host')")
    page.wait_for_selector(".value-picker [aria-pressed='true']")
    got = page.evaluate(
        """() => { const cs = getComputedStyle(document.querySelector('.value-picker [aria-pressed="true"]'));
             return { bg: cs.backgroundColor, color: cs.color }; }""")
    page.keyboard.press("Escape")
    assert got["bg"] == _token(page, "backgroundColor", "--accent")
    assert got["color"] == _token(page, "color", "--accent-fg")


def test_blueprint_paints_close_and_tab_count_in_the_accent_foreground(page):
    """Blueprint fills modal headers and the active tab with the solid
    accent; the Close button and the row count kept their dim grey and
    measured 1.08:1 on it."""
    prev = page.evaluate("() => document.documentElement.getAttribute('data-style')")
    page.evaluate("() => document.documentElement.setAttribute('data-style', 'blueprint')")
    try:
        page.locator("#btnSettings").click()
        page.wait_for_selector("#modal:not([hidden])")
        fg = _token(page, "color", "--accent-fg")
        assert page.evaluate("() => getComputedStyle(document.getElementById('modalClose')).color") == fg
        page.keyboard.press("Escape")
        assert page.evaluate(
            "() => getComputedStyle(document.querySelector('#sourceTabs .tab[aria-selected=\"true\"] .count')).color") == fg
    finally:
        page.evaluate("(s) => document.documentElement.setAttribute('data-style', s)", prev)


def test_settings_dialog_holds_still_while_sections_expand(page):
    page.locator("#btnSettings").click()
    page.wait_for_selector("#modal:not([hidden])")
    heads = page.locator("#modal .settings-section-head")
    before = _rect(page, ".modal-card")
    for i in (3, 5, 8):
        heads.nth(i).click()
        page.wait_for_timeout(100)
        after = _rect(page, ".modal-card")
        assert (after["y"], after["h"]) == (before["y"], before["h"]), (i, before, after)
    page.keyboard.press("Escape")


def test_help_text_clears_the_focus_ring(page):
    """The help line under an input had no top margin, so the focused
    input's ring sat on its ascenders."""
    page.evaluate("() => __winnow.openJumpTsModal()")
    page.wait_for_selector("#modal:not([hidden])")
    gap = page.evaluate(
        """() => { const row = document.querySelector('#modalBody .row-actions');
             const help = row.nextElementSibling;
             return help.getBoundingClientRect().top - row.getBoundingClientRect().bottom; }""")
    page.keyboard.press("Escape")
    assert gap >= 4, gap


def test_dashboard_sidebar_count_follows_saved_widgets(page):
    """The sidebar's per-board count comes from the list endpoint; saving a
    widget wrote the board but never re-read the list, so it said 0."""
    did = page.evaluate("""async () => {
      const h = { 'Content-Type': 'application/json', 'X-Timeline-Lite-Client': '1' };
      const d = await fetch('/api/dashboards', { method: 'POST', headers: h,
        body: JSON.stringify({ name: 'Count board' }) }).then(r => r.json());
      await __winnow.loadDashboards();
      await __winnow.showDashboard(d.id);
      return d.id;
    }""")
    page.wait_for_selector("#dashboardview:not([hidden])")
    try:
        src = page.evaluate("() => __winnow.S.sources[0].id")
        page.locator("#dashBar button", has_text="Add widget").click()
        page.locator("#modal .confirm-input").first.fill("Rows")
        page.locator("#modal select").first.select_option("sql")
        page.locator("#modal .dash-sql").fill(f"SELECT COUNT(*) AS n FROM src_{src}")
        page.locator("#modal button", has_text="Save widget").click()
        page.wait_for_selector("#modal", state="hidden")
        page.wait_for_function(
            "() => [...document.querySelectorAll('#sidebarList .sidebar-row')]"
            ".some(r => /Count board/.test(r.textContent) && r.querySelector('.sidebar-row-count').textContent === '1')")
    finally:
        page.evaluate("""(id) => fetch('/api/dashboards/' + id,
          { method: 'DELETE', headers: { 'X-Timeline-Lite-Client': '1' } })""", did)
        page.evaluate("async () => { await __winnow.loadDashboards(); __winnow.renderSidebar(); }")


def test_widget_editor_pairs_every_label_with_its_control(page):
    """The editor's labels used to butt straight against the next control
    and the SQL label dangled at the end of the template row."""
    did = page.evaluate("""async () => {
      const h = { 'Content-Type': 'application/json', 'X-Timeline-Lite-Client': '1' };
      const d = await fetch('/api/dashboards', { method: 'POST', headers: h,
        body: JSON.stringify({ name: 'Editor board' }) }).then(r => r.json());
      await __winnow.loadDashboards();
      await __winnow.showDashboard(d.id);
      return d.id;
    }""")
    page.wait_for_selector("#dashboardview:not([hidden])")
    try:
        page.locator("#dashBar button", has_text="Add widget").click()
        page.wait_for_selector("#modal .dash-form")
        bad = page.evaluate(
            """() => [...document.querySelectorAll('#modal .dash-field')].filter(f => {
                 const l = f.querySelector('label'), c = l && l.nextElementSibling;
                 if (!l) return false;   // the SQL box under Advanced is labelled by its summary
                 if (!c) return true;
                 const lb = l.getBoundingClientRect(), cb = c.getBoundingClientRect();
                 return !(cb.top >= lb.bottom - 1 && Math.abs(cb.left - lb.left) < 2);   // control sits under its label
               }).map(f => f.textContent.trim())""")
        assert bad == [], bad
        page.keyboard.press("Escape")
    finally:
        page.evaluate("""(id) => fetch('/api/dashboards/' + id,
          { method: 'DELETE', headers: { 'X-Timeline-Lite-Client': '1' } })""", did)
        page.evaluate("async () => { await __winnow.loadDashboards(); __winnow.renderSidebar(); }")


def test_group_strip_marks_whether_it_holds_pills(page):
    """Empty, the strip is one clipped line; with pills it wraps. The class
    is what the CSS switches on."""
    assert not page.evaluate("() => document.getElementById('groupStrip').classList.contains('has-groups')")
    page.evaluate("() => { __winnow.setGrouping(['Host'], 'count', 'desc'); __winnow.renderGroupStrip(); }")
    try:
        assert page.evaluate("() => document.getElementById('groupStrip').classList.contains('has-groups')")
    finally:
        page.evaluate("() => { __winnow.setGrouping([], 'count', 'desc'); __winnow.renderHead(); }")


def test_notes_preview_renders_markdown_tables(page):
    html = page.evaluate("() => __winnow.renderMarkdown('| a | b |\\n|---|---|\\n| 1 | **2** |\\n\\nafter')")
    assert "<table>" in html and "<th>a</th>" in html and "<td><strong>2</strong></td>" in html
    assert "<p>after</p>" in html
    # A pipe line with no separator under it is just a paragraph.
    assert "<table>" not in page.evaluate("() => __winnow.renderMarkdown('| not | a table |')")


def test_no_match_filter_says_so_and_leaves_the_filter_reachable(page):
    box = page.locator('#filterRow input[data-col="Host"]')
    box.fill("zzzz-no-such-host")
    page.wait_for_selector("#noRows:not([hidden])")
    assert "No rows match" in page.locator("#noRows").inner_text()
    # The message is not a cover: the filter box is still there to edit.
    box.click()
    assert page.evaluate("() => document.activeElement === document.querySelector('#filterRow input[data-col=\"Host\"]')")
    page.locator("#noRowsClear").click()
    page.wait_for_selector("#noRows", state="hidden")
    page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 200")
