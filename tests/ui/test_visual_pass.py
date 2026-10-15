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
        # Escape in the search box clears it and rebuilds the view, and a
        # view that lands mid-selection resets it: when that fell between
        # the two clicks below, the Shift-click selected one row, "1"
        # untagged only that, and the wait timed out with seven rows still
        # tagged on the shared case. Wait for the rebuild to land first.
        before = page.evaluate("() => __winnow.S.view.view_id")
        page.keyboard.press("Escape")
        page.wait_for_function(
            "(vid) => __winnow.S.view && __winnow.S.view.view_id !== vid"
            " && document.getElementById('searchWrap').hidden", arg=before)
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


def test_timeframe_dialog_labels_sit_on_rows_of_their_own(page):
    """The dialog's first labels were glued into "EnabledColumn": a
    checkbox and the label after it sharing a line. The Enabled box has
    gone since (Apply turns the filter on); what leads now is the "Keep
    applied when switching tables" box, then Column, then the range's
    Start, and each still has to sit on a row of its own."""
    page.evaluate("() => __winnow.openTimeRangeModal()")
    page.wait_for_selector("#modal:not([hidden])")
    tops = page.evaluate(
        "() => [...document.querySelectorAll('#modalBody label')].slice(0, 3).map(l => [l.textContent.trim(), l.getBoundingClientRect().top])")
    page.keyboard.press("Escape")
    assert [t[0] for t in tops] == ["Keep applied when switching tables", "Column", "Start"]
    assert tops[1][1] - tops[0][1] >= 10, tops
    assert tops[2][1] - tops[1][1] >= 10, tops


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


# What a pressed segment and an unpressed one beside it actually paint, in
# every skin and theme: each one's background composited over its
# ancestors' down to the first opaque one (the builders' tint is 16% accent
# over the dialog's panel, so its own background-color alone says nothing),
# and the pressed one's text colour. Transitions are switched off while it
# runs — .btn eases its colours, and a value read straight after a skin
# change would be the old skin's.
_SEGMENT_PAINT = """([sel, styles]) => {
  const parse = (s) => {
    let m = /^rgba?\\(([^)]*)\\)$/.exec(s);
    if (m) {
      const p = m[1].split(/[\\s,\\/]+/).filter(Boolean).map(Number);
      return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
    }
    m = /^color\\(srgb ([^)]*)\\)$/.exec(s);
    if (m) {
      const p = m[1].split(/[\\s\\/]+/).filter(Boolean).map(Number);
      return [p[0] * 255, p[1] * 255, p[2] * 255, p.length > 3 ? p[3] : 1];
    }
    throw new Error('unreadable colour ' + s);
  };
  const painted = (node) => {
    const layers = [];
    for (let n = node; n && n.nodeType === 1; n = n.parentElement) {
      const c = parse(getComputedStyle(n).backgroundColor);
      layers.push(c);
      if (c[3] >= 1) break;
    }
    let out = [0, 0, 0];
    for (const [r, g, b, a] of layers.reverse()) {
      out = [r * a + out[0] * (1 - a), g * a + out[1] * (1 - a), b * a + out[2] * (1 - a)];
    }
    return out;
  };
  const calm = document.createElement('style');
  calm.textContent = '*, *::before, *::after { transition: none !important; }';
  document.head.append(calm);
  const out = [];
  try {
    for (const style of styles) {
      for (const theme of ['dark', 'light']) {
        __winnow.applyStyle(style);
        __winnow.applyThemeMode(theme);
        const on = document.querySelector(sel + '[aria-pressed="true"]');
        const off = [...on.parentElement.children].find((b) => b !== on && b.getAttribute('aria-pressed') !== 'true');
        out.push({ skin: style + ' ' + theme, on: painted(on), off: painted(off),
                   text: parse(getComputedStyle(on).color).slice(0, 3) });
      }
    }
  } finally {
    calm.remove();
  }
  return out;
}"""


def _luminance(rgb):
    def chan(c):
        c = c / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _delta_e(a, b):
    """CIE76 ΔE between two sRGB colours: ~2 is the smallest difference
    anyone notices, 10 one nobody misses."""
    def lab(rgb):
        r, g, b = (((c / 255) / 12.92) if c / 255 <= 0.04045 else (((c / 255) + 0.055) / 1.055) ** 2.4
                   for c in rgb)
        x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
        y = 0.2126 * r + 0.7152 * g + 0.0722 * b
        z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
        f = [t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116 for t in (x, y, z)]
        return 116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])
    return sum((p - q) ** 2 for p, q in zip(lab(a), lab(b))) ** 0.5


def test_pressed_dialog_segments_read_in_every_skin(page):
    """A pressed segment painted accent text on the accent-dim fill
    (1.4–2.7:1); the filled accent that replaced it put --accent-fg on
    --accent, 3.3:1 in Panel light and the loudest thing in each dialog.
    Dialog segments are the builders' tint now — 16% accent under --text —
    and in every skin and theme the pressed option has to read (4.5:1) and
    has to look different from the one beside it."""
    styles = page.evaluate("() => Object.keys(__winnow.STYLES)")
    surfaces = [
        ("Settings → Theme", "() => __winnow.openSettings()", "#modalBody .segmented button"),
        ("Log", "() => __winnow.openLog()", "#modalBody .vp-seg .btn"),
        ("Search tables", "() => __winnow.openSearchAllModal()", "#modalBody .search-mode-toggle .btn"),
        ("Plugins", "() => __winnow.openPluginManager()", "#modalBody .pl-scope button"),
        ("Value picker", "() => __winnow.openValuePickerForColumn('Host')", ".value-picker .vp-seg .btn"),
    ]
    bad = []
    try:
        for name, opener, sel in surfaces:
            page.evaluate(opener)
            page.wait_for_selector(sel + "[aria-pressed='true']", state="attached")
            for m in page.evaluate(_SEGMENT_PAINT, [sel, styles]):
                ratio, gap = _contrast(m["text"], m["on"]), _delta_e(m["on"], m["off"])
                if ratio < 4.5 or gap < 8:
                    bad.append(f"{name} / {m['skin']}: text {ratio:.2f}:1, {gap:.1f} ΔE from unpressed")
            page.keyboard.press("Escape")
            page.wait_for_function("() => document.getElementById('modal').hidden && !document.querySelector('.value-picker')")
    finally:
        page.evaluate("() => { __winnow.applyStyle('harvest'); __winnow.applyThemeMode('dark'); }")
    assert not bad, bad


def test_toolbar_toggles_keep_their_fill(page):
    """The tint is for segments inside a dialog. The search box's own
    Contains / Regex / Advanced toggle borrows the same class the search
    dialog does, and stays the toolbar's accent-dim fill."""
    got = page.evaluate(
        "() => getComputedStyle(document.querySelector('#searchModeToggle button[aria-pressed=\"true\"]')).backgroundColor")
    assert got == _token(page, "backgroundColor", "--accent-dim")


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
    vh = page.evaluate("() => window.innerHeight")
    # The card is sized to content and anchored at the top: a section
    # opening grows it DOWNWARD, so the top edge (and everything above the
    # click) stays put, and it never passes the 82vh it used to be pinned to.
    prev_h = before["h"]
    for i in (3, 5, 8):
        heads.nth(i).click()
        page.wait_for_timeout(100)
        after = _rect(page, ".modal-card")
        assert after["y"] == before["y"], (i, before, after)
        assert after["h"] >= prev_h and after["h"] <= vh * 0.82 + 1, (i, prev_h, after)
        prev_h = after["h"]
    page.keyboard.press("Escape")


def test_help_text_clears_the_focus_ring(page):
    """The help line under an input had no top margin, so the focused
    input's ring sat on its ascenders."""
    page.evaluate("() => __winnow.openJumpTsModal()")
    page.wait_for_selector("#modal:not([hidden])")
    gap = page.evaluate(
        """() => { const box = document.querySelector('#modalBody input[placeholder="YYYY-MM-DD HH:MM:SS"]');
             const help = [...document.querySelectorAll('#modalBody .fb-help')]
               .find((p) => p.textContent.startsWith('24-hour'));
             return help.getBoundingClientRect().top - box.getBoundingClientRect().bottom; }""")
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
