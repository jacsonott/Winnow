"""winnow.tabState: a plugin mount's own row in the case file.

A mount is torn down without a word to the plugin — a case switch, a
Settings toggle, a profile apply, F5 — so a plugin cannot save on the way
out, and the host writes as the analyst works instead. These drive the
context object a mount receives, through the fake plugin from conftest, and
cover what that shape has to guarantee: what one mount saves another mount
of the same tab reads back, a tab and a panel of one plugin do not share a
row, a burst of edits is one write, a write still inside its debounce when
the case changes is dropped rather than landed in the case that just
opened, and a read that FAILED is told apart from a mount that has never
saved.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

KEYS = ("tab:fake.t", "panel:fake.t")

# Fire a promise into a window slot and wait for the slot: a promise
# returned from page.evaluate has no timeout of its own.
SET = """(v) => { window.__r = undefined;
  __ctx.tabState.set(v).then((ok) => { window.__r = ok; }); }"""
GET = """(kind) => { window.__g = undefined;
  const ctx = __winnow.buildPluginTabContext(__winnow.S.pluginTabs[0], kind);
  ctx.tabState.get().then((v) => { window.__g = v; }); }"""


@pytest.fixture
def tabstate(page, fake_plugin_mount, server_post):
    """The fake mount's context on window.__ctx, and the rows it writes
    taken back out of the shared case file afterwards."""
    yield
    for key in KEYS:
        server_post("/api/plugin_state", {"key": key, "payload": None})


def _set(page, value):
    page.evaluate(SET, value)
    page.wait_for_function("() => window.__r !== undefined", timeout=10_000)
    return page.evaluate("() => window.__r")


def _get(page, kind="tab"):
    page.evaluate(GET, kind)
    page.wait_for_function("() => window.__g !== undefined", timeout=10_000)
    return page.evaluate("() => window.__g")


def test_what_one_mount_saves_the_next_one_reads(page, tabstate):
    """The whole point: the next mount is a different closure, and often a
    different module instance, so the state has to come from the case."""
    assert _get(page) is None
    assert _set(page, {"v": 1, "sheets": ["Logons by host"]}) is True

    got = _get(page)
    assert got["payload"] == {"v": 1, "sheets": ["Logons by host"]}
    assert got["savedAt"], "a restore has to be able to say when it was saved"


def test_a_tab_and_a_panel_of_one_plugin_do_not_share_a_row(page, tabstate):
    """Both are 'fake.t'; only the mount kind tells them apart, and a
    plugin that registers a tab and a panel under one id would otherwise
    have one overwrite the other."""
    _set(page, {"v": 1, "where": "tab"})
    page.evaluate("""() => { window.__p = undefined;
      const ctx = __winnow.buildPluginTabContext(__winnow.S.pluginTabs[0], 'panel');
      ctx.tabState.set({ v: 1, where: 'panel' }).then((ok) => { window.__p = ok; }); }""")
    page.wait_for_function("() => window.__p !== undefined", timeout=10_000)

    assert _get(page, "tab")["payload"]["where"] == "tab"
    assert _get(page, "panel")["payload"]["where"] == "panel"


def test_a_burst_of_edits_is_one_write_and_the_last_value_wins(page, tabstate):
    """Every keystroke in a description calls set(); one POST per keystroke
    would be a write per character into the case file."""
    posts = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST"
            and "/api/plugin_state" in r.url else None)
    page.evaluate("""() => { window.__r = undefined;
      __ctx.tabState.set({ v: 1, n: 1 });
      __ctx.tabState.set({ v: 1, n: 2 });
      __ctx.tabState.set({ v: 1, n: 3 }).then((ok) => { window.__r = ok; }); }""")
    page.wait_for_function("() => window.__r !== undefined", timeout=10_000)

    assert len(posts) == 1, posts
    assert _get(page)["payload"]["n"] == 3


def test_a_write_still_pending_when_the_case_changes_is_dropped(page, tabstate):
    """openCase swaps the server's Store before the mounts are torn down,
    so a save that fired a moment later would write the previous case's
    spec into the case that just opened — the same trap the note binding is
    blanked for. Dropped, not flushed."""
    posts = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST"
            and "/api/plugin_state" in r.url else None)
    page.evaluate("""() => { window.__r = undefined;
      __ctx.tabState.set({ v: 1, belongs: 'the other case' }).then((ok) => { window.__r = ok; });
      __winnow.dropPendingTabState(); }""")
    page.wait_for_function("() => window.__r !== undefined", timeout=10_000)

    assert page.evaluate("() => window.__r") is False
    assert posts == []
    assert _get(page) is None


def test_a_payload_too_large_is_refused_and_leaves_the_saved_one_alone(page, tabstate):
    """The cap is the enforcement behind "save the definition, not the
    rows" — and a refused write must not take the good one with it."""
    _set(page, {"v": 1, "sheets": ["spec"]})
    assert _set(page, {"v": 1, "rows": ["x" * 512] * 200}) is False
    assert _get(page)["payload"]["sheets"] == ["spec"]


def test_clear_takes_the_row_away(page, tabstate):
    _set(page, {"v": 1, "sheets": ["gone soon"]})
    page.evaluate("""() => { window.__c = undefined;
      __ctx.tabState.clear().then((ok) => { window.__c = ok; }); }""")
    page.wait_for_function("() => window.__c !== undefined", timeout=10_000)
    assert page.evaluate("() => window.__c") is True
    assert _get(page) is None


def test_a_read_that_fails_is_not_reported_as_nothing_saved(page, tabstate):
    """`null` means "this mount has never saved", which is an invitation to
    start from an empty tab — and the first edit after that replaces the
    row. A read that failed has to be a different answer, because a mount
    cannot protect state it was never shown."""
    _set(page, {"v": 1, "sheets": ["still there"]})

    def fail_the_read(route, request):
        if request.method == "GET":
            route.fulfill(status=500, content_type="application/json", body='{"detail": "nope"}')
        else:
            route.continue_()

    page.route("**/api/plugin_state**", fail_the_read)
    try:
        assert _get(page) == {"error": True}
    finally:
        page.unroute("**/api/plugin_state**", fail_the_read)
    assert _get(page)["payload"]["sheets"] == ["still there"]
