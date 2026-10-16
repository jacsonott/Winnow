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

And a tag created after the case was opened. The filter used to snapshot
the tags that were on when the case opened, so a later tag started off:
its rows were missing from the timeline until its chip was clicked or the
case reopened. Every tag starts on; only one the analyst switched off
stays off.
"""

from __future__ import annotations

import urllib.request

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
def one_tagged_row(page, tag_written):
    """Tags a row with the first tag, leaves the shared case as found."""
    page.locator(".row").nth(1).locator(".cell").nth(1).click()
    with tag_written(page):
        page.keyboard.press("1")
    page.locator("#tabTimeline").click()
    page.wait_for_selector("#timelineview:not([hidden])")
    page.wait_for_selector(".timeline-row", timeout=10_000)
    yield
    src = page.evaluate("() => __winnow.S.sources.find((s) => !s.is_merge).id")
    page.evaluate("(id) => __winnow.openSource(id)", src)
    page.wait_for_selector(".row")
    page.locator(".row").nth(1).locator(".cell").nth(1).click()
    with tag_written(page):
        page.keyboard.press("1")


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
    assert page.evaluate("() => __winnow.timelineTagIds().length") == 0


# ----------------------------------------------- tags created mid-session

def _delete(server, route):
    urllib.request.urlopen(urllib.request.Request(
        server.rstrip("/") + route, method="DELETE",
        headers={"X-Timeline-Lite-Client": "1"}), timeout=15).read()


def _add_tag_in_the_editor(page):
    """"Add tag" in Edit tags…, the way an analyst makes one mid-case.
    Returns the new tag's id."""
    before = page.evaluate("() => __winnow.S.tags.map((t) => t.id)")
    page.evaluate("() => __winnow.openTagEditor()")
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Tags'")
    page.locator("#modalBody button", has_text="Add tag").click()
    page.wait_for_function("(n) => __winnow.S.tags.length === n + 1", arg=len(before))
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")
    return page.evaluate("(before) => __winnow.S.tags.find((t) => !before.includes(t.id)).id", before)


def _chip_pressed(page, tag_id):
    return page.evaluate("""(id) => {
      const name = __winnow.S.tags.find((t) => t.id === id).name;
      const chips = [...document.querySelectorAll('#timelineTagFilter .tag-chip')]
        .filter((c) => c.textContent.trim() === name);
      return chips.map((c) => c.getAttribute('aria-pressed'));
    }""", tag_id)


def _wait_for_timeline_row_tagged(page, tag_id):
    page.wait_for_function("""(id) => {
      const v = __winnow.S.timeline.view;
      if (!v || !v.row_count) return false;
      for (let p = 0; p < v.row_count; p++) {
        const r = __winnow.timelineRowAt(p);
        if (r && r.tags.includes(id)) return true;
      }
      return false;
    }""", arg=tag_id, timeout=10_000)


def _timeline_tags(page):
    """Every tag id on every row the timeline is showing, once its rows
    have landed."""
    page.wait_for_function("""() => {
      const v = __winnow.S.timeline.view;
      return v && [...Array(v.row_count).keys()].every((p) => __winnow.timelineRowAt(p));
    }""", timeout=10_000)
    return page.evaluate("""() => [...Array(__winnow.S.timeline.view.row_count).keys()]
      .flatMap((p) => __winnow.timelineRowAt(p).tags)""")


def _open_timeline(page):
    page.locator("#tabTimeline").click()
    page.wait_for_selector("#timelineview:not([hidden])")


def test_a_tag_created_after_the_case_opened_starts_on(page, server, server_post):
    sid = page.evaluate("() => __winnow.S.sourceId")
    new_id = _add_tag_in_the_editor(page)
    try:
        server_post("/api/row_tags", {"source_id": sid, "rids": [3], "tag_id": new_id, "on": True})
        _open_timeline(page)
        # The symptom: the chip came up unpressed and the row it tags was
        # not on the timeline at all.
        _wait_for_timeline_row_tagged(page, new_id)
        assert _chip_pressed(page, new_id) == ["true"]
    finally:
        _delete(server, f"/api/tags/{new_id}")


def test_a_tag_switched_off_stays_off_when_another_is_created(page, server, server_post):
    sid = page.evaluate("() => __winnow.S.sourceId")
    ta = page.evaluate("() => __winnow.S.tags.find((t) => t.name === 'TA').id")
    server_post("/api/row_tags", {"source_id": sid, "rids": [4], "tag_id": ta, "on": True})
    new_id = None
    try:
        _open_timeline(page)
        _wait_for_timeline_row_tagged(page, ta)
        _click_chip(page, "TA")
        page.wait_for_function("() => document.querySelectorAll('.timeline-row').length === 0",
                               timeout=10_000)

        new_id = _add_tag_in_the_editor(page)
        server_post("/api/row_tags", {"source_id": sid, "rids": [5], "tag_id": new_id, "on": True})
        assert _chip_pressed(page, ta) == ["false"]
        assert _chip_pressed(page, new_id) == ["true"]

        # Not awaited from here: page.evaluate on a promise has no timeout
        # of its own. The wait below is what bounds it.
        page.evaluate("() => { __winnow.buildTimeline(); }")
        _wait_for_timeline_row_tagged(page, new_id)
        assert ta not in _timeline_tags(page)
    finally:
        server_post("/api/row_tags", {"source_id": sid, "rids": [4], "tag_id": ta, "on": False})
        if new_id is not None:
            _delete(server, f"/api/tags/{new_id}")
