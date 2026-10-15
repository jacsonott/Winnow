"""The lookup derived column: join a value in from another table, keyed
by the input column — VLOOKUP, materialized."""

from __future__ import annotations

import pytest

HOSTS = [["IP", "Hostname"],
         ["10.0.0.5", "DC01"],
         ["10.0.0.9", "WKS-ALICE"],
         ["10.0.0.5", "DC01-ALIAS"]]  # duplicate key — 'multi' decides
EVENTS = [["When", "Src"],
          ["2024-01-01 10:00", "10.0.0.5"],
          ["2024-01-01 11:00", "10.0.0.9"],
          ["2024-01-01 12:00", "172.16.0.1"]]  # no match


def _mk(store, write_csv):
    lk = store.ingest_csv(write_csv(HOSTS, "hosts.csv"), name="hosts", build_fts=False)["id"]
    ev = store.ingest_csv(write_csv(EVENTS, "events.csv"), name="events", build_fts=False)["id"]
    return lk, ev


def _add(store, sid, params, name="Host"):
    res = store.add_derived_column(sid, name, "Src", "lookup", params)
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    return res


def _values(store, sid, column):
    src = store.get_source(sid)
    idx = [c["name"] for c in src["columns"]].index(column)
    v = store.build_view(sid, {"source_id": sid, "filters": [], "sort": []})
    return [r["cells"][idx] for r in store.fetch_rows(v["view_id"], 0, 10)["rows"]]


def test_lookup_first_match(store, write_csv):
    lk, ev = _mk(store, write_csv)
    _add(store, ev, {"other_source_id": lk, "match_column": "IP",
                     "value_column": "Hostname", "multi": "first"})
    assert _values(store, ev, "Host") == ["DC01", "WKS-ALICE", None]


def test_lookup_all_matches_comma_joined(store, write_csv):
    lk, ev = _mk(store, write_csv)
    _add(store, ev, {"other_source_id": lk, "match_column": "IP",
                     "value_column": "Hostname", "multi": "all"})
    assert _values(store, ev, "Host") == ["DC01, DC01-ALIAS", "WKS-ALICE", None]


def test_lookup_filters_groups_and_chains(store, write_csv):
    lk, ev = _mk(store, write_csv)
    _add(store, ev, {"other_source_id": lk, "match_column": "IP",
                     "value_column": "Hostname", "multi": "first"})
    v = store.build_view(ev, {"source_id": ev, "sort": [],
                              "filters": [{"column": "Host", "op": "equals", "value": "DC01"}]})
    assert v["row_count"] == 1
    # chains: regex on the looked-up value
    res = store.add_derived_column(ev, "HostKind", "Host", "regex_extract", {"pattern": r"^([A-Z]+)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert _values(store, ev, "HostKind") == ["DC", "WKS", None]


def test_lookup_validates_at_creation(store, write_csv):
    lk, ev = _mk(store, write_csv)
    with pytest.raises(ValueError, match="no key column"):
        store.add_derived_column(ev, "X", "Src", "lookup",
                                 {"other_source_id": lk, "match_column": "Nope", "value_column": "Hostname"})
    with pytest.raises(ValueError, match="No table"):
        store.add_derived_column(ev, "X", "Src", "lookup",
                                 {"other_source_id": 999, "match_column": "IP", "value_column": "Hostname"})
    with pytest.raises(ValueError, match="real table"):
        store.add_derived_column(ev, "X", "Src", "lookup",
                                 {"other_source_id": -1, "match_column": "IP", "value_column": "Hostname"})


def test_lookup_on_a_merge_fans_out(store, write_csv):
    lk, ev = _mk(store, write_csv)
    ev2 = store.ingest_csv(write_csv(EVENTS, "events2.csv"), name="events2", build_fts=False)["id"]
    mid = store.create_merge("evm", [ev, ev2])["id"]
    res = store.add_derived_column(mid, "Host", "Src", "lookup",
                                   {"other_source_id": lk, "match_column": "IP",
                                    "value_column": "Hostname", "multi": "first"})
    for j in res["job_ids"]:
        store.wait_for_ingest_job(j, timeout=30)
    vals = _values(store, mid, "Host")
    assert vals.count("DC01") == 2 and vals.count("WKS-ALICE") == 2


def test_rederive_repoints_the_lookup(store, write_csv):
    lk, ev = _mk(store, write_csv)
    res = _add(store, ev, {"other_source_id": lk, "match_column": "IP",
                           "value_column": "Hostname", "multi": "first"})
    did = res["definition"]["id"]
    out = store.rederive_column(did, {"other_source_id": lk, "match_column": "IP",
                                      "value_column": "IP", "multi": "first"})
    store.wait_for_ingest_job(out["job_id"], timeout=30)
    assert _values(store, ev, "Host") == ["10.0.0.5", "10.0.0.9", None]


def test_preview_loads_the_mapping(store, write_csv):
    lk, ev = _mk(store, write_csv)
    prev = store.preview_derived(ev, "Src", "lookup",
                                 {"other_source_id": lk, "match_column": "IP",
                                  "value_column": "Hostname", "multi": "first"})
    outs = {p["input"]: p["output"] for p in prev["preview"]}
    assert outs["10.0.0.5"] == "DC01"
    assert prev["failures"] == 1  # the unmatched 172.16.0.1


def _session_lookup_case(tmp_path, write_csv, order):
    """A case holding two owner tables and an events table, the owner
    tables imported in `order` — so the same evidence lands on different
    source ids in different cases."""
    from winnow.store import DEFAULT_TAGS, Store
    files = {
        "owners_q3.csv": write_csv([["Host", "Owner"], ["web01", "alice"]], "owners_q3.csv"),
        "owners_q4.csv": write_csv([["Host", "Owner"], ["web01", "mallory"]], "owners_q4.csv"),
    }
    s = Store(str(tmp_path / f"case-{'-'.join(order)}.db"), default_tags=DEFAULT_TAGS)
    ids = {n: s.ingest_csv(files[n], name=n, build_fts=False)["id"] for n in order}
    ev = s.ingest_csv(write_csv([["Host", "Msg"], ["web01", "logon"]], "events.csv"),
                      name="events.csv", build_fts=False)["id"]
    return s, ids, ev


def test_a_session_binds_a_lookup_to_the_same_evidence_not_the_same_id(tmp_path, write_csv):
    """A lookup column's definition names its table by this case's source
    id. Carried to a case that imported the same files in another order,
    the id named a different table and the column filled from it with no
    warning — alice became mallory. The session now says WHICH table (by
    the evidence's fingerprint), and the receiving case resolves that."""
    a, ids_a, ev_a = _session_lookup_case(tmp_path, write_csv, ["owners_q3.csv", "owners_q4.csv"])
    try:
        r = a.add_derived_column(
            ev_a, "Owner", "Host", "lookup",
            {"other_source_id": ids_a["owners_q3.csv"], "match_column": "Host", "value_column": "Owner"})
        a.wait_for_ingest_job(r["job_id"], timeout=30)
        sess = a.export_session(ev_a)
    finally:
        a.close()

    b, ids_b, ev_b = _session_lookup_case(tmp_path, write_csv, ["owners_q4.csv", "owners_q3.csv"])
    try:
        res = b.import_session(ev_b, sess)
        for j in res["derived_job_ids"]:
            b.wait_for_ingest_job(j, timeout=30)
        assert res["warnings"] == []
        assert [r[0] for r in b.db.execute(f'SELECT "Owner" FROM drv_{ev_b}')] == ["alice"]
        d = b.list_derived_columns(ev_b)[0]
        assert int(d["params"]["other_source_id"]) == ids_b["owners_q3.csv"]
    finally:
        b.close()


def test_a_lookup_table_from_a_multi_table_file_is_matched_like_a_session_source(tmp_path, write_csv):
    """Every table of one SQLite file shares its hash, so the hash cannot
    say which one a lookup read. Telling them apart by name alone refused
    the column the moment the receiving analyst named their tables their
    own way; the session records the table's columns and row count too, and
    the receiving side matches it the way it matches session sources."""
    import sqlite3

    from winnow.store import DEFAULT_TAGS, Store
    db = str(tmp_path / "triage.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE hosts (Host TEXT, Owner TEXT)")
    c.execute("INSERT INTO hosts VALUES ('web01', 'alice')")
    c.execute("CREATE TABLE users (Sid TEXT, Name TEXT)")
    c.executemany("INSERT INTO users VALUES (?, ?)", [("S-1", "x"), ("S-2", "y")])
    c.commit()
    c.close()
    events = write_csv([["Host", "Msg"], ["web01", "logon"]], "events.csv")

    def case(fname, names):
        s = Store(str(tmp_path / fname), default_tags=DEFAULT_TAGS)
        ids = {t: s.ingest_sqlite_table(db, t, name=names[t], build_fts=False)["id"]
               for t in ("users", "hosts")}
        ev = s.ingest_csv(events, name="events.csv", build_fts=False)["id"]
        return s, ids, ev

    a, ids_a, ev_a = case("a.db", {"hosts": "hosts", "users": "users"})
    try:
        r = a.add_derived_column(ev_a, "Owner", "Host", "lookup",
                                 {"other_source_id": ids_a["hosts"], "match_column": "Host",
                                  "value_column": "Owner"})
        a.wait_for_ingest_job(r["job_id"], timeout=30)
        sess = a.export_session(ev_a)
    finally:
        a.close()

    b, ids_b, ev_b = case("b.db", {"hosts": "Hosts (triage.db)", "users": "Users (triage.db)"})
    try:
        res = b.import_session(ev_b, sess)
        for j in res["derived_job_ids"]:
            b.wait_for_ingest_job(j, timeout=30)
        assert res["warnings"] == []
        d = b.list_derived_columns(ev_b)[0]
        assert int(d["params"]["other_source_id"]) == ids_b["hosts"]
        assert [r[0] for r in b.db.execute(f'SELECT "Owner" FROM drv_{ev_b}')] == ["alice"]
    finally:
        b.close()


def test_a_lookup_on_a_table_made_in_winnow_follows_that_table_not_its_name(tmp_path, write_csv):
    """A SQL-saved lookup table had no hash, so the session bound it by
    NAME — in another case, to whatever table someone had saved under that
    name: mallory where the analyst's column said alice. It now carries an
    identity of its own (made:…), found again in its own case and never in
    a table another case made the same way."""
    from winnow.store import DEFAULT_TAGS, Store
    owners = write_csv([["Host", "Owner"], ["web01", "alice"]], "owners.csv")
    events = write_csv([["Host", "Msg"], ["web01", "logon"]], "events.csv")

    def case(fname, owner_expr):
        s = Store(str(tmp_path / fname), default_tags=DEFAULT_TAGS)
        o = s.ingest_csv(owners, name="owners.csv", build_fts=False)["id"]
        lk = s.sql_to_table(f"SELECT Host, {owner_expr} AS Owner FROM src_{o}", "owner map")["source"]["id"]
        ev = s.ingest_csv(events, name="events.csv", build_fts=False)["id"]
        return s, lk, ev

    a, lk_a, ev_a = case("a.db", "Owner")
    try:
        r = a.add_derived_column(ev_a, "Owner", "Host", "lookup",
                                 {"other_source_id": lk_a, "match_column": "Host", "value_column": "Owner"})
        a.wait_for_ingest_job(r["job_id"], timeout=30)
        sess = a.export_session(ev_a)
        # Its own case: the same table, found again by its identity.
        a.remove_derived_column(a.list_derived_columns(ev_a)[0]["id"])
        res = a.import_session(ev_a, sess)
        for j in res["derived_job_ids"]:
            a.wait_for_ingest_job(j, timeout=30)
        assert res["warnings"] == []
        assert int(a.list_derived_columns(ev_a)[0]["params"]["other_source_id"]) == lk_a
    finally:
        a.close()

    b, _lk_b, ev_b = case("b.db", "'mallory'")
    try:
        res = b.import_session(ev_b, sess)
        assert res["derived_columns_added"] == 0
        assert any("owner map" in w and "different table" in w for w in res["warnings"]), res["warnings"]
        assert b.list_derived_columns(ev_b) == []
    finally:
        b.close()


def test_a_lookup_whose_table_is_not_in_the_case_is_not_recreated(tmp_path, write_csv):
    """No table with that fingerprint here: say so and leave the column out,
    rather than filling it from whichever table took the old id."""
    a, ids_a, ev_a = _session_lookup_case(tmp_path, write_csv, ["owners_q3.csv", "owners_q4.csv"])
    try:
        r = a.add_derived_column(
            ev_a, "Owner", "Host", "lookup",
            {"other_source_id": ids_a["owners_q3.csv"], "match_column": "Host", "value_column": "Owner"})
        a.wait_for_ingest_job(r["job_id"], timeout=30)
        sess = a.export_session(ev_a)
    finally:
        a.close()

    b, _ids_b, ev_b = _session_lookup_case(tmp_path, write_csv, ["owners_q4.csv"])
    try:
        res = b.import_session(ev_b, sess)
        assert res["derived_columns_added"] == 0
        assert any("owners_q3.csv" in w and "Owner" in w for w in res["warnings"])
        assert b.list_derived_columns(ev_b) == []
    finally:
        b.close()


def test_a_session_from_before_lookup_identities_is_not_trusted_by_id(tmp_path, write_csv):
    """A session written before the table's identity travelled with the
    definition has only the id, which proves nothing in another case."""
    a, ids_a, ev_a = _session_lookup_case(tmp_path, write_csv, ["owners_q3.csv", "owners_q4.csv"])
    try:
        r = a.add_derived_column(
            ev_a, "Owner", "Host", "lookup",
            {"other_source_id": ids_a["owners_q3.csv"], "match_column": "Host", "value_column": "Owner"})
        a.wait_for_ingest_job(r["job_id"], timeout=30)
        sess = a.export_session(ev_a)
    finally:
        a.close()
    for d in sess["derived_columns"]:
        d.pop("lookup_table", None)

    b, _ids_b, ev_b = _session_lookup_case(tmp_path, write_csv, ["owners_q4.csv", "owners_q3.csv"])
    try:
        res = b.import_session(ev_b, sess)
        assert res["derived_columns_added"] == 0
        assert any("Owner" in w for w in res["warnings"])
    finally:
        b.close()


def test_a_blank_key_does_not_join_a_blank_key(store, write_csv):
    """An empty Host is the absence of a key, not a key. It used to pick
    up the lookup table's blank-key row — every unattributed event labelled
    with whatever the lookup table said about its own blank row."""
    lk = store.ingest_csv(write_csv([["Host", "Owner"], ["", "UNASSIGNED-ROW"], ["web01", "alice"]],
                                    "look.csv"), name="look", build_fts=False)["id"]
    ev = store.ingest_csv(write_csv([["Host", "Msg"], ["web01", "a"], ["", "b"], ["", "c"]],
                                    "data.csv"), name="data", build_fts=False)["id"]
    res = store.add_derived_column(ev, "Owner", "Host", "lookup",
                                   {"other_source_id": lk, "match_column": "Host",
                                    "value_column": "Owner"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert [r[0] for r in store.db.execute(f'SELECT "Owner" FROM drv_{ev} ORDER BY rid')] == [
        "alice", None, None]
    # Nothing to look up is not a failed lookup.
    assert store.get_derived_column(res["definition"]["id"])["parse_failures"] == 0


def test_preview_loads_the_mapping_once(store, write_csv, monkeypatch):
    """The preview loaded the whole lookup table twice per request — once
    for the sample pairs, once for the failure count."""
    from winnow import timeparse
    lk, ev = _mk(store, write_csv)
    calls = []
    real = timeparse.OPERATIONS["lookup"]["prepare"]
    monkeypatch.setitem(timeparse.OPERATIONS["lookup"], "prepare",
                        lambda s, p, st: (calls.append(1), real(s, p, st)))
    store.preview_derived(ev, "Src", "lookup", {"other_source_id": lk, "match_column": "IP",
                                                "value_column": "Hostname", "multi": "first"})
    assert len(calls) == 1
