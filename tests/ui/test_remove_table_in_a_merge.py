"""The Tables manager refuses to remove a table a merge is built on.

The server refuses it too (409). Asking on the client as well is so the
analyst gets the reason and the merge's NAME without a round trip, and so
the Remove button never appears to do nothing.

Refusing rather than warning-and-proceeding is the maintainer's call, and
the reason is in the store: source ids are reused by the next import, so
dropping a member does not merely break the merge — the next file
imported takes the freed id and silently becomes part of a merge nobody
added it to.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.ui


@pytest.fixture
def merged(page, server, server_post, tmp_path):
    """A second table and a merge over it and the fixture table, torn down
    afterwards — the UI suite shares one case file."""
    f = tmp_path / "second.csv"
    f.write_text("Timestamp,EventId,Host,ExtremelyLongColumnHeaderName,CommandLine\n"
                 "2026-03-14 09:00:00,4624,H9,v,cmd.exe\n", encoding="utf-8")
    server_post("/api/ingest/jobs/path", {"path": str(f), "name": "second.csv", "kind": "csv"})
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        names = page.evaluate(
            "() => __winnow.loadSources().then(() => __winnow.S.sources.map((s) => s.name))")
        if "second.csv" in names:
            break
        time.sleep(0.25)
    else:
        pytest.fail("second.csv never appeared")
    ids = page.evaluate("""() => __winnow.S.sources.filter((s) => !s.is_merge)
        .filter((s) => ['ui.csv', 'second.csv'].includes(s.name)).map((s) => s.id)""")
    merge = page.evaluate("""async (ids) => {
        const m = await __winnow.post('/api/merges',
          { name: 'Merged for the test', source_ids: ids });
        // The client's own refusal reads S.sources; a merge the browser
        // has not loaded yet is a merge it cannot warn about.
        await __winnow.loadSources();
        return m;
      }""", ids)
    assert page.evaluate("() => __winnow.S.sources.some((s) => s.is_merge)") is True
    yield ids, merge
    page.evaluate("""async (mid) => {
      await __winnow.api('/api/merges/' + mid, { method: 'DELETE' });
    }""", -merge["id"])
    sid = [i for i in ids if i != page.evaluate("() => __winnow.S.sources[0].id")]
    page.evaluate("""async (name) => {
      const s = __winnow.S.sources.find((x) => x.name === name);
      if (s) await __winnow.api('/api/source/' + s.id, { method: 'DELETE' });
      await __winnow.loadSources();
    }""", "second.csv")


def test_removing_a_merged_table_is_refused_and_names_the_merge(page, merged):
    page.evaluate("() => __winnow.openTablesManager()")
    page.wait_for_selector("#modal:not([hidden]) .tables-row", timeout=10_000)
    row = page.locator("#modal .tables-row", has_text="second.csv").first
    row.locator("button", has_text="Remove").click()

    dlg = page.locator(".confirm-overlay")
    dlg.wait_for(state="visible", timeout=10_000)
    text = dlg.inner_text()
    # The merge by name: "it is used in a merge" sends an analyst hunting
    # the sidebar for which one.
    assert "Merged for the test" in text, text
    # And it says what to do, including the part that makes it safe to do.
    assert "Delete that merge first" in text, text
    assert "keeps them" in text, text

    # A refusal, not a confirm: there is no way to proceed from here.
    assert dlg.locator("button", has_text="Remove").count() == 0, text
    page.keyboard.press("Escape")
    page.wait_for_selector(".confirm-overlay", state="detached")

    # …and the table is still there.
    assert page.evaluate(
        "() => __winnow.S.sources.some((s) => s.name === 'second.csv')") is True
    page.keyboard.press("Escape")


def test_the_server_refuses_it_too(page, merged):
    """The dialog is a courtesy; this is the guard. A plugin, a script or
    a stale tab can all reach the route directly."""
    res = page.evaluate("""async () => {
      const s = __winnow.S.sources.find((x) => x.name === 'second.csv');
      const r = await fetch('/api/source/' + s.id,
        { method: 'DELETE', headers: { 'X-Timeline-Lite-Client': '1' } });
      return { status: r.status, body: await r.text() };
    }""")
    assert res["status"] == 409, res
    assert "Merged for the test" in res["body"], res
