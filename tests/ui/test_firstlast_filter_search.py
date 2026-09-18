"""First/Last's filter editor: the value list behind *is any of* has a
search box that lands focus after the values arrive, All and None act on
what the search leaves, Enter in the search applies, and the tag list has
the same search."""

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

SEARCH = "#modalBody input[type=search]"
ROWS = "#modalBody label input[type=checkbox]"
STATUS_IS = "(t) => [...document.querySelectorAll('.note-status')].some((n) => n.textContent === t)"


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


def _open_host_filter(pg):
    """Drag Host into Filters: the editor opens on its value list."""
    pg.evaluate(DRAG, ["[data-field='Host']", "[data-zone='filters']"])
    pg.wait_for_selector(SEARCH, timeout=10_000)
    pg.wait_for_function(f"() => document.querySelectorAll('{ROWS}').length === 5", timeout=10_000)


def _rows(pg):
    return pg.evaluate(f"() => [...document.querySelectorAll('{ROWS}')].map((cb) => [cb.parentNode.textContent, cb.checked])")


def _checked(pg):
    return [text for text, on in _rows(pg) if on]


def _drop_filter(pg):
    pg.locator("[data-zone='filters'] [data-field='Host'] button").click()
    pg.wait_for_function("() => !document.querySelector(\"[data-zone='filters'] [data-field='Host']\")")


def test_the_value_list_searches_and_enter_applies_the_ticked_match(fl_page):
    pg = fl_page
    if not pg.locator("input.fl-auto").is_checked():
        pg.locator("input.fl-auto").check()
    pg.evaluate(DRAG, ["[data-field='Host']", "[data-zone='groupBy']"])
    pg.wait_for_function(STATUS_IS, arg="5 groups", timeout=10_000)
    _open_host_filter(pg)
    # The list paints after the fetch, so the modal cannot focus the
    # search itself — it has to land there anyway.
    assert pg.evaluate(f"() => document.activeElement === document.querySelector('{SEARCH}')")
    search = pg.locator(SEARCH)
    search.fill("H1")
    pg.wait_for_function(f"() => document.querySelectorAll('{ROWS}').length === 1")
    assert [text for text, _ in _rows(pg)][0].startswith("H1")
    pg.locator(ROWS).first.check()
    search.press("Enter")
    pg.wait_for_function("() => document.getElementById('modal').hidden")
    pg.wait_for_function(STATUS_IS, arg="1 group", timeout=10_000)
    assert "is any of 1" in pg.locator("[data-zone='filters'] [data-field='Host']").inner_text()
    _drop_filter(pg)
    pg.wait_for_function(STATUS_IS, arg="5 groups", timeout=10_000)


def test_all_and_none_act_on_the_searched_rows(fl_page):
    pg = fl_page
    _open_host_filter(pg)
    search = pg.locator(SEARCH)
    search.fill("H1")
    pg.wait_for_function(f"() => document.querySelectorAll('{ROWS}').length === 1")
    pg.locator("#modalBody button", has_text="All").click()
    search.fill("")
    pg.wait_for_function(f"() => document.querySelectorAll('{ROWS}').length === 5")
    # All ticked only what the search was showing, not every value.
    assert len(_checked(pg)) == 1 and _checked(pg)[0].startswith("H1")
    pg.locator("#modalBody button", has_text="All").click()
    assert len(_checked(pg)) == 5
    search.fill("H2")
    pg.wait_for_function(f"() => document.querySelectorAll('{ROWS}').length === 1")
    pg.locator("#modalBody button", has_text="None").click()
    search.fill("")
    pg.wait_for_function(f"() => document.querySelectorAll('{ROWS}').length === 5")
    # None unticked only the searched value; the other four stay.
    checked = _checked(pg)
    assert len(checked) == 4 and not any(t.startswith("H2") for t in checked)
    pg.keyboard.press("Escape")
    pg.wait_for_function("() => document.getElementById('modal').hidden")
    _drop_filter(pg)


def test_the_tag_list_has_the_same_search(fl_page):
    pg = fl_page
    sel = pg.locator("select[title='Keep only rows with (or without) tags']")
    sel.select_option("ids")
    tag_search = pg.locator("input[placeholder='Find a tag…']")
    tag_search.wait_for(timeout=5_000)
    rows = "input[placeholder='Find a tag…'] + div label"
    # The plugin lists winnow.state.tags, so the expected count is read
    # from there rather than from the seed: a tag some earlier module left
    # on the shared server must not fail a test about the search box.
    expected = pg.evaluate("() => __winnow.S.tags.length")
    assert expected >= 2 and pg.locator(rows).count() == expected
    tag_search.fill("sus")
    pg.wait_for_function(f"() => document.querySelectorAll(\"{rows}\").length === 1")
    assert pg.locator(rows).first.inner_text().strip() == "Suspicious"
    sel.select_option("")
    pg.wait_for_function("() => !document.querySelector(\"input[placeholder='Find a tag…']\")")
