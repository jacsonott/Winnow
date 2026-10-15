"""The First/Last page, round three, as an analyst meets it: the table
picker said the imported file's name where the rest of the app said the
nickname, a new sheet opened with the last description's default already
typed in, the description sat after the whole-row JSON cell, and there was
no way to list a group's distinct values."""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui

TMPL = "input[style*='var(--mono)']"


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



def _headers_are(pg, want):
    pg.wait_for_function(
        "(want) => [...document.querySelectorAll('thead th')].map((h) => h.textContent).join('|') === want",
        arg=want, timeout=10_000)


def _open(browser, server):
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
    return ctx, pg, errors


def _new_sheet(pg):
    """A new sheet rather than Start fresh: saved sheets live in the case
    file every module here shares (see test_firstlast_group_includes.py)."""
    before = pg.locator(".sql-tab").count()
    pg.locator(".sql-tab", has_text="+").click()
    pg.wait_for_function("(n) => document.querySelectorAll('.sql-tab').length > n",
                         arg=before, timeout=10_000)
    pg.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='groupBy'] [data-field]\").length === 0",
        timeout=10_000)


@pytest.fixture
def fl(browser, server):
    ctx, pg, errors = _open(browser, server)
    _new_sheet(pg)
    yield pg
    ctx.close()
    assert not errors, "uncaught JS errors: " + " | ".join(errors)


def test_the_table_picker_shows_a_nickname(browser, server):
    sid = next(s["id"] for s in json.loads(urllib.request.urlopen(server + "/api/sources").read())
               if s["name"] == "ui.csv")
    _post(server, f"/api/source/{sid}/nickname", {"nickname": "Logons"})
    try:
        ctx, pg, errors = _open(browser, server)
        opt = pg.locator(f"select option[value='{sid}']").first
        pg.wait_for_function(
            f"() => (document.querySelector(\"select option[value='{sid}']\") || {{}}).textContent === 'Logons (200)'",
            timeout=10_000)
        # The file it came from is still a hover away.
        assert "ui.csv" in opt.get_attribute("title")
        ctx.close()
        assert not errors, "uncaught JS errors: " + " | ".join(errors)
    finally:
        # The case is shared with every other UI module; leave it as found.
        _post(server, f"/api/source/{sid}/nickname", {"nickname": ""})


def test_a_new_sheet_starts_with_an_empty_description(fl):
    tmpl = fl.locator(TMPL).first
    tmpl.fill("{which} at {Host}")
    _new_sheet(fl)
    assert tmpl.input_value() == ""
    # Empty is not blank output: the box says what it will read as.
    assert tmpl.get_attribute("placeholder") == "{which} of {count}"
    fl.evaluate(DRAG, ["[data-field='Host']", "[data-zone='groupBy']"])
    fl.wait_for_function(
        "() => { const td = document.querySelector('table tbody tr td:last-child');"
        " return !!td && /^(First|Last|Only) of \\d+$/.test(td.textContent.trim()); }", timeout=10_000)
    # ...and the sheet it came from kept its own.
    fl.locator(".sql-tab").nth(fl.locator(".sql-tab").count() - 3).click()
    fl.wait_for_function(f"() => document.querySelector(\"{TMPL}\").value === '{{which}} at {{Host}}'",
                         timeout=10_000)


def test_the_description_reads_before_the_json_cell(fl):
    fl.evaluate(DRAG, ["[data-field='Host']", "[data-zone='groupBy']"])
    _headers_are(fl, "Timestamp|Host|Description")
    fl.get_by_text("Add the whole row as a JSON cell").click()
    _headers_are(fl, "Timestamp|Host|Description|Row (JSON)")


def test_list_values_adds_a_column_of_each_groups_values(fl):
    fl.evaluate(DRAG, ["[data-field='Host']", "[data-zone='groupBy']"])
    fl.evaluate(DRAG, ["[data-field='EventId']", "[data-zone='values']"])
    _headers_are(fl, "Timestamp|Host|Values of EventId|Description")
    # Every event id each host saw, not the two bookends' — by value,
    # since EventId is a number column.
    cells = fl.evaluate("() => [...document.querySelectorAll('table tbody tr')]"
                        ".map((tr) => tr.children[2].textContent)")
    assert cells and set(cells) == {"1, 4624, 4625, 4688"}
    # It offers its placeholder, and the placeholder renders the same list.
    fl.locator("button", has_text="{values:EventId}").click()
    fl.wait_for_function(
        "() => [...document.querySelectorAll('table tbody tr td:last-child')]"
        ".every((td) => td.textContent.endsWith('1, 4624, 4625, 4688'))", timeout=10_000)


def test_which_number_puts_the_position_in_the_description(fl):
    fl.evaluate(DRAG, ["[data-field='Host']", "[data-zone='groupBy']"])
    fl.wait_for_selector("table tbody tr", timeout=10_000)
    fl.locator(TMPL).first.fill("{which:number} of {count}")
    fl.wait_for_function(
        "() => { const d = [...document.querySelectorAll('table tbody tr td:last-child')]"
        ".map((td) => td.textContent); return d.includes('1 of 40') && d.includes('40 of 40'); }",
        timeout=10_000)
