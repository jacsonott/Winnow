"""Store.time_histogram — time buckets over the CURRENT view (filtered,
merged, root_virtual alike) — and the GET /api/histogram route the
built-in histogram strip (static/js/histogram.js) draws from, plus the
tombstone that keeps a stale copy of the retired table_histogram example
from loading beside it."""

from __future__ import annotations

import json
import textwrap

import pytest

from winnow.plugin_api import PluginRegistry

ROWS = [["When", "Host"]] + [
    [f"2026-03-14 08:{m:02d}:00", "A" if (m // 2) % 2 else "B"] for m in range(0, 60, 2)   # 30 rows, 2 min apart, hosts alternating
] + [["not a date", "A"], ["", "B"]]


@pytest.fixture
def src(store, write_csv):
    return store.ingest_csv(write_csv(ROWS, "h.csv"), name="h", build_fts=False)["id"]


def test_unfiltered_view_buckets_the_whole_table(store, src):
    view = store.build_view(src, {"source_id": src})
    h = store.time_histogram(view["view_id"], "When")
    assert h["total"] == 30                       # the two unparsable rows fall out
    assert h["start"] == "2026-03-14 08:00:00" and h["end"] == "2026-03-14 08:58:00"
    assert sum(n for _, n in h["buckets"]) == 30
    # 58 min span / 160 max buckets → 30-second buckets.
    assert h["bucket_seconds"] == 30
    assert all(b % 30 == 0 for b, _ in h["buckets"])


def test_filtered_view_only_counts_its_rows(store, src):
    view = store.build_view(src, {"source_id": src, "filters": [{"column": "Host", "op": "equals", "value": "A"}]})
    h = store.time_histogram(view["view_id"], "When")
    assert h["total"] == 15
    assert sum(n for _, n in h["buckets"]) == 15


def test_merge_view_unions_members(store, src, write_csv):
    sid2 = store.ingest_csv(write_csv([["When", "Host"], ["2026-03-14 09:00:00", "Z"]], "h2.csv"),
                            name="h2", build_fts=False)["id"]
    mid = store.create_merge("hm", [src, sid2])["id"]
    view = store.build_view(mid, {"source_id": mid})
    h = store.time_histogram(view["view_id"], "When")
    assert h["total"] == 31 and h["end"] == "2026-03-14 09:00:00"


def test_bucket_width_fits_max_buckets(store, src):
    view = store.build_view(src, {"source_id": src})
    h = store.time_histogram(view["view_id"], "When", max_buckets=4)
    # 58 min into ≤4 bars → 15-minute buckets.
    assert h["bucket_seconds"] == 900 and len(h["buckets"]) <= 4


def test_non_datetime_column_is_refused_and_empty_view_is_empty(store, src):
    view = store.build_view(src, {"source_id": src})
    with pytest.raises(ValueError, match="not a datetime"):
        store.time_histogram(view["view_id"], "Host")
    none = store.build_view(src, {"source_id": src, "filters": [{"column": "Host", "op": "equals", "value": "nope"}]})
    assert store.time_histogram(none["view_id"], "When")["total"] == 0


def test_pre_1970_rows_land_in_their_own_bucket(store, write_csv):
    """A bucket is [b, b + bucket_seconds) — the strip finds a row's bar
    that way. SQLite's integer division truncates toward zero, so a
    negative epoch was bucketed one bar late: 1969-12-31 23:00:01 drawn
    in a bar starting at 23:05, after the instant it happened."""
    sid = store.ingest_csv(write_csv([["When"], ["1969-12-31 23:00:01"], ["1970-01-01 01:00:00"],
                                      ["1970-01-01 02:00:00"]], "old.csv"),
                           name="old", build_fts=False)["id"]
    view = store.build_view(sid, {"source_id": sid})
    for stack in (None, "tags"):
        h = store.time_histogram(view["view_id"], "When", stack=stack)
        width = h["bucket_seconds"]
        assert width == 300
        assert h["buckets"] == [[-3600, 1], [3600, 1], [7200, 1]]
        for epoch in (-3599, 3600, 7200):
            assert any(b <= epoch < b + width for b, _ in h["buckets"])
        if stack:
            assert [b for b, _ in h["stack"]["buckets"]] == [-3600, 3600, 7200]


def test_a_1601_minimum_is_labelled_without_gmtime(store, write_csv, monkeypatch):
    """FILETIME zero (1601-01-01) is a value real artefacts carry. The
    span labels came from time.gmtime, which on Windows raises OSError
    for any negative epoch — a 500 for that column there. Simulated here
    with a gmtime that refuses negatives the way Windows' does."""
    import time

    real = time.gmtime

    def windows_gmtime(secs=None):
        if secs is not None and secs < 0:
            raise OSError(22, "Invalid argument")
        return real(secs)

    monkeypatch.setattr(time, "gmtime", windows_gmtime)
    sid = store.ingest_csv(write_csv([["When"], ["1601-01-01 00:00:00"], ["2026-03-14 08:00:00"]], "ft.csv"),
                           name="ft", build_fts=False)["id"]
    view = store.build_view(sid, {"source_id": sid})
    h = store.time_histogram(view["view_id"], "When")
    assert h["start"] == "1601-01-01 00:00:00" and h["end"] == "2026-03-14 08:00:00"
    assert h["total"] == 2


# ------------------------------------------------------------ one pass

# Every shape the normaliser has a branch for, the ones the SQL fast path
# takes and the ones it must leave alone, plus the near misses between.
NORMALIZE_CORPUS = [
    "2026-03-14 08:00:00", "2026-03-14T08:00:00", "2026-03-14T08:00:00.1234567Z",
    "2026-03-14 08:00:00.123456", "2026-03-14T08:00:00+02:00", "2026-03-14 08:00:00 UTC",
    "2026-03-14 08:00", "2026-03-14T08:00", "2026-03-14", "2026-03-14 8:00:00",
    "2026-03-14t08:00:00", "2026-03-14_08:00:00", "  2026-03-14 08:00:00", "2026-03-14 08:00:00  ",
    "1601-01-01 00:00:00", "1969-12-31 23:59:59", "2026-13-45 99:99:99", "0000-00-00 00:00:00",
    "２０２６-03-14 08:00:00", "2026-03-14 08:00:0١",
    "3/14/2026 8:00:00 PM", "03/14/2026 20:00", "3/14/2026", "Mar 14 2026 08:00:00",
    "14 March 2026", "March 14th, 2026 8:00 pm", "not a date", "", "20260314080000",
]


def _normalize_both(values):
    """(TS_NORMALIZE in Python, ts_normalize_sql in SQLite) per value."""
    import sqlite3

    from winnow.store import _ts_normalize, ts_normalize_sql

    db = sqlite3.connect(":memory:")
    db.create_function("TS_NORMALIZE", 1, _ts_normalize, deterministic=True)
    db.execute("CREATE TABLE t (i INTEGER PRIMARY KEY, v)")
    db.executemany("INSERT INTO t (v) VALUES (?)", [(v,) for v in values])
    got = [r[0] for r in db.execute(f"SELECT {ts_normalize_sql('v')} FROM t ORDER BY i")]
    db.close()
    return [_ts_normalize(v) for v in values], got


def test_the_sql_fast_path_normalises_exactly_as_ts_normalize_does():
    """time_histogram takes the ISO shape out of Python with
    ts_normalize_sql. A value it read differently would put a row in a
    different bar from the one the timeframe filter (TS_NORMALIZE in
    Python) puts it in, and a drag across that bar would miss it."""
    values = NORMALIZE_CORPUS + [b"2026-03-14 08:00:00", 1773475200, 1773475200.5, None]
    want, got = _normalize_both(values)
    assert got == want


def test_the_sql_fast_path_agrees_on_random_timestamp_shaped_text():
    import random

    rng = random.Random(20261020)
    alphabet = "0123456789-: T./Z+"
    base = "2026-03-14 08:00:00.123Z"
    values = []
    for _ in range(3000):
        chars = list(base)
        for _ in range(rng.randint(1, 3)):
            chars[rng.randrange(len(chars))] = rng.choice(alphabet)
        values.append("".join(chars)[: rng.randint(8, len(chars))])
    want, got = _normalize_both(values)
    assert got == want


def _day_both(values):
    """(DAY_BUCKET in Python, day_bucket_sql in SQLite) per value."""
    import sqlite3

    from winnow.store import _day_bucket, day_bucket_sql

    db = sqlite3.connect(":memory:")
    db.create_function("DAY_BUCKET", 1, _day_bucket, deterministic=True)
    db.execute("CREATE TABLE t (i INTEGER PRIMARY KEY, v)")
    db.executemany("INSERT INTO t (v) VALUES (?)", [(v,) for v in values])
    got = [r[0] for r in db.execute(f"SELECT {day_bucket_sql('v')} FROM t ORDER BY i")]
    db.close()
    return [_day_bucket(v) for v in values], got


def test_the_sql_day_agrees_with_day_bucket():
    """The Groups view's day for a datetime grouping comes from
    day_bucket_sql; the grid's group strip from DAY_BUCKET. A value the two
    read differently would be counted under one group in the grid and
    another in the bars."""
    import random

    rng = random.Random(20261022)
    alphabet = "0123456789-: T./Z+"
    base = "2026-03-14 08:00:00.123Z"
    values = NORMALIZE_CORPUS + [b"2026-03-14 08:00:00", 1773475200, 1773475200.5, None]
    for _ in range(3000):
        chars = list(base)
        for _ in range(rng.randint(1, 3)):
            chars[rng.randrange(len(chars))] = rng.choice(alphabet)
        values.append("".join(chars)[: rng.randint(8, len(chars))])
    want, got = _day_both(values)
    assert got == want


def test_unixepoch_reads_a_normalized_timestamp_as_strftime_does():
    """epoch_sql uses unixepoch() where SQLite has it (3.38+), strftime('%s')
    cast where it does not. The two must agree on everything TS_NORMALIZE
    can produce, 1601 and the seconds either side of 1970 included."""
    import sqlite3

    from winnow.store import _ts_normalize

    if sqlite3.sqlite_version_info < (3, 38, 0):
        pytest.skip("no unixepoch() in this SQLite; epoch_sql uses strftime")
    values = [_ts_normalize(v) for v in NORMALIZE_CORPUS]
    values += ["1601-01-01 00:00:00", "1969-12-31 23:59:59", "1970-01-01 00:00:00",
               "1970-01-01 00:00:01", "9999-12-31 23:59:59", "2026-02-30 00:00:00", None]
    db = sqlite3.connect(":memory:")
    got = [db.execute("SELECT unixepoch(?), CAST(strftime('%s', ?) AS INTEGER)", (v, v)).fetchone() for v in values]
    db.close()
    assert [a for a, _ in got] == [b for _, b in got]


def test_each_row_is_parsed_once(write_csv, tmp_path, monkeypatch):
    """The cost this removed. The span and the per-bucket counts were two
    passes over the view and the tag split a third, each calling
    TS_NORMALIZE (Python) on every row again — 4.8 s on a 500k-row view,
    for a strip that refreshes on every filter change. Counted on values
    the SQL fast path does not take, so each of them reaches Python."""
    from winnow import store as store_mod

    calls = {"n": 0}
    real = store_mod._ts_normalize

    def counting(raw):
        calls["n"] += 1
        return real(raw)

    monkeypatch.setattr(store_mod, "_ts_normalize", counting)
    rows = [["When", "Host"]] + [[f"3/14/2026 {h}:00:00 AM", "A"] for h in range(1, 13)]
    s = store_mod.Store(str(tmp_path / "once.db"))   # its connections register the counting one
    s.HISTOGRAM_ANSWERS_KEPT = 0   # a sort of the whole table is otherwise answered with no pass at all
    try:
        sid = s.ingest_csv(write_csv(rows, "us.csv"), name="us", build_fts=False)["id"]
        other = s.ingest_csv(write_csv(rows, "us2.csv"), name="us2", build_fts=False)["id"]
        mid = s.create_merge("both", [sid, other])["id"]
        # Every shape the query takes: the virtual root reads the table, a
        # sorted view joins v.view_N, and a merge is a UNION ALL — which
        # SQLite optimises differently again (it pushes the WHERE into
        # each branch rather than flattening).
        for table, sort, total in ((sid, [], 12), (sid, [{"column": "Host", "dir": "asc"}], 12),
                                   (mid, [], 24), (mid, [{"column": "Host", "dir": "asc"}], 24)):
            view = s.build_view(table, {"source_id": table, "sort": sort})
            for stack in (None, "tags"):
                calls["n"] = 0
                h = s.time_histogram(view["view_id"], "When", stack=stack)
                assert h["total"] == total
                assert calls["n"] == total, (table, sort, stack, calls["n"])
    finally:
        s.close()


# ---------------------------------------------------------------- route

def test_route_returns_the_buckets_of_the_view(client, store, src):
    view = store.build_view(src, {"source_id": src, "filters": [{"column": "Host", "op": "equals", "value": "A"}]})
    r = client.get(f"/api/histogram?view_id={view['view_id']}&column=When")
    assert r.status_code == 200, r.text
    h = r.json()
    assert h["total"] == 15 and h["column"] == "When" and sum(n for _, n in h["buckets"]) == 15


def test_route_serves_a_merge_view(client, store, src, write_csv):
    sid2 = store.ingest_csv(write_csv([["When", "Host"], ["2026-03-14 09:00:00", "Z"]], "h2.csv"),
                            name="h2", build_fts=False)["id"]
    mid = store.create_merge("hm", [src, sid2])["id"]
    view = store.build_view(mid, {"source_id": mid})
    r = client.get(f"/api/histogram?view_id={view['view_id']}&column=When")
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 31 and r.json()["end"] == "2026-03-14 09:00:00"


def test_route_clamps_the_bucket_count_to_what_a_canvas_can_show(client, store, src):
    view = store.build_view(src, {"source_id": src})
    # 58 min at "one bar per second" would be 3,480 bars; 400 is the ceiling → 10s buckets.
    r = client.get(f"/api/histogram?view_id={view['view_id']}&column=When&max_buckets=100000")
    assert r.status_code == 200 and r.json()["bucket_seconds"] == 10
    # And below the floor of 20: 58 min into ≤20 bars → 5-minute buckets.
    r = client.get(f"/api/histogram?view_id={view['view_id']}&column=When&max_buckets=1")
    assert r.status_code == 200 and r.json()["bucket_seconds"] == 300


def test_route_400s_a_column_it_cannot_chart(client, store, src):
    """Both are the analyst's problem, not the view's: a 409 here would have
    the strip waiting for a view change that fixes nothing."""
    view = store.build_view(src, {"source_id": src})
    r = client.get(f"/api/histogram?view_id={view['view_id']}&column=Host")
    assert r.status_code == 400 and "not a datetime" in r.json()["detail"]
    r = client.get(f"/api/histogram?view_id={view['view_id']}&column=Missing")
    assert r.status_code == 400
    assert "Missing" in r.json()["detail"] and "expired" not in r.json()["detail"].lower()


def test_route_409s_an_expired_view(client, store, src):
    """The /api/rows contract: the strip treats an 'expired' 409 as a
    rebuild in progress and waits for the view change that ends it."""
    r = client.get("/api/histogram?view_id=nope&column=When")
    assert r.status_code == 409 and "expired" in r.json()["detail"].lower()
    view = store.build_view(src, {"source_id": src})
    store.close_view(view["view_id"])
    r = client.get(f"/api/histogram?view_id={view['view_id']}&column=When")
    assert r.status_code == 409 and "expired" in r.json()["detail"].lower()


# ------------------------------------------------------------ tombstone

STALE_EXAMPLE = """
    PLUGIN = {"name": "table-histogram", "version": "1.0.0", "description": "stale copy"}

    def register(api):
        api.register_toolbar_panel(id="histogram", label="Histogram", entry="ui/panel.js")
"""


def _copy_of_the_example(root):
    (root / "table_histogram" / "ui").mkdir(parents=True)
    (root / "table_histogram" / "__init__.py").write_text(textwrap.dedent(STALE_EXAMPLE))
    (root / "table_histogram" / "ui" / "panel.js").write_text("export default function mount() {}\n")


def _plugins_from(monkeypatch, installed, bundled):
    import server

    monkeypatch.setattr(server, "PLUGINS", PluginRegistry())
    monkeypatch.setattr(server, "PLUGIN_DIRS", [installed, bundled])
    monkeypatch.setattr(server, "BUNDLED_PLUGIN_DIR", bundled)
    server._reload_plugins()
    return server


def test_a_stale_copy_of_the_retired_example_is_never_enabled(client, store, tmp_path, monkeypatch):
    """A zip install that never ran an update keeps examples/plugins/
    table_histogram beside the built-in strip. The folder is still listed —
    it is the thing to delete — with the reason in its row; the toggle
    refuses it in either scope and persists nothing; and a case file that
    already carries an override for it passes through the same policy the
    tombstone sits in, so it cannot bring back a second Histogram button."""
    bundled, installed = tmp_path / "bundled", tmp_path / "plugins"
    _copy_of_the_example(bundled)
    installed.mkdir()
    server = _plugins_from(monkeypatch, installed, bundled)

    def state():
        r = client.get("/api/plugins").json()
        rec = next(p for p in r["plugins"] if p["fs_name"] == "table_histogram")
        return rec["enabled"], rec["error"], [p["id"] for p in r["panels"]]

    enabled, why, panels = state()
    assert (enabled, panels) == (False, [])
    assert why and "built into Winnow" in why and "examples/plugins/table_histogram" in why

    r = client.post("/api/plugins/toggle", json={"fs_name": "table_histogram", "scope": "on_all"})
    assert r.status_code == 400 and "built into Winnow" in r.json()["detail"]
    assert "table_histogram" not in server.WS.plugin_prefs.enabled_bundled()
    r = client.post("/api/plugins/toggle", json={"fs_name": "table_histogram", "scope": "on_case"})
    assert r.status_code == 400
    assert store.get_case_settings().get("plugin_overrides") is None
    assert state()[0] is False and state()[2] == []

    store.set_case_setting("plugin_overrides", json.dumps({"table_histogram": True}))
    server._reload_plugins()
    assert state()[0] is False and state()[2] == []


def test_an_analysts_own_copy_of_the_example_is_theirs(client, tmp_path, monkeypatch):
    """The tombstone is about the shipped folder. The same code installed
    into plugins/ is the analyst's plugin: it loads, and its toggle is an
    ordinary toggle."""
    bundled, installed = tmp_path / "bundled", tmp_path / "plugins"
    bundled.mkdir()
    _copy_of_the_example(installed)
    _plugins_from(monkeypatch, installed, bundled)

    listing = client.get("/api/plugins").json()
    rec = next(p for p in listing["plugins"] if p["fs_name"] == "table_histogram")
    assert rec["enabled"] is True and rec["error"] is None and not rec["bundled"]
    assert [p["id"] for p in listing["panels"]] == ["table-histogram.histogram"]
    r = client.post("/api/plugins/toggle", json={"fs_name": "table_histogram", "scope": "off_all"})
    assert r.status_code == 200, r.text
    assert next(p for p in r.json()["plugins"] if p["fs_name"] == "table_histogram")["enabled"] is False
