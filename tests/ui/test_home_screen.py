"""The home screen in the Tables manager's frame: the case list's sections
down a side list (Recent, All cases, each group, No group), the chosen one
on the right as a list with the Tables manager's columns, one ⋯ per case
instead of four buttons, and Quick look at files… along the foot.

Most of these answer /api/cases themselves, so the list is the one each
test describes and the shared server's registry is never touched."""

from __future__ import annotations

import json
import time

import pytest

from spawn import running_server

pytestmark = pytest.mark.ui

DAY = 86400


def _ago(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - seconds))


def _case(cid, name, *, group="", notes="", opened=None, tables=1, rows=100, tagged=0, exists=True):
    return {"id": cid, "path": f"/srv/cases/{name.lower().replace(' ', '-')}.db-winnow", "name": name,
            "group": group, "notes": notes, "created_at": _ago(90 * DAY),
            "last_opened": None if opened is None else _ago(opened), "exists": exists,
            **({"source_count": tables, "row_count": rows, "tagged_row_count": tagged} if exists else {})}


def _cases():
    # Built when the test runs, not at import: collection can come half an
    # hour before a test in a full run, and "10 min ago" would be stale.
    return [
        _case(1, "Rivera intrusion", group="ACME-031", notes="Lateral movement via PsExec",
              opened=600, tables=6, rows=108_400, tagged=84),
        _case(2, "Rivera file server", group="ACME-031", opened=DAY + 600, tables=2, rows=62_000, tagged=12),
        _case(3, "Halcyon insider", group="HAL-112", opened=47 * DAY, tables=2, rows=20_120, tagged=9),
        _case(4, "Kestrel webshell", opened=12 * DAY, tables=2, rows=45_000, tagged=57),
        _case(5, "Meridian share", group="ACME-031", opened=9 * DAY, exists=False),
        _case(6, "Orion pilot", tables=1, rows=500),
    ]


def _home(page, cases=None):
    body = json.dumps(_cases() if cases is None else cases)
    page.route("**/api/cases", lambda route: route.fulfill(
        status=200, content_type="application/json", body=body)
        if route.request.method == "GET" else route.fallback())
    page.evaluate("() => { __winnow.S.homeSection = 'recent'; __winnow.S.homeSearch = ''; "
                  "__winnow.S.homeSort = 'opened'; __winnow.showHome(); return __winnow.refreshCases(); }")
    page.wait_for_selector("#home:not([hidden]) :is(.home-panes, .home-empty)")


def _listed(page):
    return page.locator("#home .home-row .home-name").all_inner_texts()


def _section(page, title):
    page.locator("#home .side-rail-item", has=page.locator(".side-rail-label", has_text=title)).first.click()
    page.wait_for_function("(t) => document.querySelector('#home .side-rail-item[aria-selected=\"true\"] .side-rail-label').textContent === t", arg=title)


def test_opened_ago_says_it_the_way_people_do(page):
    """The registry's timestamp sat at the end of the path line, and was
    the part an ellipsis took. The list says how long ago instead."""
    now = page.evaluate("() => new Date(2026, 9, 22, 15, 0, 0).getTime()")
    ago = lambda iso: page.evaluate("([iso, now]) => __winnow.openedAgo(iso, now)", [iso, now])
    assert ago(None) == "never"
    assert ago("2026-10-22T14:59:30") == "just now"
    assert ago("2026-10-22T14:23:00") == "37 min ago"
    assert ago("2026-10-22T09:10:00") == "5 h ago"
    # Calendar days, not 24-hour steps: last night is yesterday.
    assert ago("2026-10-21T23:50:00") == "yesterday"
    assert ago("2026-10-19T08:00:00") == "3 days ago"
    assert ago("2026-10-10T08:00:00") == "2 weeks ago"
    assert ago("2026-09-05T08:00:00") == "Sep 5"
    assert ago("2025-12-18T08:00:00") == "2025-12-18"


def test_the_side_list_holds_recent_every_case_and_each_group(page):
    _home(page)
    labels = page.locator("#home .side-rail-label").all_inner_texts()
    # Groups by their newest work: ACME's last open was minutes ago, HAL's
    # over a month. Cases in no group get an entry of their own.
    assert labels == ["Recent", "All cases", "ACME-031", "HAL-112", "No group"], labels
    hints = page.locator("#home .side-rail-hint").all_inner_texts()
    assert hints[0] == "5 opened in the last 30 days", hints
    assert hints[1] == "6 · 1 file missing", hints

    # Recent leaves out the case not opened in 47 days, and keeps the one
    # never opened (it needs attention, not hiding). Newest first.
    assert _listed(page) == ["Rivera intrusion", "Rivera file server", "⚠ Meridian share",
                             "Kestrel webshell", "Orion pilot"]
    assert page.locator("#home .home-row .home-group-chip").count() == 3

    # A group's own section lists its cases, without a chip saying the same
    # group on every row.
    _section(page, "HAL-112")
    assert _listed(page) == ["Halcyon insider"]
    assert page.locator("#home .home-row .home-group-chip").count() == 0

    # All cases: every case, under its group's heading.
    _section(page, "All cases")
    heads = page.locator("#home .home-group .tables-group-name").all_inner_texts()
    assert heads == ["ACME-031", "HAL-112", "No group"], heads
    assert len(_listed(page)) == 6


def test_a_row_shows_its_counts_and_when_it_was_opened(page):
    """Today's card was a path line with the counts and the opened time
    on the end of it, and the end is what the ellipsis took. A row is one
    line now, with the path in the name's tooltip, and nothing in it is
    cut short at the width the app opens at."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _home(page)
    row = page.locator("#home .home-row", has=page.locator(".home-name", has_text="Rivera intrusion"))
    assert row.locator(".tables-rows").inner_text() == "108,400"
    assert row.locator(".tables-tagged").inner_text() == "84"
    # The wording itself is test_opened_ago_says_it_the_way_people_do's,
    # against a fixed clock; ten minutes ago is "yesterday" just after midnight.
    opened = row.locator(".home-opened")
    assert opened.inner_text() == page.evaluate("(t) => __winnow.openedAgo(t)", opened.get_attribute("title").replace(" ", "T"))
    assert opened.inner_text().endswith(("min ago", "yesterday"))
    assert "/srv/cases/rivera-intrusion.db-winnow" not in row.inner_text()
    assert "/srv/cases/rivera-intrusion.db-winnow" in row.locator(".home-name").get_attribute("title")
    clipped = page.evaluate("""() => [...document.querySelectorAll('#home .home-row .home-opened, #home .home-row .tables-num')]
      .filter((n) => n.scrollWidth > n.clientWidth + 1).map((n) => n.textContent)""")
    assert clipped == [], clipped
    assert row.bounding_box()["height"] < 40
    # No tagged rows is a dim dash, so the cases with findings stand out.
    pilot = page.locator("#home .home-row", has=page.locator(".home-name", has_text="Orion pilot"))
    assert pilot.locator(".tables-tagged").inner_text() == "—"
    assert pilot.locator(".home-opened").inner_text() == "never"


def test_one_menu_replaces_the_four_buttons(page):
    _home(page)
    assert page.locator("#home .home-row button", has_text="Remove from list").count() == 0
    page.locator("#home .home-row", has=page.locator(".home-name", has_text="Kestrel webshell")).locator(".tables-more").click()
    page.wait_for_selector(".menu")
    items = page.locator(".menu .menu-item").all_inner_texts()
    assert items == ["Open", "Edit name, group and notes…", "Copy case file path",
                     "Remove from list…", "Delete case file…"], items
    assert page.locator(".menu .menu-item:disabled").count() == 0
    page.keyboard.press("Escape")
    page.wait_for_selector(".menu", state="detached")

    # A case whose file is gone can't be opened or deleted, only fixed or
    # forgotten — and right-clicking its row says so the same way.
    page.locator("#home .home-row.missing").click(button="right")
    page.wait_for_selector(".menu")
    disabled = page.locator(".menu .menu-item:disabled").all_inner_texts()
    assert disabled == ["Open", "Delete case file…"], disabled
    page.keyboard.press("Escape")


def test_the_menu_s_items_do_what_they_say(page, copied, until):
    """The ⋯ items call what the four buttons called. The registry is the
    shared server's, so the forget itself is answered here."""
    _home(page)
    kestrel = page.locator("#home .home-row", has=page.locator(".home-name", has_text="Kestrel webshell"))
    kestrel.locator(".tables-more").click()
    with copied(page) as clip:
        page.locator(".menu .menu-item", has_text="Copy case file path").click()
    assert clip.text == "/srv/cases/kestrel-webshell.db-winnow"

    forgot = []
    page.route("**/api/cases/4?*", lambda route: (forgot.append(route.request.method + " " + route.request.url),
                                                    route.fulfill(json={"ok": True})))
    kestrel.locator(".tables-more").click()
    page.locator(".menu .menu-item", has_text="Remove from list").click()
    assert "left untouched on disk" in page.locator(".confirm-card").inner_text()
    page.locator(".confirm-card .btn", has_text="Remove").click()
    until(page, lambda: forgot, "the case to be forgotten")
    assert len(forgot) == 1 and forgot[0].startswith("DELETE "), forgot
    assert "delete_file" not in forgot[0] and "path=%2Fsrv%2Fcases%2Fkestrel-webshell.db-winnow" in forgot[0], forgot

    kestrel.locator(".tables-more").click()
    page.locator(".menu .menu-item", has_text="Edit name, group and notes").click()
    page.wait_for_function("() => /edit case/i.test(document.getElementById('modalTitle').textContent)")
    page.keyboard.press("Escape")


def test_a_column_head_sorts_the_list(page):
    _home(page)
    page.locator("#home .tables-sort", has_text="Rows").click()
    page.wait_for_function("() => document.querySelector('#home .tables-sort[data-sort=\"row_count\"]').getAttribute('aria-pressed') === 'true'")
    assert _listed(page)[:3] == ["Rivera intrusion", "Rivera file server", "Kestrel webshell"]
    page.locator("#home .tables-sort", has_text="Case").click()
    page.wait_for_function("() => document.querySelector('#home .home-name').textContent === 'Kestrel webshell'")
    assert _listed(page) == ["Kestrel webshell", "⚠ Meridian share", "Orion pilot", "Rivera file server", "Rivera intrusion"]


def test_a_filter_in_recent_says_where_an_older_match_is(page):
    """The filter looks inside the section it was typed in. A case not
    opened for a month is not in Recent, and "no match" over a case that
    exists would send the analyst looking for a file on disk."""
    _home(page)
    page.locator("#home .home-filter").fill("halcyon")
    page.wait_for_selector("#home .home-elsewhere")
    assert _listed(page) == []
    assert page.locator("#home .home-elsewhere").inner_text() == "1 more match in All cases"
    page.locator("#home .home-elsewhere").click()
    page.wait_for_function("() => document.querySelector('#home .side-rail-item[aria-selected=\"true\"] .side-rail-label').textContent === 'All cases'")
    assert _listed(page) == ["Halcyon insider"]
    # Notes are searched too.
    page.locator("#home .home-filter").fill("psexec")
    page.wait_for_function("() => document.querySelectorAll('#home .home-row').length === 1")
    assert _listed(page) == ["Rivera intrusion"]


def test_the_keyboard_walks_the_list(page):
    _home(page)
    page.locator("#home .home-filter").focus()
    page.keyboard.press("ArrowDown")
    assert page.evaluate("() => document.activeElement.textContent") == "Rivera intrusion"
    page.keyboard.press("ArrowDown")
    # A missing case's name can't be opened, so the walk steps over it.
    page.keyboard.press("ArrowDown")
    assert page.evaluate("() => document.activeElement.textContent") == "Kestrel webshell"
    for _ in range(3):
        page.keyboard.press("ArrowUp")
    assert page.evaluate("() => document.activeElement.classList.contains('home-filter')")


def test_the_section_survives_a_refresh(page):
    """Remove from list, Edit and coming back from a case all re-read the
    registry and repaint; the analyst stays on the section they were on."""
    _home(page)
    _section(page, "HAL-112")
    page.evaluate("() => __winnow.refreshCases()")
    page.wait_for_selector("#home .home-panes")
    assert page.locator("#home .side-rail-item[aria-selected='true'] .side-rail-label").inner_text() == "HAL-112"


def test_a_narrow_window_puts_the_sections_across_the_top(page):
    page.set_viewport_size({"width": 460, "height": 800})
    _home(page)
    rail = page.locator("#home .side-rail").bounding_box()
    box = page.locator("#home .home-box").bounding_box()
    assert rail["y"] + rail["height"] <= box["y"] + 1, (rail, box)
    wide = page.evaluate("""() => [...document.querySelectorAll('#home .home-row')]
      .filter((r) => r.scrollWidth > r.clientWidth + 1).length""")
    assert wide == 0
    assert page.evaluate("() => document.getElementById('home').scrollWidth <= window.innerWidth")


def test_no_cases_says_how_to_start(page):
    _home(page, cases=[])
    assert "No cases yet" in page.locator("#home .home-empty").inner_text()
    assert page.locator("#home .home-foot .btn", has_text="New case").count() == 1
    assert page.locator("#home .home-foot .btn", has_text="Quick look at files").count() == 1


def test_quick_look_at_files_opens_a_quick_look_ready_to_import(own_page, tmp_path):
    """Dropping files on the home screen was the only way into a quick look,
    and nothing on screen said so. Its own server: a quick look switches
    the case, and the shared server's case is every module's."""
    with running_server(tmp_path, "--no-fts") as srv:
        with own_page(base=srv.base + "/", viewport=(1200, 800), wait_until="load", ready="#home:not([hidden]) .home-foot",
                      ready_timeout=20_000) as pg:
            # A server that has never run asks where case files go first.
            pg.locator(".confirm-card .btn", has_text="Use the default").click()
            pg.wait_for_selector(".confirm-overlay", state="detached")
            pg.locator("#home .home-foot .btn", has_text="Quick look at files").click()
            pg.wait_for_selector("#tempBanner:not([hidden])", timeout=15_000)
            pg.wait_for_function("() => /import/i.test(document.getElementById('modalTitle').textContent)")
            cur = pg.evaluate("() => fetch('/api/case/current').then((r) => r.json())")
            assert cur["temp"] is True


def test_the_case_just_left_can_be_deleted_from_the_list(own_page, tmp_path):
    """Going back to the case list leaves the server's case open, and the
    delete refused a case the server had open: the one an analyst had just
    come from answered "Could not delete: Close this case before deleting
    its file" until they opened another case and came back (reported). Its
    own server: the case is opened, then deleted."""
    import os

    with running_server(tmp_path, "--no-fts") as srv:
        left = str(tmp_path / "cases" / "left.db-winnow")
        srv.post("/api/cases", {"path": left, "name": "Left behind", "group": "", "notes": ""})
        # A case on the list already, so no first-run folder prompt.
        with own_page(base=srv.base + "/", viewport=(1200, 800), wait_until="load", ready="#home:not([hidden]) .home-row",
                      ready_timeout=20_000) as pg:
            row = lambda name: pg.locator("#home .home-row", has=pg.locator(".home-name", has_text=name))
            row("Left behind").locator(".home-name").click()
            pg.wait_for_selector("#app:not([hidden])")
            pg.click("#btnHome")
            # Home paints the list it already has, from before the case was
            # opened, and the refresh that knows it is open lands after. A
            # menu opened on the old row asks without the note below (the
            # flake hunt's loaded runner got there first).
            pg.wait_for_function("() => __winnow.S.cases.some((c) => c.name === 'Left behind' && c.open)")

            row("Left behind").locator(".tables-more").click()
            pg.locator(".menu .menu-item", has_text="Delete case file…").click()
            assert "still has this case open" in pg.locator(".confirm-card").inner_text()
            pg.locator(".confirm-card .btn", has_text="Delete permanently").click()
            pg.wait_for_function("() => ![...document.querySelectorAll('#home .home-name')]"
                                 ".some((n) => n.textContent.includes('Left behind'))", timeout=15_000)
            assert "Could not delete" not in pg.locator("#toast").inner_text()
            assert not os.path.exists(left)
            assert srv.get("/api/case/current")["open"] is False

            # The page carries on with no case: the next one it opens is
            # its own, not a case another window switched to.
            nxt = str(tmp_path / "cases" / "next.db-winnow")
            srv.post("/api/cases", {"path": nxt, "name": "Next one", "group": "", "notes": ""})
            pg.evaluate("() => __winnow.refreshCases()")
            row("Next one").locator(".home-name").click()
            pg.wait_for_selector("#app:not([hidden])")
            pg.wait_for_function("() => document.getElementById('brandLabel').textContent === 'Next one'")
            assert pg.locator("#caseGone").is_hidden()
