"""Advanced search (AND / OR / NOT term chips) on a merged table.

Invariant 9: whatever works on a table works on a merge, and the merge case
is part of the feature's tests. Contains and Regex on a merge are covered
in test_merge_mismatched.py; Advanced was not. It is also the mode whose SQL
differs member by member: each term goes through a member's trigram index
when that member has one built and is a scan when it has not, so a merge of
an indexed table and one still indexing runs two kinds of query for one
search.

The answer is worked out twice: by each member's own Advanced search on
its own table, and in Python from the rows as written. A merge row matches
when its own table's row does, over that table's own columns, whatever the
merge shows (test_merge_mismatched.py's "search sees a member's hidden
columns" is the Contains half of that).
"""

from __future__ import annotations

import pytest

# Mismatched on purpose: B spells two headers differently and adds Extra,
# C shares only Time and Msg, so a term can sit in a column only one
# member has.
TABLES = {
    "a": [["Time", "EventId", "Host", "Msg"],
          ["2024-01-01 10:00:00", "4624", "WS01", "alpha logon svchost.exe"],
          ["2024-01-01 11:00:00", "4688", "WS01", "beta process cmd.exe"],
          ["2024-01-01 12:00:00", "4625", "DC01", "failed logon"]],
    "b": [["TIME", "EventID", "Host", "Msg", "Extra"],
          ["2024-01-02 10:00:00", "4624", "WS02", "gamma logon", "svchost.exe"],
          ["2024-01-02 11:00:00", "4625", "WS02", "delta failed", "x"],
          ["2024-01-02 12:00:00", "4688", "DC01", "epsilon", "powershell.exe -enc"]],
    "c": [["Time", "Msg", "Other"],
          ["2024-01-03 09:00:00", "zeta note", "needle"],
          ["2024-01-03 10:00:00", "eta logon", "WS01"]],
}


def _t(term, connector="AND", exclude=False):
    return {"term": term, "connector": connector, "exclude": exclude}


QUERIES = {
    "one term": [_t("logon")],
    "and": [_t("logon"), _t("WS01")],
    "or, in a column one table lacks": [_t("svchost"), _t("powershell", "OR")],
    "leading not": [_t("logon", exclude=True)],
    "not in the middle": [_t("logon"), _t("WS0", exclude=True)],
    "and binds tighter than or": [_t("failed"), _t("needle", "OR"), _t("DC", "AND")],
    "short terms scan, any case": [_t("ws"), _t("46")],
    "nothing matches": [_t("zzz")],
    "not nothing is everything": [_t("zzz", exclude=True)],
}


def _expected(rows, terms):
    """What the chips mean, in Python: SQL's precedence (NOT on a term,
    then AND, then OR), each term a substring of the row's own columns
    joined by a space, ASCII case folded as LIKE folds it."""
    fold = lambda s: "".join(ch.lower() if ch.isascii() else ch for ch in s)
    hits = []
    for rid, row in enumerate(rows, start=1):
        text = fold(" ".join(row))
        groups: list[list[bool]] = []
        for i, t in enumerate(terms):
            atom = (fold(t["term"]) in text) != t["exclude"]
            if i == 0 or t["connector"] == "OR":
                groups.append([atom])
            else:
                groups[-1].append(atom)
        if any(all(g) for g in groups):
            hits.append(rid)
    return hits


def _keys(store, sid, terms):
    view = store.build_view(sid, {"source_id": sid, "filters": [], "sort": [],
                                  "search_mode": "advanced", "search_terms": terms})
    return {(r["source_id"], r["rid"]) for r in store.fetch_rows(view["view_id"], 0, 1000)["rows"]}


@pytest.fixture(params=["indexed", "not indexed", "one indexed, two not"])
def tables(request, store, write_csv):
    ids = {}
    for name, rows in TABLES.items():
        indexed = request.param == "indexed" or (request.param.startswith("one") and name == "a")
        ids[name] = store.ingest_csv(write_csv(rows, f"{name}.csv"), name=f"{name}.csv", build_fts=indexed)["id"]
        if indexed:
            assert store.wait_for_fts(ids[name], timeout=10)
    # Keep the rest scanning: a search on an unindexed table starts its
    # index in the background, which would change the state under test.
    store._ensure_fts_building = lambda *a, **k: None
    has_fts = {name: store._source_lite(sid)["has_fts"] for name, sid in ids.items()}
    assert has_fts == {name: request.param == "indexed" or (request.param.startswith("one") and name == "a")
                       for name in ids}
    return ids


@pytest.mark.parametrize("col_mode", ["all", "shared"])
@pytest.mark.parametrize("query", QUERIES.values(), ids=QUERIES.keys())
def test_a_merge_finds_what_each_member_finds_on_its_own(store, tables, query, col_mode):
    mid = store.create_merge(f"merge {col_mode}", list(tables.values()), col_mode=col_mode)["id"]
    want = {(sid, rid) for name, sid in tables.items() for rid in _expected(TABLES[name][1:], query)}
    each = set().union(*(_keys(store, sid, query) for sid in tables.values()))
    assert each == want
    assert _keys(store, mid, query) == want


def test_the_queries_are_not_vacuous():
    """Guards the table above from passing by asking nothing: between them
    the queries pick out rows from every member, and no two agree."""
    answers = {name: tuple(tuple(_expected(rows[1:], q)) for rows in TABLES.values())
               for name, q in QUERIES.items()}
    assert len(set(answers.values())) == len(answers)
    for i in range(len(TABLES)):
        assert any(a[i] for a in answers.values())


def test_the_merge_view_tags_and_exports_its_hits(store, tables):
    """Past the row set: the view an Advanced search builds on a merge is
    an ordinary merge view, so tagging it tags those rows on their own
    tables, and exporting it writes those rows and says whose they are."""
    mid = store.create_merge("m", list(tables.values()))["id"]
    view = store.build_view(mid, {"source_id": mid, "filters": [], "sort": [], "search_mode": "advanced",
                                  "search_terms": QUERIES["or, in a column one table lacks"]})
    hits = {(tables["a"], 1), (tables["b"], 1), (tables["b"], 3)}
    assert view["row_count"] == len(hits)
    tag = store.upsert_tag(None, "Hit", "#ff0000", "1")["id"]
    store.tag_view(view["view_id"], tag, True)
    with store.lock:
        tagged = {tuple(r) for r in store.db.execute("SELECT source_id, rid FROM row_tags WHERE tag_id=?", (tag,))}
    assert tagged == hits
    lines = "".join(store.export_view_csv(view["view_id"])).splitlines()
    assert lines[0].startswith("Line,Table,Tags,")
    assert sorted((ln.split(",")[1], ln.split(",")[0]) for ln in lines[1:]) == [("a.csv", "1"), ("b.csv", "1"), ("b.csv", "3")]
