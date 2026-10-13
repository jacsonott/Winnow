"""Derived columns meeting merges, and the derived-column menu's edit item,
driven through the real dialogs.

- "Merge tables…" grouped tables by every column, derived ones included,
  while the server decides on the file's own columns: a derived column on
  one of two same-shaped tables put each in a group of its own, and the
  dialog said no tables matched for a merge the server accepts.
- On a merge, Re-derive… and Remove derived column… sent the first
  member's definition id alone and changed only that member.
- A regex-capture or lookup column was offered "Change the field path…",
  which neither op has, so it failed every time and their settings could
  not be edited. They take Re-derive…, the derive modal prefilled."""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.ui

# A header no other module ingests, so the merge builder's group for it
# holds exactly the tables this module imports.
HEADER = "DmWhen,DmWho,DmNote"


def _ingest(api, path, name):
    rec = api("/api/ingest/path", "POST", {"path": str(path), "name": name, "build_fts": False})
    assert "id" in rec, rec
    return rec["id"]


def _pair(api, tmp_path, stem):
    ids = []
    for n, (user, host) in enumerate((("alice", "SRV1"), ("carol", "WKS2"))):
        f = tmp_path / f"{stem}_{n}.csv"
        f.write_text(f"{HEADER}\n2026-03-1{n} 09:00:00,u={user};h={host},x\n"
                     f"2026-03-1{n} 10:00:00,u=bob;h={host},y\n", encoding="utf-8")
        ids.append(_ingest(api, f, f"{stem}_{n}.csv"))
    return ids


def _defs(api, sid):
    return api(f"/api/derived?source_id={sid}")


def _wait_ready(api, sids, timeout=25.0):
    """Polled from Python: page.wait_for_function does not await a
    promise predicate (see test_merge_flow.py)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(d["status"] == "ready" for sid in sids for d in _defs(api, sid)):
            return
        time.sleep(0.2)
    pytest.fail(f"derived columns on {sids} never finished: {[_defs(api, s) for s in sids]}")


def _open(page, sid, col):
    page.evaluate("(id) => __winnow.loadSources().then(() => __winnow.openSource(id))", sid)
    page.wait_for_function("(id) => __winnow.S.sourceId === id", arg=sid)
    page.wait_for_selector(f'.hcell[data-col="{col}"]')
    page.wait_for_selector(".row")


def _header_menu(page, col):
    page.locator(f'.hcell[data-col="{col}"]').click(button="right")
    page.wait_for_selector(".menu .menu-item")
    return page.locator(".menu .menu-item").all_inner_texts()


def _cleanup(page, api, merge_ids, source_ids, home):
    for mid in merge_ids:
        api(f"/api/merges/{-mid}", "DELETE")
    for sid in source_ids:
        api(f"/api/source/{sid}", "DELETE")
    page.evaluate("""(home) => (async () => {
      __winnow.S.sourceId = null;
      await __winnow.loadSources();
      await __winnow.openSource(home);
    })()""", home)
    page.wait_for_selector(".row")


def test_merge_dialog_groups_by_the_files_own_columns(page, api, tmp_path):
    home = page.evaluate("() => __winnow.S.sourceId")
    a, b = _pair(api, tmp_path, "dm_group")
    merge_id = None
    try:
        api("/api/derived", "POST", {"source_id": a, "name": "DmUser", "input_column": "DmWho",
                                     "op_id": "regex_extract", "params": {"pattern": r"u=(\w+)"}})
        _wait_ready(api, [a])
        page.evaluate("() => __winnow.loadSources()")
        page.wait_for_function(
            "(id) => (__winnow.S.sources.find((s) => s.id === id) || {columns: []})"
            ".columns.some((c) => c.derived)", arg=a)

        page.evaluate("() => __winnow.openMergeBuilder()")
        page.wait_for_selector("#modal:not([hidden])")
        for n in ("dm_group_0.csv", "dm_group_1.csv"):
            box = page.locator("#modal label", has_text=n).locator("input[type=checkbox]")
            assert box.count() == 1, f"the merge dialog did not offer {n}: " + \
                page.locator("#modalBody").inner_text()
            box.check()
        # One group holds both — it is titled by the file's columns, not
        # the analyst's derived one.
        head = page.locator("#modal h4", has_text="DmWhen").first.inner_text()
        assert "DmUser" not in head
        page.locator("#modal button", has_text="Create merge").click()
        page.wait_for_function("() => __winnow.S.sourceId < 0", timeout=10_000)
        merge = page.evaluate("() => __winnow.S.sources.find((s) => s.id === __winnow.S.sourceId)")
        merge_id = merge["id"]
        assert sorted(merge["member_source_ids"]) == sorted([a, b])
        assert merge["row_count"] == 4
        page.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count === 4")
    finally:
        _cleanup(page, api, [merge_id] if merge_id else [], [a, b], home)


def test_rederive_and_remove_on_a_merge_reach_every_member(page, api, tmp_path):
    home = page.evaluate("() => __winnow.S.sourceId")
    a, b = _pair(api, tmp_path, "dm_edit")
    merge_id = api("/api/merges", "POST", {"name": "dm edit merge", "source_ids": [a, b]})["id"]
    try:
        api("/api/derived", "POST", {"source_id": merge_id, "name": "DmUser", "input_column": "DmWho",
                                     "op_id": "regex_extract", "params": {"pattern": r"u=(\w+)"}})
        _wait_ready(api, [a, b])
        _open(page, merge_id, "DmUser")

        # A regex column edits its settings in the derive modal, prefilled.
        items = _header_menu(page, "DmUser")
        assert any(i.startswith("Re-derive") for i in items), items
        assert not any("field path" in i for i in items), items
        page.locator(".menu .menu-item", has_text="Re-derive").click()
        page.wait_for_selector("#modal:not([hidden])")
        pattern = page.locator(".derived-param input").first
        page.wait_for_function(
            "() => document.querySelector('.derived-param input').value === 'u=(\\\\w+)'")
        pattern.fill(r"h=(\w+)")
        page.wait_for_selector(".derived-preview-row")
        page.locator("#modalBody .btn", has_text="Re-derive").click()
        page.wait_for_selector("#modal", state="hidden")
        _wait_ready(api, [a, b])
        # Both members were re-derived — the second used to keep u=(\w+).
        for sid in (a, b):
            assert [d["params"]["pattern"] for d in _defs(api, sid)] == [r"h=(\w+)"], sid
        page.evaluate("(id) => __winnow.openSource(id)", merge_id)
        page.wait_for_function("""() => {
          const i = __winnow.S.columns.findIndex((c) => c.name === 'DmUser');
          const vals = [0, 1, 2, 3].map((p) => (__winnow.rowAt(p) || {cells: []}).cells[i]);
          return ['SRV1', 'WKS2'].every((h) => vals.includes(h)) && !vals.includes('alice');
        }""", timeout=15_000)

        # Remove takes it off every member, not just the first.
        _header_menu(page, "DmUser")
        page.locator(".menu .menu-item", has_text="Remove derived column").click()
        page.wait_for_selector(".confirm-overlay")
        assert "from all 2 tables in this merge" in page.locator(".confirm-message").inner_text()
        page.locator(".confirm-card .btn", has_text="Remove").click()
        page.wait_for_function(
            "() => !__winnow.S.columns.some((c) => c.name === 'DmUser')", timeout=10_000)
        assert _defs(api, a) == [] and _defs(api, b) == []
    finally:
        _cleanup(page, api, [merge_id], [a, b], home)


def test_lookup_column_reopens_its_settings_instead_of_a_field_path(page, api, tmp_path):
    home = page.evaluate("() => __winnow.S.sourceId")
    events = tmp_path / "dm_events.csv"
    events.write_text("DmHost,DmMsg\nH0,a\nH1,b\n", encoding="utf-8")
    owners = tmp_path / "dm_owners.csv"
    owners.write_text("Host,Owner,Dept\nH0,alice,Finance\nH1,bob,Ops\n", encoding="utf-8")
    ev = _ingest(api, events, "dm_events.csv")
    ow = _ingest(api, owners, "dm_owners.csv")
    try:
        api("/api/derived", "POST", {"source_id": ev, "name": "DmOwner", "input_column": "DmHost",
                                     "op_id": "lookup", "params": {"other_source_id": ow,
                                                                   "match_column": "Host",
                                                                   "value_column": "Owner"}})
        _wait_ready(api, [ev])
        _open(page, ev, "DmOwner")
        items = _header_menu(page, "DmOwner")
        assert not any("field path" in i for i in items), items
        page.locator(".menu .menu-item", has_text="Re-derive").click()
        page.wait_for_selector("#modal:not([hidden])")
        # Prefilled with what the column has now: the table and both columns.
        page.wait_for_function("""(ow) => {
          const s = [...document.querySelectorAll('.derived-param select')];
          return s.length >= 3 && s[0].value === String(ow) && s[1].value === 'Host' && s[2].value === 'Owner';
        }""", arg=ow)
        page.locator(".derived-param select").nth(2).select_option("Dept")
        page.wait_for_selector(".derived-preview-row")
        page.locator("#modalBody .btn", has_text="Re-derive").click()
        page.wait_for_selector("#modal", state="hidden")
        _wait_ready(api, [ev])
        assert _defs(api, ev)[0]["params"]["value_column"] == "Dept"
        page.evaluate("(id) => __winnow.openSource(id)", ev)
        page.wait_for_function("""() => {
          const i = __winnow.S.columns.findIndex((c) => c.name === 'DmOwner');
          const r = __winnow.rowAt(0);
          return r && r.cells[i] === 'Finance';
        }""", timeout=15_000)
    finally:
        _cleanup(page, api, [], [ev, ow], home)


def test_a_json_field_column_still_offers_its_field_path(page, api, tmp_path):
    home = page.evaluate("() => __winnow.S.sourceId")
    f = tmp_path / "dm_json.csv"
    f.write_text('DmDoc\n"{""user"": ""alice""}"\n', encoding="utf-8")
    sid = _ingest(api, f, "dm_json.csv")
    try:
        api("/api/derived", "POST", {"source_id": sid, "name": "DmJsonUser", "input_column": "DmDoc",
                                     "op_id": "json_field", "params": {"path": "$.user"}})
        _wait_ready(api, [sid])
        _open(page, sid, "DmJsonUser")
        items = _header_menu(page, "DmJsonUser")
        assert any(i.startswith("Change the field path") for i in items), items
        assert not any(i.startswith("Re-derive") for i in items), items
        page.keyboard.press("Escape")
    finally:
        _cleanup(page, api, [], [sid], home)
