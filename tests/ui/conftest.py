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

import collections
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

REQUIRE_BROWSER = os.environ.get("WINNOW_UI_REQUIRE_BROWSER") == "1"

if REQUIRE_BROWSER:
    from playwright.sync_api import sync_playwright   # an ImportError fails the run
else:
    sync_playwright = pytest.importorskip(
        "playwright.sync_api", reason="playwright not installed (pip install -r requirements-dev.txt)"
    ).sync_playwright


def _free_port() -> int:
    """A port the OS just told us was free. Racy in principle; in practice the
    window between closing this socket and uvicorn binding it is microseconds,
    and the alternative (a fixed port) collides with the analyst's own running
    Winnow, or with a second session on the same box — which is the far more
    likely failure here."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
def ui_csv(tmp_path_factory) -> Path:
    """Small, but shaped for the things the UI tests measure: a column whose
    *header* is far longer than its values (autofit), one whose values are far
    longer than its header (the autofit cap), and enough repetition for the
    value picker to have something to list."""
    path = tmp_path_factory.mktemp("ui") / "ui.csv"
    rows = ["Timestamp,EventId,Host,ExtremelyLongColumnHeaderName,CommandLine"]
    for i in range(200):
        rows.append(
            f"2026-03-14 08:{i // 60:02d}:{i % 60:02d},"
            f"{[4624, 4625, 4688, 1][i % 4]},"
            f"H{i % 5},"
            f"v,"
            f"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe -Enc {'A' * 300}"
        )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


class ServerLog:
    """What the shared server writes, read as it writes it. The pipe used to
    be read only if the server died during startup, so a traceback behind a
    failing test never reached its report, and a server that wrote more
    than the pipe holds (64 KB on Linux) would have blocked mid-request and
    hung the test. Keeps the last MAX_LINES; `mark()` and `since()` let a
    report take just the lines written during one test."""

    MAX_LINES = 5000

    def __init__(self, stream):
        self._lines: collections.deque[str] = collections.deque(maxlen=self.MAX_LINES)
        self._total = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream):
        for raw in iter(stream.readline, b""):
            with self._lock:
                self._lines.append(raw.decode(errors="replace").rstrip("\r\n"))
                self._total += 1

    def mark(self) -> int:
        with self._lock:
            return self._total

    def since(self, mark: int) -> list[str]:
        with self._lock:
            kept_from = self._total - len(self._lines)
            return list(self._lines)[max(mark - kept_from, 0):]

    def finish(self, timeout: float = 5) -> str:
        """Everything still held, once the process has exited."""
        self._thread.join(timeout)
        return "\n".join(self.since(0))


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
    file, like every other test in the suite."""
    global _server_log
    port = _free_port()
    case = tmp_path_factory.mktemp("case") / "ui.db"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py"), "--case", str(case),
         "--open", str(ui_csv), "--port", str(port), "--host", "127.0.0.1",
         "--no-browser", "--no-fts"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        # A subprocess can't be reached by the isolate_workspace
        # monkeypatch, so isolate it by env — otherwise every UI run
        # registers this throwaway case in the developer's real registry.
        env={**os.environ,
             "WINNOW_WORKSPACE_DIR": str(tmp_path_factory.mktemp("ws")),
             # The WINNOW_* token store: without this, Windows falls
             # through to the developer's real HKCU\Environment.
             "WINNOW_ENV_FILE": str(tmp_path_factory.mktemp("env") / "env"),
             # Same idea for the association adapters' target dirs: a
             # Settings → File associations test must never write the
             # developer's real ~/.local/share or ~/.config.
             "XDG_DATA_HOME": str(tmp_path_factory.mktemp("xdg-data")),
             "XDG_CONFIG_HOME": str(tmp_path_factory.mktemp("xdg-config")),
             # A print() to a pipe is block-buffered; the log should arrive
             # while the test that caused it is still the one running.
             "PYTHONUNBUFFERED": "1"},
    )
    log = _server_log = ServerLog(proc.stdout)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 45
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"server exited during startup:\n{log.finish()}")
        try:
            with urllib.request.urlopen(base + "/api/sources", timeout=1) as r:
                if r.status == 200:
                    break
        except (urllib.error.URLError, OSError):
            time.sleep(0.15)
    else:
        proc.kill()
        pytest.fail(f"server did not come up within 45s:\n{log.finish()}")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.finish()
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
        return json.loads(urllib.request.urlopen(req, timeout=10).read())
    return _post


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


@pytest.fixture
def page(browser, server):
    """A fresh browser context per test: localStorage is where the keymap,
    appearance and sidebar state live, so sharing one would let a test that
    rebinds a key or changes the autofit cap decide the next test's outcome."""
    ctx = browser.new_context(viewport={"width": 1500, "height": 900},
                              permissions=["clipboard-read", "clipboard-write"])
    ctx.add_init_script(FIRST_RUN_INIT)
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(server, wait_until="networkidle")
    pg.wait_for_selector(".row", timeout=30_000)
    yield pg
    # An uncaught exception in app.js doesn't fail a test on its own — the
    # assertion usually still passes, because the grid was already painted
    # before the handler threw. Surfacing it here is what makes these tests
    # catch a broken menu handler rather than just a broken layout.
    ctx.close()
    assert not errors, "uncaught JS errors: " + " | ".join(errors)


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
    column each ask the server; the answer can move Type and Operation
    and rebuilds the parameters, so a test that goes on before it lands
    can have its choices redrawn under it, or check a Type the answer
    hasn't had a chance to move yet."""
    def _wait():
        return page.wait_for_function(
            "() => { const n = document.querySelector('#modalBody .fb-help');"
            " return n && n.textContent && n.textContent !== 'Detecting format…' && n.textContent; }").json_value()
    return _wait


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
