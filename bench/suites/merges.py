"""A merge of many small tables against the same rows as one table.

Analysts merging 60 hosts' exports found the merge several times slower
at everything than joining the files outside Winnow and importing one
(docs/notes/store.md, "A merge of many tables has to cost about what the
same rows imported as one file cost"). Every benchmark here is a pair:
`one_table.*` on the fixture's no-FTS table, `many.*` on a merge of
MANY_MEMBERS tables holding the same number of rows, so the gap between
the two halves is the per-member cost. A change to any loop over a
merge's members should leave the gaps where they are.

The merge is built once, lazily, on first use (untimed), beside the shared
case: MANY_MEMBERS imports of one rows/MANY_MEMBERS-row file.
"""

from __future__ import annotations

from ..fixtures import DATETIME_COLUMN, EQ_COLUMN, EQ_VALUE, GROUP_COLUMN, ensure_column_index, events_csv, SEED
from ..harness import Timed, benchmark

MANY_MEMBERS = 60
TERM = "base64"


def _many(env):
    """(fixture, merge source id, member ids, total rows)."""
    fx = env.case()
    got = getattr(env, "_many_merge", None)
    if got is None:
        per = max(1, fx.rows // MANY_MEMBERS)
        path = events_csv(per, seed=SEED + 31)
        if env.verbose:
            print(f"\n  ingesting {MANY_MEMBERS} merge members ({per:,} rows each) — untimed setup",
                  flush=True)
        ids = [fx.store.ingest_csv(path, name=f"host{k:02d}.csv", build_fts=False)["id"]
               for k in range(MANY_MEMBERS)]
        mid = fx.store.create_merge(f"{MANY_MEMBERS} hosts", ids, source_column=True)["id"]
        got = env._many_merge = (fx, mid, ids, per * MANY_MEMBERS)
    return got


def _forget(fx, source_id: int) -> None:
    """Let go of the unfiltered views a merge keeps for reuse, so each
    repetition times a real build. getattr: `--vs-ref` runs this file
    against revisions from before the views were kept."""
    forget = getattr(fx.store, "_forget_merge_views", None)
    if forget is not None and source_id < 0:
        with fx.store.lock, fx.store.db:
            forget(source_id)


def _build(fx, source_id: int, spec: dict, items: int, *, keep: bool = False) -> Timed:
    held: dict = {}

    def run():
        held["v"] = fx.store.build_view(source_id, {"source_id": source_id, "filters": [], "sort": [], **spec})

    def after():
        v = held.pop("v", None)
        if v:
            fx.store.close_view(v["view_id"])
        if not keep:
            _forget(fx, source_id)

    return Timed(run, after=after, items=items)


SORTED = {"sort": [{"column": DATETIME_COLUMN, "dir": "asc"}]}
SEARCH = {"search": TERM, "search_mode": "contains"}
EQUALS = {"filters": [{"column": EQ_COLUMN, "op": "equals", "value": EQ_VALUE}]}


@benchmark("merges/one_table.sorted", note="the default view: sorted by the first timestamp")
def _(env):
    fx = env.case()
    return _build(fx, fx.nofts, SORTED, fx.rows)


@benchmark("merges/many.sorted", note="every member's sort index merged into one order")
def _(env):
    fx, mid, _, total = _many(env)
    return _build(fx, mid, SORTED, total)


@benchmark("merges/many.sorted_again", note="Clear filters: the kept view, not a rebuild")
def _(env):
    fx, mid, _, total = _many(env)
    fx.store.build_view(mid, {"source_id": mid, "filters": [], **SORTED})
    return _build(fx, mid, SORTED, total, keep=True)


@benchmark("merges/one_table.search_scan", note="no search index: the parallel scan")
def _(env):
    fx = env.case()
    return _build(fx, fx.nofts, SEARCH, fx.rows)


@benchmark("merges/many.search_scan",
           note="each member under the parallel-scan threshold; scanned in one pool")
def _(env):
    fx, mid, _, total = _many(env)
    return _build(fx, mid, SEARCH, total)


@benchmark("merges/one_table.filter_equals", note="through the column index")
def _(env):
    fx = env.case()
    ensure_column_index(fx, fx.nofts, EQ_COLUMN)
    return _build(fx, fx.nofts, EQUALS, fx.rows)


@benchmark("merges/many.filter_equals", note="one index search per member")
def _(env):
    fx, mid, ids, total = _many(env)
    for sid in ids:
        ensure_column_index(fx, sid, EQ_COLUMN)
    return _build(fx, mid, EQUALS, total)


@benchmark("merges/one_table.fetch_rows", note="a deep page of the sorted view")
def _(env):
    fx = env.case()
    v = fx.store.build_view(fx.nofts, {"source_id": fx.nofts, "filters": [], **SORTED})
    start = v["row_count"] // 2
    return Timed(lambda: fx.store.fetch_rows_json(v["view_id"], start, 200), items=200)


@benchmark("merges/many.fetch_rows", note="the page's rows resolved per member")
def _(env):
    fx, mid, _, _ = _many(env)
    v = fx.store.build_view(mid, {"source_id": mid, "filters": [], **SORTED})
    start = v["row_count"] // 2
    return Timed(lambda: fx.store.fetch_rows_json(v["view_id"], start, 200), items=200)


@benchmark("merges/one_table.column_values", note="the Values list, through the column index")
def _(env):
    fx = env.case()
    ensure_column_index(fx, fx.nofts, GROUP_COLUMN)
    return Timed(lambda: fx.store.column_values(fx.nofts, GROUP_COLUMN), items=fx.rows)


@benchmark("merges/many.column_values", note="counted per member, then summed")
def _(env):
    fx, mid, ids, total = _many(env)
    for sid in ids:
        ensure_column_index(fx, sid, GROUP_COLUMN)
    return Timed(lambda: fx.store.column_values(mid, GROUP_COLUMN), items=total)
