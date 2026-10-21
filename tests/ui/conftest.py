"""Fixtures for the browser-driven UI tests.

These drive the *real* app — a real uvicorn serving a real case file, a real
Chromium rendering `static/app.js` — because the bugs they exist to catch
are ones no backend test can see. Every one of the regressions in
`test_regressions.py` shipped past a green backend suite: a CSS change that
silently unpinned the filter row from its columns, an autofit that measured
the wrong thing, a Reset button that had never worked. They're only
observable as geometry and state in a live document.

Cost and dependencies are deliberately kept off the analyst's machine.
Playwright and its Chromium live in `requirements-dev.txt` only — the
airgapped target still needs nothing but the standard library plus FastAPI —
and the whole module skips, rather than fails, when either is missing, so
`pytest` on a box without a browser stays green and honest about what it
covered. CI sets WINNOW_UI_REQUIRE_BROWSER=1, which turns that skip into a
failure: there a missing browser means the required `browser` check would
pass having run nothing.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import pytest

from spawn import ServerLog, spawn_server

REQUIRE_BROWSER = os.environ.get("WINNOW_UI_REQUIRE_BROWSER") == "1"

if REQUIRE_BROWSER:
    from playwright.sync_api import sync_playwright   # an ImportError fails the run
else:
    sync_playwright = pytest.importorskip(
        "playwright.sync_api", reason="playwright not installed (pip install -r requirements-dev.txt)"
    ).sync_playwright


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # no browser binary installed
            msg = f"chromium unavailable ({e}); run `playwright install chromium`"
            if REQUIRE_BROWSER:
                pytest.fail(msg, pytrace=False)
            pytest.skip(msg)
        yield b
        b.close()


@pytest.fixture(scope="session")
def scrollbar_browser(browser):
    """A second Chromium, one that draws its scrollbars. Playwright launches
    with --hide-scrollbars, which takes every scrollbar's thickness to zero
    — the ::-webkit-scrollbar ones the grid draws included — so in
    `browser` a scrollbar can be neither seen nor grabbed, and a test about
    where the grid's scrollbar is, what it covers or what colour it is
    passes whatever the stylesheet says. Open the page here instead:

        with own_page(browser=scrollbar_browser) as pg:
            ...
    """
    b = browser.browser_type.launch(ignore_default_args=["--hide-scrollbars"])
    yield b
    b.close()


@pytest.fixture(scope="session")
def ui_csv(tmp_path_factory) -> Path:
    """Small, but shaped for the things the UI tests measure: a column whose
    *header* is far longer than its values (autofit), one whose values are far
    longer than its header (the autofit cap), enough repetition for the
    value picker to have something to list, and a column of JSON, so the
    detail pane's structured view and the extract-a-field flows have
    something to take apart."""
    path = tmp_path_factory.mktemp("ui") / "ui.csv"
    rows = ["Timestamp,EventId,Host,ExtremelyLongColumnHeaderName,CommandLine,Payload"]
    for i in range(200):
        payload = json.dumps({"user": f"u{i % 3}", "proc": {"pid": 4000 + i, "name": "cmd.exe"}})
        rows.append(
            f"2026-03-14 08:{i // 60:02d}:{i % 60:02d},"
            f"{[4624, 4625, 4688, 1][i % 4]},"
            f"H{i % 5},"
            f"v,"
            f"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe -Enc {'A' * 300},"
            '"' + payload.replace('"', '""') + '"'
        )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


# The shared server's log, for pytest_runtest_makereport. Set by `server`.
_server_log: ServerLog | None = None
_LOG_MARK = pytest.StashKey[int]()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    item.stash[_LOG_MARK] = _server_log.mark() if _server_log else 0


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """A failing UI test's report carries what the server logged while it
    ran: when a UI test fails it is usually the server behind it."""
    outcome = yield
    rep = outcome.get_result()
    if not rep.failed or _server_log is None:
        return
    time.sleep(0.3)   # uvicorn logs a handler's traceback after sending the 500
    lines = _server_log.since(item.stash.get(_LOG_MARK, 0))
    if lines:
        rep.sections.append(("Captured server log", "\n".join(lines)))


@pytest.fixture(scope="session")
def server(tmp_path_factory, ui_csv):
    """One server for the whole UI session — startup is ~1s. Every module
    shares its case, so each one leaves the case as it found it, and
    case_left_as_found below holds them to that. Uses its own tmp case
    file, like every other test in the suite, and is isolated from the
    developer's real workspace the way every spawned server is (see
    tests/spawn.py)."""
    global _server_log
    tmp = tmp_path_factory.mktemp("server")
    srv = spawn_server(tmp, "--case", tmp / "ui.db", "--open", ui_csv, "--no-fts",
                       ready="/api/sources")
    _server_log = srv.log
    yield srv.base
    srv.stop()
    _server_log = None


@pytest.fixture(scope="session")
def server_post(server):
    """POST JSON to the shared server from OUTSIDE the page — for
    module-scoped setup (toggling a plugin, a case setting) that has no
    `page` to go through. Carries the client header every non-GET route
    requires. One copy here rather than one per test module."""
    def _post(route, body):
        req = urllib.request.Request(
            server.rstrip("/") + route, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    return _post


@pytest.fixture(scope="session")
def server_call(server):
    """server_post's other half: a GET, or a bodiless DELETE, from outside
    the page."""
    def _call(route, method="GET"):
        req = urllib.request.Request(server.rstrip("/") + route, method=method,
                                     headers={"X-Timeline-Lite-Client": "1"})
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    return _call


@pytest.fixture
def restore_plugin_overrides(server, server_post):
    """For a test that applies a profile: applying gives every plugin an
    explicit on/off override for this case, which every later module in
    the shard would inherit. Puts back the override each plugin had."""
    def overrides():
        with urllib.request.urlopen(server.rstrip("/") + "/api/plugins", timeout=10) as r:
            return {p["fs_name"]: p["case_override"] for p in json.loads(r.read())["plugins"]}
    before = overrides()
    yield
    scope = {None: "follow_case", True: "on_case", False: "off_case"}
    for name, now in overrides().items():
        if now != before.get(name):
            server_post("/api/plugins/toggle", {"fs_name": name, "scope": scope[before.get(name)]})


# The bundled tab plugins now restore what they had from the case file
# (plugin_ui_state, via winnow.tabState), and the case here is shared by
# the whole session — so one module's grouping would arrive in the next
# module's supposedly fresh tab, complete with a restore banner. Every
# module starts from nothing saved and leaves nothing behind; the module
# that tests restoring does its own saving inside that window.
@pytest.fixture(autouse=True, scope="module")
def clean_plugin_tab_state(server_post):
    keys = ("tab:first-last.firstlast", "tab:pivot-table.pivot")
    for key in keys:
        server_post("/api/plugin_state", {"key": key, "payload": None})
    yield
    for key in keys:
        server_post("/api/plugin_state", {"key": key, "payload": None})


def _case_state(server):
    """What one UI module may not leave behind for the next, as a set of
    labels, so that two snapshots diff into exactly what changed."""
    def get(route):
        with urllib.request.urlopen(server.rstrip("/") + route, timeout=30) as r:
            return json.loads(r.read())

    # An import a module queued is part of what it leaves, even when it is
    # still running as the module ends.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and any(
            j["status"] in ("queued", "running") for j in get("/api/ingest/jobs")["jobs"]):
        time.sleep(0.1)
    tags = {str(t["id"]): t["name"] for t in get("/api/tags")["tags"]}
    state = {f"tag {name!r}" for name in tags.values()}
    for src in get("/api/sources"):
        state.add(f"table {src['name']!r}")
        for tag_id, n in get(f"/api/tags?source_id={src['id']}")["counts"].items():
            if n:
                state.add(f"{n} row(s) of {src['name']!r} tagged {tags.get(tag_id, tag_id)!r}")
        state |= {f"derived column {d['name']!r} on {src['name']!r}"
                  for d in get(f"/api/derived?source_id={src['id']}")}
    for kind, route, key in (("merge", "/api/merges", "name"),
                             ("folder", "/api/folders", "name"),
                             ("case variable", "/api/case/variables", "name"),
                             ("watchlist indicator", "/api/watchlist", "value"),
                             ("dashboard", "/api/dashboards", "name"),
                             ("saved filter", "/api/saved_filters", "name"),
                             ("session file", "/api/sessions", "name")):
        state |= {f"{kind} {d[key]!r}" for d in get(route)}
    state |= {f"case session {d['name']!r}" for d in get("/api/case_sessions")["sessions"]}
    # A plugin left on puts its tab, routes and formats in every later module.
    state |= {f"plugin {p['fs_name']!r} {'on' if p['enabled'] else 'off'}, case override {p['case_override']!r}"
              for p in get("/api/plugins")["plugins"]}
    return state


# The server above is shared by every module in a shard, and so is its
# case. A module that left a table, a tag or a dashboard behind used to
# surface as a failure in whichever unrelated module the shard dealt next,
# and only in the full run (#227). This fails the module that leaves it,
# at its own teardown, wherever the shard puts it.
@pytest.fixture(autouse=True, scope="module")
def case_left_as_found(server):
    before = _case_state(server)
    yield
    after = _case_state(server)
    left, gone = sorted(after - before), sorted(before - after)
    assert not (left or gone), (
        "this module changed the case every later module in the shard shares; clean up in a finally:"
        + "".join(f"\n  left behind: {x}" for x in left)
        + "".join(f"\n  removed: {x}" for x in gone))


# Every shared context is a "first run on this machine" — pre-answer the
# one-time remote-mode prompt so it can't overlay the app mid-test, and
# turn the launch animation off. The splash covers the whole viewport
# for several seconds, which every click in every test would otherwise
# wait out. test_first_run_prompt.py and test_splash.py build their own
# contexts without this.
# Two appearance keys are pinned here rather than left to whatever ships,
# so that a default moving cannot quietly change what the modules below
# are pointing at. Each has a module of its own that turns the other
# surface on for itself.
# `pagesMenu: false`: most tests here click the individual
# #tabSql/#tabTimeline nodes, which the Pages dropdown hides — see
# tests/ui/test_pages_dropdown.py.
# `filterUi: 'row'`: most tests type into `.fcell input[data-col=…]`,
# which exists for every column only under the classic filter row; under
# the bar it exists for a column whose box has been opened — see
# tests/ui/test_filter_bar.py.
# Both happen to match what ships today. They are stated anyway, because
# each of them has moved once already and it was these modules that broke.
# A test that builds its own context to seed something more takes the
# `first_run_init` fixture and appends to this, so the seed stays one.
FIRST_RUN_INIT = ("localStorage.setItem('winnow.remotePrompt', 'seen');"
                  "localStorage.setItem('winnow.appearance',"
                  " JSON.stringify({ splash: false, pagesMenu: false, filterUi: 'row' }));"
                  # The sidebar defaults CLOSED now; most tests predate
                  # that and address rows in it, so the shared context
                  # models an analyst who chose to keep it open.
                  "localStorage.setItem('winnow.sidebar',"
                  " JSON.stringify({ collapsed: false }))")


@pytest.fixture
def first_run_init():
    return FIRST_RUN_INIT


class OwnPage:
    """A page in a browser context of its own, for what the shared `page`
    can't give a test: another viewport or localStorage, a page that lasts
    a whole module, a second server.

        with own_page(viewport=(1100, 700)) as pg:
            ...

    Like `page`, it fails on an uncaught JS error when it closes. Most of
    the hand-built contexts it replaced never listened for one, so a
    handler that threw in them still passed.

    `seed` is the localStorage the page starts with: FIRST_RUN_INIT unless
    the test is about another one ("" for a browser that has never seen
    Winnow). `init` is added after it. `route` is a (pattern, handler)
    pair set on the context before anything loads. The page loads `base`
    (the shared server unless given) and waits for `ready`; `ready=None`
    waits for nothing more than the load, and `goto=False` doesn't load at
    all. `extra_page()` opens another tab in the same context, checked the
    same way. Anything else goes to browser.new_context."""

    def __init__(self, browser, base, *, seed=FIRST_RUN_INIT, init="", viewport=(1500, 900),
                 route=None, goto=True, wait_until="networkidle", ready=".row",
                 ready_timeout=30_000, **context):
        self.browser, self.base = browser, base
        self.script = ";".join(s for s in (seed, init) if s)
        self.viewport, self.route, self.goto, self.wait_until = viewport, route, goto, wait_until
        self.ready, self.ready_timeout = ready, ready_timeout
        self.context = context
        self.errors: list[str] = []

    def extra_page(self):
        pg = self.ctx.new_page()
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        return pg

    def __enter__(self):
        w, h = self.viewport
        self.ctx = self.browser.new_context(viewport={"width": w, "height": h}, **self.context)
        if self.script:
            self.ctx.add_init_script(self.script)
        if self.route:
            self.ctx.route(*self.route)
        self.page = self.extra_page()
        if self.goto:
            self.page.goto(self.base, wait_until=self.wait_until)
            if self.ready:
                self.page.wait_for_selector(self.ready, timeout=self.ready_timeout)
        return self.page

    def __exit__(self, kind, *_):
        self.ctx.close()
        if kind is None:
            assert not self.errors, "uncaught JS errors: " + " | ".join(self.errors)


@pytest.fixture(scope="session")
def own_page(browser, server):
    """OwnPage against the shared server; pass `base=` for another one,
    and `browser=` for another browser (scrollbar_browser)."""
    shared = browser

    def _make(*, base=None, browser=None, **kw):
        return OwnPage(browser or shared, base or server, **kw)
    return _make


@pytest.fixture(scope="session")
def first_last_page(own_page, server_post):
    """The First/Last plugin switched on for every case and its page open,
    in an OwnPage; switched off again on the way out.

        with first_last_page() as pg:
            ...

    `open_tab=False` stops short of clicking the tab, for a test about what
    the tab restores when it opens. Everything else goes to own_page."""
    @contextlib.contextmanager
    def _open(*, open_tab=True, **kw):
        server_post("/api/plugins/toggle", {"fs_name": "first_last", "scope": "on_all"})
        try:
            with own_page(**kw) as pg:
                pg.evaluate("() => __winnow.loadPlugins()")
                pg.wait_for_function(
                    "() => __winnow.S.pluginTabs.some((t) => t.id.includes('firstlast'))",
                    timeout=10_000)
                if open_tab:
                    pg.locator(".tab-plugin", has_text="First/Last").click()
                    pg.wait_for_selector("[data-zone='groupBy']", timeout=10_000)
                yield pg
        finally:
            server_post("/api/plugins/toggle", {"fs_name": "first_last", "scope": "off_all"})
    return _open


def _new_fl_sheet(pg):
    # A NEW sheet, rather than "Start fresh". Sheet definitions live in the
    # case file and the case is shared with every other module in this
    # directory, so wiping them is somebody else's test failing later —
    # test_firstlast_state.py is about exactly that state. A new sheet is
    # the plugin's own way to get an empty working surface and leaves the
    # saved ones alone.
    before = pg.locator(".sql-tab").count()
    pg.locator(".sql-tab", has_text="+").click()
    pg.wait_for_function("(n) => document.querySelectorAll('.sql-tab').length > n",
                         arg=before, timeout=10_000)
    pg.wait_for_function(
        "() => document.querySelectorAll(\"[data-zone='groupBy'] [data-field]\").length === 0",
        timeout=10_000)


@pytest.fixture
def fl_new_sheet():
    """Open a new, empty First/Last sheet on a page already showing it."""
    return _new_fl_sheet


@pytest.fixture
def fl_sheet(first_last_page):
    """A First/Last page of its own per test, on a new, empty sheet: the
    zones are sheet state, and these tests are about what one gesture does
    to an empty sheet."""
    with first_last_page() as pg:
        _new_fl_sheet(pg)
        yield pg


@pytest.fixture(scope="module")
def fl_page(first_last_page):
    """One First/Last page for a whole module. The plugin's auto-run
    setting is cleared on every load, so a module that turns it on and
    reloads gets its own choice back only by setting it again."""
    with first_last_page(init="localStorage.removeItem('winnow.firstlast.auto')",
                         permissions=["clipboard-read", "clipboard-write"]) as pg:
        yield pg


@pytest.fixture
def page(browser, server):
    """A fresh browser context per test: localStorage is where the keymap,
    appearance and sidebar state live, so sharing one would let a test that
    rebinds a key or changes the autofit cap decide the next test's outcome.

    An uncaught exception in app.js fails the test on the way out (see
    OwnPage). It wouldn't on its own, because the assertion usually still
    passes: the grid was already painted before the handler threw. That
    is what makes these tests catch a broken menu handler rather than just
    a broken layout."""
    with OwnPage(browser, server, permissions=["clipboard-read", "clipboard-write"]) as pg:
        yield pg


@pytest.fixture
def api(page):
    """JSON call against the shared server from inside the page, carrying
    the CSRF header every non-GET /api/* route requires.

    A non-2xx answer raises rather than coming back as `{"detail": ...}`.
    It used to come back, and three modules' cleanup deleted their tables
    through a route that does not exist: every one of those 404s read as
    a dict, so the tables stayed in the case every later module shares
    (#227). A test that means to provoke an error passes `expect=` with
    the status it wants."""
    def _api(path, method="GET", body=None, expect=None):
        status, text = page.evaluate(
            """([path, method, body]) => fetch(path, { method,
                  headers: { 'X-Timeline-Lite-Client': '1', 'Content-Type': 'application/json' },
                  body: body == null ? undefined : JSON.stringify(body) })
                .then(async (r) => [r.status, await r.text()])""",
            [path, method, body])
        if expect is None:
            if not 200 <= status < 300:
                raise AssertionError(f"{method} {path} -> {status}: {text[:500]}")
        elif status != expect:
            raise AssertionError(f"{method} {path} -> {status}, expected {expect}: {text[:500]}")
        return json.loads(text) if text else None
    return _api


@pytest.fixture
def until():
    """Wait, bounded, for something wait_for_function can't see: a list a
    route handler fills, an answer only the server has.

        until(page, lambda: len(held) == 2, "both previews held")

    It polls with the page's own wait_for_timeout rather than time.sleep,
    because route handlers only run while Playwright is waiting on the
    page: a test sleeping in Python would never see the list grow."""
    def _until(page, probe, what, timeout=10.0):
        deadline = time.monotonic() + timeout
        while not probe():
            if time.monotonic() >= deadline:
                pytest.fail(f"timed out waiting for {what}")
            page.wait_for_timeout(25)
    return _until


@pytest.fixture
def painted():
    """Wait for two animation frames: whatever a change put off to the
    next frame (the grid's repaint on scroll, a chart drawing its bars)
    has drawn by the end of the second.

        page.keyboard.press("Control+ArrowRight")
        painted(page)

    One frame is not enough. A scroll handler asks for its frame during
    the frame the scroll lands in, which can be after a callback the test
    asked for first; the second frame comes after both."""
    def _painted(page):
        page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
    return _painted


class WheelSettled:
    """See the `wheel_settled` fixture."""

    def __init__(self, page, selector="#body"):
        self.page, self.selector = page, selector

    def __enter__(self):
        self.page.evaluate("(sel) => { const s = document.querySelector(sel);"
                           " window.__wheelWatch = { at: s.scrollTop + ',' + s.scrollLeft, moved: false, still: 0, frames: 0 }; }",
                           self.selector)
        return self

    def __exit__(self, kind, *_):
        try:
            if kind is None:
                self.page.wait_for_function("""(sel) => {
                  const s = document.querySelector(sel), w = window.__wheelWatch;
                  const at = s.scrollTop + ',' + s.scrollLeft;
                  w.frames++;
                  if (at !== w.at) { w.at = at; w.moved = true; w.still = 0; } else w.still++;
                  return w.moved ? w.still >= 5 : w.frames >= 30;
                }""", arg=self.selector, polling="raf")
        finally:
            self.page.evaluate("() => { delete window.__wheelWatch; }")


@pytest.fixture
def wheel_settled():
    """Wait for a wheel to finish scrolling:

        with wheel_settled(page, "#body"):
            page.mouse.wheel(0, 100)

    mouse.wheel returns before the scroll has started, and a smooth scroll
    then animates over several frames. This waits until the element has
    moved and held still for five frames, or, for a wheel with nowhere to
    go, until thirty frames have passed without it moving."""
    return WheelSettled


class Copied:
    """See the `copied` fixture."""
    MARK = "⁣nothing copied yet"

    def __init__(self, page, timeout=5.0):
        self.page, self.timeout, self.text = page, timeout, None

    def __enter__(self):
        self.page.evaluate("(m) => navigator.clipboard.writeText(m)", self.MARK)
        return self

    def __exit__(self, kind, *_):
        if kind is not None:
            return
        deadline = time.monotonic() + self.timeout
        while (text := self.page.evaluate("() => navigator.clipboard.readText()")) == self.MARK:
            if time.monotonic() >= deadline:
                pytest.fail("nothing reached the clipboard")
            self.page.wait_for_timeout(25)
        self.text = text


@pytest.fixture
def copied():
    """What a gesture put on the clipboard:

        with copied(page) as clip:
            page.keyboard.press("Control+c")
        assert clip.text == "..."

    navigator.clipboard.writeText resolves after the handler that called
    it has returned, so a read straight after the gesture can still find
    whatever was there before it. Entering puts a marker on the clipboard
    and leaving waits for the copy to replace it. wait_for_function can't
    do this: it doesn't wait for a promise, and readText returns one."""
    return Copied


class Rebuilt:
    """See the `view_rebuilt` fixture."""

    def __init__(self, page, timeout=15_000):
        self.page, self.timeout = page, timeout

    def __enter__(self):
        self.before = self.page.evaluate("() => __winnow.S.view && __winnow.S.view.view_id")
        return self

    def __exit__(self, kind, *_):
        if kind is None:
            self.page.wait_for_function(
                "(was) => __winnow.S.view && __winnow.S.view.view_id !== was && __winnow.busyCount === 0",
                arg=self.before, timeout=self.timeout)


@pytest.fixture
def view_rebuilt():
    """Wait for the grid to land on a new view after a gesture that
    filters, sorts or searches:

        with view_rebuilt(page):
            page.click(".vp-actions .btn:has-text('Apply')")
        assert page.evaluate("() => __winnow.S.view.row_count") == 100

    It waits for the change rather than for the expected answer, so a
    wrong answer fails on its own assertion instead of as a timeout."""
    return Rebuilt


@pytest.fixture
def tag_written():
    """Wait for the write a tag gesture sends:

        with tag_written(page):
            page.keyboard.press("1")

    Either of the two routes a tag key can take: picked rows by id, or a
    whole view less its exclusions."""
    def _expect(page, timeout=15_000):
        return page.expect_response(
            lambda r: r.request.method == "POST"
            and urlparse(r.url).path in ("/api/row_tags", "/api/row_tags/view"), timeout=timeout)
    return _expect


@pytest.fixture
def layout_saved(page, api, until):
    """Wait for the open table's layout (widths, pins, hidden columns,
    order, hide-empty-rows) to reach the server. saveLayout waits 400 ms
    for the changes to stop before it posts, so a test that reopens or
    reloads straight after one reads back the layout from before it, and
    a cleanup that ends without waiting leaves its save to land in the
    middle of the next test."""
    def _wait():
        sid, want = page.evaluate("""() => [__winnow.S.sourceId, JSON.stringify({ columns: __winnow.S.layout,
          order: __winnow.S.order, hide_empty_rows: __winnow.S.hideEmptyRows })]""")
        want = json.loads(want)

        def landed():
            got = api(f"/api/layout?source_id={sid}")
            return all(got.get(k) == v for k, v in want.items())
        until(page, landed, "the layout to be saved")
    return _wait


@pytest.fixture
def format_detected(page):
    """Wait for the Add derived column dialog's format detection to
    answer, and return what it said. Opening the dialog and changing its
    column each ask the server; the answer can pick the timestamp format
    and rebuilds the parameters, so a test that goes on before it lands
    can have its choices redrawn under it. The dialog copies the note's
    text onto the body as data-detected once the answer is in, and
    clears it while a new one is on its way."""
    def _wait():
        return page.wait_for_function(
            "() => { const b = document.getElementById('modalBody');"
            " return (b && b.dataset.detected) || false; }").json_value()
    return _wait


@pytest.fixture
def derive_pick(page):
    """derive_pick(entry, fmt=None): choose what to make in the Add
    derived column dialog — click `entry` (its label, as the side list
    shows it: "Regex capture", "Parse a timestamp") and, for the
    timestamp entry, pick `fmt` (a format's label) from the Format list.
    Clicked rather than set, so a list that stopped answering clicks
    fails here."""
    def _pick(entry, fmt=None):
        item = page.locator("#modalBody .side-rail-item", has=page.locator(
            ".side-rail-label", has_text=re.compile("^" + re.escape(entry) + "$")))
        item.click()
        page.wait_for_selector(f'#modalBody .side-rail-item[aria-selected="true"] >> text="{entry}"')
        if fmt is not None:
            page.locator('#modalBody select[data-role="format"]').select_option(label=fmt)
    return _pick


@pytest.fixture
def derive_preview(page):
    """Wait for the Add derived column dialog's preview to show the
    answer to the latest change, and return its text. While a request is
    on its way the previous answer stays on screen (dimmed, aria-busy),
    so waiting for .derived-preview-row alone can return the stale rows."""
    def _wait(timeout=15_000):
        page.wait_for_function(
            "() => { const p = document.querySelector('#modalBody .derived-preview');"
            " return !!p && p.getAttribute('aria-busy') === 'false'; }", timeout=timeout)
        return page.locator("#modalBody .derived-preview").inner_text()
    return _wait


def _open_sql(page):
    page.click("#tabSql")
    page.wait_for_selector("#sqlview:not([hidden])")
    # The tab list (and the starter query it seeds into the editor) loads
    # async: filling or reading the editor before that lands gets the
    # wrong text.
    page.wait_for_function(
        "() => __winnow.S.sqlTabs.length > 0 && !document.getElementById('sqlText').disabled")


@pytest.fixture
def open_sql():
    """open_sql(page): show the SQL pane, ready to type into."""
    return _open_sql


@pytest.fixture
def row_menu(page):
    """Right-click a grid cell and wait for the row menu (the root, not a
    flyout). Returns the root locator. One gesture for every row-menu
    test, so a change to how it opens is one edit."""
    def _open(row=2, cell=2):
        page.locator(".row").nth(row).locator(".cell").nth(cell).click(button="right")
        page.wait_for_selector(".menu:not(.menu-sub)")
        return page.locator(".menu:not(.menu-sub)")
    return _open


@pytest.fixture
def flyout(page):
    """Click a submenu parent in the open root menu by label and wait for
    its flyout. Returns the flyout locator."""
    def _open(label):
        page.locator(".menu:not(.menu-sub) .menu-item-sub", has_text=label).click()
        page.wait_for_selector(".menu-sub")
        return page.locator(".menu-sub")
    return _open


@pytest.fixture
def fake_plugin_mount(page):
    """A plugin tab that exists in client state only — 'fake.t' from a
    plugin whose folder and display name are both 'fake' — plus the
    `winnow` context object its mount() would receive, on `window.__ctx`.
    Tests drive the context (notify, showTab, dialogs) without a plugin
    on disk; showTab lands on a view whose module 404s, which is fine,
    the assertion is on where the app went. Everything is taken away
    again after the test."""
    page.evaluate("() => { __winnow.S.pluginTabs = [{ id: 'fake.t', plugin: 'fake', plugin_fs: 'fake', "
                  "label: 'Fake', entry: 'ui/tab.js', description: 'fake', gen: 0 }]; __winnow.renderPageTabs(); "
                  "window.__ctx = __winnow.buildPluginTabContext(__winnow.S.pluginTabs[0], 'tab'); }")
    yield
    page.evaluate("() => { __winnow.disposePluginMount('tab:fake.t'); __winnow.S.pluginTabs = []; "
                  "__winnow.resetPluginTabMounts(); __winnow.showGridTab(); __winnow.renderPageTabs(); delete window.__ctx; }")


@pytest.fixture
def fake_row_action(page):
    """Register a stand-in plugin row action ('Look up on VT' from a
    plugin whose folder and id are both 'demo' / 'vt' — the pair the pin
    key is built from) straight into client state, and take it away
    again after the test. `max_rows` sets where the entry greys out."""
    def _register(max_rows=50):
        page.evaluate("(n) => { __winnow.S.pluginRowActions = [{ id: 'demo.vt', local_id: 'vt', plugin: 'demo', "
                      "plugin_fs: 'demo', label: 'Look up on VT', description: 'demo', max_rows: n }]; }", max_rows)
    yield _register
    page.evaluate("() => { __winnow.S.pluginRowActions = []; }")
