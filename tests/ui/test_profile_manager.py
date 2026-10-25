"""The two-pane profile manager and the apply sheet in front of it.

What these are guarding is narrow and expensive: applying a profile sets an
explicit on/off override for EVERY installed plugin, replaces boards by
name, seeds indicators and starts a scan — and before the sheet, the only
thing on screen was a truncated row and a button. So the assertions here
are on what the analyst is TOLD (the numbers in the sheet come from this
case, not from the profile) and on a part left unticked changing nothing.

The shared case is session-scoped, so every test here either changes
nothing in it or puts back what it changed. The one exception is noted at
_clean_case: a `profiles_applied` case setting the apply records, which is
additive and which no other test reads.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui

PROF = "UI Manager Profile"
BOARD = "UI Manager Board"
IOCS = ["ui-manager-ioc-a", "ui-manager-ioc-b"]


def _open_manager(page):
    page.keyboard.press("p")
    page.wait_for_selector("#modal:not([hidden])")
    page.wait_for_selector(".pm-item", timeout=15_000)


def _seed(api, **over):
    body = {"name": PROF, "plugins": ["pivot"],
            "description": "what the UI test opens with",
            "dashboard": [{"title": "UI card one", "source": "sql", "render": "stat",
                           "query": {"sql": "SELECT 1"}, "span": 2},
                          {"title": "UI card two", "source": "watchlist", "render": "stat",
                           "live": True}],
            "watchlist": [{"value": v, "kind": "other"} for v in IOCS],
            "variables": [{"name": "ui_engagement", "label": "UI engagement", "required": True}]}
    body.update(over)
    return api("/api/plugin_bundles", "POST", body)


def _clean(api):
    """Take back every profile this module made. Profiles are machine
    state (workspace/plugin_bundles.json), so they outlive the page."""
    for b in api("/api/plugin_bundles"):
        if not b.get("shipped") and b["name"].startswith("UI Manager"):
            api(f"/api/plugin_bundles/{b['id']}", "DELETE")


def _clean_case(api):
    """And every change an apply made to the shared case: the board it
    created, the indicators it seeded, the variables it declared. What
    cannot be put back is the `profiles_applied` case setting the apply
    records — there is no route that clears a case setting, it is additive,
    and nothing else in the suite reads it."""
    for d in api("/api/dashboards"):
        if d["name"] in (BOARD, PROF):
            api(f"/api/dashboards/{d['id']}", "DELETE")
    wl = api("/api/watchlist")
    for i in (wl["indicators"] if isinstance(wl, dict) else wl):
        if i["value"] in IOCS:
            api(f"/api/watchlist/{i['id']}", "DELETE")
    for v in api("/api/case/variables"):
        if v["name"] == "ui_engagement":
            api(f"/api/case/variables/{v['name']}", "DELETE")


# ------------------------------------------------------------- the manager


def test_the_manager_shows_the_whole_profile_not_one_truncated_row(page):
    """The list this replaced printed one ellipsised line per profile — in
    the screenshot that started this, the shipped description pushed Apply
    and Copy off the row entirely. Every part of the selected profile is on
    screen now, including every widget with its render kind."""
    from winnow import defaults

    kape = next(p for p in defaults.profiles() if p["name"] == "KAPE triage")
    _open_manager(page)
    assert page.locator("#modalTitle").inner_text().lower() == "profiles"
    assert page.locator(".pm-item-label", has_text="KAPE triage").count() == 1
    assert page.locator(".pm-detail h3").inner_text() == "KAPE triage"

    page.wait_for_function(
        "(n) => document.querySelectorAll('.pm-widget').length === n",
        arg=len(kape["dashboard"]), timeout=15_000)
    assert page.locator(".pm-w-title").all_inner_texts() == [w["title"] for w in kape["dashboard"]]
    # The render kind of each card, and its span when it is not 1 — every
    # card against its own definition, rather than the first card and the
    # first wide one. Which card is wide is the board's business and it has
    # changed once already; what this test is about is that the kind and the
    # span shown are the ones the profile declares.
    def kind_label(w):
        # The widget editor's vocabulary, deliberately spelled out here
        # rather than imported: a card quietly losing its span, or a
        # non-SQL source going unsaid, is what this is watching for.
        bits = []
        if w.get("source") and w["source"] != "sql":
            bits.append(w["source"])
        bits.append(w.get("render") or "stat")
        span = w.get("span") or 1
        if span > 1:
            bits.append(f"span {span}")
        return " · ".join(bits)
    assert page.locator(".pm-w-kind").all_inner_texts() == [kind_label(w) for w in kape["dashboard"]]
    assert any((w.get("span") or 1) > 1 for w in kape["dashboard"]), \
        "the shipped board has a wide card; this test needs one to prove the span is shown"
    # and whether each one re-runs on every open or paints its last result
    live = [w["title"] for w in kape["dashboard"] if w.get("live")]
    assert live, "the shipped board marks some widgets live; this test needs one"
    assert page.locator(".pm-w-run.live").count() == len(live)
    for title in live:
        row = page.locator(".pm-widget", has_text=title)
        assert "every open" in row.inner_text()

    assert page.locator('.pm-sec[data-sec="plugins"] .pm-pill').all_inner_texts() == kape["plugins"]
    assert page.locator('.pm-sec[data-sec="watchlist"] .pm-pill').all_inner_texts() == \
        [i["value"] for i in kape["watchlist"]]
    assert page.locator('.pm-sec[data-sec="variables"] .pm-pill').count() == len(kape["variables"])
    page.keyboard.press("Escape")


def test_apply_and_copy_stay_on_screen_when_the_profile_overflows(page, api):
    """The detail pane scrolls; the head and the foot must not scroll with
    it. A profile with more cards than the pane is tall leaves a footer
    pinned only by `margin-top: auto` below the fold, and the analyst has to
    scroll past every card to reach the button the manager exists for.
    Playwright auto-scrolls before a click, so this has to be asserted as
    geometry, not by clicking.

    The overflow is seeded here rather than borrowed from the shipped KAPE
    profile. How many cards that board carries is the board's business, it
    has already changed once, and a layout claim that quietly stops being
    tested when a board gets denser is worse than no claim."""
    cards = 40
    try:
        _seed(api, dashboard=[{"title": f"Overflow card {i}", "source": "sql",
                               "render": "stat", "query": {"sql": "SELECT 1"}}
                              for i in range(cards)])
        _open_manager(page)
        page.locator(".pm-item", has_text=PROF).click()
        page.wait_for_function(
            "(n) => document.querySelectorAll('.pm-widget').length === n",
            arg=cards, timeout=15_000)
        geom = page.evaluate("""() => {
          const pane = document.querySelector('.pm-detail');
          pane.scrollTop = 0;
          const p = pane.getBoundingClientRect();
          const foot = document.querySelector('.pm-foot').getBoundingClientRect();
          const head = document.querySelector('.pm-head').getBoundingClientRect();
          return {overflows: pane.scrollHeight - pane.clientHeight,
                  paneTop: p.top, paneBottom: p.bottom,
                  footTop: foot.top, footBottom: foot.bottom,
                  headTop: head.top, headBottom: head.bottom};
        }""")
        assert geom["overflows"] > 100, \
            "the seeded profile has to overflow the pane or this proves nothing"
        assert geom["footBottom"] <= geom["paneBottom"] + 1 and geom["footTop"] >= geom["paneTop"], \
            "“Apply to this case…” is outside the visible pane at the top of the scroll"
        # and the head is still there once the analyst HAS scrolled
        geom = page.evaluate("""() => {
          const pane = document.querySelector('.pm-detail');
          pane.scrollTop = pane.scrollHeight;
          const p = pane.getBoundingClientRect();
          const head = document.querySelector('.pm-head').getBoundingClientRect();
          return {paneTop: p.top, paneBottom: p.bottom, headTop: head.top, headBottom: head.bottom};
        }""")
        assert geom["headTop"] >= geom["paneTop"] - 1 and geom["headBottom"] <= geom["paneBottom"], \
            "the head scrolled away with the widget list"
    finally:
        page.keyboard.press("Escape")
        _clean(api)


def test_a_shipped_profile_is_read_only_and_copies_instead(page):
    _open_manager(page)
    page.locator(".pm-item", has_text="KAPE triage").click()
    head = page.locator(".pm-head-acts")
    assert head.locator(".btn", has_text="Copy to edit").count() == 1
    assert head.locator(".btn", has_text="Edit…").count() == 0
    assert head.locator(".pm-del").count() == 0, "a shipped profile cannot be deleted"
    # (the badge is uppercased by CSS, so compare in one case)
    assert "read-only" in page.locator(".pm-head .pm-tag").first.inner_text().lower()
    page.keyboard.press("Escape")


# ----------------------------------------------------------- the apply sheet


def test_the_sheet_names_every_part_with_numbers_from_this_case(page, api):
    """Each line is counted against the open case: which plugins go on and
    which go off, what the board replaces, which indicators are new, which
    variables will be asked for."""
    try:
        _seed(api, variables=[{"name": "ui_engagement", "label": "UI engagement", "required": True},
                              {"name": "ui_client", "label": "UI client", "required": True,
                               "default": "Acme IR"}])
        on_now = sorted(p["fs_name"] for p in api("/api/plugins")["plugins"] if p["enabled"])
        _open_manager(page)
        page.locator(".pm-item", has_text=PROF).click()
        page.locator(".pm-foot .btn", has_text="Apply to this case").click()
        page.wait_for_selector(".ap-part", timeout=15_000)
        assert page.locator("#modalTitle").inner_text().lower() == f"apply “{PROF}” to this case".lower()

        plugins = page.locator('.ap-part[data-part="plugins"]').inner_text()
        if "pivot" in on_now:
            assert "pivot already on" in plugins
        else:
            assert "Turn on: pivot" in plugins
        for fs in on_now:
            if fs != "pivot":
                assert fs in plugins, f"{fs} is on in this case and the sheet does not say it goes off"

        boards = page.locator('.ap-part[data-part="boards"]').inner_text()
        assert f"Create “{PROF}” (2 widgets)" in boards
        assert "1 run every time, the rest cache" in boards

        watchlist = page.locator('.ap-part[data-part="watchlist"]').inner_text()
        assert "Add 2 indicators: " + ", ".join(IOCS) in watchlist
        tables = len(api("/api/sources"))
        assert f"starts a scan of {tables} table" in watchlist

        variables = page.locator('.ap-part[data-part="variables"]').inner_text()
        assert "UI engagement" in variables and "required, not set" in variables
        # One of the two declares a DEFAULT, and apply creates its row
        # carrying it — so it is never among the ones anything asks for.
        assert "UI client (required, defaults to \u201cAcme IR\u201d)" in variables
        assert "you will be asked for these after applying" in variables

        # Nothing has happened yet — that is the sentence at the bottom.
        assert "Nothing is applied until you press Apply." in page.locator(".ap-note").inner_text()
        assert not any(d["name"] == PROF for d in api("/api/dashboards"))
        page.locator(".row-actions .btn", has_text="Cancel").click()
        page.wait_for_selector(".pm-item", timeout=15_000)   # Cancel goes back to the manager
    finally:
        page.keyboard.press("Escape")
        _clean(api)
        _clean_case(api)


def test_a_profile_that_moves_no_plugin_still_offers_to_pin_the_set(page, api):
    """Applying writes an explicit override for EVERY installed plugin, so
    the case's plugin set stops depending on the machine's defaults. The
    sheet used to untick and disable the Plugins part whenever the case
    already matched the profile — so Apply wrote no override at all, and
    the analyst who later switched a plugin off machine-wide silently lost
    it from this case. Nothing to change is not nothing to do."""
    try:
        installed = api("/api/plugins")["plugins"]
        assert installed, "this test needs at least one installed plugin"
        on_now = sorted(p["fs_name"] for p in installed if p["enabled"])
        _seed(api, plugins=on_now, dashboard=[], dashboards=[], watchlist=[], variables=[])
        _open_manager(page)
        page.locator(".pm-item", has_text=PROF).click()
        page.locator(".pm-foot .btn", has_text="Apply to this case").click()
        page.wait_for_selector('.ap-part[data-part="plugins"]', timeout=15_000)

        row = page.locator('.ap-part[data-part="plugins"]')
        text = row.inner_text()
        assert "Every plugin is already where this profile wants it" in text
        assert f"all {len(installed)} pinned for this case" in text
        box = row.locator("input")
        assert box.is_checked() and box.is_enabled(), \
            "the part writes the overrides that pin this case; it cannot untick itself"
        page.locator(".row-actions .btn", has_text="Cancel").click()
        page.wait_for_selector(".pm-item", timeout=15_000)
    finally:
        page.keyboard.press("Escape")
        _clean(api)


def test_unticking_a_part_leaves_that_part_of_the_case_alone(page, api):
    """Take the board, leave the plugins and the watchlist. Before the
    sheet, this was one button that did all four."""
    try:
        _seed(api, dashboard=[{"title": "UI card one", "source": "sql", "render": "stat",
                               "query": {"sql": "SELECT 1"}}],
              dashboards=[{"name": BOARD, "widgets": [{"title": "Extra", "render": "kv"}]}])
        before_plugins = sorted(p["fs_name"] for p in api("/api/plugins")["plugins"] if p["enabled"])
        # What the overrides ARE before, not an absolute: the session case is
        # shared, and a module that ran earlier may have pinned one (applying a
        # profile is how several of them set up). The claim under test is that
        # unticking the part changes nothing, which is a comparison.
        before_overrides = {p["fs_name"]: p["case_override"] for p in api("/api/plugins")["plugins"]}
        wl = api("/api/watchlist")
        before_iocs = sorted(i["value"] for i in (wl["indicators"] if isinstance(wl, dict) else wl))
        before_vars = sorted(v["name"] for v in api("/api/case/variables"))

        _open_manager(page)
        page.locator(".pm-item", has_text=PROF).click()
        page.locator(".pm-foot .btn", has_text="Apply to this case").click()
        page.wait_for_selector(".ap-part", timeout=15_000)
        for part in ("plugins", "watchlist", "variables"):
            box = page.locator(f'.ap-part[data-part="{part}"] input')
            if box.is_checked():
                box.uncheck()
        page.locator(".row-actions .btn", has_text="Apply").click()
        page.wait_for_selector("#modal[hidden]", state="attached", timeout=20_000)

        names = sorted(d["name"] for d in api("/api/dashboards"))
        assert PROF in names and BOARD in names, names
        after = api("/api/plugins")["plugins"]
        assert sorted(p["fs_name"] for p in after if p["enabled"]) == before_plugins
        assert {p["fs_name"]: p["case_override"] for p in after} == before_overrides, \
            "the plugins part was unticked; the case's overrides should be exactly as they were"
        wl = api("/api/watchlist")
        assert sorted(i["value"] for i in (wl["indicators"] if isinstance(wl, dict) else wl)) == before_iocs
        assert sorted(v["name"] for v in api("/api/case/variables")) == before_vars
    finally:
        page.keyboard.press("Escape")
        _clean(api)
        _clean_case(api)


def test_a_board_the_analyst_edited_is_called_out_before_it_is_replaced(page, api):
    """upsert-by-name replaces outright. A board that is this profile's own
    untouched copy is nothing to warn about; one that has been edited since
    is the only warning worth printing."""
    try:
        _seed(api, plugins=[], watchlist=[], variables=[],
              dashboard=[{"title": "UI card one", "source": "sql", "render": "stat",
                          "query": {"sql": "SELECT 1"}}])
        rec = next(b for b in api("/api/plugin_bundles") if b["name"] == PROF)
        api(f"/api/plugin_bundles/{rec['id']}/apply", "POST", {"parts": ["boards"]})

        _open_manager(page)
        page.locator(".pm-item", has_text=PROF).click()
        page.locator(".pm-foot .btn", has_text="Apply to this case").click()
        page.wait_for_selector(".ap-part", timeout=15_000)
        assert page.locator(".ap-warn").count() == 0, "its own copy is not a warning"
        assert "Replace “UI Manager Profile” (1 widget → 1)" in \
            page.locator('.ap-part[data-part="boards"]').inner_text()
        page.locator(".row-actions .btn", has_text="Cancel").click()
        page.wait_for_selector(".pm-item", timeout=15_000)

        board = next(d for d in api("/api/dashboards") if d["name"] == PROF)
        api(f"/api/dashboards/{board['id']}", "POST", {"widgets": [{"title": "Mine", "render": "kv"}]})
        page.locator(".pm-foot .btn", has_text="Apply to this case").click()
        page.wait_for_selector(".ap-warn", timeout=15_000)
        assert PROF in page.locator(".ap-warn").inner_text()
        page.locator(".row-actions .btn", has_text="Cancel").click()
        page.wait_for_selector(".pm-item", timeout=15_000)
    finally:
        page.keyboard.press("Escape")
        _clean(api)
        _clean_case(api)


# ----------------------------------------------------------------- lineage


def test_a_stale_copy_is_offered_the_diff_and_nothing_else(page, api):
    """Taking an update is a button. Until it is pressed the copy is
    untouched, which is the promise the whole lineage line rests on."""
    try:
        kape = next(b for b in api("/api/plugin_bundles") if b["name"] == "KAPE triage")
        _seed(api, name="UI Manager Copy", plugins=[], dashboard=[], dashboards=[],
              watchlist=[], variables=[], from_profile="KAPE triage",
              from_version=kape["version"] - 1)
        _open_manager(page)
        item = page.locator(".pm-item", has_text="UI Manager Copy")
        assert item.locator(".pm-tag.update").count() == 1
        item.click()
        line = page.locator(".pm-update")
        assert "KAPE triage" in line.inner_text() and "has changed since you copied it" in line.inner_text()

        line.locator(".btn", has_text="See what changed").click()
        page.wait_for_selector(".pm-diff-row", timeout=15_000)
        rows = page.locator(".pm-diff").inner_text()
        assert f"0 widgets → {len(kape['dashboard'])}" in rows
        assert "lateral_movement" in rows

        mine = next(b for b in api("/api/plugin_bundles") if b["name"] == "UI Manager Copy")
        assert mine["dashboard"] == [] and mine["from_version"] == kape["version"] - 1, \
            "looking at the diff must not take it"

        page.locator(".row-actions .btn", has_text="Keep mine").click()
        page.wait_for_selector(".pm-item", timeout=15_000)
        mine = next(b for b in api("/api/plugin_bundles") if b["name"] == "UI Manager Copy")
        assert mine["dashboard"] == []
    finally:
        page.keyboard.press("Escape")
        _clean(api)


def test_importing_a_profile_file_lands_it_beside_the_others(page, api, tmp_path):
    try:
        path = tmp_path / "winnow-profile.json"
        path.write_text(json.dumps({"format": "winnow-profile/1", "profile": {
            "name": "UI Manager Imported", "plugins": ["pivot"], "description": "from a file",
            "dashboard": [{"title": "Imported card", "render": "stat"}],
            "watchlist": [{"value": "ui-manager-ioc-a"}]}}), encoding="utf-8")
        _open_manager(page)
        page.locator(".pm-list-acts input[type=file]").set_input_files(str(path))
        page.wait_for_selector(".pm-item:has-text('UI Manager Imported')", timeout=15_000)
        assert page.locator(".pm-detail h3").inner_text() == "UI Manager Imported"
        assert page.locator('.pm-sec[data-sec="board"] .pm-w-title').inner_text() == "Imported card"
        assert any(b["name"] == "UI Manager Imported" for b in api("/api/plugin_bundles"))
    finally:
        page.keyboard.press("Escape")
        _clean(api)
