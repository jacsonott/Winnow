"""The watchlist page in the browser: what it opens on, and what its
counts say.

Three things this pins, all of them visible only in a rendered page:

* The right half used to say "Select an indicator to see its hits" while
  the case held thousands of findings. It opens on the latest flagged rows
  across every indicator now; picking one still narrows to it, and there is
  a way back.
* Two indicators covering the same rows were invisible, and the summary
  added their counts as though they were separate findings. The rows carry
  an overlap marker, the summary leads with distinct rows, and the marker
  opens a dialog that merges them.
* A bare "0" meant both "scanned, not here" and "never scanned". Those are
  different words now.
"""
import json
import re
import urllib.request

import pytest

pytestmark = pytest.mark.ui

# Both match every row of ui.csv (its CommandLine holds
# "...WindowsPowerShell\\v1.0\\powershell.exe -Enc ..."), so their hit sets
# are identical — the shape that reported one finding as two.
DUPE_A = "powershell.exe"
DUPE_B = "powershell"
ABSENT = "zzz-not-in-this-case"
UNSCANNED = "zzz-never-scanned"


def _clear_indicators(server):
    req = urllib.request.Request(server.rstrip("/") + "/api/watchlist",
                                 headers={"X-Timeline-Lite-Client": "1"})
    for ind in json.loads(urllib.request.urlopen(req).read()):
        urllib.request.urlopen(urllib.request.Request(
            server.rstrip("/") + f"/api/watchlist/{ind['id']}", method="DELETE",
            headers={"X-Timeline-Lite-Client": "1"})).read()


@pytest.fixture
def watchlist(server, server_post):
    """Four indicators in three states: two that cover identical rows, one
    scanned and clean, one never scanned at all. The last is added AFTER
    the scan on purpose — that is what makes its zero a different zero."""
    _clear_indicators(server)
    for value in (DUPE_A, DUPE_B, ABSENT):
        server_post("/api/watchlist", {"value": value, "kind": "other"})
    server_post("/api/watchlist/scan", {})
    server_post("/api/watchlist", {"value": UNSCANNED, "kind": "other"})
    yield
    _clear_indicators(server)


def _open(page):
    page.locator("#tabWatchlist").click()
    page.wait_for_selector("#watchlistview:not([hidden])")


def _row(page, value):
    return page.locator(".wl-row").filter(
        has=page.locator(".wl-val", has_text=re.compile(rf"^{re.escape(value)}$")))


def _head(page):
    """The heading is uppercased by CSS and inner_text reports what is
    rendered, so compare in one case — the claim is which indicator the
    pane narrowed to, not how the heading is styled. The back button
    shares the element, hence the last line."""
    return page.locator(".wl-hits-head").inner_text().strip().splitlines()[-1].lower()


def test_the_pane_opens_on_the_latest_hits_not_an_instruction(page, watchlist):
    _open(page)
    page.wait_for_selector(".wl-latest")
    assert "Select an indicator" not in page.locator("#wlHits").inner_text()
    # Newest first, and every line says which indicator, which table, what
    # the row is and when.
    stamps = [r.inner_text() for r in page.locator(".wl-latest-when").all()]
    assert len(stamps) > 1
    assert stamps == sorted(stamps, reverse=True)
    first = page.locator(".wl-latest").first
    assert first.locator(".wl-latest-table").inner_text().endswith(".csv")
    assert "powershell" in first.locator(".wl-latest-what").inner_text()
    # One line per flagged ROW, naming both indicators that matched it —
    # not one line per hit, which would list this row twice.
    assert {b.inner_text() for b in first.locator(".wl-latest-ioc").all()} == {DUPE_A, DUPE_B}
    assert re.match(r"\d{4}-\d{2}-\d{2} ", stamps[0])


def test_picking_an_indicator_narrows_and_there_is_a_way_back(page, watchlist):
    _open(page)
    page.wait_for_selector(".wl-latest")
    # The value, not the middle of the row: the overlap chip sits there and
    # is its own control (it opens the merge dialog), so a click aimed at
    # the row's centre lands on the chip.
    _row(page, DUPE_A).locator(".wl-val").click()
    page.wait_for_selector(".wl-hit-group")
    assert page.locator(".wl-latest").count() == 0
    assert _head(page) == f'hits for "{DUPE_A}"'
    page.locator(".wl-hits-back").click()
    page.wait_for_selector(".wl-latest")
    assert page.locator(".wl-hit-group").count() == 0


def test_an_indicator_name_in_a_hit_line_narrows_to_it(page, watchlist):
    _open(page)
    page.wait_for_selector(".wl-latest")
    # Anchored: "powershell" is a substring of "powershell.exe", and both
    # names are on this line.
    page.locator(".wl-latest").first.locator(
        ".wl-latest-ioc", has_text=re.compile(rf"^{re.escape(DUPE_B)}$")).click()
    page.wait_for_selector(".wl-hit-group")
    assert _head(page) == f'hits for "{DUPE_B}"'


def test_zero_says_which_kind_of_zero_it_is(page, watchlist):
    _open(page)
    page.wait_for_selector(".wl-row")
    # Scanned every table, matched nothing — the one an analyst can write
    # "not present" from.
    clean = _row(page, ABSENT).locator(".wl-state")
    assert clean.inner_text() == "clean"
    assert "no row anywhere in the case matched it" in clean.get_attribute("title")
    # Added since the scan: nothing has read it yet, and that is not the
    # same claim.
    never = _row(page, UNSCANNED).locator(".wl-state")
    assert never.inner_text() == "not scanned"
    # They are told apart by more than a tooltip.
    assert clean.inner_text() != never.inner_text()


def test_duplicates_are_marked_and_the_summary_counts_rows_not_hits(page, watchlist):
    _open(page)
    page.wait_for_selector(".wl-row")
    # Both entries report the same rows, so the hit total is exactly twice
    # the number of findings — which is what the old summary printed as
    # the headline number.
    summary = page.locator("#wlSummary").inner_text()
    rows = int(re.search(r"([\d,]+) rows flagged", summary).group(1).replace(",", ""))
    hits = int(re.search(r"([\d,]+) hits counted across indicators", summary).group(1).replace(",", ""))
    assert hits == rows * 2
    for value, other in ((DUPE_A, DUPE_B), (DUPE_B, DUPE_A)):
        chip = _row(page, value).locator(".wl-dup")
        assert chip.inner_text() == f"⧉ same rows as {other}"
    # The one that matched nothing is not a duplicate of anything.
    assert _row(page, ABSENT).locator(".wl-dup").count() == 0


def test_merging_a_duplicate_removes_it_and_the_summary_stops_double_counting(page, watchlist):
    _open(page)
    page.wait_for_selector(".wl-row")
    _row(page, DUPE_A).locator(".wl-dup").click()
    page.wait_for_selector("#modal:not([hidden])")
    # Quotes around the name are curly in the button label; match around them.
    page.locator("#modalBody .btn", has_text=re.compile(rf"Keep .{re.escape(DUPE_B)}.$")).click()
    page.wait_for_selector("#modal[hidden]", state="attached")
    page.wait_for_selector(".wl-row .wl-dup", state="detached")
    assert _row(page, DUPE_A).count() == 0
    assert _row(page, DUPE_B).count() == 1
    summary = page.locator("#wlSummary").inner_text()
    # One indicator over those rows now: nothing is counted twice, so the
    # summary is back to a plain hit total.
    assert "rows flagged" not in summary
    assert re.search(r"1 with hits · [\d,]+ hits$", summary)
