"""The row menu's Plugins section: registered row actions appear as
entries, grey out past their max_rows, and post the selection to the
dispatch route (the server half is tests/test_plugin_row_actions.py)."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui

def test_plugin_section_lists_actions_and_respects_max_rows(page, row_menu, flyout, fake_row_action):
    fake_row_action(max_rows=2)
    root = row_menu(row=1, cell=1)
    # Plugin actions live under a Plugins ▸ submenu, counted in its hint.
    plugins = root.locator(".menu-item-sub", has_text="Plugins")
    assert plugins.count() == 1 and plugins.locator(".menu-item-hint").inner_text() == "1"
    item = flyout("Plugins").locator(".menu-item", has_text="Look up on VT")
    assert item.get_attribute("aria-disabled") in (None, "false") and not item.is_disabled()
    page.keyboard.press("Escape")

    # Select 3 rows → the entry is disabled (max_rows 2).
    page.locator(".row").nth(0).locator(".cell").nth(1).click()
    page.locator(".row").nth(2).locator(".cell").nth(1).click(modifiers=["Shift"])
    row_menu(row=1, cell=1)
    item = flyout("Plugins").locator(".menu-item", has_text="Look up on VT")
    assert item.is_disabled() or item.get_attribute("aria-disabled") == "true"
    page.keyboard.press("Escape")


# ---------------------------------------------------------- running one
#
# The entry had never been clicked. The dispatch route is answered here:
# 'demo' is a stand-in with no plugin on disk behind it, and the server
# half is tests/test_plugin_row_actions.py. What the page sends, and what
# it does with each kind of answer, is what these pin.

ROUTE = "**/api/plugins/row_action/demo/vt"


@pytest.fixture
def dispatch(page):
    """Every body the page posts to the action's route, and a queue of
    (status, body) answers — 200 {} once it runs dry."""
    sent, answers = [], []

    def handle(route):
        sent.append(route.request.post_data_json)
        status, body = answers.pop(0) if answers else (200, {})
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route(ROUTE, handle)
    try:
        yield sent, answers
    finally:
        page.unroute(ROUTE)


def _toast(page, text):
    page.wait_for_function("(t) => (document.getElementById('toast').textContent || '').includes(t)", arg=text)


def _run(page, row_menu, flyout, row, column):
    """Right-click `row` (its place on screen) in `column` and click the
    action; returns that row's position in the view."""
    ci = page.evaluate("(c) => __winnow.visibleCols().indexOf(c)", column)
    row_menu(row=row, cell=ci)
    pos = int(page.locator(".row").nth(row).get_attribute("data-pos"))
    flyout("Plugins").locator(".menu-item", has_text="Look up on VT").click()
    return pos


def _pair(page, pos):
    return page.evaluate("(p) => [__winnow.S.sourceId, __winnow.rowAt(p).rid]", pos)


def test_running_it_on_one_row_posts_that_row_and_the_clicked_cell(page, row_menu, flyout, fake_row_action,
                                                                   dispatch, until):
    sent, answers = dispatch
    fake_row_action()
    answers.append((200, {"message": "VT: 0 of 1 known"}))
    pos = _run(page, row_menu, flyout, row=1, column="Host")
    until(page, lambda: sent, "the action to be posted")
    host = page.evaluate("""(p) => __winnow.rowAt(p).cells[
        __winnow.S.columns.findIndex((c) => c.name === 'Host')]""", pos)
    assert sent == [{"source_id": page.evaluate("() => __winnow.S.sourceId"),
                     "pairs": [_pair(page, pos)], "column": "Host", "value": host}]
    _toast(page, "VT: 0 of 1 known")


def test_running_it_on_a_selection_posts_every_picked_row(page, row_menu, flyout, fake_row_action, dispatch, until):
    sent, _ = dispatch
    fake_row_action()
    # A three-row range in the column the right-click lands in: a click
    # outside the range would move the selection to that one cell.
    host = page.evaluate("() => __winnow.visibleCols().indexOf('Host')")
    page.locator(".row").nth(0).locator(".cell").nth(host).click()
    page.locator(".row").nth(2).locator(".cell").nth(host).click(modifiers=["Shift"])
    _run(page, row_menu, flyout, row=1, column="Host")
    until(page, lambda: sent, "the action to be posted")
    picked = [int(page.locator(".row").nth(i).get_attribute("data-pos")) for i in range(3)]
    assert sent[0]["pairs"] == [_pair(page, p) for p in picked]
    # With nothing in the answer to say, it says the action finished.
    _toast(page, "Look up on VT: done")


def test_an_answer_with_a_link_opens_it(page, server, row_menu, flyout, fake_row_action, dispatch):
    _, answers = dispatch
    fake_row_action()
    link = server.rstrip("/") + "/api/version"
    answers.append((200, {"open_url": link, "message": "Opened the report"}))
    with page.context.expect_page() as opened:
        _run(page, row_menu, flyout, row=1, column="Host")
    tab = opened.value
    try:
        tab.wait_for_load_state()
        assert tab.url == link
    finally:
        tab.close()
    _toast(page, "Opened the report")


def test_a_refusal_is_said_with_the_server_s_reason(page, row_menu, flyout, fake_row_action, dispatch):
    _, answers = dispatch
    fake_row_action()
    answers.append((400, {"detail": "No hash column in this table"}))
    _run(page, row_menu, flyout, row=1, column="Host")
    _toast(page, "Look up on VT failed: No hash column in this table")


def test_on_a_merge_each_row_is_posted_with_the_table_it_lives_in(page, row_menu, flyout, fake_row_action,
                                                                  dispatch, until, server_post, tmp_path):
    """Invariant 9: the route resolves rows by (source_id, rid) on the
    real table, so a merged row goes with its member's id, not the
    merge's."""
    sent, _ = dispatch
    fake_row_action()
    a, b = tmp_path / "ra_a.csv", tmp_path / "ra_b.csv"
    a.write_text("When,Host\n2026-01-01 00:00:00,HA\n", encoding="utf-8")
    b.write_text("When,Host\n2026-01-02 00:00:00,HB\n", encoding="utf-8")
    home = page.evaluate("() => __winnow.S.sourceId")
    server_post("/api/ingest/jobs/path", {"path": str(a)})
    server_post("/api/ingest/jobs/path", {"path": str(b)})
    ids, merge = {}, None
    try:
        # Polled from Python: a promise returned to wait_for_function is
        # truthy at once, so it would not wait for the imports at all.
        until(page, lambda: page.evaluate("""() => __winnow.loadSources().then(() => __winnow.S.sources
            .filter((s) => /^ra_[ab]\\.csv$/.test(s.name)).length)""") == 2,
              "both tables imported", timeout=15)
        ids = page.evaluate("""() => Object.fromEntries(__winnow.S.sources
            .filter((s) => /^ra_[ab]\\.csv$/.test(s.name)).map((s) => [s.name, s.id]))""")
        merge = server_post("/api/merges", {"name": "row-action-merge", "source_ids": [ids["ra_a.csv"], ids["ra_b.csv"]]})
        page.evaluate("(id) => __winnow.loadSources().then(() => __winnow.openSource(id))", merge["id"])
        page.wait_for_function("(id) => __winnow.S.sourceId === id && __winnow.busyCount === 0", arg=merge["id"])
        page.wait_for_selector(".row[data-pos='1']")
        host = page.evaluate("() => __winnow.visibleCols().indexOf('Host')")
        page.locator(".row").nth(0).locator(".cell").nth(host).click()
        page.locator(".row").nth(1).locator(".cell").nth(host).click(modifiers=["Shift"])
        _run(page, row_menu, flyout, row=0, column="Host")
        until(page, lambda: sent, "the action to be posted")
        assert sent[0]["source_id"] == merge["id"]
        assert sorted(map(tuple, sent[0]["pairs"])) == [(ids["ra_a.csv"], 1), (ids["ra_b.csv"], 1)]
    finally:
        routes = ([f"/api/merges/{abs(int(merge['id']))}"] if merge else []) + [f"/api/source/{i}" for i in ids.values()]
        for route in routes:
            page.evaluate("(r) => fetch(r, { method: 'DELETE', headers: { 'X-Timeline-Lite-Client': '1' } })", route)
        page.evaluate("(id) => __winnow.loadSources().then(() => __winnow.openSource(id))", home)
