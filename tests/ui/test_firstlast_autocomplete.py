"""First/Last's description completes placeholder names: an unclosed `{`
before the caret lists {which}, {count}, a Total-up column's sum:/min:/max:
forms and every column of the table, narrowed by what follows the brace.
Enter or a click inserts the whole `{name}` and leaves the caret in the
box; Escape closes the list without the app's Escape blurring the field;
and an inserted column renders whether or not it is grouped or included."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


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

TMPL = "input[style*='var(--mono)']"
ITEMS = ".fl-ac .menu-item"
DESC = "document.querySelector('table tbody tr td:last-child').textContent.trim()"


@pytest.fixture(scope="module")
def fl_page(browser, server, server_post):
    server_post("/api/plugins/toggle", {"fs_name": "first_last", "scope": "on_all"})
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    ctx.add_init_script("localStorage.setItem('winnow.remotePrompt', 'seen');"
                        "localStorage.removeItem('winnow.firstlast.auto');"
                        "localStorage.setItem('winnow.appearance',"
                        " JSON.stringify({ splash: false, pagesMenu: false }))")
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(server, wait_until="networkidle")
    pg.wait_for_selector(".row")
    pg.evaluate("() => __winnow.loadPlugins()")
    pg.wait_for_function("() => __winnow.S.pluginTabs.some((t) => t.id.includes('firstlast'))", timeout=10_000)
    pg.locator(".tab-plugin", has_text="First/Last").click()
    pg.wait_for_selector("[data-zone='groupBy']", timeout=10_000)
    yield pg
    ctx.close()
    server_post("/api/plugins/toggle", {"fs_name": "first_last", "scope": "off_all"})
    assert not errors, "uncaught JS errors: " + " | ".join(errors)


def _items(pg):
    return pg.evaluate("() => [...document.querySelectorAll('.fl-ac .menu-item')].map((n) => n.firstChild.textContent)")


def _value(pg):
    return pg.evaluate(f"() => document.querySelector(\"{TMPL}\").value")


def _focused(pg):
    return pg.evaluate(f"() => document.activeElement === document.querySelector(\"{TMPL}\")")


def test_a_brace_offers_names_and_enter_inserts_a_placeholder_that_renders(fl_page):
    pg = fl_page
    if not pg.locator("input.fl-auto").is_checked():
        pg.locator("input.fl-auto").check()
    pg.evaluate(DRAG, ["[data-field='Host']", "[data-zone='groupBy']"])
    pg.wait_for_selector("table tbody tr", timeout=10_000)
    tmpl = pg.locator(TMPL).first
    tmpl.fill("{which} of {count} — ")
    pg.keyboard.type("{Eve")
    pg.wait_for_selector(ITEMS, timeout=5_000)
    # What follows the brace narrows the list to the one name containing it.
    assert _items(pg) == ["EventId"]
    pg.keyboard.press("Enter")
    pg.wait_for_function("() => !document.querySelector('.fl-ac')")
    assert _value(pg) == "{which} of {count} — {EventId}"
    # The caret stayed in the box, right after what was inserted — Enter
    # was consumed by the list, not by the page.
    assert _focused(pg)
    assert pg.evaluate(f"() => document.querySelector(\"{TMPL}\").selectionStart") == len("{which} of {count} — {EventId}")
    # EventId is neither grouped nor included, and the description still
    # renders its value.
    pg.wait_for_function(f"() => /^(First|Last|Only) of \\d+ — \\d+$/.test({DESC})", timeout=10_000)
    tmpl.fill("{which} of {count}")
    pg.wait_for_function(f"() => /^(First|Last|Only) of \\d+$/.test({DESC})", timeout=10_000)


def test_a_total_up_column_completes_its_aggregate_forms_and_a_click_inserts(fl_page):
    pg = fl_page
    pg.evaluate(DRAG, ["[data-field='EventId']", "[data-zone='sums']"])
    pg.wait_for_selector("button:has-text('{sum:EventId}')", timeout=5_000)
    tmpl = pg.locator(TMPL).first
    tmpl.fill("{which} of {count} — ")
    pg.keyboard.type("{sum:")
    pg.wait_for_selector(ITEMS, timeout=5_000)
    assert _items(pg) == ["sum:EventId"]
    # A click on the row must insert, not blur the input and lose the list.
    pg.locator(ITEMS).first.click()
    pg.wait_for_function("() => !document.querySelector('.fl-ac')")
    assert _value(pg) == "{which} of {count} — {sum:EventId}"
    assert _focused(pg)
    pg.wait_for_function(f"() => /^(First|Last|Only) of \\d+ — \\d{{4,}}$/.test({DESC})", timeout=10_000)
    # Leave the sheet as found: the template first (a total the chip no
    # longer covers would fail the preview), then the Total-up chip.
    tmpl.fill("{which} of {count}")
    pg.wait_for_function(f"() => /^(First|Last|Only) of \\d+$/.test({DESC})", timeout=10_000)
    pg.locator("[data-zone='sums'] [data-field='EventId'] button").click()
    pg.wait_for_function("() => !document.querySelector(\"[data-zone='sums'] [data-field='EventId']\")")


def test_escape_closes_the_list_and_keeps_the_caret_in_the_box(fl_page):
    pg = fl_page
    tmpl = pg.locator(TMPL).first
    tmpl.fill("{which} of {count} ")
    pg.keyboard.type("{")
    pg.wait_for_selector(ITEMS, timeout=5_000)
    items = _items(pg)
    assert items[:2] == ["which", "count"]
    assert "Host" in items and "CommandLine" in items
    pg.keyboard.press("Escape")
    pg.wait_for_function("() => !document.querySelector('.fl-ac')")
    # The app's Escape blurs whatever input has focus; the list consumed
    # this one, so the analyst is still typing where they were.
    assert _focused(pg)
    assert _value(pg) == "{which} of {count} {"
    # Ctrl+Space opens the list with no brace at all.
    tmpl.fill("{which} of {count}")
    pg.keyboard.press("Control+Space")
    pg.wait_for_selector(ITEMS, timeout=5_000)
    assert _items(pg)[:2] == ["which", "count"]
    pg.keyboard.press("Escape")
    pg.wait_for_function("() => !document.querySelector('.fl-ac')")
    assert _value(pg) == "{which} of {count}"
