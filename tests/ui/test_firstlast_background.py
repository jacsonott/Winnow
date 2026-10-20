"""First/Last works in the background: the rest of the app keeps answering
while a result is worked out, and the jobs panel says when it is ready.

The report: "when the First/Last page is rendering output, it locks up the
other tabs". On a 1M-row table each preview took 15 s, the tab waited on it
with a POST held open for all of it, and an edit under Auto-update never
stopped the one before. A browser opens six connections to Winnow and the
presence stream keeps one, so a handful of edits held the rest, and a scroll
in the grid took 67 s to paint: its page fetch was queued in the browser
behind them. Create table did the same inside a dialog that covers the app.

The shared fixture's 200 rows are previewed before anything can be seen
waiting, so this module boots its own server on a table big enough for a
preview to take seconds. The first test checks the preview really was
still running when it looked, so a machine fast enough to outrun it fails
loudly rather than passing having proved nothing.
"""

from __future__ import annotations

import re

import pytest

from spawn import running_server

pytestmark = pytest.mark.ui

N_ROWS = 300_000
KEY = "tab:first-last.firstlast"
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
START = "**/api/plugin/first_last/job/start"
# A preview going out, however the tab asks for it: a job's start now, the
# preview route itself before — so the symptom test below says what is
# wrong on either, rather than only that a route is missing.
PREVIEW_ASKED = re.compile(r"/api/plugin/first_last/(preview|job/start)$")


@pytest.fixture(scope="module")
def big_server(tmp_path_factory):
    d = tmp_path_factory.mktemp("flbg")
    csv = d / "big.csv"
    with csv.open("w", encoding="utf-8") as f:
        # Host + User is a group per row (300k groups, a few seconds a
        # preview); Host alone is 400 groups.
        f.write("When,Host,User,Bytes\n")
        for i in range(N_ROWS):
            f.write(f"2026-03-{1 + i % 28:02d} {i // 3600 % 24:02d}:{i // 60 % 60:02d}:{i % 60:02d},"
                    f"H{i % 400:03d},u{i * 7919 % 3001:04d},{i * 31 % 100000}\n")
    with running_server(d, "--case", d / "big.db", "--open", csv, "--no-fts",
                        ready="/api/sources", timeout=90) as srv:
        srv.post("/api/plugins/toggle", {"fs_name": "first_last", "scope": "on_all"})
        yield srv


@pytest.fixture
def fl(own_page, big_server):
    # Each test starts from one empty sheet: what the last one left is
    # saved in the case, and a restored sheet runs its preview on open.
    big_server.post("/api/plugin_state", {"key": KEY, "payload": None})
    # "load", not "networkidle": a table this size keeps background work
    # (column index builds) going for a while after the first paint.
    with own_page(base=big_server.base, wait_until="load",
                  ready="#rows .row:not(.pending)", ready_timeout=60_000) as pg:
        pg.evaluate("() => __winnow.loadPlugins()")
        pg.wait_for_function("() => __winnow.S.pluginTabs.some((t) => t.id.includes('firstlast'))",
                             timeout=10_000)
        pg.locator(".tab-plugin", has_text="First/Last").click()
        pg.wait_for_selector(".pluginview:not([hidden]) [data-zone='groupBy']", timeout=10_000)
        yield pg


def _to_grid(pg):
    pg.locator("#sourceTabs .tab", has_text="big").first.click()
    pg.wait_for_selector("#rows .row:not(.pending)", timeout=15_000)


def _previewing(pg):
    return pg.evaluate("() => [...document.querySelectorAll('.pluginview .note-status')]"
                       ".some((n) => n.textContent === 'Previewing…')")


def test_the_grid_answers_while_first_last_works_out_a_big_result(fl, until):
    pg = fl
    sent, ended = [], set()     # every First/Last request; the ones that have finished
    starts, cancels = [], []

    def went(r):
        if "/api/plugin/first_last/" not in r.url:
            return
        sent.append(r)
        if PREVIEW_ASKED.search(r.url):
            starts.append((r.post_data_json or {}).get("id"))
        elif r.url.endswith("/job/cancel"):
            cancels.append(r.post_data_json["id"])

    def done(r):
        ended.add(r)

    pg.on("request", went)
    pg.on("requestfinished", done)
    pg.on("requestfailed", done)

    # Two drags and six edits, each past the debounce: eight previews of a
    # 300k-group result, the last one still running.
    asked = lambda r: bool(PREVIEW_ASKED.search(r.url))  # noqa: E731
    for field in ("Host", "User"):
        with pg.expect_request(asked, timeout=10_000):
            pg.evaluate(DRAG, [f"[data-field='{field}']", "[data-zone='groupBy']"])
    tmpl = pg.locator(".pluginview input[style*='var(--mono)']").first
    for i in range(6):
        with pg.expect_request(asked, timeout=10_000):
            tmpl.fill(f"{{which}} of {{count}} · edit {i}")
    assert _previewing(pg), "the preview finished before anything could wait on it — the table is too small here"

    # The grid in another tab answers: a scroll paints rows from the middle
    # of the table while the last preview is still being worked out.
    # Scrolled from inside the wait, not once before it: opening the table
    # puts back its own scroll position when its view is in (openSource),
    # which can land after a scroll made the moment the first rows painted
    # and leave the grid at the top, waiting for rows nobody asked for.
    _to_grid(pg)
    pg.wait_for_function("""() => {
      const b = document.querySelector('#body');
      if (b.scrollTop < b.scrollHeight / 3) b.scrollTop = b.scrollHeight / 2;
      return [...document.querySelectorAll('#rows .row:not(.pending)')]
        .some((r) => Number(r.dataset.pos) > 100000);
    }""", timeout=15_000)

    # Because nothing was held open for the work. It used to be all eight
    # previews, each a POST answered only when its preview was — seconds
    # apiece, and together more connections than the browser gives Winnow.
    # A job's start answers within a quarter second, and a poll is a status
    # read. Measured once everything has finished, by the browser's own
    # timing of each request.
    until(pg, lambda: all(r in ended for r in sent), "First/Last's requests to finish", timeout=120)
    slow = sorted((round(r.timing["responseEnd"]), r.url.rsplit("/", 2)[-1])
                  for r in sent if r.timing["responseEnd"] > 1500)
    assert not slow, f"First/Last requests held open (ms, route): {slow}"

    # And every preview an edit superseded was stopped on the server, not
    # just forgotten here: left running, each kept a core busy for the rest
    # of its few seconds.
    assert len(starts) == 8
    assert set(starts[:-1]) <= set(cancels), f"superseded and still running: {set(starts[:-1]) - set(cancels)}"


def test_a_preview_that_lands_while_away_says_so_and_show_goes_back_to_it(fl, until):
    pg = fl
    held = []
    pg.route(START, lambda route: held.append(route))   # answered by hand below
    try:
        pg.evaluate(DRAG, ["[data-field='Host']", "[data-zone='groupBy']"])
        until(pg, lambda: len(held) == 1, "the preview's start")
        assert _previewing(pg)

        # Away while it runs: nothing stops, and a card says it carries on.
        _to_grid(pg)
        running = pg.locator("#jobsPanel .job-notice", has_text="Previewing")
        running.wait_for(timeout=5_000)
        assert "Bookends 1" in running.inner_text()

        held.pop().continue_()
        ready = pg.locator("#jobsPanel .job-notice", has_text="the preview is ready")
        ready.wait_for(timeout=60_000)
        assert "400 groups" in ready.inner_text()
        # Landing did not pull the analyst back: they are where they went.
        assert pg.evaluate("() => __winnow.S.activeTab") == "grid"

        ready.locator("button", has_text="Show").click()
        pg.wait_for_selector(".pluginview:not([hidden]) table tbody tr", timeout=10_000)
        assert pg.locator(".pluginview:not([hidden]) .sql-tab[aria-selected='true']").inner_text().startswith("Bookends 1")
        # Back on the tab, it says what the cards said; they have gone.
        pg.wait_for_function("() => !document.querySelector('#jobsPanel .job-notice')")
    finally:
        pg.unroute(START)


def test_create_table_closes_its_dialog_at_once_and_lands_in_the_background(fl, until, view_rebuilt):
    pg = fl
    with pg.expect_request(START, timeout=10_000):
        pg.evaluate(DRAG, ["[data-field='Host']", "[data-zone='groupBy']"])
    held = []
    pg.route(START, lambda route: held.append(route))   # create's; answered by hand below
    name = "hosts, made in the background"
    try:
        pg.locator(".pluginview button", has_text="Create table…").click()
        pg.wait_for_selector("#modal:not([hidden])")
        pg.locator("#modal .confirm-input").first.fill(name)
        pg.locator("#modal button", has_text="Create table").click()
        until(pg, lambda: len(held) == 1, "create's start")

        # The dialog — which covers the whole app — is gone while the table
        # is still being made, and the jobs panel carries it instead.
        assert pg.locator("#modal").is_hidden()
        creating = pg.locator("#jobsPanel .job-notice", has_text=f'Creating "{name}"')
        creating.wait_for(timeout=5_000)
        assert creating.locator("button", has_text="Cancel").count() == 1

        # The rest of the app is there to use meanwhile: a search on the
        # grid, then on to the SQL pane.
        _to_grid(pg)
        pg.click("#btnSearchToggle")
        with view_rebuilt(pg, timeout=60_000):
            pg.locator("#search").fill("H007")
        grid = "() => [__winnow.S.sourceId, __winnow.S.view.view_id]"
        before = pg.evaluate(grid)
        pg.evaluate("() => __winnow.showSqlTab()")
        pg.wait_for_function("() => __winnow.S.activeTab === 'sql'")

        held.pop().continue_()
        made = pg.locator("#jobsPanel .job-notice", has_text=f'Created "{name}"')
        made.wait_for(timeout=60_000)
        assert "800 rows" in made.inner_text().replace(",", "")   # a First and a Last per host
        # Nobody was pulled off the page they went to, and the grid behind
        # it is as they left it. The refresh that puts the new table in the
        # app's list used to re-open the table on screen, which switches to
        # the grid.
        pg.wait_for_function("() => __winnow.busyCount === 0")
        assert pg.evaluate("() => __winnow.S.activeTab") == "sql"
        assert pg.evaluate(grid) == before
        assert pg.locator("#search").input_value() == "H007"

        made.locator("button", has_text="Open table").click()
        # The table's own view, not just S.sourceId, which openSource swaps
        # before it reads anything for the table. The finally below removes
        # the table, and Open table doesn't await the open: when the removal
        # beat the open's tags read, that read's 404 ("That table is no
        # longer in this case") went uncaught and failed this test.
        pg.wait_for_function(
            """(name) => { const s = __winnow.S.sources.find((x) => x.name === name);
                           return s && __winnow.S.sourceId === s.id
                             && !!__winnow.S.view && __winnow.S.view.source_id === s.id; }""",
            arg=name, timeout=10_000)
    finally:
        pg.unroute(START)
        pg.evaluate("""async (name) => {
          const s = __winnow.S.sources.find((x) => x.name === name);
          if (!s) return;
          await fetch('/api/source/' + s.id, { method: 'DELETE', headers: { 'X-Timeline-Lite-Client': '1' } });
          __winnow.S.viewCache.delete(s.id);
          if (__winnow.S.sourceId === s.id) __winnow.S.sourceId = null;
          await __winnow.loadSources();
        }""", name)
