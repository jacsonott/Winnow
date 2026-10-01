"""The Timeline's tag filter is the grid's tag chips.

It asks the same question the tag ribbon asks — which tags am I looking
at — from the same strip across the top, and it was the one tag control
in the app answering it with a form: a checkbox and a bare swatch, with
none of the pressed state, hover or per-skin treatment the chips have.
These pin the shape (.tag-chip buttons carrying aria-pressed and the
tag's colour, as renderTagRibbon builds them) and that the filtering
still works through it.

Also here: what the empty line says. With a one-click way to switch a tag
off, "tag some rows in any table, then come back" is reachable over a
case full of findings, which sends the analyst looking for work they have
already done.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _chips(page):
    return page.evaluate("""() => [...document.querySelectorAll('#timelineTagFilter > *')].map((c) => ({
      tag: c.tagName,
      cls: c.className,
      pressed: c.getAttribute('aria-pressed'),
      name: (c.querySelector('span:not(.swatch)') || {}).textContent || '',
      swatch: !!c.querySelector('.swatch'),
      color: c.style.color,
    }))""")


def _click_chip(page, name):
    page.evaluate("""(name) => {
      const c = [...document.querySelectorAll('#timelineTagFilter .tag-chip')]
        .find((x) => x.textContent.trim() === name);
      c.click();
    }""", arg=name)


def _empty_text(page):
    return page.evaluate("() => document.getElementById('timelineEmpty').textContent.trim()")


@pytest.fixture
def one_tagged_row(page):
    """Tags a row with the first tag, leaves the shared case as found."""
    page.locator(".row").nth(1).locator(".cell").nth(1).click()
    page.keyboard.press("1")
    page.wait_for_timeout(250)
    page.locator("#tabTimeline").click()
    page.wait_for_selector("#timelineview:not([hidden])")
    page.wait_for_selector(".timeline-row", timeout=10_000)
    yield
    src = page.evaluate("() => __winnow.S.sources.find((s) => !s.is_merge).id")
    page.evaluate("(id) => __winnow.openSource(id)", src)
    page.wait_for_selector(".row")
    page.locator(".row").nth(1).locator(".cell").nth(1).click()
    page.keyboard.press("1")
    page.wait_for_timeout(250)


def test_the_filter_is_chips_not_checkboxes(page, one_tagged_row):
    chips = _chips(page)
    assert chips, "no tag filter rendered"
    assert all(c["tag"] == "BUTTON" for c in chips), [c["tag"] for c in chips]
    assert all("tag-chip" in c["cls"] for c in chips), [c["cls"] for c in chips]
    assert page.locator("#timelineTagFilter input").count() == 0
    # The ribbon's parts: a colour swatch and the tag's name.
    assert all(c["swatch"] for c in chips)
    assert [c["name"] for c in chips] == page.evaluate("() => __winnow.S.tags.map((t) => t.name)")


def test_every_tag_starts_on_and_carries_its_colour(page, one_tagged_row):
    """A timeline is every tagged row until it is narrowed, so the chips
    start pressed — and a pressed chip takes the tag's own colour, the way
    the ribbon's does."""
    chips = _chips(page)
    assert all(c["pressed"] == "true" for c in chips), [c["pressed"] for c in chips]
    assert all(c["color"] for c in chips), [c["color"] for c in chips]


def test_switching_a_tag_off_narrows_the_timeline(page, one_tagged_row):
    before = page.locator(".timeline-row").count()
    assert before >= 1
    _click_chip(page, "TA")
    page.wait_for_function("() => document.querySelectorAll('.timeline-row').length === 0",
                           timeout=10_000)
    off = [c for c in _chips(page) if c["name"] == "TA"][0]
    assert off["pressed"] == "false"
    assert not off["color"], "an unpressed chip should not keep the tag's colour"
    # Some tags on, none of their rows here — not "nothing is tagged".
    assert _empty_text(page) == "No rows carry the tags switched on above."
    _click_chip(page, "TA")
    page.wait_for_function("() => document.querySelectorAll('.timeline-row').length >= 1",
                           timeout=10_000)
    assert page.locator(".timeline-row").count() == before


def test_every_tag_off_says_so(page, one_tagged_row):
    for name in page.evaluate("() => __winnow.S.tags.map((t) => t.name)"):
        _click_chip(page, name)
    page.wait_for_function("() => document.querySelectorAll('.timeline-row').length === 0",
                           timeout=10_000)
    assert _empty_text(page) == "Every tag is switched off — turn one back on above to see its rows."
    assert page.evaluate("() => __winnow.S.timeline.tagFilter.length") == 0
