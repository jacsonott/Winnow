"""The sidebar's tree: that nesting is visible, and the bulk opens.

A folder two levels down was 12px of whitespace and a 10px grey chevron —
nothing that says "this is inside that", which on a directory import
reproducing an evidence tree is most of what the panel is for. Rows carry
one indent guide per level now, and the disclosure is a control you can
see and aim at.

The guides are painted as a background-image on a flat list of rows (see
the CSS), which has one trap worth a test of its own: anything
highlighting a row has to set background-COLOR, because the shorthand
takes the guides with it.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

INDENT = 14    # --tree-indent


def _folder(api, name, parent=None):
    return api("/api/folders", "POST", {"name": name, "parent_id": parent})


def _row_for(page, name):
    return page.locator(".sidebar-folder", has_text=name).first


def test_a_nested_row_carries_one_indent_guide_per_level(page, api):
    parent = _folder(api, "Tree Parent")
    child = _folder(api, "Tree Child", parent["id"])
    try:
        page.evaluate("() => __winnow.loadSources(undefined, { navigate: false })")
        page.wait_for_function(
            "() => !!document.querySelector('.sidebar-folder')"
            " && [...document.querySelectorAll('.folder-name')].some((n) => n.textContent === 'Tree Child')")
        got = page.evaluate("""() => {
          const of = (name) => [...document.querySelectorAll('.sidebar-folder')]
            .find((r) => r.querySelector('.folder-name').textContent === name);
          const read = (r) => {
            const cs = getComputedStyle(r);
            return { depth: cs.getPropertyValue('--depth').trim(),
                     size: cs.backgroundSize, image: cs.backgroundImage,
                     pad: cs.paddingLeft };
          };
          return { parent: read(of('Tree Parent')), child: read(of('Tree Child')) };
        }""")
        # Depth 0: indented by nothing, so there is no guide to paint.
        assert got["parent"]["depth"] == "0"
        assert got["parent"]["pad"] == "0px"
        assert got["parent"]["size"].startswith("0px")
        # Depth 1: one level of indent, and exactly that much guide.
        assert got["child"]["depth"] == "1"
        assert got["child"]["pad"] == f"{INDENT}px"
        assert got["child"]["size"].startswith(f"{INDENT}px")
        assert "gradient" in got["child"]["image"]
    finally:
        api(f"/api/folders/{child['id']}", "DELETE")
        api(f"/api/folders/{parent['id']}", "DELETE")
        page.evaluate("() => __winnow.loadSources(undefined, { navigate: false })")


def test_the_guides_survive_the_row_being_highlighted(page, api):
    """`background: var(--ink)` on .drop-into would drop the guides
    exactly while a table is being dragged onto the folder — the moment
    the analyst most needs to see which folder they are over."""
    parent = _folder(api, "Tree Parent")
    child = _folder(api, "Tree Child", parent["id"])
    try:
        page.evaluate("() => __winnow.loadSources(undefined, { navigate: false })")
        page.wait_for_function(
            "() => [...document.querySelectorAll('.folder-name')].some((n) => n.textContent === 'Tree Child')")
        image = page.evaluate("""() => {
          const row = [...document.querySelectorAll('.sidebar-folder')]
            .find((r) => r.querySelector('.folder-name').textContent === 'Tree Child');
          row.classList.add('drop-into');
          const cs = getComputedStyle(row);
          const out = { image: cs.backgroundImage, colour: cs.backgroundColor };
          row.classList.remove('drop-into');
          return out;
        }""")
        assert "gradient" in image["image"], "the highlight took the indent guides with it"
        assert image["colour"] not in ("rgba(0, 0, 0, 0)", "transparent"), \
            "the highlight has to still highlight"
    finally:
        api(f"/api/folders/{child['id']}", "DELETE")
        api(f"/api/folders/{parent['id']}", "DELETE")
        page.evaluate("() => __winnow.loadSources(undefined, { navigate: false })")


def test_the_disclosure_is_something_you_can_see_and_aim_at(page, api):
    parent = _folder(api, "Tree Parent")
    try:
        page.evaluate("() => __winnow.loadSources(undefined, { navigate: false })")
        page.wait_for_function(
            "() => [...document.querySelectorAll('.folder-name')].some((n) => n.textContent === 'Tree Parent')")
        got = page.evaluate("""() => {
          const row = [...document.querySelectorAll('.sidebar-folder')]
            .find((r) => r.querySelector('.folder-name').textContent === 'Tree Parent');
          const tw = row.querySelector('.folder-twisty');
          const cs = getComputedStyle(tw);
          const text = getComputedStyle(document.documentElement).getPropertyValue('--text').trim();
          const probe = document.createElement('span');
          probe.style.color = text;
          document.body.append(probe);
          const resolved = getComputedStyle(probe).color;
          probe.remove();
          return { size: parseFloat(cs.fontSize), colour: cs.color, text: resolved,
                   w: tw.getBoundingClientRect().width };
        }""")
        assert got["size"] >= 12, "the chevron is the one thing you must hit to walk the tree"
        assert got["w"] >= 16
        assert got["colour"] == got["text"], "the chevron is a control, not a hint"
    finally:
        api(f"/api/folders/{parent['id']}", "DELETE")
        page.evaluate("() => __winnow.loadSources(undefined, { navigate: false })")


def test_open_all_opens_the_tables_that_were_closed(page, api):
    """The sidebar could close every tab and not open one. Both buttons
    live on the All tables header because that is what they act on —
    everything in the tree below, open or not — while close all stays with
    the tabs it closes."""
    open_now = page.evaluate("() => __winnow.S.sources.filter((s) => s.is_open).map((s) => s.id)")
    try:
        page.evaluate("() => __winnow.closeAllTabs()")
        page.wait_for_function("() => __winnow.S.sources.every((s) => !s.is_open)")
        header = page.locator("#sidebarList .sidebar-all-header")
        btn = header.locator("button", has_text="open all")
        assert btn.count() == 1, "every table is closed and the sidebar offers no way to open them"
        btn.click()
        page.wait_for_function(
            "() => __winnow.S.sources.every((s) => s.is_open || s.error)", timeout=15_000)
        # …and it takes itself away once there is nothing left to open.
        # Waited for, not read once: the tables open one request at a time
        # and the sidebar repaints on its own schedule, so the state above
        # goes true a repaint BEFORE the button leaves the DOM. Reading the
        # count at that instant caught the old sidebar about a third of the
        # time — on CI, where it reads as this PR's fault rather than a
        # race that was always here.
        header.locator("button", has_text="open all").wait_for(state="detached", timeout=15_000)
        assert header.locator("button", has_text="open all").count() == 0
    finally:
        page.evaluate("(ids) => __winnow.openTables(__winnow.S.sources.filter((s) => ids.includes(s.id)))",
                      open_now)
        page.wait_for_function("() => __winnow.busyCount === 0")


def test_which_tables_count_as_tagged_is_decided_in_one_place(page):
    """The sidebar's "with tags" button and the Tables manager's "Open all
    tagged" used to be two copies of the same filter; they share one now.
    Merges stay out of it: a merge's tagged_row_count is its members'
    summed, so opening it as well shows the same rows twice."""
    got = page.evaluate("""() => {
      const all = __winnow.S.sources;
      const picked = __winnow.taggedTablesToOpen();
      return { picked: picked.map((s) => s.id),
               wanted: all.filter((s) => !s.is_open && !s.error && !s.is_merge && s.tagged_row_count > 0)
                          .map((s) => s.id) };
    }""")
    assert got["picked"] == got["wanted"]
