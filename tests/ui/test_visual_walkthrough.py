"""What a browser walk through the app turned up, pinned as geometry.

A one-column table that painted 93px wide; a toolbar that folded its
chips into a column and drew the row count over them at laptop widths;
header buttons breaking into two lines; a flatten suggestion that read
an EvtxECmd "@Name" key as an XML attribute; a Mac glyph on every
platform's Run button; blockquotes the Notes preview showed as raw ">".
Each was only visible as a rendered page, so each is checked as one.
"""
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui


def _rect(page, selector):
    return page.evaluate(
        "(s) => { const r = document.querySelector(s).getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; }",
        selector)


def _single_line(page, selector, limit=30):
    return _rect(page, selector)["h"] < limit


def test_a_one_column_table_takes_the_whole_grid(page, api, ui_csv):
    log = Path(ui_csv).parent / "walk.log"
    log.write_text("\n".join(f"2026-03-14T08:00:{i:02d}Z info hostd[{2000 + i}] Event {i}: user root logged in" for i in range(40)) + "\n")
    api("/api/ingest/jobs/path", "POST", {"path": str(log)})
    # An import queued from outside the page doesn't start the jobs poll,
    # so wait on the job here and refresh the source list ourselves.
    for _ in range(100):
        jobs = api("/api/ingest/jobs")
        if any(j.get("name") == "walk.log" and j.get("status") == "done" for j in (jobs.get("jobs") if isinstance(jobs, dict) else jobs)):
            break
        time.sleep(0.2)
    else:
        pytest.fail("walk.log never finished importing")
    page.evaluate("() => __winnow.loadSources(null, { navigate: false })")
    page.wait_for_function("() => __winnow.S.sources.some((s) => s.name === 'walk.log' && s.row_count === 40)", timeout=20_000)
    sid = page.evaluate("() => __winnow.S.sources.find((s) => s.name === 'walk.log').id")
    page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_function("(id) => __winnow.S.sourceId === id && !!document.querySelector('.hcell[data-col=\"Message\"]')", arg=sid)
    try:
        body_w = page.evaluate("() => document.getElementById('body').clientWidth")
        col_w = _rect(page, '.hcell[data-col="Message"]')["w"]
        # Gutter (104) plus a little for the scrollbar is all that isn't column.
        assert col_w >= body_w - 104 - 40, (col_w, body_w)
        # And the rows agree with the header — nothing is cut to "2026-03-1…".
        cell = page.locator("#body .row").first.locator(".cell").first
        assert abs(cell.bounding_box()["width"] - col_w) < 2
        assert "logged in" in cell.inner_text()
        # The window shrinking takes the column with it, header included.
        page.set_viewport_size({"width": 1100, "height": 700})
        page.wait_for_timeout(300)
        body_w2 = page.evaluate("() => document.getElementById('body').clientWidth")
        col_w2 = _rect(page, '.hcell[data-col="Message"]')["w"]
        assert col_w2 < col_w and col_w2 >= body_w2 - 104 - 40, (col_w2, body_w2)
    finally:
        api(f"/api/sources/{sid}", "DELETE")


def _tag_row_with_hotkey(page, nth, hotkey):
    """Tag the nth painted row by pressing its tag's hotkey, and hand back
    what it takes to put that row back: (tag id, source id, rid)."""
    row = page.locator("#body .row").nth(nth)
    pos = int(row.get_attribute("data-pos"))
    source_id, rid = page.evaluate("(p) => { const r = __winnow.rowAt(p); return [r.source_id, r.rid]; }", pos)
    tag_id = page.evaluate("(k) => __winnow.S.tags.find((t) => String(t.hotkey) === k).id", hotkey)
    # The hotkey toggles: on a row that already wears the tag it would take
    # it off, and the wait below would sit there. Say so instead.
    has = "([p, id]) => (__winnow.rowAt(p)?.tags || []).includes(id)"
    assert not page.evaluate(has, [pos, tag_id]), f"row {pos} already carries tag {tag_id}"
    row.click()
    page.keyboard.press(hotkey)
    page.wait_for_function(has, arg=[pos, tag_id])
    return tag_id, source_id, rid


def test_toolbar_and_header_keep_their_shape_on_a_laptop(page, api):
    # Tag a couple of rows so the chips carry counts, as a real case's do —
    # the counts are what these measurements are about, so they have to be
    # real. The server's case is shared by the whole UI session, though, so
    # the tags come off again before this test hands it on: a row left
    # striped is a row the next module finds already tagged (the watchlist
    # scan's repaint tests read the stripes in the viewport). Untagged by
    # row identity, straight to the server, so the cleanup leans on neither
    # the cached row object nor a hotkey press resolving to "untag" from
    # it — and the ribbon's own counts say the case really is as it was
    # found.
    source_id = page.evaluate("() => __winnow.S.sourceId")
    counts = api(f"/api/tags?source_id={source_id}")["counts"]
    tagged = []
    try:
        tagged.append(_tag_row_with_hotkey(page, 0, "1"))
        tagged.append(_tag_row_with_hotkey(page, 1, "2"))
        page.wait_for_function("() => [...document.querySelectorAll('.tag-chip .n')].some((n) => n.textContent === '1')")
        page.set_viewport_size({"width": 1024, "height": 640})
        page.click("#btnSearchToggle")
        page.wait_for_selector("#search:visible")
        page.wait_for_timeout(200)
        # Every chip is one line; no chip sits under the row count.
        chips = page.evaluate("() => [...document.querySelectorAll('#tagRibbon .tag-chip')].map((c) => { const r = c.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; })")
        assert chips and all(c["h"] < 30 for c in chips), chips
        stats = _rect(page, "#viewStats")
        for c in chips:
            overlap = not (c["x"] + c["w"] <= stats["x"] or stats["x"] + stats["w"] <= c["x"]
                           or c["y"] + c["h"] <= stats["y"] or stats["y"] + stats["h"] <= c["y"])
            assert not overlap, (c, stats)
        # The header's buttons never break their labels, and the table strip
        # keeps enough room to show the open table.
        assert _single_line(page, "#btnSearchAll") and _single_line(page, "#btnCase")
        assert _rect(page, "#sourceTabs")["w"] >= 140
        assert page.locator("#sourceTabs .tab").first.is_visible()
        # And the toolbar's own buttons are still one line each.
        for sel in ("#btnTimeRange", "#btnFilters", "#btnReset"):
            assert _single_line(page, sel), sel
    finally:
        for tag_id, sid, rid in tagged:
            api("/api/row_tags", "POST", {"pairs": [[sid, rid]], "tag_id": tag_id, "on": False})
        assert api(f"/api/tags?source_id={source_id}")["counts"] == counts


def test_tab_strip_fades_the_edge_with_more_tabs_behind_it(page):
    # Squeezing the strip is enough on its own — no render of the strip
    # happens here, so the fade has to follow the strip's box.
    page.evaluate("() => { const s = document.getElementById('sourceTabs'); s.style.flex = '0 0 60px'; s.style.minWidth = '0'; }")
    page.wait_for_function("() => document.getElementById('sourceTabs').dataset.overflow === 'right'")
    page.evaluate("() => { const s = document.getElementById('sourceTabs'); s.scrollLeft = s.scrollWidth; }")
    page.wait_for_function("() => document.getElementById('sourceTabs').dataset.overflow === 'left'")
    page.evaluate("() => { const s = document.getElementById('sourceTabs'); s.style.flex = ''; s.style.minWidth = ''; s.scrollLeft = 0; __winnow.syncTabOverflow(); }")
    assert page.get_attribute("#sourceTabs", "data-overflow") is None


def test_flatten_names_json_attribute_keys_like_their_siblings(page):
    names = page.evaluate("""() => [
        __winnow.suggestColumnName('$.EventData.Data[0].@Name', 'json'),
        __winnow.suggestColumnName('$.EventData.Data[0].#text', 'json'),
        __winnow.suggestColumnName('Event/System/Provider/@Name', 'xml'),
    ]""")
    assert names == ["EventData.Data[0].@Name", "EventData.Data[0].#text", "Provider Name"]


def test_run_shortcut_names_this_platforms_modifier(page):
    # Chromium on the CI box is not a Mac; the label must not claim ⌘.
    is_mac = page.evaluate("() => /Mac|iPhone|iPad/.test(navigator.platform + navigator.userAgent)")
    glyph = "⌘⏎" if is_mac else "Ctrl+⏎"
    page.click("#tabSql")
    page.wait_for_selector("#sqlview:not([hidden])")
    assert page.evaluate("() => document.getElementById('btnRunSql').textContent") == f"Run  {glyph}"
    page.evaluate("() => __winnow.showGridTab()")
    page.click("#btnSearchAll")
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator("#modalBody button", has_text="Search").first.evaluate("(b) => b.textContent") == f"Search  {glyph}"
    page.keyboard.press("Escape")


def test_notes_preview_renders_blockquotes(page):
    html = page.evaluate("() => __winnow.renderMarkdown('> one\\n> **two**\\nafter\\n\\n> again')")
    assert html.count("<blockquote>") == 2 and html.count("</blockquote>") == 2
    assert "<blockquote>\n<p>one</p>\n<p><strong>two</strong></p>\n</blockquote>" in html
    assert "<p>after</p>" in html and "&gt;" not in html
