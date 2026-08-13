"""workspace.py: cross-case JSON stores (cases, saved filters, column
layouts, default tag template). The autouse isolate_workspace fixture in
conftest.py redirects WORKSPACE_DIR to a per-test tmp dir, so these use the
module's real singletons (WS.cases, WS.filters, ...) directly."""

from __future__ import annotations

import pytest

import workspace as WS
from store import DEFAULT_TAGS


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
    # must stay exactly where they were, not get pushed around.
    WS.filters.reorder([c["id"], b["id"], a["id"]])
    names_in_order = [f["name"] for f in WS.filters.list()]
    assert names_in_order == ["C", "X", "B", "Y", "A"]


def test_saved_filters_import_merges_by_name_and_columns():
    WS.filters.create("Shared", ["A", "B"], {"search": "old"})
    export = WS.filters.export_all()
    assert export["format"] == "winnow-filters/1"

    # Importing the same export again (merge=True) must not duplicate —
    # same name + same column set is considered "the same filter."
    added = WS.filters.import_all(export, merge=True)
    assert added == 0
    assert len(WS.filters.list()) == 1

    # A different column set with the same name is NOT a duplicate skip.
    export2 = {"format": "winnow-filters/1", "filters": [
        {"name": "Shared", "col_names": ["C", "D"], "payload": {}},
    ]}
    added2 = WS.filters.import_all(export2, merge=True)
    assert added2 == 1
    assert len(WS.filters.list()) == 2


def test_header_nicknames_save_find_overwrites_and_delete():
    assert WS.header_nicknames.find(["A", "B"]) is None

    rec = WS.header_nicknames.save(["A", "B"], "My header set")
    assert rec["nickname"] == "My header set"
    found = WS.header_nicknames.find(["b", "a"])  # order-independent, case-insensitive
    assert found is not None
    assert found["nickname"] == "My header set"

    # Saving again for the same set overwrites in place, not a second record.
    WS.header_nicknames.save(["A", "B"], "Renamed")
    assert WS.header_nicknames.find(["A", "B"])["nickname"] == "Renamed"
    assert len(WS.header_nicknames.list()) == 1

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
