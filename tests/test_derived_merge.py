"""Derived columns on merged tables: creating on the merge fans out to
every member, the merge exposes a derived column only when EVERY member
has one under that name (each read through its own drv_ sidecar), and
every read path — views, filters, sort, grouping, values, export —
resolves it per member."""

from __future__ import annotations

import time

import pytest

A_ROWS = [["When", "Who"],
          ["2024-01-01 10:00:00", "u=alice;h=SRV1"],
          ["2024-01-01 11:00:00", "u=bob;h=SRV1"]]
B_ROWS = [["When", "Who"],
          ["2024-01-02 09:00:00", "u=carol;h=WKS2"],
          ["2024-01-02 09:30:00", "u=alice;h=WKS2"]]

RX = {"pattern": r"u=(\w+)"}


def _mk(store, write_csv):
    a = store.ingest_csv(write_csv(A_ROWS, "a.csv"), name="a", build_fts=False)["id"]
    b = store.ingest_csv(write_csv(B_ROWS, "b.csv"), name="b", build_fts=False)["id"]
    merge = store.create_merge("both", [a, b])
    return a, b, merge["id"]


def _fanout(store, mid, name="User", column="Who", params=RX):
    res = store.add_derived_column(mid, name, column, "regex_extract", params)
    for jid in res["job_ids"]:
        store.wait_for_ingest_job(jid, timeout=30)
    return res


def test_fanout_creates_on_every_member_and_the_merge_exposes_it(store, write_csv):
    a, b, mid = _mk(store, write_csv)
    res = _fanout(store, mid)
    assert [d["source_id"] for d in res["member_definitions"]] == [a, b]
    cols = store.get_source(mid)["columns"]
    user = next(c for c in cols if c["name"] == "User")
    assert user.get("derived")
    # the values come from each member's own sidecar
    v = store.build_view(mid, {"source_id": mid, "filters": [], "sort": [{"column": "When", "dir": "asc"}]})
    rows = store.fetch_rows(v["view_id"], 0, 10)["rows"]
    idx = [c["name"] for c in cols].index("User")
    assert [r["cells"][idx] for r in rows] == ["alice", "bob", "carol", "alice"]


def test_merge_filters_and_sorts_on_the_derived_column(store, write_csv):
    _, _, mid = _mk(store, write_csv)
    _fanout(store, mid)
    v = store.build_view(mid, {"source_id": mid, "sort": [],
                               "filters": [{"column": "User", "op": "equals", "value": "alice"}]})
    assert v["row_count"] == 2
    v2 = store.build_view(mid, {"source_id": mid, "filters": [],
                                "sort": [{"column": "User", "dir": "asc"}]})
    rows = store.fetch_rows(v2["view_id"], 0, 10)["rows"]
    cols = [c["name"] for c in store.get_source(mid)["columns"]]
    assert [r["cells"][cols.index("User")] for r in rows][:2] == ["alice", "alice"]


def test_merge_groups_by_the_derived_column(store, write_csv):
    _, _, mid = _mk(store, write_csv)
    _fanout(store, mid)
    v = store.build_view(mid, {"source_id": mid, "filters": [], "sort": []})
    groups = store.group_summary(v["view_id"], "User")["groups"]
    assert {g["value"]: g["count"] for g in groups} == {"alice": 2, "bob": 1, "carol": 1}


def test_partial_coverage_is_not_exposed(store, write_csv):
    a, _, mid = _mk(store, write_csv)
    res = store.add_derived_column(a, "OnlyA", "Who", "regex_extract", RX)
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert "OnlyA" not in [c["name"] for c in store.get_source(mid)["columns"]]
    # ...and views over the merge still build fine
    assert store.build_view(mid, {"source_id": mid, "filters": [], "sort": []})["row_count"] == 4


def test_fanout_is_all_or_nothing_on_a_member_collision(store, write_csv):
    a, b, mid = _mk(store, write_csv)
    res = store.add_derived_column(b, "Clash", "Who", "regex_extract", RX)
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    with pytest.raises(ValueError, match="already has a column"):
        store.add_derived_column(mid, "Clash", "Who", "regex_extract", RX)
    # member a was left untouched by the failed fan-out
    assert "Clash" not in [c["name"] for c in store.get_source(a)["columns"]]


def test_merge_lists_previews_and_values_for_derived(store, write_csv):
    _, _, mid = _mk(store, write_csv)
    _fanout(store, mid)
    assert [d["name"] for d in store.list_derived_columns(mid)] == ["User"]
    prev = store.preview_derived(mid, "Who", "regex_extract", RX)
    assert prev["preview"][0]["output"] == "alice"
    vals = {v["value"] for v in store.column_values(mid, "User")}
    assert vals == {"alice", "bob", "carol"}


def test_merge_export_carries_the_derived_column(store, write_csv):
    _, _, mid = _mk(store, write_csv)
    _fanout(store, mid)
    v = store.build_view(mid, {"source_id": mid, "filters": [], "sort": []})
    text = "".join(store.export_view_csv(v["view_id"]))
    lines = text.strip().splitlines()
    assert lines[0].endswith(",User")
    assert lines[1].endswith(",alice")


# ------------------------------------------------- edits made on the merge
#
# Creating on a merge makes one definition per member, but the merge's
# column list carries only the FIRST member's id. Remove, Re-derive and
# "Change the field path…" sent that id alone, so they acted on the first
# member and left every other member's copy as it was. The client now
# sends the table the edit was made from, and the merge's id there means
# every member that carries the column.

J_A = [["When", "Doc"],
       ["2024-01-01 10:00:00", '{"user": "alice", "host": "SRV1"}'],
       ["2024-01-01 11:00:00", '{"user": "bob", "host": "SRV1"}']]
J_B = [["When", "Doc"],
       ["2024-01-02 09:00:00", '{"user": "carol", "host": "WKS2"}'],
       ["2024-01-02 09:30:00", '{"user": "alice", "host": "WKS2"}']]


def _merge_col(store, mid, name="User"):
    return next(c for c in store.get_source(mid)["columns"] if c["name"] == name)


def _names(store, sid):
    return [c["name"] for c in store.get_source(sid)["columns"]]


def _merge_values(store, mid, name="User"):
    v = store.build_view(mid, {"source_id": mid, "filters": [],
                               "sort": [{"column": "When", "dir": "asc"}]})
    cols = _names(store, mid)
    return [r["cells"][cols.index(name)] for r in store.fetch_rows(v["view_id"], 0, 10)["rows"]]


def _wait_all(store, res):
    for jid in res.get("job_ids") or [res["job_id"]]:
        store.wait_for_ingest_job(jid, timeout=30)


def _json_merge(store, write_csv):
    a = store.ingest_csv(write_csv(J_A, "ja.csv"), name="ja", build_fts=False)["id"]
    b = store.ingest_csv(write_csv(J_B, "jb.csv"), name="jb", build_fts=False)["id"]
    return a, b


def test_remove_on_the_merge_removes_every_members_copy(client, store, write_csv):
    a, b, mid = _mk(store, write_csv)
    _fanout(store, mid)
    r = client.delete(f"/api/derived/{_merge_col(store, mid)['derived_id']}",
                      params={"source_id": mid})
    assert r.status_code == 200, r.text
    assert "User" not in _names(store, a)
    assert "User" not in _names(store, b)   # used to stay behind on every member but the first
    assert store.list_derived_columns(b) == []


def test_remove_on_a_member_still_removes_only_that_member(client, store, write_csv):
    a, b, mid = _mk(store, write_csv)
    res = _fanout(store, mid)
    a_def, b_def = (d["id"] for d in res["member_definitions"])
    r = client.delete(f"/api/derived/{b_def}", params={"source_id": b})
    assert r.status_code == 200, r.text
    assert "User" in _names(store, a) and "User" not in _names(store, b)
    store.remove_derived_column(a_def)   # no table named: the definition alone, as before
    assert "User" not in _names(store, a)


def test_remove_on_the_merge_is_all_or_nothing(store, write_csv):
    """A column computed from this one on ANY member refuses the removal
    on every member — the first must not lose a column the second still
    builds on."""
    a, b, mid = _mk(store, write_csv)
    _fanout(store, mid)
    res = store.add_derived_column(b, "Initial", "User", "regex_extract", {"pattern": r"^(\w)"})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    with pytest.raises(ValueError, match=r"'Initial' on table 'b' is computed from this column"):
        store.remove_derived_column(_merge_col(store, mid)["derived_id"], mid)
    assert "User" in _names(store, a)
    assert "User" in _names(store, b)


def test_remove_refuses_a_definition_from_another_table(store, write_csv):
    a, _, mid = _mk(store, write_csv)
    c = store.ingest_csv(write_csv(A_ROWS, "c.csv"), name="c", build_fts=False)["id"]
    res = store.add_derived_column(c, "User", "Who", "regex_extract", RX)
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    with pytest.raises(KeyError):
        store.remove_derived_column(res["definition"]["id"], mid)   # c is not in the merge
    with pytest.raises(KeyError):
        store.remove_derived_column(res["definition"]["id"], a)
    assert "User" in _names(store, c)


def test_rederive_on_the_merge_recomputes_every_member(client, store, write_csv):
    _, _, mid = _mk(store, write_csv)
    _fanout(store, mid)
    r = client.post(f"/api/derived/{_merge_col(store, mid)['derived_id']}/rederive",
                    json={"params": {"pattern": r"h=(\w+)"}, "source_id": mid})
    assert r.status_code == 200, r.text
    _wait_all(store, r.json())
    # The second member used to keep its old values (carol, alice).
    assert _merge_values(store, mid) == ["SRV1", "SRV1", "WKS2", "WKS2"]


def test_changing_the_field_path_on_the_merge_changes_every_member(client, store, write_csv):
    a, b = _json_merge(store, write_csv)
    mid = store.create_merge("json", [a, b])["id"]
    _wait_all(store, store.add_derived_column(mid, "User", "Doc", "json_field", {"path": "$.user"}))
    # What "Change the field path…" sends.
    r = client.post(f"/api/derived/{_merge_col(store, mid)['derived_id']}/rederive",
                    json={"params": {"path": "$.host"}, "source_id": mid})
    assert r.status_code == 200, r.text
    _wait_all(store, r.json())
    assert _merge_values(store, mid) == ["SRV1", "SRV1", "WKS2", "WKS2"]
    assert all(d["params"] == {"path": "$.host"}
               for sid in (a, b) for d in store.list_derived_columns(sid))


def test_rederive_on_the_merge_is_all_or_nothing(store, write_csv):
    """Same name, two different operations (made on each table by hand
    before the merge): one set of params cannot mean the same thing to
    both, so the re-derive is refused with neither member touched."""
    a, b = _json_merge(store, write_csv)
    _wait_all(store, store.add_derived_column(a, "User", "Doc", "regex_extract",
                                              {"pattern": r'"user": "(\w+)"'}))
    _wait_all(store, store.add_derived_column(b, "User", "Doc", "json_field", {"path": "$.user"}))
    mid = store.create_merge("json", [a, b])["id"]
    before = [store.list_derived_columns(sid) for sid in (a, b)]
    with pytest.raises(ValueError, match="not computed the same way"):
        store.rederive_column(_merge_col(store, mid)["derived_id"], {"pattern": r'"host": "(\w+)"'}, mid)
    assert [store.list_derived_columns(sid) for sid in (a, b)] == before
    # A pattern that won't compile is refused before any member is put in
    # 'building', the same as on one table.
    _, _, mid2 = _mk(store, write_csv)
    _fanout(store, mid2)
    with pytest.raises(ValueError):
        store.rederive_column(_merge_col(store, mid2)["derived_id"], {"pattern": "(unclosed"}, mid2)
    assert _merge_col(store, mid2)["derived_status"] == "ready"
    assert _merge_values(store, mid2) == ["alice", "bob", "carol", "alice"]


def test_the_merges_column_reports_every_member(store, write_csv):
    """One column over every member: its failure count is all of theirs
    (it is what "Show N unparsed rows" says, and that filter matches rows
    in every member), and it is as unfinished as its least finished
    member — a cancelled re-derive on one member leaves that member's
    column 'partial' (kept, not dropped), and the merge says so."""
    a, b, mid = _mk(store, write_csv)
    res = _fanout(store, mid, params={"pattern": r"h=(SRV\d)"})   # matches nothing in b
    assert [store.get_derived_column(d["id"])["parse_failures"]
            for d in res["member_definitions"]] == [0, 2]
    col = _merge_col(store, mid)
    assert col["parse_failures"] == 2
    assert col["derived_status"] == "ready"
    b_def = res["member_definitions"][1]["id"]
    with pytest.raises(Exception):
        store.backfill_derived_column(b_def, cancel=lambda: True, drop_on_cancel=False)
    assert _merge_col(store, mid)["derived_status"] == "partial"
    assert "User" in _names(store, a) and "User" in _names(store, b)


# ------------------------------------------ one job for the whole merge
#
# Creating on a merge started a job per member, and those ran two at a
# time (MAX_CONCURRENT_INGESTS): two CPU-bound passes fighting over the
# GIL and the writer lock, so a merge of four 250k-row tables took about
# twice as long as one table of the same rows, and the column filled in a
# table or two at a time. One job now walks the members in turn.

C_ROWS = [["When", "Who"],
          ["2024-01-03 08:00:00", "u=dave;h=DC1"]]


def _mk3(store, write_csv):
    ids = [store.ingest_csv(write_csv(rows, f"{n}.csv"), name=n, build_fts=False)["id"]
           for n, rows in (("a", A_ROWS), ("b", B_ROWS), ("c", C_ROWS))]
    return (*ids, store.create_merge("three", ids)["id"])


def _new_jobs(store, before):
    return [j for j in store.list_ingest_jobs() if j["job_id"] not in before]


def test_creating_on_a_merge_is_one_job_over_every_member(store, write_csv):
    a, b, c, mid = _mk3(store, write_csv)
    before = {j["job_id"] for j in store.list_ingest_jobs()}
    res = _fanout(store, mid, params={"pattern": r"h=(SRV\d)"})   # matches only a's rows
    jobs = _new_jobs(store, before)
    assert len(jobs) == 1, [j["source_ids"] for j in jobs]   # used to be one per member
    assert res["job_ids"] == [res["job_id"]] == [jobs[0]["job_id"]]
    job = jobs[0]
    assert job["status"] == "done"
    assert job["source_ids"] == [a, b, c]
    assert (job["rows_done"], job["tables_done"], job["tables_total"]) == (5, 3, 3)
    # What the toast reads: the merge's one column, its rows and failures
    # over every member — not "3 columns: 2 rows read".
    assert [(r["name"], r["rows"], r["parse_failures"]) for r in job["result"]] == [("User", 5, 3)]
    assert _merge_values(store, mid) == ["SRV1", "SRV1", None, None, None]


def test_a_batch_on_a_merge_is_one_job_too(store, write_csv):
    a, b, mid = _mk(store, write_csv)
    before = {j["job_id"] for j in store.list_ingest_jobs()}
    res = store.add_derived_columns(mid, [
        {"name": "User", "input_column": "Who", "op_id": "regex_extract", "params": RX},
        {"name": "Host", "input_column": "Who", "op_id": "regex_extract", "params": {"pattern": r"h=(\w+)"}},
    ])
    _wait_all(store, res)
    (job,) = _new_jobs(store, before)
    assert res["job_ids"] == [job["job_id"]]
    assert [len(r["definitions"]) for r in res["member_results"]] == [2, 2]
    assert [(r["name"], r["rows"]) for r in job["result"]] == [("User", 4), ("Host", 4)]
    assert _merge_values(store, mid, "Host") == ["SRV1", "SRV1", "WKS2", "WKS2"]


def test_rederive_and_its_cascade_on_a_merge_are_one_job_each(store, write_csv):
    a, b, mid = _mk(store, write_csv)
    _fanout(store, mid)
    _wait_all(store, store.add_derived_column(mid, "Initial", "User", "regex_extract", {"pattern": r"^(\w)"}))
    before = {j["job_id"] for j in store.list_ingest_jobs()}
    res = store.rederive_column(_merge_col(store, mid)["derived_id"], {"pattern": r"h=(\w+)"}, mid)
    assert res["cascades_to"] == ["Initial"]
    deadline = time.monotonic() + 30
    while any(j["status"] in ("queued", "running") for j in _new_jobs(store, before)) or \
            len(_new_jobs(store, before)) < 2:
        assert time.monotonic() < deadline, _new_jobs(store, before)
        time.sleep(0.02)
    jobs = sorted(_new_jobs(store, before), key=lambda j: j["job_id"])
    assert [(j["name"], j["source_ids"]) for j in jobs] == [
        ("User", [a, b]), ("Initial (recomputed — its input changed)", [a, b])]
    assert _merge_values(store, mid, "Initial") == ["S", "S", "W", "W"]


def _stop_on_member(store, monkeypatch, n, how):
    """Stop a merge's derive job on its `n`th member (1-based): `how` is
    "cancel" (the job's own cancel, seen at that member's first batch) or
    "fail" (an error after that member's first batch is written)."""
    real = store.backfill_derived_columns
    seen = []

    def backfill(def_ids, **kw):
        seen.append(def_ids)
        if len(seen) == n:
            if how == "cancel":
                kw["cancel"] = lambda: True
            else:
                def boom(*_):
                    raise OSError("disk full")
                kw["progress"] = boom
        return real(def_ids, **kw)

    monkeypatch.setattr(store, "backfill_derived_columns", backfill)
    return seen


@pytest.mark.parametrize("how", ["cancel", "fail"])
def test_a_create_stopped_part_way_leaves_no_member_with_the_column(store, write_csv, monkeypatch, how):
    """Member one finished, member two stopped, member three never ran:
    the column goes from all three, so the merge has no half-made column
    hidden on some members whose name would refuse the retry."""
    a, b, c, mid = _mk3(store, write_csv)
    seen = _stop_on_member(store, monkeypatch, 2, how)
    res = store.add_derived_column(mid, "User", "Who", "regex_extract", RX)
    job = store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert job["status"] == ("cancelled" if how == "cancel" else "error"), job
    assert len(seen) == 2   # the third member was never started
    for sid in (a, b, c):
        assert "User" not in _names(store, sid), sid
        assert store.list_derived_columns(sid) == []
    monkeypatch.undo()
    _wait_all(store, store.add_derived_column(mid, "User", "Who", "regex_extract", RX))
    assert _merge_values(store, mid) == ["alice", "bob", "carol", "alice", "dave"]


def test_a_rederive_cancelled_part_way_keeps_what_each_member_has(store, write_csv, monkeypatch):
    """The member it finished is re-derived, the one it was on is
    'partial' (kept, not dropped — a re-derive is not a delete), and the
    one it never reached is the column it was, under its old params."""
    a, b, c, mid = _mk3(store, write_csv)
    _fanout(store, mid)
    _stop_on_member(store, monkeypatch, 2, "cancel")
    res = store.rederive_column(_merge_col(store, mid)["derived_id"], {"pattern": r"h=(\w+)"}, mid)
    assert store.wait_for_ingest_job(res["job_id"], timeout=30)["status"] == "cancelled"
    (da,), (db,), (dc,) = (store.list_derived_columns(sid) for sid in (a, b, c))
    assert (da["status"], da["params"]["pattern"]) == ("ready", r"h=(\w+)")
    assert db["status"] == "partial"
    assert (dc["status"], dc["params"]["pattern"]) == ("ready", r"u=(\w+)")
    assert _merge_col(store, mid)["derived_status"] == "partial"
    vals = _merge_values(store, mid)
    assert vals[:2] == ["SRV1", "SRV1"] and vals[-1] == "dave"
