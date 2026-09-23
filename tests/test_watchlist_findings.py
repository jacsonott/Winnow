"""What a watchlist count cannot say on its own.

Three things the watchlist reported wrongly on a real case, all pinned
here in the terms they were wrong in:

* `mimikatz` and `mimikatz.exe` each reported 2,323 hits — the same rows,
  counted twice — and the summary added them into "9,297 hits" as though
  they were distinct findings. `watchlist_overlaps` is what notices, and
  `distinct_rows` is the honest number behind the total.
* A bare `0` meant both "every table was read and it is not in this case"
  and "nothing has looked yet". `watchlist_scans` records the units a scan
  completed, so `scanned_sources` / `scan_targets` tells them apart.
* Nothing answered "what has the watchlist found lately" without picking
  an indicator first. `latest_hits` answers it across all of them, one
  entry per flagged ROW rather than per hit.
"""

from __future__ import annotations

import pytest


def _csv(write_csv, name, rows):
    return write_csv([["Timestamp", "Process", "User"]] + rows, name=name)


def _ingest(store, write_csv, name, rows):
    return store.ingest_csv(_csv(write_csv, name, rows), name=name, build_fts=False)["id"]


@pytest.fixture
def two_tables(store, write_csv):
    """Six rows in one table, two in another. Three of the six carry
    "mimikatz.exe", which is also every row "mimikatz" can match — the
    shape that made one finding look like two."""
    a = _ingest(store, write_csv, "a.csv", [
        ["2026-03-10 10:00:00", "mimikatz.exe", "u0"],
        ["2026-03-11 10:01:00", "cmd.exe", "u1"],
        ["2026-03-12 10:02:00", "mimikatz.exe", "u2"],
        ["2026-03-13 10:03:00", "cmd.exe", "u3"],
        ["2026-03-14 10:04:00", "mimikatz.exe", "u4"],
        ["2026-03-15 10:05:00", "cmd.exe", "u5"],
    ])
    b = _ingest(store, write_csv, "b.csv", [
        ["2026-04-01 01:00:00", "psexesvc", "svc"],
        ["2026-04-02 01:00:00", "explorer.exe", "u9"],
    ])
    return a, b


# ---------------------------------------------------------------- overlap

def test_identical_hit_sets_are_reported_both_ways_and_counted_once(store, two_tables):
    wide = store.add_indicator("mimikatz", "filename")["id"]
    narrow = store.add_indicator("mimikatz.exe", "filename")["id"]
    store.scan_all()

    ov = store.watchlist_overview()
    counts = {i["id"]: i["hit_count"] for i in ov["indicators"]}
    # The symptom: two entries, the same rows, both counting them.
    assert counts[wide] == counts[narrow] == 3
    assert ov["total_hits"] == 6
    # ...and the honest number behind it.
    assert ov["distinct_rows"] == 3
    assert ov["overlap_checked"] is True

    rels = {(r["id"], r["other_id"]): r for r in ov["relations"]}
    assert set(rels) == {(wide, narrow), (narrow, wide)}
    assert rels[(wide, narrow)]["relation"] == "same"
    assert rels[(narrow, wide)]["relation"] == "same"
    assert rels[(wide, narrow)]["shared"] == 3


def test_a_contained_set_is_subset_one_way_and_superset_the_other(store, write_csv):
    _ingest(store, write_csv, "a.csv", [
        ["2026-03-10 10:00:00", "mimikatz.exe", "u0"],
        ["2026-03-11 10:01:00", "mimikatz-x64", "u1"],
    ])
    wide = store.add_indicator("mimikatz", "filename")["id"]
    narrow = store.add_indicator("mimikatz.exe", "filename")["id"]
    store.scan_all()

    ov = store.watchlist_overview()
    rels = {(r["id"], r["other_id"]): r for r in ov["relations"]}
    assert rels[(narrow, wide)]["relation"] == "subset"
    assert rels[(wide, narrow)]["relation"] == "superset"
    # Both directions agree on how many rows are shared: the smaller set.
    assert rels[(narrow, wide)]["shared"] == rels[(wide, narrow)]["shared"] == 1
    assert (ov["total_hits"], ov["distinct_rows"]) == (3, 2)


def test_a_partial_overlap_is_not_a_duplicate(store, write_csv):
    _ingest(store, write_csv, "a.csv", [
        ["2026-03-10 10:00:00", "alpha beta", "u0"],
        ["2026-03-11 10:01:00", "alpha", "u1"],
        ["2026-03-12 10:02:00", "beta", "u2"],
    ])
    store.add_indicator("alpha", "other")
    store.add_indicator("beta", "other")
    store.scan_all()
    ov = store.watchlist_overview()
    # They share a row, which is ordinary and worth nothing on the row.
    assert ov["relations"] == []
    assert (ov["total_hits"], ov["distinct_rows"]) == (4, 3)


def test_an_indicator_with_no_hits_is_never_called_a_duplicate(store, two_tables):
    store.add_indicator("mimikatz", "filename")
    empty = store.add_indicator("nothing-like-this-anywhere", "other")["id"]
    store.scan_all()
    ov = store.watchlist_overview()
    # The empty set is a subset of every other set; saying so would report
    # an indicator nobody has ever matched as redundant against all of them.
    assert [r for r in ov["relations"] if empty in (r["id"], r["other_id"])] == []


def test_overlap_is_skipped_rather_than_guessed_above_the_budget(store, two_tables, monkeypatch):
    store.add_indicator("mimikatz", "filename")
    store.add_indicator("mimikatz.exe", "filename")
    store.scan_all()
    monkeypatch.setattr(type(store), "WATCHLIST_OVERLAP_MAX_HITS", 2)
    ov = store.watchlist_overview()
    # "Not checked" is not "no duplicates" — the client must be able to
    # tell, or it would print a clean summary it has no grounds for.
    assert ov["overlap_checked"] is False
    assert ov["relations"] == [] and ov["distinct_rows"] is None
    assert ov["total_hits"] == 6


# -------------------------------------------------- scanned vs not scanned

def test_zero_hits_says_which_kind_of_zero_it_is(store, two_tables):
    wid = store.add_indicator("nothing-like-this-anywhere", "other")["id"]
    ov = store.watchlist_overview()
    assert ov["scan_targets"] == 2
    # Added, never scanned: a 0 here would not mean "not present".
    assert ov["indicators"][0]["hit_count"] == 0
    assert ov["indicators"][0]["scanned_sources"] == 0

    store.scan_all()
    ov = store.watchlist_overview()
    # Scanned every table and found nothing — the one an analyst can quote.
    assert ov["indicators"][0]["scanned_sources"] == ov["scan_targets"] == 2
    assert ov["indicators"][0]["hit_count"] == 0
    assert wid == ov["indicators"][0]["id"]


def test_a_table_imported_after_the_scan_makes_the_fraction_partial(store, write_csv, two_tables):
    store.add_indicator("mimikatz.exe", "filename")
    store.scan_all()
    assert store.watchlist_overview()["indicators"][0]["scanned_sources"] == 2

    _ingest(store, write_csv, "c.csv", [["2026-05-01 00:00:00", "cmd.exe", "u"]])
    ov = store.watchlist_overview()
    # Three tables now, two of them read for it: neither "clean" nor
    # "never scanned", and the difference is the new table.
    assert ov["scan_targets"] == 3
    assert ov["indicators"][0]["scanned_sources"] == 2


def test_a_scan_of_one_table_only_records_that_table(store, two_tables):
    a, _b = two_tables
    store.add_indicator("mimikatz.exe", "filename")
    store.scan_source(a)
    ov = store.watchlist_overview()
    assert (ov["indicators"][0]["scanned_sources"], ov["scan_targets"]) == (1, 2)


def test_dropping_a_table_takes_its_scan_record_with_it(store, two_tables):
    a, b = two_tables
    store.add_indicator("mimikatz.exe", "filename")
    store.scan_all()
    store.drop_source(b)
    ov = store.watchlist_overview()
    # SQLite hands b's id to the next import; a record left behind would
    # claim a file nothing has ever read was scanned clean.
    assert (ov["indicators"][0]["scanned_sources"], ov["scan_targets"]) == (1, 1)
    assert store.db.execute(
        "SELECT COUNT(*) FROM watchlist_scans WHERE source_id=?", (b,)).fetchone()[0] == 0


def test_deleting_an_indicator_takes_its_scan_record_with_it(store, two_tables):
    first = store.add_indicator("mimikatz.exe", "filename")["id"]
    store.scan_all()
    store.delete_indicator(first)
    # watchlist.id is not AUTOINCREMENT: the next indicator takes this id.
    again = store.add_indicator("something-else", "other")["id"]
    assert again == first
    ov = store.watchlist_overview()
    assert ov["indicators"][0]["scanned_sources"] == 0


def test_a_merge_is_in_neither_half_of_the_fraction(store, two_tables, write_csv):
    a = two_tables[0]
    c = _ingest(store, write_csv, "c.csv", [["2026-05-01 00:00:00", "mimikatz.exe", "u"]])
    store.create_merge("both", [a, c])
    store.add_indicator("mimikatz.exe", "filename")
    store.scan_all()
    ov = store.watchlist_overview()
    # Invariant #9: a merge has no src_N, its rows are its members' and are
    # scanned there. Counting it would make "scanned every table"
    # unreachable forever.
    assert ov["scan_targets"] == 3
    assert ov["indicators"][0]["scanned_sources"] == 3


# ------------------------------------------------------------------ merge

def test_merging_folds_the_redundant_entry_and_carries_its_auto_tag(store, two_tables):
    tag = store.upsert_tag(None, "IOC", "#fff", None)["id"]
    wide = store.add_indicator("mimikatz", "filename")["id"]
    narrow = store.add_indicator("mimikatz.exe", "filename", auto_tag_id=tag)["id"]
    store.scan_all()

    res = store.merge_indicators(wide, narrow)
    assert res["dropped"] == narrow and res["dropped_value"] == "mimikatz.exe"
    # The only thing that could have been lost by removing it: the rows are
    # already the keeper's, the auto-tag was not.
    assert res["auto_tag_moved"] is True
    assert res["kept"]["auto_tag_id"] == tag

    ov = store.watchlist_overview()
    assert [i["id"] for i in ov["indicators"]] == [wide]
    # One finding, reported once, with no overlap left to report.
    assert (ov["total_hits"], ov["distinct_rows"]) == (3, 3)
    assert ov["relations"] == []


def test_merging_refuses_to_drop_rows_the_keeper_does_not_have(store, write_csv):
    _ingest(store, write_csv, "a.csv", [
        ["2026-03-10 10:00:00", "mimikatz.exe", "u0"],
        ["2026-03-11 10:01:00", "mimikatz-x64", "u1"],
    ])
    wide = store.add_indicator("mimikatz", "filename")["id"]
    narrow = store.add_indicator("mimikatz.exe", "filename")["id"]
    store.scan_all()
    # Keeping the narrow one would throw away the row only the wide one
    # matched, silently — the check is against the hits on disk, so an
    # overlap report a scan out of date cannot talk anyone into it.
    with pytest.raises(ValueError) as e:
        store.merge_indicators(narrow, wide)
    assert "1 row" in str(e.value)
    assert len(store.list_indicators()) == 2
    # The other direction loses nothing and is allowed.
    store.merge_indicators(wide, narrow)
    assert [i["id"] for i in store.list_indicators()] == [wide]


def test_merging_keeps_the_keepers_own_auto_tag(store, two_tables):
    keep_tag = store.upsert_tag(None, "Keep", "#fff", None)["id"]
    drop_tag = store.upsert_tag(None, "Drop", "#fff", None)["id"]
    wide = store.add_indicator("mimikatz", "filename", auto_tag_id=keep_tag)["id"]
    narrow = store.add_indicator("mimikatz.exe", "filename", auto_tag_id=drop_tag)["id"]
    store.scan_all()
    res = store.merge_indicators(wide, narrow)
    assert res["auto_tag_moved"] is False
    assert res["kept"]["auto_tag_id"] == keep_tag


def test_merging_an_indicator_into_itself_is_refused(store, two_tables):
    wid = store.add_indicator("mimikatz", "filename")["id"]
    with pytest.raises(ValueError):
        store.merge_indicators(wid, wid)
    with pytest.raises(KeyError):
        store.merge_indicators(wid, wid + 999)


# ------------------------------------------------------------ latest hits

def test_latest_hits_opens_on_every_indicator_newest_first(store, two_tables):
    wide = store.add_indicator("mimikatz", "filename")["id"]
    narrow = store.add_indicator("mimikatz.exe", "filename")["id"]
    psx = store.add_indicator("psexesvc", "other")["id"]
    store.scan_all()

    res = store.latest_hits()
    assert [r["ts"] for r in res["rows"]] == [
        "2026-04-01 01:00:00", "2026-03-14 10:04:00",
        "2026-03-12 10:02:00", "2026-03-10 10:00:00",
    ]
    # One entry per flagged ROW, naming every indicator on it — the two
    # that cover the same rows fill the pane once, not twice.
    assert res["rows"][0]["watchlist_ids"] == [psx]
    assert res["rows"][1]["watchlist_ids"] == [wide, narrow]
    assert res["rows"][1]["source_name"] == "a.csv"
    # The row as one line, WITHOUT the timestamp: it already has a column
    # of its own on the line, and repeating it ate the width the rest of
    # the row needed.
    assert res["rows"][1]["body"] == "mimikatz.exe | u4"


def test_latest_hits_honours_its_limit(store, two_tables):
    store.add_indicator("mimikatz.exe", "filename")
    store.scan_all()
    res = store.latest_hits(limit=2)
    assert [r["ts"] for r in res["rows"]] == ["2026-03-14 10:04:00", "2026-03-12 10:02:00"]
    assert res["limit"] == 2


def test_a_table_with_no_datetime_column_sorts_last_rather_than_vanishing(store, write_csv):
    _ingest(store, write_csv, "a.csv", [["2026-03-10 10:00:00", "evil.exe", "u0"]])
    store.ingest_csv(write_csv([["Path"], ["C:\\evil.exe"]], name="n.csv"),
                     name="n.csv", build_fts=False)
    store.add_indicator("evil.exe", "filename")
    store.scan_all()
    rows = store.latest_hits()["rows"]
    # A row that cannot be placed in time is still a finding.
    assert [r["ts"] for r in rows] == ["2026-03-10 10:00:00", None]
    assert rows[1]["source_name"] == "n.csv"


def test_latest_hits_lists_the_member_not_the_merge(store, two_tables, write_csv):
    a = two_tables[0]
    c = _ingest(store, write_csv, "c.csv", [["2026-05-01 00:00:00", "mimikatz.exe", "u"]])
    store.create_merge("both", [a, c])
    store.add_indicator("mimikatz.exe", "filename")
    store.scan_all()
    rows = store.latest_hits()["rows"]
    # Invariant #9: a merge's rows are its members', scanned and flagged
    # there — a merge branch would list each of them a second time.
    assert {r["source_id"] for r in rows} == {a, c}
    assert rows[0]["source_name"] == "c.csv"


def test_latest_hits_is_empty_before_anything_is_scanned(store, two_tables):
    store.add_indicator("mimikatz.exe", "filename")
    assert store.latest_hits() == {"rows": [], "limit": store.LATEST_HITS}


# ------------------------------------------------------------------ routes

def test_routes_answer_the_overview_latest_and_merge_shapes(client, store, two_tables):
    wide = client.post("/api/watchlist", json={"value": "mimikatz", "kind": "filename"}).json()["id"]
    narrow = client.post("/api/watchlist", json={"value": "mimikatz.exe", "kind": "filename"}).json()["id"]
    client.post("/api/watchlist/scan")

    ov = client.get("/api/watchlist/overview").json()
    assert ov["scan_targets"] == 2 and ov["distinct_rows"] == 3 and ov["total_hits"] == 6
    assert {(r["id"], r["relation"]) for r in ov["relations"]} == {(wide, "same"), (narrow, "same")}
    assert all("scanned_sources" in i for i in ov["indicators"])

    latest = client.get("/api/watchlist/latest?limit=1").json()
    assert len(latest["rows"]) == 1 and latest["rows"][0]["watchlist_ids"] == [wide, narrow]

    bad = client.post("/api/watchlist/merge", json={"keep_id": wide, "drop_id": wide})
    assert bad.status_code == 400
    missing = client.post("/api/watchlist/merge", json={"keep_id": wide, "drop_id": 9999})
    assert missing.status_code == 404
    ok = client.post("/api/watchlist/merge", json={"keep_id": wide, "drop_id": narrow})
    assert ok.status_code == 200 and ok.json()["dropped"] == narrow
    assert [i["id"] for i in client.get("/api/watchlist").json()] == [wide]
