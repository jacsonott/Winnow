"""store.py search: Contains mode (true substring, via a trigram FTS5
index when ready, LIKE fallback otherwise), regex, Advanced AND/OR/NOT
(incl. the leading-NOT regression), the background/lazy index build, and
search_all_sources."""

from __future__ import annotations

from store import _fts_like_pattern, _blob_expr, TRIGRAM_MIN_LEN, q


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
    # has nothing to look up for it, so this must still go through the
    # blob-LIKE fallback even once the index is built, and still find the
    # real substring match.
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
    # term chip — must still find the real match via the per-term LIKE
    # fallback in _advanced_fts_clause, not silently drop the term.
    store, source_id = ingested
    assert store.wait_for_fts(source_id, timeout=5)
    terms = [{"term": "ja", "connector": "AND", "exclude": False}]
    spec = {"source_id": source_id, "filters": [], "sort": [], "search_mode": "advanced", "search_terms": terms}
    view = store.build_view(source_id, spec)
    assert len(_cells(store, view["view_id"])) == 2


def test_advanced_leading_not(ingested):
    # _advanced_fts_clause's own docstring documents a prior bug: FTS5's NOT
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
    # Same regression, exercised through _advanced_like_clause (the non-FTS
    # fallback) instead of _advanced_fts_clause.
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


def test_fts_like_pattern_routing():
    # Indexable terms get the bare pattern (no escaping — safe only
    # because wildcard-bearing terms are refused below).
    assert _fts_like_pattern("jacso") == "%jacso%"
    assert _fts_like_pattern("hello world") == "%hello world%"  # space preserved -> literal substring
    assert _fts_like_pattern("C:\\Windows") == "%C:\\Windows%"  # backslash is literal in bare LIKE
    # Refused: under the trigram floor, or containing a LIKE wildcard the
    # unescaped pushdown form would misinterpret — these must take the
    # escaped blob-LIKE fallback instead.
    assert _fts_like_pattern("ja") is None
    assert _fts_like_pattern("100%") is None
    assert _fts_like_pattern("under_score") is None


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

    from store import Store, DEFAULT_TAGS
    reopened = Store(case_path, default_tags=DEFAULT_TAGS)
    try:
        assert reopened.get_source(source_id)["has_fts"] == 0
    finally:
        reopened.close()


def test_legacy_fat_trigram_fts_gets_downgraded_and_dropped_on_open(store, write_csv, case_path):
    # The first-generation trigram shape (multi-column, detail=full) is
    # also stale now: the doc-LIKE query form would be a SQL error against
    # it, and it's ~6x bigger than the index needs to be. Opening such a
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

    from store import Store, DEFAULT_TAGS
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
    # wildcards — these terms can't take the unescaped indexed form (bare
    # LIKE pushdown), so they route through the escaped fallback even when
    # the index is ready. "under_score" must NOT match "underXscore".
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
    assert hits == [{"source_id": rec["id"], "name": "c1.csv", "match_count": 5, "capped": False}]


def test_search_all_sources_caps_the_count(store, write_csv, monkeypatch):
    """The modal only ranks which tables hit and roughly how hard; an exact
    count over a source whose index isn't built is a full scan of every
    matching row, so it stops at the cap and says so."""
    import store as store_module

    monkeypatch.setattr(store_module, "SEARCH_ALL_COUNT_CAP", 3)
    rows = [["Process"]] + [["svchost.exe"]] * 10
    store.ingest_csv(write_csv(rows, "c2.csv"), name="c2.csv", build_fts=False)
    hit = store.search_all_sources(query="svchost")[0]
    assert hit["match_count"] == 3
    assert hit["capped"] is True


def test_search_all_sources_caps_on_the_indexed_and_advanced_paths_too(store, write_csv, monkeypatch):
    import store as store_module

    monkeypatch.setattr(store_module, "SEARCH_ALL_COUNT_CAP", 2)
    rows = [["Process"]] + [["svchost.exe"]] * 6
    rec = store.ingest_csv(write_csv(rows, "c3.csv"), name="c3.csv")
    assert store.wait_for_fts(rec["id"], timeout=5)
    assert store.search_all_sources(query="svchost")[0] == {
        "source_id": rec["id"], "name": "c3.csv", "match_count": 2, "capped": True,
    }
    terms = [{"term": "svchost", "connector": "AND", "exclude": False}]
    assert store.search_all_sources(terms=terms)[0]["capped"] is True


class _CountingLock:
    """Delegates to a real lock while counting how many times it goes fully
    unheld — i.e. how many separate units of work an operation splits into,
    rather than how many times it re-enters an outer hold."""

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
    assert counting.depth == 0
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
    assert counting.depth == 0
    assert counting.released_to_zero >= 4
