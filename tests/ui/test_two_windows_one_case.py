"""Two windows on one Winnow, and one of them opens another case.

A server has one case open. The other window went on showing the case it
had, and its next tag went to the server's case instead: source 1, rid 4
is a row in every case, so the tag landed on whatever line of the other
case had that rid. Reported as tags from one case appearing in the other,
on the wrong lines. Now every request names the case it is about, the
server refuses one about a case it no longer has open, and the window that
was left behind says so and offers a reload.

Its own server: opening another case on the shared one would pull it out
from under every other module (see test_case_switch.py).
"""

from __future__ import annotations

import pytest

from spawn import running_server
from winnow.store import DEFAULT_TAGS, Store

pytestmark = pytest.mark.ui

ROWS = "When,Host,Cmd\n" + "".join(f"2020-01-01 08:{i:02d}:00,H{i},cmd{i}\n" for i in range(30))


@pytest.fixture
def own_server(tmp_path):
    for name in ("alpha", "bravo"):
        (tmp_path / f"{name}.csv").write_text(ROWS, encoding="utf-8")
    bravo = tmp_path / "bravo.db-winnow"
    st = Store(str(bravo), default_tags=DEFAULT_TAGS)
    st.ingest_csv(str(tmp_path / "bravo.csv"), name="bravo.csv", build_fts=False)
    st.close()
    with running_server(tmp_path, "--case", tmp_path / "alpha.db-winnow", "--open", tmp_path / "alpha.csv",
                        "--no-fts", env={"WINNOW_IDLE_EXIT_S": "600", "WINNOW_NEVER_CONNECTED_EXIT_S": "600"},
                        cwd=tmp_path, ready="/api/sources") as srv:
        yield {"base": srv.base, "get": srv.get, "bravo": str(bravo)}


def test_a_window_left_on_the_old_case_cannot_tag_the_new_one(own_page, own_server):
    win = own_page(base=own_server["base"], viewport=(1400, 900))
    with win as left:
        left.wait_for_function("() => __winnow.S.sources.length === 1")
        # The row this window is about to tag, in case alpha.
        left.locator("#body .row").nth(3).locator(".cell").first.click()
        left.wait_for_function("() => __winnow.S.cursor === 3")

        # Another window opens bravo on the same Winnow.
        other = win.extra_page()
        other.goto(own_server["base"], wait_until="networkidle")
        other.wait_for_selector(".row")
        other.evaluate("(p) => __winnow.openCase(p)", own_server["bravo"])
        other.wait_for_function("() => __winnow.S.sources.map((s) => s.name).join() === 'bravo.csv'")

        # The left-behind window tags its selected row, as a hotkey would.
        with left.expect_response(lambda r: r.url.endswith("/api/row_tags")) as resp:
            left.evaluate("() => __winnow.applyTag(__winnow.S.tags[0], true)")
        assert resp.value.status == 409
        bravo = own_server["get"]("/api/sources")
        assert [(s["name"], s["tagged_row_count"]) for s in bravo] == [("bravo.csv", 0)], \
            "the tag meant for alpha's row landed on bravo's"

        # And it says why, over everything, with the way out.
        left.wait_for_selector("#caseGone", state="visible")
        text = left.locator("#caseGone").inner_text()
        assert "alpha" in text and "bravo" in text
        # The window that opened bravo is fine.
        assert other.locator("#caseGone").is_hidden()

        left.click("#caseGone button")
        left.wait_for_function("() => document.getElementById('brandLabel').textContent === 'bravo'")
        assert left.locator("#caseGone").is_hidden()


def test_a_windows_own_case_switch_is_not_mistaken_for_another_windows(own_page, own_server):
    """Its own open lands on the presence stream as a change of case too;
    that one is this window's, and must not put the notice up."""
    with own_page(base=own_server["base"], viewport=(1400, 900)) as page:
        page.evaluate("(p) => __winnow.openCase(p)", own_server["bravo"])
        page.wait_for_function("() => __winnow.S.sources.map((s) => s.name).join() === 'bravo.csv'")
        # Whatever the stream says about the switch arrives within this.
        page.wait_for_function("() => __winnow.pageCaseToken() !== null")
        page.wait_for_timeout(1000)   # nothing to wait FOR: the notice must not appear
        assert page.locator("#caseGone").is_hidden()
        assert page.evaluate("() => __winnow.S.sources[0].tagged_row_count") == 0
