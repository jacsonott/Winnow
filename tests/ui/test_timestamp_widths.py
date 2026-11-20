"""A datetime column is as wide as the text it SHOWS, not the text it
stores. Every timestamp format writes its values at one fixed length,
which can be longer than the file's own: "6/3/2026 1:02:03" is 16
characters and its YYYY-MM-DD HH:MM:SS form 19. Autofit measured the
stored text, so the reformatted value was cut off with an ellipsis right
where its seconds start, while "As stored" showed them in full. These
read the painted cell, where the clipping is."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

STAMPS = [f"6/3/2026 1:02:0{i}" for i in range(5)]

# The first row's When cell: what it shows, and whether that is cut off
# (the grid's own test for titling a cell, grid.js: scrollWidth past
# clientWidth + 1).
WHEN = """() => {
  const names = __winnow.visibleCols();
  const cell = document.querySelector(`#body .row .cell[data-col="${names.indexOf('When')}"]`);
  return { text: cell.textContent, clipped: cell.scrollWidth > cell.clientWidth + 1,
           w: (__winnow.S.layout.When || {}).w || null };
}"""


def _open(page, tmp_path, imported, lines, name):
    f = tmp_path / name
    f.write_text("When,Note\n" + "".join(f"{v},x\n" for v in lines), encoding="utf-8")
    sid = imported({"path": str(f), "name": name, "kind": "csv"})[0]
    page.evaluate("() => __winnow.loadSources()")
    page.evaluate("(id) => __winnow.openSource(id)", sid)
    page.wait_for_selector('.hcell[data-col="When"]')
    page.wait_for_selector("#body .row .cell")
    return sid


def _close(page, sid):
    page.evaluate("""async (id) => {
      const h = { 'X-Timeline-Lite-Client': '1' };
      await fetch('/api/source/' + id, { method: 'DELETE', headers: h });
      __winnow.S.viewCache.delete(id);
      if (__winnow.S.sourceId === id) __winnow.S.sourceId = null;
      await __winnow.loadSources();
      const first = __winnow.S.sources.find((s) => !s.is_merge);
      if (first) __winnow.openSource(first.id);
    }""", sid)
    page.wait_for_selector(".row")


def _autofit(page):
    page.click("#body")
    page.keyboard.press("=")
    page.wait_for_selector("#toast:not([hidden]):has-text('Columns autofit to content')")


def _pick(page, label):
    """Choose a display format from When's header menu, and wait for the
    width it measures for it (a column with a width of its own asks the
    server what the new format shows) to have been answered and painted."""
    page.locator('.hcell[data-col="When"]').click(button="right")
    page.wait_for_selector(".menu")
    item = page.locator(".menu .menu-item").get_by_text(label, exact=True)
    if page.evaluate("() => !!(__winnow.S.layout.When || {}).w"):
        with page.expect_response(lambda r: "/api/column_maxlen" in r.url):
            item.click()
        page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
    else:
        item.click()
    return page.evaluate(WHEN)


def test_autofit_fits_the_format_a_timestamp_is_shown_in(page, tmp_path, imported):
    sid = _open(page, tmp_path, imported, STAMPS, "us_short.csv")
    try:
        _autofit(page)
        iso = page.evaluate(WHEN)
        assert iso["text"] == "2026-06-03 01:02:00"
        assert not iso["clipped"], f"autofit cut {iso['text']!r} off at {iso['w']}px"

        stored = _pick(page, "As stored")
        assert stored["text"] == "6/3/2026 1:02:00" and not stored["clipped"]
        _autofit(page)
        stored = page.evaluate(WHEN)
        assert not stored["clipped"] and stored["w"] < iso["w"], (
            "fitted to what it shows, the stored text is the shorter of the two")

        # A width fitted to "As stored" is too narrow for the ISO form:
        # picking it widens the column instead of cutting off the seconds.
        back = _pick(page, "YYYY-MM-DD HH:MM:SS")
        assert back["text"] == "2026-06-03 01:02:00"
        assert not back["clipped"], f"{back['text']!r} cut off at {back['w']}px"
        us = _pick(page, "YYYY-MM-DD HH:MM:SS.ffffff")
        assert us["text"] == "2026-06-03 01:02:00.000000" and not us["clipped"]
        # Widened, never narrowed: a shorter format leaves the width alone.
        assert _pick(page, "HH:MM:SS")["w"] == us["w"]
    finally:
        _close(page, sid)


def test_a_value_that_is_not_a_timestamp_still_fits(page, tmp_path, imported):
    """A reformatted column still shows a value no format reads exactly as
    stored, so autofit has to make room for the longest of those too."""
    note = "clock was not set at boot"
    sid = _open(page, tmp_path, imported, [note] + STAMPS, "with_note.csv")
    try:
        _autofit(page)
        cell = page.evaluate(WHEN)
        assert cell["text"] == note and not cell["clipped"], cell
    finally:
        _close(page, sid)


def test_the_default_width_holds_the_format(page, tmp_path, imported):
    """A column nobody has sized takes its default width from the format
    it is shown in: 190px held YYYY-MM-DD HH:MM:SS but not the 26
    characters of the microsecond form."""
    sid = _open(page, tmp_path, imported, STAMPS, "default_width.csv")
    try:
        assert page.evaluate(WHEN)["w"] is None
        us = _pick(page, "YYYY-MM-DD HH:MM:SS.ffffff")
        assert us["text"] == "2026-06-03 01:02:00.000000"
        assert not us["clipped"] and us["w"] is None, us
        head = page.evaluate("""() => {
          const names = __winnow.visibleCols();
          const cell = document.querySelector(`#body .row .cell[data-col="${names.indexOf('When')}"]`);
          return [document.querySelector('.hcell[data-col="When"]').getBoundingClientRect().width,
                  cell.getBoundingClientRect().width];
        }""")
        assert head[0] == head[1], f"header {head[0]}px over a {head[1]}px column"
    finally:
        _close(page, sid)
