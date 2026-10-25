"""Importing a list of indicators asks about the list.

The two selects the import read were the ADD row's, at the other end of
the page and set for whatever single indicator was typed last. So a file
of two hundred hashes went in as whatever kind that happened to be, with
an auto-tag nobody had chosen for it, and nothing said so before or
after. Type and auto-tag are decisions about the batch, so the batch
asks — and the dialog reports what it read out of the file, because "47
indicators" beside the file's name is the part an analyst can check.

Every value here is chosen not to appear in the fixture table: an
auto-tag that actually matched would tag rows of the case these modules
share.
"""

from __future__ import annotations

import json
import time
import urllib.request

import pytest

pytestmark = pytest.mark.ui

VALUES = [f"QQZZ{i:02d}" for i in range(1, 13)]


def _get(server, route):
    """Read over HTTP from Python, with a timeout. NOT through the page:
    `page.evaluate` on a promise has no timeout of its own, so one fetch
    that never settles hangs the whole job rather than failing a test —
    which is how this module once burned a six-hour CI run."""
    req = urllib.request.Request(server.rstrip("/") + route,
                                 headers={"X-Timeline-Lite-Client": "1"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def _delete(server, route):
    urllib.request.urlopen(urllib.request.Request(
        server.rstrip("/") + route, method="DELETE",
        headers={"X-Timeline-Lite-Client": "1"}), timeout=15).read()


def _clear(server):
    for i in _get(server, "/api/watchlist"):
        _delete(server, f"/api/watchlist/{i['id']}")


@pytest.fixture(autouse=True)
def _clean(page, server):
    _clear(server)
    yield
    page.keyboard.press("Escape")
    page.evaluate("() => { document.getElementById('modal').hidden = true; }")
    _clear(server)


def _open_import(page, tmp_path, text, name="iocs.txt"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    page.locator("#tabWatchlist").click()
    page.wait_for_selector("#watchlistview:not([hidden])")
    page.locator("#wlImportFile").set_input_files(str(path))
    page.wait_for_function("() => document.getElementById('modalTitle').textContent === 'Import indicators'")
    return path


def _dialog(page):
    return page.evaluate("""() => {
      const b = document.getElementById('modalBody');
      const sels = [...b.querySelectorAll('select')];
      return {
        head: (b.querySelector('.note-status') || {}).textContent || '',
        preview: [...b.querySelectorAll('.wl-import-preview > div')].map((d) => d.textContent),
        fields: [...b.querySelectorAll('.wl-import-field > span:first-child')].map((s) => s.textContent),
        kinds: sels.length ? [...sels[0].options].map((o) => o.value) : [],
        tags: sels.length > 1 ? [...sels[1].options].map((o) => o.textContent) : [],
        button: (b.querySelector('.row-actions button') || {}).textContent || '',
      };
    }""")


def _import_with(page, kind=None, tag_label=None):
    page.evaluate("""([kind, tag]) => {
      const sels = [...document.querySelectorAll('#modalBody select')];
      if (kind) sels[0].value = kind;
      if (tag) sels[1].value = [...sels[1].options].find((o) => o.textContent === tag).value;
      document.querySelector('#modalBody .row-actions button').click();
    }""", [kind, tag_label])
    page.wait_for_selector("#modal[hidden]", state="attached", timeout=10_000)


def _indicators(server, want=1):
    """Polled from Python, which is also what makes the wait bounded —
    `page.wait_for_function` does not await a promise predicate at all
    (tests/test_ui_test_hygiene.py), and `page.evaluate` awaits one
    forever."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        got = _get(server, "/api/watchlist")
        if len(got) >= want:
            return got
        time.sleep(0.2)
    raise AssertionError(f"the watchlist never reached {want} indicators")


def test_the_dialog_says_what_it_read(page, tmp_path):
    _open_import(page, tmp_path, "# a comment\n\n" + "\n".join(VALUES) + "\n")
    d = _dialog(page)
    assert "12 indicators from iocs.txt" in d["head"], d["head"]
    assert "2 blank or comment lines ignored" in d["head"], d["head"]
    assert d["preview"][:3] == VALUES[:3]
    assert d["preview"][-1] == "…and 4 more"
    assert d["button"] == "Import 12"


def test_it_offers_the_type_and_the_auto_tag(page, tmp_path):
    _open_import(page, tmp_path, "\n".join(VALUES))
    d = _dialog(page)
    assert d["fields"] == ["Type", "Auto-tag"]
    # The same type list the Add row offers, cloned from it rather than
    # re-listed — one place names the kinds.
    assert d["kinds"] == page.evaluate(
        "() => [...document.getElementById('wlKind').options].map((o) => o.value)")
    assert d["tags"][0] == "no auto-tag"
    assert d["tags"][1:] == page.evaluate("() => __winnow.S.tags.map((t) => t.name)")


def test_the_dialogs_choice_decides_not_the_add_rows(page, server, tmp_path):
    """The whole point. The Add row is left on one kind and the dialog set
    to another; what lands is the dialog's."""
    page.locator("#tabWatchlist").click()
    page.wait_for_selector("#watchlistview:not([hidden])")
    page.evaluate("() => { document.getElementById('wlKind').value = 'hash'; }")
    _open_import(page, tmp_path, "\n".join(VALUES[:3]))
    _import_with(page, kind="ip", tag_label="Suspicious")

    got = _indicators(server, want=3)
    assert {i["value"] for i in got} == set(VALUES[:3])
    assert {i["kind"] for i in got} == {"ip"}
    suspicious = page.evaluate("() => __winnow.S.tags.find((t) => t.name === 'Suspicious').id")
    assert {i["auto_tag_id"] for i in got} == {suspicious}


def test_a_per_line_kind_still_wins(page, server, tmp_path):
    """The server's own rule, which the dialog's help names rather than
    overriding: a `value,kind` line carries its own."""
    _open_import(page, tmp_path, f"{VALUES[0]}\n{VALUES[1]},domain\n")
    _import_with(page, kind="ip")
    kinds = {i["value"]: i["kind"] for i in _indicators(server, want=2)}
    assert kinds == {VALUES[0]: "ip", VALUES[1]: "domain"}


def test_no_auto_tag_is_the_default(page, server, tmp_path):
    _open_import(page, tmp_path, VALUES[0])
    _import_with(page)
    assert [i["auto_tag_id"] for i in _indicators(server)] == [None]


def test_a_file_with_nothing_in_it_says_so_and_imports_nothing(page, server, tmp_path):
    _open_import(page, tmp_path, "# only a comment\n\n\n")
    d = _dialog(page)
    assert d["button"] == "", "an empty import should offer no Import button"
    assert "Nothing to import" in page.evaluate(
        "() => document.querySelector('#modalBody .fb-help').textContent")
    page.keyboard.press("Escape")
    page.wait_for_selector("#modal[hidden]", state="attached")
    assert _get(server, "/api/watchlist") == []
