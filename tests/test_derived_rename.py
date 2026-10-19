"""Renaming a derived column: the values stay, and everything in the case
file that names the column — the sidecar, the columns computed from it,
lookups on other tables, saved layouts — follows the new name, all in one
transaction; on a merge every member's copy is renamed."""

from __future__ import annotations

import json
import time

import pytest

ROWS = [["When", "Who", "Start"],
        ["2024-01-01 10:00:00", "u=alice;h=SRV1", "2024-01-01 09:59:00"],
        ["2024-01-01 11:00:00", "u=bob;h=SRV1", "2024-01-01 10:30:00"]]
B_ROWS = [["When", "Who", "Start"],
          ["2024-01-02 09:00:00", "u=carol;h=WKS2", "2024-01-02 08:00:00"]]
RX = {"pattern": r"u=(\w+)"}


def _add(store, sid, name, column, op_id, params):
    res = store.add_derived_column(sid, name, column, op_id, params)
    for jid in res.get("job_ids") or [res["job_id"]]:
        store.wait_for_ingest_job(jid, timeout=30)
    return res["definition"]["id"]


def _table(store, write_csv, rows=ROWS, name="a"):
    return store.ingest_csv(write_csv(rows, f"{name}.csv"), name=name, build_fts=False)["id"]


def _values(store, sid, column):
    src = store.get_source(sid)
    idx = [c["name"] for c in src["columns"]].index(column)
    v = store.build_view(sid, {"source_id": sid, "filters": [], "sort": []})
    return [r["cells"][idx] for r in store.fetch_rows(v["view_id"], 0, 10)["rows"]]


def _names(store, sid):
    return [c["name"] for c in store.get_source(sid)["columns"]]


def test_rename_keeps_the_values_and_the_new_name_works_everywhere(store, write_csv):
    sid = _table(store, write_csv)
    def_id = _add(store, sid, "User", "Who", "regex_extract", RX)

    res = store.rename_derived_column(def_id, "  Account  ")
    assert res["definition"]["name"] == "Account"
    assert "User" not in _names(store, sid)
    assert _values(store, sid, "Account") == ["alice", "bob"]
    # filter, sort and group by the new name: it is the column's name now,
    # not a label over the old one
    v = store.build_view(sid, {"source_id": sid, "sort": [{"column": "Account", "dir": "desc"}],
                               "filters": [{"column": "Account", "op": "equals", "value": "bob"}]})
    assert v["row_count"] == 1
    groups = store.group_summary(store.build_view(sid, {"source_id": sid, "filters": [], "sort": []})["view_id"],
                                 "Account")["groups"]
    assert {g["value"] for g in groups} == {"alice", "bob"}
    phys = [r[1] for r in store.db.execute(f'PRAGMA table_info("drv_{sid}")')]
    assert "Account" in phys and "User" not in phys


def test_a_change_of_capitals_alone_is_a_rename(store, write_csv):
    sid = _table(store, write_csv)
    def_id = _add(store, sid, "user", "Who", "regex_extract", RX)
    store.rename_derived_column(def_id, "User")
    assert "User" in _names(store, sid) and "user" not in _names(store, sid)
    assert _values(store, sid, "User") == ["alice", "bob"]


@pytest.mark.parametrize("name, why", [
    ("Who", "already has a column called"),     # a base column
    ("who", "already has a column called"),     # ...whatever the capitals
    ("Other", "already has a column called"),   # another derived column
    ("rid", "reserved"),
    ("   ", "needs a name"),
    ("x" * 201, "too long"),
])
def test_refused_names_change_nothing(store, write_csv, name, why):
    sid = _table(store, write_csv)
    def_id = _add(store, sid, "User", "Who", "regex_extract", RX)
    _add(store, sid, "Other", "Who", "regex_extract", {"pattern": r"h=(\w+)"})
    with pytest.raises(ValueError, match=why):
        store.rename_derived_column(def_id, name)
    assert store.get_derived_column(def_id)["name"] == "User"
    assert _values(store, sid, "User") == ["alice", "bob"]


def test_same_name_is_a_no_op(store, write_csv):
    sid = _table(store, write_csv)
    def_id = _add(store, sid, "User", "Who", "regex_extract", RX)
    assert store.rename_derived_column(def_id, "User")["definition"]["name"] == "User"


def test_columns_computed_from_it_follow_the_new_name(store, write_csv):
    """Its input, a `column` param and a `columns` list all name the
    renamed column afterwards — proved by re-deriving the parent, whose
    cascade recomputes each child through the name it now holds."""
    sid = _table(store, write_csv)
    parent = _add(store, sid, "Time", "When", "iso8601", {})
    chained = _add(store, sid, "Hour", "Time", "regex_extract", {"pattern": r" (\d\d):"})
    dur = _add(store, sid, "Took", "Start", "duration_delta", {"other_column": "Time"})
    first = _add(store, sid, "Any", "Who", "coalesce", {"extra_columns": ["Start", "Time"]})

    store.rename_derived_column(parent, "Event time")
    assert store.get_derived_column(chained)["input_column"] == "Event time"
    assert store.get_derived_column(dur)["params"]["other_column"] == "Event time"
    assert store.get_derived_column(first)["params"]["extra_columns"] == ["Start", "Event time"]

    res = store.rederive_column(parent, {})
    assert set(res["cascades_to"]) == {"Hour", "Took", "Any"}
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    for d in store.list_derived_columns(sid):
        for _ in range(300):
            if store.get_derived_column(d["id"])["status"] != "building":
                break
            time.sleep(0.02)
    assert {d["name"]: d["status"] for d in store.list_derived_columns(sid)} == {
        "Event time": "ready", "Hour": "ready", "Took": "ready", "Any": "ready"}
    assert _values(store, sid, "Hour") == ["10", "11"]
    assert _values(store, sid, "Took") == ["-60.000000", "-1800.000000"]


def test_a_lookup_on_another_table_follows_the_new_name(store, write_csv):
    hosts = _table(store, write_csv, [["Pair"], ["alice=DC01"], ["bob=WKS7"]], name="hosts")
    key = _add(store, hosts, "Key", "Pair", "regex_extract", {"pattern": r"^(\w+)="})
    val = _add(store, hosts, "Host", "Pair", "regex_extract", {"pattern": r"=(\w+)$"})
    ev = _table(store, write_csv)
    user = _add(store, ev, "User", "Who", "regex_extract", RX)
    lk = _add(store, ev, "Machine", "User", "lookup",
              {"other_source_id": hosts, "match_column": "Key", "value_column": "Host"})

    store.rename_derived_column(key, "Account")
    store.rename_derived_column(val, "Hostname")
    params = store.get_derived_column(lk)["params"]
    assert (params["match_column"], params["value_column"]) == ("Account", "Hostname")
    # and its own input, on its own table
    store.rename_derived_column(user, "Who parsed")
    assert store.get_derived_column(lk)["input_column"] == "Who parsed"
    res = store.rederive_column(lk, None)
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert _values(store, ev, "Machine") == ["DC01", "WKS7"]


def test_the_saved_layout_follows_the_new_name(store, write_csv):
    sid = _table(store, write_csv)
    def_id = _add(store, sid, "User", "Who", "regex_extract", RX)
    store.save_layout(sid, {
        "columns": {"User": {"w": 222, "hidden": False}, "Gone": {"w": 50}, "When": {"w": 90}},
        "order": ["When", "User", "Gone", "Who", "Start"],
        "sort": [{"column": "User", "dir": "desc"}],
        "value_filters": "auto",
    })
    store.rename_derived_column(def_id, "Gone")
    lay = store.get_layout(sid)
    # the leftover "Gone" entry described a column that no longer exists;
    # the renamed column's own settings win
    assert lay["columns"] == {"Gone": {"w": 222, "hidden": False}, "When": {"w": 90}}
    assert lay["order"] == ["When", "Gone", "Who", "Start"]
    assert lay["sort"] == [{"column": "Gone", "dir": "desc"}]


def test_rename_on_a_merge_renames_every_member(store, write_csv):
    a = _table(store, write_csv, ROWS, "a")
    b = _table(store, write_csv, B_ROWS, "b")
    mid = store.create_merge("both", [a, b])["id"]
    def_id = _add(store, mid, "User", "Who", "regex_extract", RX)
    store.save_layout(mid, {"columns": {"User": {"w": 140}}, "order": ["When", "User"], "sort": []})

    res = store.rename_derived_column(def_id, "Account", mid)
    assert [d["name"] for d in res["member_definitions"]] == ["Account", "Account"]
    assert "Account" in _names(store, mid) and "User" not in _names(store, mid)
    assert sorted(_values(store, mid, "Account")) == ["alice", "bob", "carol"]
    assert store.get_layout(mid)["order"] == ["When", "Account"]


def test_rename_on_one_member_leaves_the_merge_layout_alone(store, write_csv):
    a = _table(store, write_csv, ROWS, "a")
    b = _table(store, write_csv, B_ROWS, "b")
    mid = store.create_merge("both", [a, b])["id"]
    _add(store, mid, "User", "Who", "regex_extract", RX)
    store.save_layout(mid, {"columns": {"User": {"w": 140}}, "order": ["When", "User"], "sort": []})
    on_a = next(d for d in store.list_derived_columns(a) if d["name"] == "User")

    store.rename_derived_column(on_a["id"], "Account", a)
    # b still has "User", so the merge's layout still has a column to describe
    assert store.get_layout(mid)["order"] == ["When", "User"]
    assert next(d for d in store.list_derived_columns(b))["name"] == "User"


def test_merge_rename_checks_members_without_the_column(store, write_csv):
    a = _table(store, write_csv, ROWS, "a")
    b = _table(store, write_csv, [["When", "Who", "Start", "Account"], ["x", "u=z", "y", "q"]], "b")
    mid = store.create_merge("both", [a, b])["id"]
    on_a = _add(store, a, "User", "Who", "regex_extract", RX)
    with pytest.raises(ValueError, match="Member table 'b' already has a column called 'Account'"):
        store.rename_derived_column(on_a, "Account", mid)
    assert store.get_derived_column(on_a)["name"] == "User"


def test_merge_rename_is_all_or_nothing(store, write_csv, monkeypatch):
    a = _table(store, write_csv, ROWS, "a")
    b = _table(store, write_csv, B_ROWS, "b")
    mid = store.create_merge("both", [a, b])["id"]
    def_id = _add(store, mid, "User", "Who", "regex_extract", RX)
    real = store._rename_drv_column
    calls = []

    def second_fails(drv, old, new):
        calls.append(drv)
        if len(calls) == 2:
            raise RuntimeError("disk gone")
        return real(drv, old, new)

    monkeypatch.setattr(store, "_rename_drv_column", second_fails)
    with pytest.raises(RuntimeError):
        store.rename_derived_column(def_id, "Account", mid)
    monkeypatch.setattr(store, "_rename_drv_column", real)
    assert [d["name"] for d in store.list_derived_columns(a) + store.list_derived_columns(b)] == ["User", "User"]
    assert sorted(_values(store, mid, "User")) == ["alice", "bob", "carol"]


def test_refused_while_it_or_a_column_computed_from_it_is_building(store, write_csv, monkeypatch):
    """A backfill reads and writes its columns by the names they had when
    it started. The child's job is held back, so it stays 'building'."""
    sid = _table(store, write_csv)
    parent = _add(store, sid, "User", "Who", "regex_extract", RX)
    start = store.start_ingest_job
    held = []
    monkeypatch.setattr(store, "start_ingest_job",
                        lambda kind, *a, **k: (held.append((a, k)), {"job_id": None})[1])
    child = store.add_derived_column(sid, "Initial", "User", "regex_extract", {"pattern": r"^(\w)"})
    child_id = child["definition"]["id"]
    with pytest.raises(ValueError, match="'Initial' is being computed from 'User'"):
        store.rename_derived_column(parent, "Account")
    with pytest.raises(ValueError, match="'Initial' is still building"):
        store.rename_derived_column(child_id, "First")

    monkeypatch.setattr(store, "start_ingest_job", start)
    for a, k in held:
        store.wait_for_ingest_job(start("derive", *a, **k)["job_id"], timeout=30)
    store.rename_derived_column(parent, "Account")
    assert store.get_derived_column(child_id)["input_column"] == "Account"


def _break_a_view(store):
    """A view in the case file that no longer parses — SQLite then refuses
    RENAME COLUMN (and DROP COLUMN) anywhere in the file."""
    with store.lock:
        store.db.execute("CREATE TABLE zz_gone(a)")
        store.db.execute("CREATE VIEW zz_broken AS SELECT a FROM zz_gone")
        store.db.execute("PRAGMA legacy_alter_table=ON")
        store.db.execute("DROP TABLE zz_gone")
        store.db.execute("PRAGMA legacy_alter_table=OFF")
        store.db.commit()


def test_falls_back_to_a_copy_when_sqlite_refuses_rename_column(store, write_csv):
    sid = _table(store, write_csv)
    def_id = _add(store, sid, "User", "Who", "regex_extract", RX)
    _break_a_view(store)
    store.rename_derived_column(def_id, "Account")
    assert _values(store, sid, "Account") == ["alice", "bob"]
    # the old column is left behind blanked, invisible like DROP COLUMN's orphan
    assert "User" not in _names(store, sid)
    assert store.db.execute(f'SELECT COUNT("User") FROM "drv_{sid}"').fetchone()[0] == 0
    # ...and a column added under that name again reuses it
    again = _add(store, sid, "User", "Who", "regex_extract", {"pattern": r"h=(\w+)"})
    assert store.get_derived_column(again)["status"] == "ready"
    assert _values(store, sid, "User") == ["SRV1", "SRV1"]


def test_a_change_of_capitals_has_no_fallback(store, write_csv):
    sid = _table(store, write_csv)
    def_id = _add(store, sid, "user", "Who", "regex_extract", RX)
    _break_a_view(store)
    with pytest.raises(ValueError, match="Could not rename 'user'"):
        store.rename_derived_column(def_id, "User")
    assert store.get_derived_column(def_id)["name"] == "user"
    assert _values(store, sid, "user") == ["alice", "bob"]


def test_rename_route(client, store, write_csv):
    sid = _table(store, write_csv)
    def_id = _add(store, sid, "User", "Who", "regex_extract", RX)
    ok = client.post(f"/api/derived/{def_id}/rename", json={"name": "Account", "source_id": sid})
    assert ok.status_code == 200 and ok.json()["definition"]["name"] == "Account"
    assert client.post(f"/api/derived/{def_id}/rename", json={"name": "Who"}).status_code == 400
    assert client.post("/api/derived/99999/rename", json={"name": "X"}).status_code == 404
    assert json.loads(json.dumps(client.get(f"/api/derived?source_id={sid}").json()))[0]["name"] == "Account"
