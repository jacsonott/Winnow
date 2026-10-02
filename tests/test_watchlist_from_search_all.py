"""Adding IOCs to the watchlist off a Search-all sweep does not re-read
every table.

The sweep and the scan ask a table the same question — the same two
WHERE shapes, indexed `doc LIKE` or the escaped blob LIKE
(`_watchlist_match_sql` names the sharing) — so a sweep that READ a table
and found no row holding a term has established the whole of what a scan
of that (indicator, table) pair would. "Add to watchlist" used to start a
full scan of every table for terms a sweep had just answered: on a case
of twenty tables that is twenty full LIKE scans per indicator, to arrive
at the answer already on screen.

The load-bearing test here is not the saving, it is
`test_a_full_scan_afterwards_changes_nothing`: whatever the handoff
wrote, a real scan over the same data has to agree with it. A shortcut
that reported "clean" for a table holding a hit would corrupt triage
state, which is the worst failure this tool has (invariant #7's
reasoning, one level out).
"""

from __future__ import annotations

import pytest

from winnow.store import DEFAULT_TAGS, Store

TERMS = ["ALPHAIOC", "BETAIOC", "GAMMAIOC"]


def _or_terms(*values):
    return [{"term": v, "connector": "AND" if i == 0 else "OR", "exclude": False}
            for i, v in enumerate(values)]


def _ingest(store, write_csv, name, rows):
    return store.ingest_csv(write_csv(rows, name=name), name=name, build_fts=False)["id"]


def _scans(store, wid=None):
    sql = "SELECT watchlist_id, source_id FROM watchlist_scans"
    args: tuple = ()
    if wid is not None:
        sql += " WHERE watchlist_id=?"
        args = (wid,)
    return sorted(tuple(r) for r in store.db.execute(sql + " ORDER BY 1,2", args))


def _hits(store):
    return sorted(tuple(r) for r in store.db.execute(
        "SELECT watchlist_id, source_id, rid FROM watchlist_hits ORDER BY 1,2,3"))


@pytest.fixture
def swept(store, write_csv, monkeypatch):
    """Three tables, one of which holds one of three IOCs, swept for all
    three. Returns (store, {name: source_id}, sweep job id). The
    background index build is quieted the way bench/'s fixture and
    test_watchlist_scan.py quiet it — an index landing mid-test would make
    the match take its other shape, which is a thing those tests cover and
    noise here."""
    monkeypatch.setattr(store, "_ensure_fts_building", lambda source_id: None)
    ids = {
        "hot": _ingest(store, write_csv, "hot.csv",
                       [["Process", "CommandLine"],
                        ["svchost.exe", "C:\\tmp\\ALPHAIOC.exe -q"],
                        ["cmd.exe", "whoami"]]),
        "cold": _ingest(store, write_csv, "cold.csv",
                        [["Process", "CommandLine"], ["lsass.exe", "nothing here"]]),
        "colder": _ingest(store, write_csv, "colder.csv",
                          [["Process", "CommandLine"], ["explorer.exe", "also nothing"]]),
    }
    job = store.start_search_all_job(terms=_or_terms(*TERMS))
    assert store.wait_for_search_all_job(timeout=30)["done"]
    return store, ids, job["job_id"]


def _add_all(store):
    return [store.add_indicator(t, "other")["id"] for t in TERMS]


def _scan_from(store, jid, wids, **kw):
    job = store.start_watchlist_scan_job(watchlist_ids=wids, from_search_all=jid, **kw)
    return store.wait_for_watchlist_scan_job(job["job_id"], timeout=60)


# --------------------------------------------------------------- the handoff

def test_only_the_table_the_sweep_found_something_in_is_read(swept):
    store, ids, jid = swept
    wids = _add_all(store)
    read: list[tuple[int, int]] = []
    real_unit = store._scan_unit
    store._scan_unit = lambda src, cols, ind: (read.append((src["id"], ind["id"])), real_unit(src, cols, ind))[1]
    try:
        job = _scan_from(store, jid, wids)
    finally:
        del store._scan_unit

    assert job["status"] == "done"
    assert job["total"] == 1, "every table was read again"
    assert job["seeded"] == {"tables": 2, "pairs": 6}
    # Nothing touched the two tables the sweep had already answered.
    assert {sid for sid, _wid in read} == {ids["hot"]}
    assert sorted(read) == sorted((ids["hot"], w) for w in wids)


def test_every_pair_is_recorded_as_scanned_either_way(swept):
    """The point of the records: an indicator the sweep cleared reads as
    `clean` in the UI, not as `not scanned`. countCell tells those apart
    off exactly these rows."""
    store, ids, jid = swept
    wids = _add_all(store)
    _scan_from(store, jid, wids)
    assert _scans(store) == sorted((w, s) for w in wids for s in ids.values())


def test_the_hits_are_the_hits_a_scan_would_have_written(swept):
    store, ids, jid = swept
    wids = _add_all(store)
    _scan_from(store, jid, wids)
    alpha = wids[0]
    assert _hits(store) == [(alpha, ids["hot"], 1)]
    counts = {i["value"]: i["hit_count"] for i in store.list_indicators()}
    assert counts == {"ALPHAIOC": 1, "BETAIOC": 0, "GAMMAIOC": 0}


def test_a_full_scan_afterwards_changes_nothing(swept):
    """The load-bearing one. Whatever the shortcut wrote, reading every
    table for every indicator has to arrive at the same thing — otherwise
    the shortcut is reporting a table clean that is not."""
    store, _ids, jid = swept
    wids = _add_all(store)
    _scan_from(store, jid, wids)
    shortcut_hits, shortcut_scans = _hits(store), _scans(store)

    store.scan_all()
    assert _hits(store) == shortcut_hits
    assert _scans(store) == shortcut_scans


# ------------------------------------------------- what it refuses to trust

def test_a_sweep_with_a_not_in_it_proves_nothing(store, write_csv, monkeypatch):
    """Under mixed AND/NOT the terms constrain each other, so "no row
    matched the query" says nothing about whether a row holds one of its
    terms."""
    monkeypatch.setattr(store, "_ensure_fts_building", lambda source_id: None)
    _ingest(store, write_csv, "a.csv", [["Cmd"], ["ALPHAIOC and BETAIOC"]])
    _ingest(store, write_csv, "b.csv", [["Cmd"], ["ALPHAIOC alone"]])
    terms = [{"term": "ALPHAIOC", "connector": "AND", "exclude": False},
             {"term": "BETAIOC", "connector": "AND", "exclude": True}]
    job = store.start_search_all_job(terms=terms)
    store.wait_for_search_all_job(timeout=30)
    wid = store.add_indicator("ALPHAIOC", "other")["id"]

    scan = _scan_from(store, job["job_id"], [wid])
    assert scan["seeded"] == {"tables": 0, "pairs": 0}
    assert scan["total"] == 2, "both tables must still be read"
    # And the answer is the one a plain scan gives: b.csv matched too.
    assert len(_hits(store)) == 2


def test_an_indicator_that_is_not_a_swept_term_is_scanned(swept):
    """An analyst who edited the box after the sweep gets a real scan.
    `all` rather than `any`: one unproven indicator sends the scan to
    every table regardless, so nothing is seeded."""
    store, _ids, jid = swept
    edited = store.add_indicator("ALPHAIO", "other")["id"]     # one character short
    scan = _scan_from(store, jid, [edited])
    assert scan["seeded"] == {"tables": 0, "pairs": 0}
    assert scan["total"] == 3
    assert len(_hits(store)) == 1                              # a substring: it does match


def test_a_table_the_sweep_never_read_is_still_scanned(swept, write_csv):
    """Scoped sweeps and tables imported since: the sweep's record names
    what it read, and absence is not proof."""
    store, ids, jid = swept
    late = _ingest(store, write_csv, "late.csv", [["Cmd"], ["BETAIOC arrived later"]])
    wids = _add_all(store)
    scan = _scan_from(store, jid, wids)
    assert scan["seeded"] == {"tables": 2, "pairs": 6}
    assert scan["total"] == 2                                  # hot.csv and late.csv
    beta = wids[1]
    assert (beta, late, 1) in _hits(store)
    assert (beta, late) in _scans(store)


def test_a_row_count_that_moved_is_not_trusted(swept):
    """Source tables are not mutated after ingest (invariant #1), so this
    is unreachable through the app — it is guarded because "clean" is a
    sentence an analyst quotes, and the guard costs one comparison."""
    store, ids, jid = swept
    with store._search_job_lock:
        store._search_job["read"][ids["cold"]]["row_count"] += 1
    wids = _add_all(store)
    scan = _scan_from(store, jid, wids)
    assert scan["seeded"] == {"tables": 1, "pairs": 3}
    assert scan["total"] == 2                                  # hot.csv and cold.csv


def test_a_source_the_sweep_failed_to_read_is_not_trusted(store, write_csv, monkeypatch):
    """A count that raises is recorded as "no match" for the progress
    report — the right answer there and a lie here. It leaves no record,
    so the table is read."""
    monkeypatch.setattr(store, "_ensure_fts_building", lambda source_id: None)
    _ingest(store, write_csv, "good.csv", [["Cmd"], ["nothing"]])
    bad = _ingest(store, write_csv, "bad.csv", [["Cmd"], ["nothing either"]])
    real = store._search_all_count_sql

    def explode(src, table, cols, query, terms):
        # A count that raises sqlite3.OperationalError — the shape a table
        # dropped mid-sweep produces, which the sweep absorbs as n = 0.
        if src["id"] == bad:
            return "SELECT 1 FROM no_such_table_at_all", ()
        return real(src, table, cols, query, terms)

    monkeypatch.setattr(store, "_search_all_count_sql", explode)
    job = store.start_search_all_job(terms=_or_terms(*TERMS))
    assert store.wait_for_search_all_job(timeout=30)["done"]
    monkeypatch.setattr(store, "_search_all_count_sql", real)

    wids = _add_all(store)
    scan = _scan_from(store, jid=job["job_id"], wids=wids)
    assert scan["seeded"] == {"tables": 1, "pairs": 3}
    assert scan["total"] == 1
    assert (wids[0], bad) in _scans(store)                     # read, not assumed


def test_an_unknown_job_id_just_scans_everything(swept):
    store, _ids, _jid = swept
    wids = _add_all(store)
    scan = _scan_from(store, 999_999, wids)
    assert scan["seeded"] == {"tables": 0, "pairs": 0}
    assert scan["total"] == 3


def test_no_handoff_asked_for_is_the_old_behaviour(swept):
    store, _ids, _jid = swept
    wids = _add_all(store)
    job = store.start_watchlist_scan_job(watchlist_ids=wids)
    done = store.wait_for_watchlist_scan_job(job["job_id"], timeout=60)
    assert done["seeded"] == {"tables": 0, "pairs": 0}
    assert done["total"] == 3


# --------------------------------------------------------------- the route

def test_the_route_takes_a_job_id(client, store, write_csv, monkeypatch):
    monkeypatch.setattr(store, "_ensure_fts_building", lambda source_id: None)
    _ingest(store, write_csv, "r1.csv", [["Cmd"], ["ALPHAIOC here"]])
    _ingest(store, write_csv, "r2.csv", [["Cmd"], ["clean"]])
    sweep = client.post("/api/search_all/start", json={"terms": _or_terms(*TERMS)}).json()
    assert store.wait_for_search_all_job(timeout=30)["done"]
    added = client.post("/api/watchlist/import",
                        json={"text": "\n".join(TERMS), "kind": "other"}).json()
    assert added["added"] == 3

    started = client.post("/api/watchlist/scan/start",
                          json={"watchlist_ids": added["added_ids"],
                                "from_search_all": sweep["job_id"]}).json()
    done = store.wait_for_watchlist_scan_job(started["job_id"], timeout=60)
    assert done["seeded"] == {"tables": 1, "pairs": 3}
    assert done["total"] == 1


# ------------------------------------------- the two shapes that prove less

def test_a_one_term_sweep_saves_the_clean_tables_and_reads_the_matching_one(store, write_csv, monkeypatch):
    """One IOC is the common way into this, and the sweep's per-term
    breakdown does not exist for it (a one-term breakdown is its own
    total). The unmatched tables are still proof — a zero union count is a
    zero for the only term in it — and the matched table is NOT: "something
    here matched" is the one shape that says nothing per term, and with one
    term it is the whole answer being withheld."""
    monkeypatch.setattr(store, "_ensure_fts_building", lambda source_id: None)
    hot = _ingest(store, write_csv, "h.csv", [["Cmd"], ["ALPHAIOC.exe -q"]])
    _ingest(store, write_csv, "c1.csv", [["Cmd"], ["nothing"]])
    _ingest(store, write_csv, "c2.csv", [["Cmd"], ["nothing either"]])
    job = store.start_search_all_job(terms=_or_terms("ALPHAIOC"))
    assert store.wait_for_search_all_job(timeout=30)["done"]

    wid = store.add_indicator("ALPHAIOC", "other")["id"]
    scan = _scan_from(store, job["job_id"], [wid])
    assert scan["seeded"] == {"tables": 2, "pairs": 2}
    assert scan["total"] == 1
    assert _hits(store) == [(wid, hot, 1)]
    store.scan_all()
    assert _hits(store) == [(wid, hot, 1)]


def test_a_bare_query_sweep_counts_as_one_term(store, write_csv, monkeypatch):
    """Contains mode sends a `query`, not a chip list — one positive term
    typed into the box instead of chipped, and proof of the same kind."""
    monkeypatch.setattr(store, "_ensure_fts_building", lambda source_id: None)
    _ingest(store, write_csv, "q1.csv", [["Cmd"], ["ALPHAIOC.exe"]])
    _ingest(store, write_csv, "q2.csv", [["Cmd"], ["clean"]])
    job = store.start_search_all_job(query="ALPHAIOC")
    assert store.wait_for_search_all_job(timeout=30)["done"]

    wid = store.add_indicator("ALPHAIOC", "other")["id"]
    scan = _scan_from(store, job["job_id"], [wid])
    assert scan["seeded"] == {"tables": 1, "pairs": 1}
    assert scan["total"] == 1


# ------------------------------- an indicator the sweep never asked about

def test_one_unswept_indicator_in_the_scope_blocks_the_whole_handoff(swept):
    """Every named indicator has to be a swept term, not just one of them.
    `lsass` was never searched for and DOES sit in cold.csv — so trusting
    the sweep per-indicator would both miss that hit and record cold.csv as
    scanned for a value nothing ever looked for there. Silent corruption of
    triage state, which is the failure this handoff is not allowed to
    introduce."""
    store, ids, jid = swept
    alpha = store.add_indicator(TERMS[0], "other")["id"]
    lsass = store.add_indicator("lsass", "filename")["id"]

    scan = _scan_from(store, jid, [alpha, lsass])
    assert scan["seeded"] == {"tables": 0, "pairs": 0}
    assert scan["total"] == 3
    assert (lsass, ids["cold"], 1) in _hits(store)
    store.scan_all()
    assert (lsass, ids["cold"], 1) in _hits(store)


def test_an_unscoped_scan_is_held_to_the_same_rule(swept):
    """watchlist_ids=None means every indicator in the case, which is
    every indicator including the ones no sweep has been near."""
    store, ids, jid = swept
    _add_all(store)
    lsass = store.add_indicator("lsass", "filename")["id"]

    job = store.start_watchlist_scan_job(watchlist_ids=None, from_search_all=jid)
    scan = store.wait_for_watchlist_scan_job(job["job_id"], timeout=60)
    assert scan["seeded"] == {"tables": 0, "pairs": 0}
    assert scan["total"] == 3
    assert (lsass, ids["cold"], 1) in _hits(store)


def test_an_errored_sweep_is_refused_whole(store, write_csv, monkeypatch):
    """Its per-source records are individually sound — each one is a count
    that returned — but the exception came from somewhere this does not
    model, and the safe direction to be wrong in is "read the table"."""
    monkeypatch.setattr(store, "_ensure_fts_building", lambda source_id: None)
    _ingest(store, write_csv, "e1.csv", [["Cmd"], ["clean"]])
    _ingest(store, write_csv, "e2.csv", [["Cmd"], ["clean too"]])
    real = store._iter_search_all_sources

    def half_then_raise(*a, **kw):
        for i, item in enumerate(real(*a, **kw)):
            yield item
            if i:
                raise RuntimeError("something nobody modelled")

    monkeypatch.setattr(store, "_iter_search_all_sources", half_then_raise)
    job = store.start_search_all_job(terms=_or_terms(*TERMS))
    assert store.wait_for_search_all_job(timeout=30)["error"]

    wids = _add_all(store)
    scan = _scan_from(store, job["job_id"], wids)
    assert scan["seeded"] == {"tables": 0, "pairs": 0}
    assert scan["total"] == 2
