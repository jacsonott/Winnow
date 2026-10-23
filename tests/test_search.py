"""store.py search: Contains mode (true substring, via a trigram FTS5
index when ready, LIKE fallback otherwise), regex, Advanced AND/OR/NOT
(incl. the leading-NOT regression), the background/lazy index build, and
search_all_sources."""

from __future__ import annotations

import pytest

from winnow.store import _blob_expr, _contains_clause, _trigram_match_query, TRIGRAM_MIN_LEN, q


def _cells(store, view_id):
    return [r["cells"] for r in store.fetch_rows(view_id, 0, 100)["rows"]]


def test_contains_matches_substring_buried_mid_token(ingested):
    # "jacso" is buried inside the compound token
    # `c:\users\jacso\desktop\file.txt` — correct either via the LIKE
    # fallback (index not built yet) or the trigram index (ready); this
    # doesn't wait for the background build, so it's a real assertion that
    # results are correct regardless of which path answers the query.
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": [], "search": "jacso", "search_mode": "contains"}
    view = store.build_view(source_id, spec)
    rows = _cells(store, view["view_id"])
    values = {c[2] for c in rows}  # User column
    # Matches both "ACME\jacson" (User) and the row whose CommandLine
    # contains "...\jacso\..." mid-path.
    assert len(rows) == 2
    assert "ACME\\jacson" in values


def test_contains_uses_trigram_index_once_built(ingested):
    # Explicitly waits for the background build so this exercises the
    # trigram-indexed path itself (not just "correct either way").
    store, source_id = ingested
    assert store.wait_for_fts(source_id, timeout=5)
    spec = {"source_id": source_id, "filters": [], "sort": [], "search": "jacso", "search_mode": "contains"}
    view = store.build_view(source_id, spec)
    rows = _cells(store, view["view_id"])
    assert len(rows) == 2
    assert {c[2] for c in rows} == {"ACME\\jacson", "ACME\\bob"}


def test_contains_works_without_fts_too(store, write_csv):
    path = write_csv([
        ["Path"],
        ["C:\\users\\jacso\\desktop\\file.txt"],
        ["C:\\Windows\\System32\\cmd.exe"],
    ])
    rec = store.ingest_csv(path, name="nofts.csv", build_fts=False)
    assert rec["has_fts"] == 0
    spec = {"source_id": rec["id"], "filters": [], "sort": [], "search": "jacso", "search_mode": "contains"}
    view = store.build_view(rec["id"], spec)
    assert len(_cells(store, view["view_id"])) == 1


def test_contains_short_term_falls_back_to_like(ingested):
    # "ja" is under the trigram tokenizer's 3-character floor — the index
    # has nothing to look up for it, so this must still scan even once the
    # index is built, and still find the real substring match.
    store, source_id = ingested
    assert store.wait_for_fts(source_id, timeout=5)
    spec = {"source_id": source_id, "filters": [], "sort": [], "search": "ja", "search_mode": "contains"}
    view = store.build_view(source_id, spec)
    rows = _cells(store, view["view_id"])
    assert len(rows) == 2  # "ACME\jacson" and the "...\jacso\..." path row


def test_regex_mode(ingested):
    store, source_id = ingested
    spec = {"source_id": source_id, "filters": [], "sort": [], "search": r"46(24|88)", "search_mode": "regex"}
    view = store.build_view(source_id, spec)
    event_ids = {c[1] for c in _cells(store, view["view_id"])}
    assert event_ids == {"4624", "4688"}


def test_advanced_and_or(ingested):
    # Once the trigram index is built, a bare term is a genuine substring
    # match, not a whole-token match — "ACME" now correctly matches inside
    # the compound token "ACME\jacson", which the old word tokenizer
    # couldn't do (the same class of limitation Contains mode's fix
    # addressed). Waits for the build so this exercises that path.
    store, source_id = ingested
    assert store.wait_for_fts(source_id, timeout=5)
    terms = [
        {"term": "4624", "connector": "AND", "exclude": False},
        {"term": "svchost.exe", "connector": "AND", "exclude": False},
    ]
    spec = {"source_id": source_id, "filters": [], "sort": [], "search_mode": "advanced", "search_terms": terms}
    view = store.build_view(source_id, spec)
    assert len(_cells(store, view["view_id"])) == 1

    terms_or = [
        {"term": "svchost.exe", "connector": "AND", "exclude": False},
        {"term": "ACME", "connector": "OR", "exclude": False},
    ]
    spec_or = {**spec, "search_terms": terms_or}
    view_or = store.build_view(source_id, spec_or)
    # svchost.exe (row 1) OR ACME-anything (rows 1-3, everyone but the
    # NT AUTHORITY\SYSTEM row) -> 3 distinct rows.
    assert len(_cells(store, view_or["view_id"])) == 3


def test_advanced_short_term_falls_back_to_like(ingested):
    # "ja" is under the 3-char trigram floor even inside an Advanced-mode
    # term chip — must still find the real match via a per-term scan in
    # _advanced_clause, not silently drop the term.
    store, source_id = ingested
    assert store.wait_for_fts(source_id, timeout=5)
    terms = [{"term": "ja", "connector": "AND", "exclude": False}]
    spec = {"source_id": source_id, "filters": [], "sort": [], "search_mode": "advanced", "search_terms": terms}
    view = store.build_view(source_id, spec)
    assert len(_cells(store, view["view_id"])) == 2


def test_advanced_leading_not(ingested):
    # _advanced_clause's own docstring documents a prior bug: FTS5's NOT
    # has no unary form, and an earlier version silently dropped a *leading*
    # term's exclude flag, so "NOT svchost" as the first term matched rows
    # *containing* svchost instead of excluding them. This is the regression
    # test for that fix — it must exclude, not include.
    store, source_id = ingested
    assert store.wait_for_fts(source_id, timeout=5)
    terms = [{"term": "svchost.exe", "connector": "AND", "exclude": True}]
    spec = {"source_id": source_id, "filters": [], "sort": [], "search_mode": "advanced", "search_terms": terms}
    view = store.build_view(source_id, spec)
    rows = _cells(store, view["view_id"])
    assert len(rows) == 3
    assert all("svchost.exe" not in " ".join(c or "" for c in row) for row in rows)


def test_advanced_leading_not_without_fts(store, write_csv):
    # Same regression, on a table with no index, where every term is a
    # scan.
    path = write_csv([
        ["Process"],
        ["svchost.exe"],
        ["cmd.exe"],
    ])
    rec = store.ingest_csv(path, name="nofts2.csv", build_fts=False)
    terms = [{"term": "svchost.exe", "connector": "AND", "exclude": True}]
    spec = {"source_id": rec["id"], "filters": [], "sort": [], "search_mode": "advanced", "search_terms": terms}
    view = store.build_view(rec["id"], spec)
    rows = _cells(store, view["view_id"])
    assert rows == [["cmd.exe"]]


def test_trigram_match_query_routing():
    # One quoted string per distinct trigram, ANDed: a longer string would
    # be a phrase, which a detail=none index refuses.
    assert _trigram_match_query("jacso") == '"jac" AND "acs" AND "cso"'
    assert _trigram_match_query("abab") == '"aba" AND "bab"'   # deduped
    assert _trigram_match_query('a"bc') == '"a""b" AND """bc"'  # a quote doubles
    # A LIKE wildcard is an ordinary character to the index (it is the
    # check that keeps it literal), so these terms use it now.
    assert _trigram_match_query("100%") == '"100" AND "00%"'
    assert _trigram_match_query("under_score") is not None
    # Under the trigram floor there is nothing to look up.
    assert _trigram_match_query("ja") is None
    assert _trigram_match_query("ab\x00c") is None


def test_contains_clause_asks_each_column_unless_the_term_has_a_space():
    clause, params = _contains_clause(["A", "B"], "jacso")
    assert params == [] and "COALESCE" not in clause
    assert '"A" LIKE \'%jacso%\' ESCAPE' in clause and '"B" LIKE \'%jacso%\' ESCAPE' in clause
    # A space can straddle the separator between two columns: that term
    # asks the joined text — after each word of three characters or more
    # has been found in some column on its own.
    clause, params = _contains_clause(["A", "B"], "o w")
    assert _blob_expr(["A", "B"]) in clause and "%o w%" in clause and params == []
    assert "'%o%'" not in clause and "'%w%'" not in clause
    clause, params = _contains_clause(["A", "B"], "hello wo")
    assert clause.index("'%hello%'") < clause.index(_blob_expr(["A", "B"]))
    assert '"B" LIKE \'%hello%\' ESCAPE' in clause and "'%wo%'" not in clause and params == []
    # The longest three words at most: a pasted sentence doesn't nest a
    # CASE per word.
    clause, _ = _contains_clause(["A"], "aaaa bbbbbb ccc ddddd eeeeeee")
    assert clause.count("CASE WHEN") == 3 and "'%ccc%'" not in clause and "'%aaaa%'" not in clause
    assert clause.index("'%eeeeeee%'") < clause.index("'%bbbbbb%'") < clause.index("'%ddddd%'")
    # A NUL can't be written into the SQL: that one binds.
    clause, params = _contains_clause(["A", "B"], "a\x00b")
    assert params == ["%a\x00b%"]
    # With an index, its candidates come first and the same check follows.
    clause, params = _contains_clause(["A", "B"], "jacso", q("fts_1"))
    assert clause.startswith('(rid IN (SELECT rowid FROM "fts_1" WHERE "fts_1" MATCH ?) AND (CASE WHEN')
    assert params == ['"jac" AND "acs" AND "cso"']


# One table, every way a substring can go wrong: a match that straddles
# two columns (only with a space), NULL cells, LIKE wildcards and the
# escape character, both quote characters, non-ASCII case (LIKE folds ASCII
# only, the index folds every script), and a term found only in a late
# column of a row.
EQUIV_ROWS = [
    ["A", "B", "C"],
    ["hello", "world", ""],
    ["progress 100% done", "", "x"],
    ["progress 100X done", "", "y"],
    ["under_score", "", ""],
    ["underXscore", "", ""],
    ["C:\\Users\\jacso", "it's", 'say "hi"'],
    ["Администратор", "", ""],
    ["", "", "ADMINISTRATOR late"],
    ["lo w", "o", "rld"],
]
EQUIV_TERMS = ["hello world", "o w", "lo", "100%", "00%", "under_score", "r_s", "\\users\\", "jacso",
               "it's", '"hi"', "администратор", "Администратор", "administrator", "o", "zzz", "llo wor",
               "w o rld", "lo w o rld", "rld o", "w o r", "hello  world", "progress 100% done", "100% done",
               "ADMINISTRATOR lat", "world hello"]


def _reference(rows, term):
    """What Contains means, in Python: the row's columns joined by a space
    hold the term, folding ASCII case only (SQLite's LIKE)."""
    fold = lambda s: "".join(ch.lower() if ch.isascii() else ch for ch in s)
    return [i + 1 for i, r in enumerate(rows) if fold(term) in fold(" ".join(r))]


@pytest.mark.parametrize("indexed", [False, True])
def test_contains_matches_the_joined_text_exactly_indexed_or_not(store, write_csv, indexed):
    path = write_csv(EQUIV_ROWS, name=f"equiv{int(indexed)}.csv")
    rec = store.ingest_csv(path, name="equiv.csv", build_fts=indexed)
    if indexed:
        assert store.wait_for_fts(rec["id"], timeout=10)
    else:
        store._ensure_fts_building = lambda *a, **k: None   # keep it scanning
    sid = rec["id"]
    # NULL cells, not just empty ones: CSV ingest stores "" — put real
    # NULLs in, the case COALESCE in the joined text exists for.
    with store.lock, store.db:
        store.db.execute(f"UPDATE {q(rec['table_name'])} SET C=NULL WHERE C=''")
    for term in EQUIV_TERMS:
        want = _reference(EQUIV_ROWS[1:], term)
        spec = {"source_id": sid, "filters": [], "sort": [], "search": term, "search_mode": "contains"}
        v = store.build_view(sid, spec)
        got = [r["rid"] for r in store.fetch_rows(v["view_id"], 0, 100)["rows"]]
        assert got == want, term
        # Excluded in Advanced is the exact complement — the check is never
        # NULL, so a row with a NULL cell is not lost from both sides.
        adv = {"source_id": sid, "filters": [], "sort": [], "search_mode": "advanced",
               "search_terms": [{"term": term, "connector": "AND", "exclude": True}]}
        v = store.build_view(sid, adv)
        got = [r["rid"] for r in store.fetch_rows(v["view_id"], 0, 100)["rows"]]
        assert got == [i for i in range(1, len(EQUIV_ROWS)) if i not in want], term
        # Search all counts the same rows.
        hits = store.search_all_sources(query=term, source_ids=[sid])
        assert (hits[0]["match_count"] if hits else 0) == len(want), term


def test_a_wildcard_term_takes_the_index(store, write_csv):
    path = write_csv([["Note"], ["under_score"], ["underXscore"]])
    rec = store.ingest_csv(path, name="wild3.csv")
    assert store.wait_for_fts(rec["id"], timeout=5)
    src = store._source_lite(rec["id"])
    where, params = store._compile_where(rec["id"], src, {"search": "under_score"}, {"Note": "text"})
    assert "MATCH" in where


def test_contains_binds_nothing_per_column(store, write_csv, stock_sqlite_limits):
    # A merge compiles the search once per member: a parameter per column
    # would be thousands on wide members. The pattern is inlined instead.
    header = [f"c{i}" for i in range(600)]
    path = write_csv([header, ["x"] * 599 + ["needle"]], name="wide_search.csv")
    rec = store.ingest_csv(path, name="wide_search.csv", build_fts=False)
    src = store._source_lite(rec["id"])
    where, params = store._compile_where(rec["id"], src, {"search": "needle"}, {})
    assert params == []
    v = store.build_view(rec["id"], {"source_id": rec["id"], "filters": [], "sort": [],
                                     "search": "needle", "search_mode": "contains"})
    assert v["row_count"] == 1


def test_blob_expr_concatenates_every_column():
    assert _blob_expr(["A", "B"]) == "COALESCE(\"A\",'') || ' ' || COALESCE(\"B\",'')"


def test_trigram_min_len_is_three():
    assert TRIGRAM_MIN_LEN == 3


def test_background_build_flips_has_fts_and_is_idempotent(ingested):
    store, source_id = ingested
    assert store.get_source(source_id)["has_fts"] in (0, 1)  # may have already finished for a tiny fixture
    assert store.wait_for_fts(source_id, timeout=5)
    assert store.get_source(source_id)["has_fts"] == 1
    # Calling it again once ready is a cheap no-op, not a second rebuild.
    store._ensure_fts_building(source_id)
    assert source_id not in store._fts_threads or not store._fts_threads[source_id].is_alive()


def test_legacy_word_tokenized_fts_gets_downgraded_on_open(store, write_csv, case_path):
    # Simulates a pre-upgrade case file: has_fts=1 pointing at an
    # old *word*-tokenized fts_<id> table. Re-opening that same case file
    # with a fresh Store must not trust the stale flag.
    path = write_csv([["Process"], ["svchost.exe"]])
    rec = store.ingest_csv(path, name="legacy.csv", build_fts=False)
    source_id = rec["id"]
    fts = q(f"fts_{source_id}")
    with store.lock, store.db:
        store.db.execute(
            f"CREATE VIRTUAL TABLE {fts} USING fts5(Process, content={q(rec['table_name'])}, "
            "content_rowid='rid', tokenize=\"unicode61 tokenchars '.-_\\@:'\")"
        )
        store.db.execute("UPDATE sources SET has_fts=1 WHERE id=?", (source_id,))
    assert store.get_source(source_id)["has_fts"] == 1
    store.close()

    from winnow.store import Store, DEFAULT_TAGS
    reopened = Store(case_path, default_tags=DEFAULT_TAGS)
    try:
        assert reopened.get_source(source_id)["has_fts"] == 0
    finally:
        reopened.close()


def test_legacy_fat_trigram_fts_gets_downgraded_and_dropped_on_open(store, write_csv, case_path):
    # The first-generation trigram shape (multi-column, detail=full) is
    # also stale now: not the one-doc-column shape the search query is
    # written for, and ~6x bigger than the index needs to be. Opening such a
    # case must reset has_fts AND reclaim the space by dropping the table
    # (on the background janitor thread).
    path = write_csv([["Process"], ["svchost.exe"]])
    rec = store.ingest_csv(path, name="fat.csv", build_fts=False)
    source_id = rec["id"]
    fts_name = f"fts_{source_id}"
    with store.lock, store.db:
        store.db.execute(
            f"CREATE VIRTUAL TABLE {q(fts_name)} USING fts5(Process, content={q(rec['table_name'])}, "
            "content_rowid='rid', tokenize='trigram')"
        )
        store.db.execute("UPDATE sources SET has_fts=1 WHERE id=?", (source_id,))
    store.close()

    from winnow.store import Store, DEFAULT_TAGS
    reopened = Store(case_path, default_tags=DEFAULT_TAGS)
    try:
        assert reopened.get_source(source_id)["has_fts"] == 0
        reopened.wait_for_fts_maintenance(timeout=5)
        with reopened.lock:
            row = reopened.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (fts_name,)
            ).fetchone()
        assert row is None  # stale fat index dropped, not left as dead weight
        # And the source still searches correctly (LIKE fallback, then a
        # fresh-shape index once the lazy rebuild completes).
        spec = {"source_id": source_id, "filters": [], "sort": [], "search": "svchost", "search_mode": "contains"}
        view = reopened.build_view(source_id, spec)
        assert view["row_count"] == 1
    finally:
        reopened.close()


def test_fts_index_shape_is_detail_none_single_doc_over_view(ingested):
    # The DDL contract the size win depends on: detail=none + columnsize=0,
    # one doc column, content= the src_<id>_doc view (which must exist).
    store, source_id = ingested
    assert store.wait_for_fts(source_id, timeout=5)
    with store.lock:
        sql = store.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (f"fts_{source_id}",)
        ).fetchone()[0]
        view_row = store.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='view' AND name=?", (f"src_{source_id}_doc",)
        ).fetchone()
    assert "detail=none" in sql
    assert "columnsize=0" in sql
    assert "tokenize='trigram'" in sql
    assert view_row is not None


def test_contains_wildcard_chars_stay_literal_with_index_built(store, write_csv):
    # '%' and '_' in a search term must match literally, not as LIKE
    # wildcards — with the index ready too, where they are ordinary
    # characters to its trigrams and the escaped check after it is what
    # keeps them literal. "under_score" must NOT match "underXscore".
    path = write_csv([
        ["Note"],
        ["progress 100% done"],
        ["progress 100X done"],
        ["under_score"],
        ["underXscore"],
    ])
    rec = store.ingest_csv(path, name="wild.csv")
    assert store.wait_for_fts(rec["id"], timeout=5)
    for term, expected in (("100%", ["progress 100% done"]), ("under_score", ["under_score"])):
        spec = {"source_id": rec["id"], "filters": [], "sort": [], "search": term, "search_mode": "contains"}
        view = store.build_view(rec["id"], spec)
        assert [c[0] for c in _cells(store, view["view_id"])] == expected, term


def test_advanced_wildcard_term_stays_literal(store, write_csv):
    path = write_csv([["Note"], ["under_score"], ["underXscore"]])
    rec = store.ingest_csv(path, name="wild2.csv")
    assert store.wait_for_fts(rec["id"], timeout=5)
    terms = [{"term": "under_score", "connector": "AND", "exclude": False}]
    spec = {"source_id": rec["id"], "filters": [], "sort": [], "search_mode": "advanced", "search_terms": terms}
    view = store.build_view(rec["id"], spec)
    assert [c[0] for c in _cells(store, view["view_id"])] == ["under_score"]


def test_search_all_sources_plain_substring(store, write_csv):
    p1 = write_csv([["User"], ["ACME\\jacson"]], name="s1.csv")
    p2 = write_csv([["User"], ["ACME\\admin"]], name="s2.csv")
    rec1 = store.ingest_csv(p1, name="s1.csv")
    store.ingest_csv(p2, name="s2.csv")
    hits = store.search_all_sources(query="jacs")
    assert len(hits) == 1
    assert hits[0]["source_id"] == rec1["id"]
    assert hits[0]["match_count"] == 1
    assert store.search_all_sources(query="nomatch") == []


def test_search_all_sources_plain_substring_via_trigram(store, write_csv):
    p1 = write_csv([["User"], ["ACME\\jacson"]], name="t1.csv")
    rec1 = store.ingest_csv(p1, name="t1.csv")
    assert store.wait_for_fts(rec1["id"], timeout=5)
    hits = store.search_all_sources(query="jacs")
    assert len(hits) == 1
    assert hits[0]["match_count"] == 1


def test_search_all_sources_advanced_terms(store, write_csv):
    p1 = write_csv([["Process"], ["svchost.exe"], ["cmd.exe"]], name="a1.csv")
    p2 = write_csv([["Process"], ["lsass.exe"]], name="a2.csv")
    rec1 = store.ingest_csv(p1, name="a1.csv")
    store.ingest_csv(p2, name="a2.csv")
    terms = [{"term": "svchost.exe", "connector": "AND", "exclude": False}]
    hits = store.search_all_sources(terms=terms)
    assert len(hits) == 1
    assert hits[0]["source_id"] == rec1["id"]


def test_search_all_sources_reports_uncapped_counts_exactly(store, write_csv):
    rows = [["Process"]] + [["svchost.exe"]] * 5 + [["cmd.exe"]] * 3
    rec = store.ingest_csv(write_csv(rows, "c1.csv"), name="c1.csv", build_fts=False)
    hits = store.search_all_sources(query="svchost")
    # `terms` is the per-term breakdown, empty for a bare query — see
    # _or_of_positive_terms.
    assert hits == [{"source_id": rec["id"], "name": "c1.csv", "match_count": 5, "capped": False, "terms": []}]


def test_search_all_sources_caps_the_count(store, write_csv, monkeypatch):
    """The modal only ranks which tables hit and roughly how hard; an exact
    count over a source whose index isn't built is a full scan of every
    matching row, so it stops at the cap and says so."""
    from winnow import store as store_module

    monkeypatch.setattr(store_module, "SEARCH_ALL_COUNT_CAP", 3)
    rows = [["Process"]] + [["svchost.exe"]] * 10
    store.ingest_csv(write_csv(rows, "c2.csv"), name="c2.csv", build_fts=False)
    hit = store.search_all_sources(query="svchost")[0]
    assert hit["match_count"] == 3
    assert hit["capped"] is True


def test_search_all_sources_caps_on_the_indexed_and_advanced_paths_too(store, write_csv, monkeypatch):
    from winnow import store as store_module

    monkeypatch.setattr(store_module, "SEARCH_ALL_COUNT_CAP", 2)
    rows = [["Process"]] + [["svchost.exe"]] * 6
    rec = store.ingest_csv(write_csv(rows, "c3.csv"), name="c3.csv")
    assert store.wait_for_fts(rec["id"], timeout=5)
    assert store.search_all_sources(query="svchost")[0] == {
        "source_id": rec["id"], "name": "c3.csv", "match_count": 2, "capped": True, "terms": [],
    }
    terms = [{"term": "svchost", "connector": "AND", "exclude": False}]
    assert store.search_all_sources(terms=terms)[0]["capped"] is True


class _CountingLock:
    """Delegates to a real lock while counting how many times it goes fully
    unheld — i.e. how many separate units of work an operation splits into,
    rather than how many times it re-enters an outer hold. Counts every
    thread on purpose: the sweep spreads its per-source work across
    threads, and each release-to-zero is a unit of work whichever thread
    did it. What that means for the reader: `depth` is a live snapshot,
    so assert it through _settled_unheld (below) rather than at one
    instant — a background index build the sweep kicks off can hold the
    lock for a moment after the sweep returns."""

    def __init__(self, inner):
        self._inner = inner
        self.depth = 0
        self.released_to_zero = 0

    def acquire(self, *a, **kw):
        got = self._inner.acquire(*a, **kw)
        if got:
            self.depth += 1
        return got

    def release(self):
        self._inner.release()
        self.depth -= 1
        if self.depth == 0:
            self.released_to_zero += 1

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def _settled_unheld(counting, timeout=3.0):
    """True once nothing holds the lock — polled, because a transient
    background hold (a column-index build) can overlap the instant after
    an operation returns, while a hold the operation LEAKED never lets go."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if counting.depth == 0:
            return True
        time.sleep(0.02)
    return counting.depth == 0


def test_search_all_sources_does_not_hold_the_lock_across_sources(store, write_csv):
    """Invariant #4: hold the lock for one unit of committed work, not for a
    whole loop over the case. This sweep is N full scans back to back on a
    case whose indexes aren't built, and wrapping all of them in a single
    `with self.lock:` froze every other request for its entire duration.

    Asserted structurally rather than by racing a competing thread: under
    the old shape the lock is taken once at the top and only goes unheld at
    the very end, so it returns to depth 0 exactly once no matter how many
    sources there are."""
    for i in range(4):
        store.ingest_csv(
            write_csv([["Process"]] + [["svchost.exe"]] * 200, name=f"lk{i}.csv"),
            name=f"lk{i}.csv", build_fts=False,
        )

    counting = _CountingLock(store.lock)
    store.lock = counting
    try:
        hits = store.search_all_sources(query="svchost")
    finally:
        store.lock = counting._inner
    assert len(hits) == 4
    assert _settled_unheld(counting)
    assert counting.released_to_zero >= 4  # at least one release per source scanned


# ------------------------------------------------- search-all as a background job

def test_search_all_job_runs_to_completion_with_same_hits_as_the_sync_sweep(store, write_csv):
    """The job layer is the same sweep on a thread — not a second
    implementation — so its finished hits must match search_all_sources
    exactly, including the heaviest-first ordering."""
    store.ingest_csv(write_csv([["Process"]] + [["svchost.exe"]] * 5, name="a.csv"),
                     name="a.csv", build_fts=False)
    store.ingest_csv(write_csv([["Process"]] + [["svchost.exe"]] * 2, name="b.csv"),
                     name="b.csv", build_fts=False)
    store.ingest_csv(write_csv([["Process"]] + [["lsass.exe"]] * 3, name="c.csv"),
                     name="c.csv", build_fts=False)

    expected = store.search_all_sources(query="svchost")

    started = store.start_search_all_job(query="svchost")
    assert started["done"] is False or started["done"] is True  # may finish very fast
    job = store.wait_for_search_all_job(timeout=10)
    assert job["done"] is True
    assert job["error"] is None
    assert job["hits"] == expected
    # Progress covers every non-errored source, not just the matching ones.
    assert job["scanned"] == job["total"] == 3


def test_search_all_job_reports_progress_over_every_source_not_just_hits(store, write_csv):
    for i in range(3):
        store.ingest_csv(write_csv([["Process"]] + [["nomatch"]], name=f"n{i}.csv"),
                         name=f"n{i}.csv", build_fts=False)
    store.start_search_all_job(query="svchost")
    job = store.wait_for_search_all_job(timeout=10)
    assert job["hits"] == []
    assert job["scanned"] == 3
    assert job["total"] == 3


def test_search_all_job_snapshot_is_available_by_id_and_none_for_others(store, write_csv):
    store.ingest_csv(write_csv([["Process"]] + [["svchost.exe"]], name="a.csv"),
                     name="a.csv", build_fts=False)
    started = store.start_search_all_job(query="svchost")
    store.wait_for_search_all_job(timeout=10)

    assert store.get_search_all_job(started["job_id"])["job_id"] == started["job_id"]
    assert store.get_search_all_job(started["job_id"] + 999) is None


def test_starting_a_second_job_supersedes_the_first(store, write_csv):
    store.ingest_csv(write_csv([["Process"]] + [["svchost.exe"]], name="a.csv"),
                     name="a.csv", build_fts=False)
    first = store.start_search_all_job(query="svchost")
    second = store.start_search_all_job(query="lsass")
    assert second["job_id"] != first["job_id"]
    # Only the newest job is addressable — a poller on the old id gets None
    # (the route turns that into a 404) rather than a stale result set.
    assert store.get_search_all_job(first["job_id"]) is None
    store.wait_for_search_all_job(timeout=10)


def test_search_all_job_with_no_terms_finishes_empty(store, write_csv):
    store.ingest_csv(write_csv([["Process"]] + [["svchost.exe"]], name="a.csv"),
                     name="a.csv", build_fts=False)
    store.start_search_all_job(query="")
    job = store.wait_for_search_all_job(timeout=10)
    assert job["done"] is True
    assert job["hits"] == []
    assert job["total"] == 0


def test_cancel_marks_the_job_cancelled(store, write_csv):
    store.ingest_csv(write_csv([["Process"]] + [["svchost.exe"]], name="a.csv"),
                     name="a.csv", build_fts=False)
    started = store.start_search_all_job(query="svchost")
    assert store.cancel_search_all_job(started["job_id"]) is True
    job = store.wait_for_search_all_job(timeout=10)
    assert job["cancelled"] is True
    assert job["done"] is True
    # Cancelling an id that isn't the live job is a no-op, not an error.
    assert store.cancel_search_all_job(started["job_id"] + 999) is False


def test_search_all_job_does_not_hold_the_lock_across_sources(store, write_csv):
    """Same invariant #4 guarantee as the synchronous sweep — the job worker
    must not have reintroduced a loop-wide hold. The job record has its own
    lock precisely so updating progress between sources doesn't touch
    self.lock at all."""
    for i in range(4):
        store.ingest_csv(
            write_csv([["Process"]] + [["svchost.exe"]] * 200, name=f"jk{i}.csv"),
            name=f"jk{i}.csv", build_fts=False,
        )

    counting = _CountingLock(store.lock)
    store.lock = counting
    try:
        store.start_search_all_job(query="svchost")
        job = store.wait_for_search_all_job(timeout=20)
    finally:
        store.lock = counting._inner
    assert len(job["hits"]) == 4
    assert _settled_unheld(counting)
    assert counting.released_to_zero >= 4


def test_search_all_breaks_a_pasted_list_down_per_term(store, write_csv):
    """A pasted IOC list is a plain OR of positive terms, and the analyst
    needs to know *which* indicators hit a table, not just that one did."""
    rows = [["Process", "Host"]]
    rows += [["evil.exe", "WS1"]] * 3
    rows += [["cmd.exe", "WS2"]] * 2
    rows += [["notepad.exe", "WS3"]]
    rec = store.ingest_csv(write_csv(rows, "b1.csv"), name="b1.csv", build_fts=False)

    terms = [
        {"term": "evil.exe", "connector": "OR", "exclude": False},
        {"term": "cmd.exe", "connector": "OR", "exclude": False},
        {"term": "nothing-here", "connector": "OR", "exclude": False},
    ]
    hit = store.search_all_sources(terms=terms)[0]
    assert hit["source_id"] == rec["id"]
    assert hit["match_count"] == 5  # the union, unchanged
    # Heaviest first, and a term that matched nothing gets no entry at all.
    assert hit["terms"] == [
        {"term": "evil.exe", "match_count": 3, "capped": False},
        {"term": "cmd.exe", "match_count": 2, "capped": False},
    ]


def test_search_all_breakdown_counts_overlapping_terms_separately(store, write_csv):
    # Two terms both matching the same rows: the per-term counts describe
    # each term on its own, so they don't have to sum to match_count.
    rows = [["Path"]] + [["C:\\Windows\\evil.exe"]] * 4
    store.ingest_csv(write_csv(rows, "b2.csv"), name="b2.csv", build_fts=False)
    terms = [
        {"term": "Windows", "connector": "OR", "exclude": False},
        {"term": "evil.exe", "connector": "OR", "exclude": False},
    ]
    hit = store.search_all_sources(terms=terms)[0]
    assert hit["match_count"] == 4
    assert [t["match_count"] for t in hit["terms"]] == [4, 4]


def test_search_all_skips_the_breakdown_for_a_constrained_query(store, write_csv):
    # AND/NOT terms constrain each other — a count for one in isolation
    # describes a query the analyst never asked for, so there's no breakdown.
    rows = [["Process", "Host"], ["evil.exe", "WS1"], ["evil.exe", "WS2"]]
    store.ingest_csv(write_csv(rows, "b3.csv"), name="b3.csv", build_fts=False)
    and_terms = [
        {"term": "evil.exe", "connector": "AND", "exclude": False},
        {"term": "WS1", "connector": "AND", "exclude": False},
    ]
    assert store.search_all_sources(terms=and_terms)[0]["terms"] == []
    not_terms = [
        {"term": "evil.exe", "connector": "OR", "exclude": False},
        {"term": "WS2", "connector": "OR", "exclude": True},
    ]
    assert store.search_all_sources(terms=not_terms)[0]["terms"] == []


def test_search_all_breakdown_matches_the_indexed_path(store, write_csv):
    # Same answers whether the term routes through the trigram index or the
    # LIKE fallback — the breakdown reuses _search_all_count_sql for exactly
    # this reason.
    rows = [["Process"]] + [["evil.exe"]] * 3 + [["cmd.exe"]] * 2
    rec = store.ingest_csv(write_csv(rows, "b4.csv"), name="b4.csv")
    assert store.wait_for_fts(rec["id"], timeout=5)
    terms = [
        {"term": "evil.exe", "connector": "OR", "exclude": False},
        {"term": "cmd.exe", "connector": "OR", "exclude": False},
    ]
    hit = store.search_all_sources(terms=terms)[0]
    assert [(t["term"], t["match_count"]) for t in hit["terms"]] == [("evil.exe", 3), ("cmd.exe", 2)]


def test_search_all_breakdown_caps_each_term_independently(store, write_csv, monkeypatch):
    from winnow import store as store_module

    monkeypatch.setattr(store_module, "SEARCH_ALL_COUNT_CAP", 2)
    rows = [["Process"]] + [["evil.exe"]] * 5 + [["cmd.exe"]]
    store.ingest_csv(write_csv(rows, "b5.csv"), name="b5.csv", build_fts=False)
    terms = [
        {"term": "evil.exe", "connector": "OR", "exclude": False},
        {"term": "cmd.exe", "connector": "OR", "exclude": False},
    ]
    hit = store.search_all_sources(terms=terms)[0]
    by_term = {t["term"]: t for t in hit["terms"]}
    assert by_term["evil.exe"] == {"term": "evil.exe", "match_count": 2, "capped": True}
    assert by_term["cmd.exe"] == {"term": "cmd.exe", "match_count": 1, "capped": False}


def test_search_all_breakdown_gives_up_on_an_absurd_term_list(store, write_csv, monkeypatch):
    # The breakdown costs one capped count per term on every source that
    # matched; past the ceiling it reports the union count alone rather than
    # turning one sweep into thousands of scans.
    from winnow import store as store_module

    monkeypatch.setattr(store_module, "SEARCH_ALL_TERM_BREAKDOWN_MAX", 2)
    rows = [["Process"], ["evil.exe"], ["cmd.exe"], ["notepad.exe"]]
    store.ingest_csv(write_csv(rows, "b6.csv"), name="b6.csv", build_fts=False)
    terms = [
        {"term": t, "connector": "OR", "exclude": False}
        for t in ("evil.exe", "cmd.exe", "notepad.exe")
    ]
    assert store.search_all_sources(terms=terms)[0]["terms"] == []


# ------------------------------------------------------------- regex safety

def _regex_case(store, write_csv, cells):
    sid = store.ingest_csv(write_csv([["Cmd"]] + [[c] for c in cells], "rx.csv"),
                           name="rx.csv", build_fts=False)["id"]
    return sid


@pytest.mark.parametrize("pattern", [r"C:\Users\bob", r"evil(", r"[a-"])
def test_an_invalid_regex_is_refused_not_a_silent_zero(store, write_csv, pattern):
    """re.error was swallowed per row, so `C:\\Users` typed in regex mode
    (\\U is a bad escape in Python re) showed 0 rows — "not in this
    table" — instead of saying the pattern was broken. Every regex entry
    point compiles the pattern before the build and refuses it by name."""
    sid = _regex_case(store, write_csv, [r"C:\Users\bob\evil.exe", r"C:\Windows\x.dll"])
    specs = [
        {"search": pattern, "search_mode": "regex"},
        {"filters": [{"column": "Cmd", "op": "regex", "value": pattern}]},
        {"filter_tree": {"type": "group", "op": "AND", "children": [
            {"type": "cond", "column": "Cmd", "op": "regex", "value": pattern}]}},
    ]
    for spec in specs:
        with pytest.raises(ValueError, match="regular expression"):
            store.build_view(sid, {"source_id": sid, "sort": [], **spec})


def test_an_invalid_regex_is_a_400_at_the_route(client, store, write_csv):
    sid = _regex_case(store, write_csv, [r"C:\Users\bob\evil.exe"])
    r = client.post("/api/view", json={"source_id": sid, "search": r"C:\Users", "search_mode": "regex"})
    assert r.status_code == 400
    assert "regular expression" in r.json()["detail"]


@pytest.mark.parametrize("pattern", [
    r"(a+)+$", r"(.*a)+", r"(a|a)*b", r"(\w+\s?)+$", r"(a+){10}",
    r"(a*)*b", r"(\w+\s?)*$", r"^(a|aa)+$", r"(.*a){3,}", r"(x+x+)+y", r"(?:\d|\d\d)+!",
    r"(a{1,2})+$", r"(\d+\.?)+$", r"(\w+\.\w+)+$", r"(a?[ab]?\s){2,}$",
    r"(\w+\s*)+$", r"(,a+.\s+)+$", r"(.\s+[ab]+)+$",
])
def test_a_catastrophic_regex_is_refused_before_it_runs(store, write_csv, pattern):
    """Cells are attacker-controlled, and one backtracking blow-up inside
    the REGEXP callback holds the GIL for the whole match: every thread,
    the cancel request included, froze until that one cell finished —
    hours, for a cell a few dozen characters long. Nothing can interrupt
    re.search from Python, so the shapes that blow up (a repeated group
    with nothing marking where one repetition ends, an unbounded repeat of
    overlapping alternatives) are refused up front, loudly. The cell here
    is short enough that running the pattern costs a moment, so without
    the guard this fails on the missing refusal rather than hanging.

    `(,a+.\\s+)+$` was let through by the delimiter rule this replaced —
    the `,` pins each repetition — and is exponential all the same: the
    `.` can take the last `a` or the first space, two ways per repetition
    (1.8s at 22 of them). `(.\\s+[ab]+)+$` is the same hole reached by
    counting `\\s+` as a delimiter."""
    sid = _regex_case(store, write_csv, ["a" * 16 + "!", "x" * 16])
    with pytest.raises(ValueError, match="catastrophic|backtrack"):
        store.build_view(sid, {"source_id": sid, "sort": [], "search": pattern, "search_mode": "regex"})
    with pytest.raises(ValueError, match="catastrophic|backtrack"):
        store.build_view(sid, {"source_id": sid, "sort": [],
                               "filters": [{"column": "Cmd", "op": "regex", "value": pattern}]})


@pytest.mark.parametrize("pattern,expect", [
    (r"46(24|88)", {"4624 logon", "4688 cmd"}),
    (r"^(GET|POST) /", {"GET /admin", "POST /login"}),
    (r"(\d{1,3}\.){3}\d{1,3}", {"10.0.0.5 beacon"}),
    (r"(?:cmd|powershell)\.exe", {"cmd.exe /c whoami"}),
    (r"(?:ab|cd)+!", {"abcdab!"}),
    (r"\w+@\w+\.com", {"mail bob@evil.com"}),
])
def test_ordinary_regexes_still_run(store, write_csv, pattern, expect):
    cells = ["4624 logon", "4688 cmd", "GET /admin", "POST /login", "10.0.0.5 beacon",
             "cmd.exe /c whoami", "abcdab!", "mail bob@evil.com", "nothing here"]
    sid = _regex_case(store, write_csv, cells)
    v = store.build_view(sid, {"source_id": sid, "sort": [], "search": pattern, "search_mode": "regex"})
    got = {r["cells"][0] for r in store.fetch_rows(v["view_id"], 0, 50)["rows"]}
    assert got == expect


_CMDLINE_CELLS = {"beacon 10.0.0.5", "dns 192.168.100.200", "version 1.2", "nothing here",
                  "net user /add x", "mimikatz kerberos list", "word2word0 seen"}
_ALL_CELLS = _CMDLINE_CELLS | {"evil.com", "cdn.evil.com/x", "bad-host.example.org", "padded "}


@pytest.mark.parametrize("pattern,expect", [
    (r"(\d+\.){3}\d+", {"beacon 10.0.0.5", "dns 192.168.100.200"}),
    (r"(\d{1,3}\.){3}\d{1,3}", {"beacon 10.0.0.5", "dns 192.168.100.200"}),
    (r"(\w+\.)+com", {"evil.com", "cdn.evil.com/x"}),
    (r"([a-z0-9-]+\.)+[a-z]{2,}", {"evil.com", "cdn.evil.com/x", "bad-host.example.org"}),
    (r"(\w+\s+){2}", {"net user /add x", "mimikatz kerberos list"}),
    (r"(\S+\s+){3}x", {"net user /add x"}),
    (r"^(\S+\s+)*\S+$", _ALL_CELLS - {"padded "}),
    (r"(\w+\s+)+", _CMDLINE_CELLS | {"padded "}),
    (r"((?:mimikatz|lsadump|logonpasswords|kerberos)\s+)+", {"mimikatz kerberos list"}),
    (r"(word0|word1|word2)+", {"word2word0 seen"}),
    (r'(?:[^,"]*|"[^"]*")(?:,(?:[^,"]*|"[^"]*"))*', _ALL_CELLS),
])
def test_ip_and_domain_patterns_are_not_refused(store, write_csv, pattern, expect):
    """The first guard refused any open-ended quantifier inside a repeated
    group, which took out the IPv4 and domain patterns analysts type every
    day. A repetition that must end in a character its open-ended part
    cannot match (`\\.` after `\\d+` or `\\w+`) can only split a cell one
    way, so these run — and filter, through both regex entry points.

    The second wanted that character to be a fixed one, and so still
    refused the "field N of a command line" shapes: `\\S+\\s+` has no
    fixed delimiter, but `\\S` and `\\s` share no character, so every
    boundary is where the text changes from one to the other.

    And a rule about alternations refused a repeated keyword list whenever
    two of its words began with the same letter (`lsadump|logonpasswords`),
    and the CSV-field pattern because one alternative can be empty —
    neither can match a cell two ways, which the automaton now decides."""
    cells = sorted(_ALL_CELLS)
    sid = _regex_case(store, write_csv, cells)
    for spec in ({"search": pattern, "search_mode": "regex"},
                 {"filters": [{"column": "Cmd", "op": "regex", "value": pattern}]}):
        v = store.build_view(sid, {"source_id": sid, "sort": [], **spec})
        got = {r["cells"][0] for r in store.fetch_rows(v["view_id"], 0, 50)["rows"]}
        assert got == expect, spec


@pytest.mark.parametrize("pattern", [r"(a|a)*b", r"^(a|aa)+$", r"(?:\d|\d\d)+!", r"(a|b|ab)*c"])
def test_overlapping_alternatives_are_refused_by_the_exact_check(store, write_csv, pattern):
    """The alternation rule now applies only where the automaton cannot
    model a repeat. These have to fail the automaton itself — two ways
    through every repetition — or narrowing that rule would have let them
    run; the reason they are refused with says which check caught them."""
    sid = _regex_case(store, write_csv, ["a" * 16 + "!"])
    with pytest.raises(ValueError, match="can split the same text more than one way"):
        store.build_view(sid, {"source_id": sid, "sort": [], "search": pattern, "search_mode": "regex"})


def test_a_regex_reads_the_whole_cell(store, write_csv):
    """The guard is on the pattern, never on the cell: truncating cells to
    bound the match would turn a hit past the cut into a silent miss."""
    sid = _regex_case(store, write_csv, ["x" * 100_000 + "mimikatz"])
    v = store.build_view(sid, {"source_id": sid, "sort": [], "search": r"mimi\w+z$",
                               "search_mode": "regex"})
    assert v["row_count"] == 1


def test_a_raw_fragments_regexp_is_checked_too(store, write_csv):
    """A raw SQL fragment calls REGEXP with a literal: an invalid or
    catastrophic one is refused, not run (or silently matched to nothing)."""
    sid = _regex_case(store, write_csv, [r"C:\Users\bob"])
    for frag in ("\"Cmd\" REGEXP 'C:\\Users'", "\"Cmd\" REGEXP '(a+)+$'"):
        with pytest.raises(ValueError, match="regular expression"):
            store.build_view(sid, {"source_id": sid, "sort": [],
                                   "filter_tree": {"type": "raw", "sql": frag}})


def test_the_sql_pane_names_a_bad_regex(store, write_csv):
    """The SQL pane reaches _regexp directly. Its literal patterns get the
    same check, so the analyst is told which pattern is wrong rather than
    SQLite's "user-defined function raised exception" — and a catastrophic
    one never runs against a crafted cell."""
    sid = _regex_case(store, write_csv, [r"C:\Users\bob"])
    table = store.get_source(sid)["table_name"]
    for pat in ("C:\\Users", "(a+)+$"):
        with pytest.raises(ValueError, match="regular expression"):
            store.run_sql(f"SELECT * FROM {table} WHERE \"Cmd\" REGEXP '{pat}'")
        with pytest.raises(ValueError, match="regular expression"):
            store.sql_to_table(f"SELECT * FROM {table} WHERE regexp('{pat}', \"Cmd\")", "out")
    # A pattern only known at run time still fails rather than matching nothing.
    with pytest.raises(Exception):
        store.run_sql(f"SELECT * FROM {table} WHERE \"Cmd\" REGEXP 'C:' || char(92) || 'Users'")
    assert store.run_sql(f"SELECT * FROM {table} WHERE \"Cmd\" REGEXP 'users'")["rows"]
