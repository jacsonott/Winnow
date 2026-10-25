"""workspace.py: cross-case JSON stores (cases, saved filters, column
layouts, default tag template). The autouse isolate_workspace fixture in
conftest.py redirects WORKSPACE_DIR to a per-test tmp dir, so these use the
module's real singletons (WS.cases, WS.filters, ...) directly."""

from __future__ import annotations

import pytest

from winnow import workspace as WS
from winnow.store import DEFAULT_TAGS


def test_case_registry_crud(tmp_path):
    fake_case = str(tmp_path / "a.db")
    rec = WS.cases.create(fake_case, name="Case A")
    assert rec["name"] == "Case A"
    assert WS.cases.find_by_path(fake_case)["id"] == rec["id"]

    # create() with the same path is idempotent — returns the existing record.
    again = WS.cases.create(fake_case, name="Different Name")
    assert again["id"] == rec["id"]
    assert again["name"] == "Case A"

    WS.cases.touch_opened(rec["id"])
    assert WS.cases.get(rec["id"])["last_opened"] is not None

    updated = WS.cases.update(rec["id"], notes="important case")
    assert updated["notes"] == "important case"

    WS.cases.delete(rec["id"])
    assert WS.cases.get(rec["id"]) is None


def test_case_registry_update_missing_raises():
    with pytest.raises(KeyError):
        WS.cases.update(99999, notes="x")


def test_case_registry_never_reuses_an_id(tmp_path):
    """Ids were max+1, so forgetting the newest case gave its id to the
    next one registered — and a home screen still listing the forgotten
    case then acted on the newcomer ("Delete with file" deleted the wrong
    case's file). An emptied registry keeps counting too."""
    a = WS.cases.create(str(tmp_path / "a.db"), name="A")
    b = WS.cases.create(str(tmp_path / "b.db"), name="B")
    WS.cases.delete(b["id"])
    c = WS.cases.create(str(tmp_path / "c.db"), name="C")
    assert c["id"] not in (a["id"], b["id"])
    for rec in (a, c):
        WS.cases.delete(rec["id"])
    assert WS.cases.create(str(tmp_path / "d.db"), name="D")["id"] > c["id"]


def test_saved_filters_crud():
    rec = WS.filters.create("My Filter", ["EventId", "User"], {"search": "svchost"})
    assert rec["name"] == "My Filter"
    assert rec in WS.filters.list()

    renamed = WS.filters.update(rec["id"], name="Renamed Filter")
    assert renamed["name"] == "Renamed Filter"

    WS.filters.delete(rec["id"])
    assert rec["id"] not in {f["id"] for f in WS.filters.list()}


def test_saved_filters_update_is_partial():
    rec = WS.filters.create("Failed logons", ["EventId", "User"], {"search": "svchost"})

    # payload-only update (the Filter builder's "Update" button) leaves the
    # name and the header set it's bound to alone.
    edited = WS.filters.update(rec["id"], payload={"search": "lsass"})
    assert edited["payload"] == {"search": "lsass"}
    assert edited["name"] == "Failed logons"
    assert edited["col_names"] == ["EventId", "User"]

    # name-only update leaves the payload alone.
    renamed = WS.filters.update(rec["id"], name="Failed logons (tuned)")
    assert renamed["payload"] == {"search": "lsass"}
    assert renamed["col_names"] == ["EventId", "User"]

    # col_names is settable, just not sent by the edit path.
    rebound = WS.filters.update(rec["id"], col_names=["EventId", "User", "Host"])
    assert rebound["col_names"] == ["EventId", "User", "Host"]


def test_saved_filters_update_unknown_id_raises():
    with pytest.raises(KeyError):
        WS.filters.update(9999, name="nope")


def test_saved_filters_reorder_only_touches_the_given_group():
    # Two header-set groups interleaved in creation order: A, X, B, Y, C
    a = WS.filters.create("A", ["Col1"], {})
    WS.filters.create("X", ["Other"], {})
    b = WS.filters.create("B", ["Col1"], {})
    WS.filters.create("Y", ["Other"], {})
    c = WS.filters.create("C", ["Col1"], {})

    # Reverse just the ["Col1"] group's order (A, B, C -> C, B, A) — X/Y
    # must stay exactly where they were, not get pushed around. list() also
    # seeds the shipped TLE defaults (appended after these five, since the
    # five were created first) — slice them off; their order isn't at issue.
    WS.filters.reorder([c["id"], b["id"], a["id"]])
    names_in_order = [f["name"] for f in WS.filters.list()][:5]
    assert names_in_order == ["C", "X", "B", "Y", "A"]


def test_saved_filters_import_merges_by_name_and_columns():
    WS.filters.create("Shared", ["A", "B"], {"search": "old"})
    export = WS.filters.export_all()
    assert export["format"] == "winnow-filters/1"

    from winnow import defaults
    seeded = len(defaults.filters()["filters"])

    # Importing the same export again (merge=True) must not duplicate —
    # same name + same column set is considered "the same filter."
    # (export_all ran before list() ever seeded, so the export carries just
    # the one analyst filter; counts below are relative to the seeds.)
    added = WS.filters.import_all(export, merge=True)
    assert added == 0
    assert len(WS.filters.list()) == 1 + seeded

    # A different column set with the same name is NOT a duplicate skip.
    export2 = {"format": "winnow-filters/1", "filters": [
        {"name": "Shared", "col_names": ["C", "D"], "payload": {}},
    ]}
    added2 = WS.filters.import_all(export2, merge=True)
    assert added2 == 1
    assert len(WS.filters.list()) == 2 + seeded


def test_header_nicknames_save_find_overwrites_and_delete():
    assert WS.header_nicknames.find(["A", "B"]) is None

    rec = WS.header_nicknames.save(["A", "B"], "My header set")
    assert rec["nickname"] == "My header set"
    found = WS.header_nicknames.find(["b", "a"])  # order-independent, case-insensitive
    assert found is not None
    assert found["nickname"] == "My header set"

    # Saving again for the same set overwrites in place, not a second record.
    # (list() also carries the seeded KAPE defaults — count relative to them.)
    from winnow import defaults
    WS.header_nicknames.save(["A", "B"], "Renamed")
    assert WS.header_nicknames.find(["A", "B"])["nickname"] == "Renamed"
    assert len(WS.header_nicknames.list()) == len(defaults.headers()["nicknames"]) + 1

    WS.header_nicknames.delete(rec["id"])
    assert WS.header_nicknames.find(["A", "B"]) is None


def test_column_layouts_find_and_save_overwrites_for_same_set():
    assert WS.column_layouts.find(["A", "B"]) is None

    saved = WS.column_layouts.save(["A", "B"], order=["B", "A"], columns={"A": {"hidden": True}})
    assert saved["order"] == ["B", "A"]

    found = WS.column_layouts.find(["b", "a"])  # order-independent, case-insensitive matching
    assert found is not None
    assert found["order"] == ["B", "A"]

    # Saving again for the same column set overwrites in place rather than
    # accumulating a second record (per its own docstring).
    WS.column_layouts.save(["A", "B"], order=["A", "B"], columns={})
    assert WS.column_layouts.find(["A", "B"])["order"] == ["A", "B"]
    all_records = WS.column_layouts._load()
    assert len(all_records) == 1


def test_column_layouts_distinct_sets_are_independent():
    WS.column_layouts.save(["A", "B"], order=["A", "B"], columns={})
    WS.column_layouts.save(["C", "D"], order=["D", "C"], columns={})
    assert WS.column_layouts.find(["A", "B"])["order"] == ["A", "B"]
    assert WS.column_layouts.find(["C", "D"])["order"] == ["D", "C"]
    assert len(WS.column_layouts._load()) == 2


def test_tag_template_seeds_default_tags_once():
    seeded = WS.tags.get()
    assert [t["name"] for t in seeded] == [n for n, _, _ in DEFAULT_TAGS]

    WS.tags.save([{"name": "Custom", "color": "#abcdef", "hotkey": "1"}])
    assert [t["name"] for t in WS.tags.get()] == ["Custom"]  # not re-seeded after an explicit save

    as_tuples = WS.tags.as_tuples()
    assert as_tuples == [("Custom", "#abcdef", "1")]


def test_import_profiles_crud():
    assert WS.import_profiles.list() == []

    rec = WS.import_profiles.upsert(
        None, "KAPE", None, [], ["*_Amcache_UnassociatedFileEntries.csv"], True,
    )
    assert rec["name"] == "KAPE"
    assert rec["extensions"] is None
    assert rec["exclude_patterns"] == ["*_Amcache_UnassociatedFileEntries.csv"]
    assert WS.import_profiles.list() == [rec]

    # Upsert with the same id updates in place rather than adding a second record.
    updated = WS.import_profiles.upsert(
        rec["id"], "KAPE (tuned)", [".csv"], ["*EvtxECmd*"],
        ["*_Amcache_UnassociatedFileEntries.csv", "RegistryHives/*"], False,
    )
    assert updated["id"] == rec["id"]
    assert updated["name"] == "KAPE (tuned)"
    assert updated["extensions"] == [".csv"]
    assert updated["include_patterns"] == ["*EvtxECmd*"]
    assert updated["recursive"] is False
    all_records = WS.import_profiles.list()
    assert len(all_records) == 1
    assert all_records[0]["name"] == "KAPE (tuned)"

    WS.import_profiles.delete(rec["id"])
    assert WS.import_profiles.list() == []


def test_import_profiles_upsert_with_unknown_id_creates_new():
    # Unlike Store.upsert_tag (a truthy-but-nonexistent tag_id there hits an
    # UPDATE matching zero rows, then crashes reading it back), upsert()
    # here explicitly looks the id up first — a stale or invalid id falls
    # through to "create" instead of erroring.
    rec = WS.import_profiles.upsert(9999, "New", None, [], [], True)
    assert rec["id"] != 9999
    assert [p["name"] for p in WS.import_profiles.list()] == ["New"]


def test_app_settings_seed_save_and_validation():
    # Seeded rather than empty: tsFormatFor's fallback chain ends here, so
    # a missing file has to still name a real format.
    assert WS.app_settings.get()["default_ts_format"] == "iso"
    assert WS.app_settings.save({"default_ts_format": "us_date"})["default_ts_format"] == "us_date"
    assert WS.app_settings.get()["default_ts_format"] == "us_date"

    import pytest
    with pytest.raises(ValueError):
        WS.app_settings.save({"default_ts_format": "not_a_format"})
    assert WS.app_settings.get()["default_ts_format"] == "us_date"  # unchanged


# ------------------------------------------------- seeded header nicknames


def test_header_nicknames_seed_from_kape_defaults():
    """A fresh workspace knows the common EZ Tools header shapes by name —
    that's the whole value of the "database of headers": the analyst's first
    EvtxECmd import already says "Event logs (EvtxECmd)" in every place a
    header set is displayed, with nothing configured."""
    from winnow import defaults

    hn = WS.HeaderNicknames()
    recs = hn.list()
    assert len(recs) == len(defaults.headers()["nicknames"])
    evtx_cols = next(cols for n, cols in defaults.headers()["nicknames"]
                     if n == "Event logs (EvtxECmd)")
    # find() matches regardless of column order/case — the store's own key.
    assert hn.find(list(reversed([c.upper() for c in evtx_cols])))["nickname"] == "Event logs (EvtxECmd)"


def test_header_nickname_seed_never_overrides_or_resurrects():
    """Seeded rows are ordinary records afterward: a rename sticks, a delete
    sticks across every later read, and re-seeding (same version) adds
    nothing back."""

    hn = WS.HeaderNicknames()
    recs = hn.list()
    target = recs[0]
    hn.save(list(target["col_names"]), "Mine now")
    hn.delete(recs[1]["id"])
    hn.ensure_seeded()  # explicit re-run, same version: a no-op
    after = hn.list()
    assert hn.find(target["col_names"])["nickname"] == "Mine now"
    assert len(after) == len(recs) - 1


def test_header_nickname_seed_version_bump_adds_only_missing(monkeypatch):
    """A later Winnow adding one new default must add exactly that one —
    existing rows (including analyst renames of old defaults) untouched."""
    from winnow import defaults

    hn = WS.HeaderNicknames()
    before = hn.list()
    hn.save(list(before[0]["col_names"]), "Renamed")
    shipped = defaults.headers()
    monkeypatch.setattr(defaults, "headers", lambda: {
        "version": shipped["version"] + 1,
        "nicknames": shipped["nicknames"] + [("New tool", ["ColA", "ColB"])]})
    after = hn.list()
    assert len(after) == len(before) + 1
    assert hn.find(["colb", "cola"])["nickname"] == "New tool"
    assert hn.find(before[0]["col_names"])["nickname"] == "Renamed"



# What a release that ADDS defaults does: same entries plus one, version+1
# (defaults/__init__.py's instruction). Both files go through it, because a
# deleted or renamed default coming back on that bump was the bug, and
# nothing before the bump can show it.

def _bump_filters(monkeypatch, extra=()):
    from winnow import defaults
    shipped = defaults.filters()
    monkeypatch.setattr(defaults, "filters", lambda: {
        "version": shipped["version"] + 1,
        "filters": list(shipped["filters"]) + list(extra)})


def _bump_headers(monkeypatch, extra=()):
    from winnow import defaults
    shipped = defaults.headers()
    monkeypatch.setattr(defaults, "headers", lambda: {
        "version": shipped["version"] + 1,
        "nicknames": list(shipped["nicknames"]) + list(extra)})


def test_a_version_bump_brings_back_no_deleted_or_renamed_default_filter(monkeypatch):
    """CLAUDE.md: analyst renames and deletes always win. Judged by what
    was PRESENT, a bump re-added the deleted default and put the original
    back beside the renamed one."""
    before = WS.filters.list()
    deleted, renamed = before[0], before[1]
    WS.filters.delete(deleted["id"])
    WS.filters.update(renamed["id"], name="MY triage")

    _bump_filters(monkeypatch, extra=[("Brand new", ["ColA", "ColB"], {"search": "x"})])
    names = [f["name"] for f in WS.filters.list()]
    assert deleted["name"] not in names
    assert renamed["name"] not in names and names.count("MY triage") == 1
    assert names.count("Brand new") == 1          # a NEW default still arrives
    assert len(names) == len(before) - 1 + 1


def test_a_version_bump_brings_back_no_deleted_nickname(monkeypatch):
    hn = WS.HeaderNicknames()
    before = hn.list()
    hn.delete(before[0]["id"])

    _bump_headers(monkeypatch, extra=[("New tool", ["ColA", "ColB"])])
    after = hn.list()
    assert hn.find(before[0]["col_names"]) is None
    assert hn.find(["ColA", "ColB"])["nickname"] == "New tool"
    assert len(after) == len(before)


def test_a_workspace_seeded_before_the_record_existed_is_recorded_at_its_version(monkeypatch):
    """Files written by a release before `seeded` existed carry only
    seeded_version. At the shipped version they must be recorded on the
    next read — that is what lets the NEXT bump know what was deleted."""
    import json

    WS.filters.list()
    path = WS.WORKSPACE_DIR / WS.SavedFilters.FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("seeded")
    victim = data["filters"].pop(0)
    path.write_text(json.dumps(data), encoding="utf-8")

    WS.filters.list()                        # records, adds nothing back
    assert "seeded" in json.loads(path.read_text(encoding="utf-8"))
    _bump_filters(monkeypatch)
    assert victim["name"] not in [f["name"] for f in WS.filters.list()]


def test_tag_template_seeds_ta_first_and_migrates_only_untouched_legacy():
    """DEFAULT_TAGS now leads with TA (hotkey 1 — triage reaches for "this
    is the adversary" far more than "this is fine"). A workspace whose
    template is byte-for-byte the old Benign-first seed was never edited,
    so it migrates; any customization is the analyst's and stays."""
    from winnow.store import DEFAULT_TAGS

    assert [n for n, _, _ in DEFAULT_TAGS] == ["TA", "Suspicious", "Benign"]
    assert DEFAULT_TAGS[0][2] == "1"

    # fresh seed: TA first
    assert [t["name"] for t in WS.tags.get()] == ["TA", "Suspicious", "Benign"]

    # untouched legacy template migrates in place
    WS.tags._save(list(WS.TagTemplate._LEGACY_SEED))
    assert [t["name"] for t in WS.tags.get()] == ["TA", "Suspicious", "Benign"]

    # a customized template (one rename) is never rewritten
    custom = [dict(t) for t in WS.TagTemplate._LEGACY_SEED]
    custom[0]["name"] = "Clean"
    WS.tags._save(custom)
    assert [t["name"] for t in WS.tags.get()] == ["Clean", "Suspicious", "TA"]


def test_saved_filters_normalize_cond_root_payloads_on_read():
    """A record whose filter_tree root is a bare condition (the v1 seeds,
    or an import from one) gets group-wrapped by list() and persisted, so
    every client that reads it sees the shape the builder and spec gate
    agree on."""
    sf = WS.SavedFilters()
    cond = {"type": "cond", "column": "EventId", "op": "in", "value": ["1"]}
    rec = sf.create("condroot", ["EventId"], {
        "filter_tree": dict(cond), "search": "", "search_mode": "contains", "search_terms": []})
    got = next(f for f in sf.list() if f["id"] == rec["id"])
    assert got["payload"]["filter_tree"] == {"type": "group", "op": "AND", "children": [cond]}
    # ...and it stuck: a re-read straight from disk shows the wrapped shape.
    got2 = next(f for f in WS.SavedFilters().list() if f["id"] == rec["id"])
    assert got2["payload"]["filter_tree"]["type"] == "group"


def test_the_module_docstring_lists_every_store():
    """This is here because the list said "eight" while there were eleven —
    app_settings, prefs and plugin_bundles were added without it. A
    docstring that inventories something is a docstring that goes stale, so
    the inventory is checked rather than trusted."""
    import inspect
    import re

    documented = set(re.findall(r"^  (\w+\.json)", WS.__doc__, re.M))
    actual = {c.FILE for _, c in inspect.getmembers(WS, inspect.isclass)
              if getattr(c, "FILE", "").endswith(".json")}
    assert documented == actual, (
        f"workspace.py's docstring and its stores disagree — "
        f"undocumented: {sorted(actual - documented)}, "
        f"documented but gone: {sorted(documented - actual)}")
    # And the count in the prose has to agree with the list under it.
    assert f"{len(actual)}" in WS.__doc__ or "Eleven" in WS.__doc__


def test_plugin_data_roundtrips_and_is_traversal_safe(tmp_path, monkeypatch):
    """req.storage: a plugin's machine-level persistent dict, one JSON
    file per plugin under workspace/plugin_data/. The fs name becomes a
    filename, so it must never escape that directory."""
    import winnow.workspace as WS
    monkeypatch.setattr(WS, "WORKSPACE_DIR", tmp_path / "ws")
    store = WS.PluginData("lateral_movement")
    assert store.get() == {}
    store.set({"saved": [{"name": "WinRM"}]})
    assert WS.PluginData("lateral_movement").get() == {"saved": [{"name": "WinRM"}]}
    # Two plugins keep separate documents.
    WS.PluginData("other").set({"x": 1})
    assert WS.PluginData("lateral_movement").get()["saved"][0]["name"] == "WinRM"
    # A traversing fs name is sanitized to a filename, not a path.
    WS.PluginData("../../etc/evil").set({"pwned": True})
    assert not (tmp_path / "etc").exists()
    import pytest
    with pytest.raises(ValueError):
        WS.PluginData("x").set(["not a dict"])


# ---------------------------------------------------------------- damaged files
#
# Every file in workspace/ is meant to be human-readable and hand-editable,
# so "an analyst opened it in Notepad" is an ordinary event, not an attack.
# Each of these was a real outcome before: a UTF-8 BOM or a trailing comma
# read as an EMPTY store and the next write replaced the file (three custom
# filters became the 28 defaults on a plain list()); a cp1252 save or a
# wrong-shaped document raised out of every route that touched the store;
# a wrong-shaped plugins.json raised out of server.py's import, so Winnow
# would not start.

def _ws_file(name):
    WS._ensure_dir()
    return WS.WORKSPACE_DIR / name


def _set_aside(name):
    return sorted(WS.WORKSPACE_DIR.glob(name + ".corrupt-*"))


def _case_doc():
    return {"cases": [{"id": 1, "path": "/cases/apt29.db-winnow", "name": "APT29 été",
                       "group": "", "notes": "", "created_at": "x", "last_opened": None}]}


def test_a_bom_saved_workspace_file_still_reads_and_keeps_its_records():
    """Notepad's "UTF-8 with BOM" is a valid save of a valid file."""
    import json
    _ws_file("cases.json").write_bytes(
        b"\xef\xbb\xbf" + json.dumps(_case_doc(), ensure_ascii=False).encode("utf-8"))
    assert [c["name"] for c in WS.cases.list()] == ["APT29 été"]
    WS.cases.create("/cases/new.db-winnow", "new")
    on_disk = json.loads(_ws_file("cases.json").read_text(encoding="utf-8"))
    assert [c["name"] for c in on_disk["cases"]] == ["APT29 été", "new"]
    assert _set_aside("cases.json") == []


def test_an_unparseable_filters_file_is_set_aside_not_overwritten():
    """The reported shape: a hand edit leaves a trailing comma in a
    filters.json holding three custom filters, and opening the filter list
    replaced it with the shipped defaults. The defaults may come back — the
    store needs SOMETHING to read — but the analyst's file must still exist,
    byte for byte, where they can find it."""
    custom = ('{"seeded_version": 1, "filters": [' + ",".join(
        f'{{"id": {i}, "name": "my filter {i}", "col_names": ["a"], "payload": {{}}, '
        f'"created_at": "x"}}' for i in (1, 2, 3)) + ",]}").encode()
    _ws_file("filters.json").write_bytes(custom)

    names = [f["name"] for f in WS.filters.list()]
    assert "my filter 1" not in names          # unreadable, so not offered...
    aside = _set_aside("filters.json")
    assert len(aside) == 1                     # ...but kept, untouched
    assert aside[0].read_bytes() == custom
    # A write afterwards goes to a fresh file and leaves the set-aside one be.
    WS.filters.create("after", ["a"], {})
    assert aside[0].read_bytes() == custom

    from winnow import log as wlog
    assert any("filters.json" in e["message"] and e["level"] == "error"
               for e in wlog.entries())


def test_a_cp1252_workspace_file_is_set_aside_instead_of_raising():
    import json
    raw = json.dumps(_case_doc(), ensure_ascii=False).encode("cp1252")
    _ws_file("cases.json").write_bytes(raw)
    assert WS.cases.list() == []
    assert [p.read_bytes() for p in _set_aside("cases.json")] == [raw]
    WS.cases.create("/cases/new.db-winnow", "new")      # and the store still works
    assert [c["name"] for c in WS.cases.list()] == ["new"]


@pytest.mark.parametrize("raw", [b"null", b"[]", b"{}", b'{"cases": null}', b'"cases"'])
def test_a_wrong_shaped_cases_file_is_set_aside_instead_of_raising(raw):
    _ws_file("cases.json").write_bytes(raw)
    assert WS.cases.list() == []
    assert [p.read_bytes() for p in _set_aside("cases.json")] == [raw]


@pytest.mark.parametrize("name, call, expected", [
    ("plugins.json", lambda: WS.plugin_prefs.disabled(), set()),
    ("plugins.json", lambda: WS.plugin_prefs.machine_enabled("x", default_on=True), True),
    ("prefs.json", lambda: WS.machine_prefs.get("cases_dir"), None),
    ("app_settings.json", lambda: WS.app_settings.get()["default_ts_format"], "iso"),
    ("dashboards.json", lambda: WS.dashboard_library.list(), []),
    ("import_profiles.json", lambda: WS.import_profiles.list(), []),
])
def test_wrong_shaped_machine_stores_fall_back_to_their_defaults(name, call, expected):
    _ws_file(name).write_bytes(b"[]")
    assert call() == expected
    assert len(_set_aside(name)) == 1


def test_plugins_json_with_a_wrong_typed_list_does_not_stop_plugin_loading():
    """plugins.json is read by the module-level _reload_plugins() in
    server.py, so anything that raises here is a server that won't start.
    `"disabled": null` used to raise out of set(None)."""
    _ws_file("plugins.json").write_bytes(b'{"disabled": null}')
    assert WS.plugin_prefs.disabled() == set()
    # enabled_bundled is younger than the file, so it is checked by the
    # store rather than required: a bare string must not become one
    # plugin per letter, and a null must not raise.
    for raw in (b'{"disabled": [], "enabled_bundled": "mft_usn"}',
                b'{"disabled": [], "enabled_bundled": null}'):
        _ws_file("plugins.json").write_bytes(raw)
        assert WS.plugin_prefs.enabled_bundled() == set()
        assert WS.plugin_prefs.machine_enabled("mft_usn", default_on=False) is False
    # ...and a file from before enabled_bundled existed still reads.
    _ws_file("plugins.json").write_bytes(b'{"disabled": ["pivot"]}')
    assert WS.plugin_prefs.disabled() == {"pivot"}
    assert WS.plugin_prefs.enabled_bundled() == set()


def test_a_damaged_tag_template_reseeds_the_default_tags():
    """tags.json seeds every new case's tag set: falling back to [] would
    create cases with no tags at all."""
    _ws_file("tags.json").write_bytes(b'{"not": "a list"}')
    assert [t["name"] for t in WS.tags.get()] == [n for n, _c, _h in DEFAULT_TAGS]
    assert len(_set_aside("tags.json")) == 1


def test_a_failed_lock_file_open_does_not_wedge_the_workspace(monkeypatch):
    """_WorkspaceLock took its in-process RLock and only then opened
    workspace/.lock; one failed open (EMFILE, a read-only install) left the
    RLock held by that thread forever, so every other worker thread blocked
    on its next workspace call — the whole server, since nearly every
    route touches one."""
    import errno
    import os
    import threading

    real_open = os.open
    fail = {"once": True}

    def flaky_open(path, *a, **kw):
        if str(path).endswith(".lock") and fail["once"]:
            fail["once"] = False
            raise OSError(errno.EMFILE, "Too many open files")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(os, "open", flaky_open)
    # A lock of this test's own: if the leak is back, the one it wedges is
    # thrown away with the test instead of hanging every test after it.
    monkeypatch.setattr(WS, "_LOCK", WS._WorkspaceLock())

    # Two DIFFERENT, simultaneously live threads, as uvicorn's worker pool
    # has: an RLock is re-entrant, and a thread started after the first
    # one exits can be handed the same ident — either way the second call
    # would sail through a leaked lock and the test would prove nothing.
    first_done, release_first = threading.Event(), threading.Event()
    out = {}

    def first():
        try:
            WS.cases.list()
        except OSError as e:
            out["first"] = e
        first_done.set()
        release_first.wait(timeout=30)   # stay alive, holding whatever leaked

    def second():
        out["second"] = WS.cases.list()

    t1 = threading.Thread(target=first, daemon=True)
    t1.start()
    assert first_done.wait(timeout=10)
    try:
        assert isinstance(out.get("first"), OSError)
        t2 = threading.Thread(target=second, daemon=True)   # daemon: a hang fails
        t2.start()                                          # this test, not the run
        t2.join(timeout=10)
        assert not t2.is_alive(), "workspace call hung: the failed open leaked the lock"
        assert out["second"] == []
    finally:
        release_first.set()
