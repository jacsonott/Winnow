"""Grouping on a column puts it in the output.

"Group by Host + User" produced a table of a timestamp and a description,
and which pair a row belonged to was recoverable only from whatever the
description template happened to name. The commonest grouping there is
needed two more drags before it said anything, and forgetting them
produced a result that looked right and could not be read.

The rule is deliberately one-way, and these pin both halves of that: every
way of adding a group column also includes it, and nothing automatically
takes an included column away again.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui


def _post(server, route, body):
    req = urllib.request.Request(
        server.rstrip("/") + route, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


DRAG = """(args) => {
  const [srcSel, dstSel] = args;
  const src = document.querySelector(srcSel);
  const dst = document.querySelector(dstSel);
  const dt = new DataTransfer();
  src.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
  dst.dispatchEvent(new DragEvent('dragover', { bubbles: true, dataTransfer: dt, cancelable: true }));
  dst.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: dt, cancelable: true }));
  src.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt }));
}"""


@pytest.fixture
def fl(browser, server):
    """A fresh context per test: the zones are sheet state, and these
    cases are about what one gesture does to an empty sheet."""
    _post(server, "/api/plugins/toggle", {"fs_name": "first_last", "scope": "on_all"})
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    ctx.add_init_script("localStorage.setItem('winnow.remotePrompt', 'seen');"
                        "localStorage.setItem('winnow.appearance',"
                        " JSON.stringify({ splash: false, pagesMenu: false }))")
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(server, wait_until="networkidle")
    pg.wait_for_selector(".row")
    pg.evaluate("() => __winnow.loadPlugins()")
    pg.wait_for_function("() => __winnow.S.pluginTabs.some((t) => t.id.includes('firstlast'))",
                         timeout=10_000)
    pg.locator(".tab-plugin", has_text="First/Last").click()
    pg.wait_for_selector("[data-zone='groupBy']", timeout=10_000)
    # A NEW sheet, rather than "Start fresh". Sheet definitions live in the
    # case file and the case is shared with every other module in this
    # directory, so wiping them is somebody else's test failing later —
    # test_firstlast_state.py is about exactly that state. A new sheet is
    # the plugin's own way to get an empty working surface and leaves the
    # saved ones alone.
    before = pg.locator(".sql-tab").count()
    pg.locator(".sql-tab", has_text="+").click()
    pg.wait_for_function("(n) => document.querySelectorAll('.sql-tab').length > n",
                         arg=before, timeout=10_000)
    pg.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='groupBy'] [data-field]\").length === 0",
        timeout=10_000)
    yield pg
    ctx.close()
    assert not errors, "uncaught JS errors: " + " | ".join(errors)


def _zone(pg, name):
    return pg.evaluate(
        "(z) => [...document.querySelectorAll(`[data-zone='${z}'] [data-field]`)]"
        ".map((c) => c.dataset.field)", name)


def _chip(field):
    return f"[data-field='{field}']"


def test_dragging_a_column_into_group_rows_on_includes_it(fl):
    fl.evaluate(DRAG, [_chip("Host"), "[data-zone='groupBy']"])
    fl.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='carry'] [data-field]\").length === 1",
        timeout=10_000)
    assert _zone(fl, "groupBy") == ["Host"]
    assert _zone(fl, "carry") == ["Host"]


def test_it_reaches_the_output_table(fl):
    """Not just the chip — the column has to actually come back in the
    result, which is the thing the analyst was missing."""
    fl.evaluate(DRAG, [_chip("Host"), "[data-zone='groupBy']"])
    fl.wait_for_function(
        "() => [...document.querySelectorAll('thead th')].map((h) => h.textContent)"
        ".join('|') === 'Timestamp|Host|Description'", timeout=10_000)


def test_a_second_group_column_is_included_too_in_order(fl):
    fl.evaluate(DRAG, [_chip("Host"), "[data-zone='groupBy']"])
    fl.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='carry'] [data-field]\").length === 1")
    fl.evaluate(DRAG, [_chip("EventId"), "[data-zone='groupBy']"])
    fl.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='carry'] [data-field]\").length === 2",
        timeout=10_000)
    assert _zone(fl, "groupBy") == ["Host", "EventId"]
    assert _zone(fl, "carry") == ["Host", "EventId"]


def test_a_column_already_included_is_not_added_twice(fl):
    fl.evaluate(DRAG, [_chip("Host"), "[data-zone='carry']"])
    fl.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='carry'] [data-field]\").length === 1")
    fl.evaluate(DRAG, [_chip("Host"), "[data-zone='groupBy']"])
    fl.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='groupBy'] [data-field]\").length === 1",
        timeout=10_000)
    assert _zone(fl, "carry") == ["Host"]


def test_ungrouping_leaves_the_column_in_the_output(fl):
    """One way only. Dropping a column the analyst can see is the worse
    half of being wrong, and Include columns is theirs to prune."""
    fl.evaluate(DRAG, [_chip("Host"), "[data-zone='groupBy']"])
    fl.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='carry'] [data-field]\").length === 1")
    fl.evaluate("""() => document.querySelector("[data-zone='groupBy'] [data-field] button").click()""")
    fl.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='groupBy'] [data-field]\").length === 0",
        timeout=10_000)
    assert _zone(fl, "carry") == ["Host"]


def test_dragging_a_chip_out_of_include_columns_is_a_move(fl):
    """The carve-out: a drag that came FROM carry is a move gesture, and
    bouncing the chip back where it started would read as the drag having
    failed."""
    fl.evaluate(DRAG, [_chip("Host"), "[data-zone='carry']"])
    fl.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='carry'] [data-field]\").length === 1")
    fl.evaluate("""() => {
      const src = document.querySelector("[data-zone='carry'] [data-field='Host']");
      const dst = document.querySelector("[data-zone='groupBy']");
      const dt = new DataTransfer();
      src.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
      dst.dispatchEvent(new DragEvent('dragover', { bubbles: true, dataTransfer: dt, cancelable: true }));
      dst.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: dt, cancelable: true }));
      src.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt }));
    }""")
    fl.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='groupBy'] [data-field]\").length === 1",
        timeout=10_000)
    assert _zone(fl, "groupBy") == ["Host"]
    assert _zone(fl, "carry") == []


def test_the_click_to_place_path_includes_it_too(fl):
    """Clicking a field offers the same placements — the keyboard and
    trackpad route through the same addField, and must not be a second
    behaviour."""
    fl.evaluate("""() => document.querySelector("[data-field='Host']").click()""")
    fl.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Place Host'",
                         timeout=10_000)
    fl.evaluate("""() => [...document.querySelectorAll('#modalBody button')]
      .find((b) => b.textContent === 'Group rows on').click()""")
    fl.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='carry'] [data-field]\").length === 1",
        timeout=10_000)
    assert _zone(fl, "groupBy") == ["Host"]
    assert _zone(fl, "carry") == ["Host"]
