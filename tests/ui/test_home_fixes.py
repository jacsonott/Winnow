"""Home / quick-look fixes: "Open existing case file…" has a Browse…
picker, new-case paths join with the directory's own separator (no
C:\\Cases/x on Windows), the quick-look banner spans the bar and stays put
when the sidebar toggles, and a fresh origin adopts the machine's saved
look instead of the default skin."""

from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = pytest.mark.ui


def _post(server, route, body):
    req = urllib.request.Request(
        server.rstrip("/") + route, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Timeline-Lite-Client": "1"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def test_join_path_uses_the_directory_s_own_separator(page):
    jp = lambda d, n: page.evaluate("([d, n]) => __winnow.joinPath(d, n)", [d, n])
    assert jp("C:\\Cases", "acme.db-winnow") == "C:\\Cases\\acme.db-winnow"
    assert jp("C:\\Cases\\", "acme.db-winnow") == "C:\\Cases\\acme.db-winnow"
    assert jp("/srv/cases", "acme.db-winnow") == "/srv/cases/acme.db-winnow"
    assert jp("cases", "x.db-winnow") == "cases/x.db-winnow"


def test_open_existing_case_has_a_browse_button(page):
    page.evaluate("() => __winnow.openExistingCasePrompt()")
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator("#modalTitle").inner_text().lower() == "open existing case file"
    assert page.locator("#modal button", has_text="Browse…").count() == 1
    page.locator("#modal button", has_text="Browse…").click()
    # The server-disk picker, in file mode.
    page.wait_for_selector("#modal:not([hidden])")
    assert "add files" in page.locator("#modalTitle").inner_text().lower()
    page.locator("#modal button", has_text="Cancel").click()
    page.wait_for_function("() => /open existing/i.test(document.getElementById('modalTitle').textContent)")
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")


def test_quicklook_banner_spans_the_bar_and_ignores_the_sidebar(page):
    page.evaluate("() => __winnow.paintTempBanner(true)")
    page.wait_for_selector("#tempBanner:not([hidden])")
    box = page.locator("#tempBanner").bounding_box()
    vw = page.evaluate("() => window.innerWidth")
    assert box["x"] < 1 and box["width"] >= vw - 2          # full width, not column 2
    page.locator("#btnTabJump").click()                      # hide the sidebar
    page.wait_for_selector("#sidebar", state="hidden")
    box2 = page.locator("#tempBanner").bounding_box()
    assert abs(box2["x"] - box["x"]) < 1 and abs(box2["width"] - box["width"]) < 1
    page.locator("#btnTabJump").click()
    page.wait_for_selector("#sidebar", state="visible")
    page.evaluate("() => __winnow.paintTempBanner(false)")


def test_fresh_origin_adopts_the_machine_s_saved_look(browser, server):
    before = _post(server, "/api/settings/app", {})   # current, unchanged
    _post(server, "/api/settings/app", {"appearance": {
        "style": "phosphor", "themeMode": "dark", "accent": "#39e881",
        "accentCustomized": False, "splash": False}})
    ctx = browser.new_context(viewport={"width": 1200, "height": 800})
    # No stored appearance at all — a brand-new origin, like a quick-look.
    ctx.add_init_script("localStorage.setItem('winnow.remotePrompt', 'seen')")
    pg = ctx.new_page()
    try:
        pg.goto(server, wait_until="networkidle")
        pg.wait_for_function("() => document.documentElement.getAttribute('data-style') === 'phosphor'",
                             timeout=10_000)
        assert pg.evaluate("() => __winnow.S.appearance.style") == "phosphor"
    finally:
        ctx.close()
        # An origin WITH its own stored look keeps it (the shared fixture
        # contexts store one) — and put the machine back as we found it.
        restore = before.get("appearance") or {"style": "harvest", "themeMode": "dark",
                                                "accent": "#d9a441", "accentCustomized": False, "splash": False}
        _post(server, "/api/settings/app", {"appearance": restore})


def test_double_clicking_a_case_card_opens_it_once(page):
    """A double-click on a case card is two clicks, and each one used to
    POST /api/case/open: two opens racing on the server (which left a
    Store open behind the switch, and the case reading as "open in another
    Winnow") and two state resets interleaving here. The open is held in
    flight until the second click has landed, so the second click cannot
    miss the first by being slower than the server."""
    page.evaluate("() => { __winnow.showHome(); return __winnow.refreshCases(); }")
    path = page.evaluate("() => fetch('/api/case/current').then((r) => r.json()).then((c) => c.path)")
    card = page.locator("#home .home-case-row", has_text=path).locator(".home-case-main")
    card.wait_for()
    held = []
    page.route("**/api/case/open", lambda route: held.append(route))
    try:
        card.dblclick()
        deadline = 20
        while not held and deadline:
            page.wait_for_timeout(50)
            deadline -= 1
        assert len(held) == 1
        # A second POST from the second click would arrive within moments;
        # give it a generous window to show up rather than looking once.
        page.wait_for_timeout(500)
        assert len(held) == 1, f"{len(held)} opens for one double-click"
    finally:
        # Released before unrouting: unroute settles a still-held route
        # itself, and continuing it afterwards is an error.
        for r in held:
            r.continue_()
        page.unroute("**/api/case/open")
    page.wait_for_selector("#app:not([hidden])")
    page.wait_for_function("() => __winnow.S.view")
    page.wait_for_selector(".row")


def test_open_existing_refuses_a_path_with_no_case_there(page, tmp_path):
    """Given a typo, or a case on a share that wasn't mounted, "Open
    existing case file…" created an empty case at that path and opened it:
    the analyst's case looked like one with no tables. It says there is no
    case file there and creates nothing — that is + New case's job."""
    missing = tmp_path / "Shares" / "acme.db-winnow"
    page.evaluate("() => __winnow.openExistingCasePrompt()")
    page.wait_for_selector("#modal:not([hidden])")
    page.locator("#modalBody input").first.fill(str(missing))
    page.locator("#modalBody .btn", has_text="Open").click()
    page.wait_for_function("() => /No case file at/.test(document.getElementById('toast').textContent)")
    assert not (tmp_path / "Shares").exists()
    cases = page.evaluate("() => fetch('/api/cases').then((r) => r.json())")
    assert str(missing) not in [c["path"] for c in cases]
    # Still asking, path and all, so the typo can be fixed in place.
    page.wait_for_selector("#modal:not([hidden])")
    assert page.locator("#modalBody input").first.input_value() == str(missing)
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")


DEFAULT_CASES = "/srv/winnow/cases"


def test_first_run_names_the_default_cases_folder_and_stores_that_one(page):
    """"Use the default" posted 'cases', which the server resolved against
    the directory it was started from rather than the install — and the
    prompt only said "next to the server". It names the folder now, and
    what it names is what it stores. /api/prefs is answered here, so the
    shared server's own setting is never touched."""
    posted = []

    def prefs(route):
        if route.request.method == "POST":
            body = json.loads(route.request.post_data)
            posted.append(body)
            route.fulfill(json={"cases_dir": body.get("cases_dir"),
                                "default_cases_dir": DEFAULT_CASES, "first_run": False})
        else:
            route.fulfill(json={"cases_dir": None, "default_cases_dir": DEFAULT_CASES,
                                "first_run": True})

    page.route("**/api/prefs", prefs)
    try:
        page.evaluate("() => { __winnow.maybeOfferStorageDir(); }")
        page.wait_for_selector(".confirm-overlay")
        assert DEFAULT_CASES in page.locator(".confirm-card").inner_text()
        page.locator(".confirm-card .btn", has_text="Use the default").click()
        page.wait_for_function("(d) => __winnow.S.casesDir === d", arg=DEFAULT_CASES)
        assert posted == [{"cases_dir": DEFAULT_CASES}]
    finally:
        page.unroute("**/api/prefs")


def test_a_new_case_goes_in_the_default_folder_when_none_was_chosen(page):
    """An install with cases but no cases folder set (one from before the
    setting) never sees the first-run prompt, and New case offered the
    relative cases/<name> — created wherever the server was started."""
    page.route("**/api/prefs", lambda route: route.fulfill(json={
        "cases_dir": None, "default_cases_dir": DEFAULT_CASES, "first_run": False}))
    try:
        page.evaluate("() => __winnow.maybeOfferStorageDir()")
    finally:
        page.unroute("**/api/prefs")
    page.evaluate("() => __winnow.openNewCaseModal({ name: 'Acme' })")
    page.wait_for_selector("#modal:not([hidden])")
    values = page.evaluate("() => [...document.querySelectorAll('#modalBody input')].map((i) => i.value)")
    assert DEFAULT_CASES + "/acme.db-winnow" in values, values
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")
