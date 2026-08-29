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
    x = WS.filters.create("X", ["Other"], {})
    b = WS.filters.create("B", ["Col1"], {})
    y = WS.filters.create("Y", ["Other"], {})
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

    from winnow import filter_defaults
    seeded = len(filter_defaults.DEFAULT_SAVED_FILTERS)

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
    from winnow import header_defaults
    WS.header_nicknames.save(["A", "B"], "Renamed")
    assert WS.header_nicknames.find(["A", "B"])["nickname"] == "Renamed"
    assert len(WS.header_nicknames.list()) == len(header_defaults.DEFAULT_HEADER_NICKNAMES) + 1

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
    from winnow import header_defaults

    hn = WS.HeaderNicknames()
    recs = hn.list()
    assert len(recs) == len(header_defaults.DEFAULT_HEADER_NICKNAMES)
    evtx_cols = next(cols for n, cols in header_defaults.DEFAULT_HEADER_NICKNAMES
                     if n == "Event logs (EvtxECmd)")
    # find() matches regardless of column order/case — the store's own key.
    assert hn.find(list(reversed([c.upper() for c in evtx_cols])))["nickname"] == "Event logs (EvtxECmd)"


def test_header_nickname_seed_never_overrides_or_resurrects():
    """Seeded rows are ordinary records afterward: a rename sticks, a delete
    sticks across every later read, and re-seeding (same version) adds
    nothing back."""
    from winnow import header_defaults

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
    from winnow import header_defaults

    hn = WS.HeaderNicknames()
    before = hn.list()
    hn.save(list(before[0]["col_names"]), "Renamed")
    monkeypatch.setattr(header_defaults, "DEFAULTS_VERSION", header_defaults.DEFAULTS_VERSION + 1)
    monkeypatch.setattr(header_defaults, "DEFAULT_HEADER_NICKNAMES",
                        header_defaults.DEFAULT_HEADER_NICKNAMES + [("New tool", ["ColA", "ColB"])])
    after = hn.list()
    assert len(after) == len(before) + 1
    assert hn.find(["colb", "cola"])["nickname"] == "New tool"
    assert hn.find(before[0]["col_names"])["nickname"] == "Renamed"



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
