"""View materialisation — `build_view`, the one-shot pass that CLAUDE.md's
invariant #2 trades for O(1) paging afterwards.

It's the operation an analyst waits on every time they change a filter or a
sort, so it's the most user-visible number in the suite. Each filter shape
gets its own benchmark rather than one composite: they exercise genuinely
different query plans (index scan, LIKE scan, functional expression, FTS
subquery), and a change that helps one can easily hurt another.

Selectivity is fixed by the generator (see fixtures.py), so the row count
each filter matches is identical run to run.
"""

from __future__ import annotations

from ..fixtures import (DATETIME_COLUMN, EQ_COLUMN, EQ_VALUE, TERM_COMMON,
                        ensure_column_index, time_window)
from ..harness import Timed, benchmark


def _spec(source_id: int, **kw) -> dict:
    spec = {"source_id": source_id, "filters": [], "sort": []}
    spec.update(kw)
    return spec


def _build(fx, source_id: int, spec: dict, items: int) -> Timed:
    """Time build_view, then close the view in the untimed teardown — an
    accumulating pile of v.view_N tables would slowly change what's being
    measured."""
    held: dict = {}

    def run():
        held["v"] = fx.store.build_view(source_id, spec)

    def after():
        v = held.pop("v", None)
        if v:
            fx.store.close_view(v["view_id"])

    return Timed(run, after=after, items=items)


@benchmark("views/build.unfiltered")
def _(env):
    fx = env.case()
    return _build(fx, fx.main, _spec(fx.main), fx.rows)


@benchmark("views/build.sort_text")
def _(env):
    fx = env.case()
    spec = _spec(fx.main, sort=[{"column": "Process", "dir": "asc"}])
    return _build(fx, fx.main, spec, fx.rows)


@benchmark("views/build.sort_numeric",
           note="_numeric_expr's gated CAST, not a bare CAST AS REAL")
def _(env):
    fx = env.case()
    spec = _spec(fx.main, sort=[{"column": "Bytes", "dir": "desc"}])
    return _build(fx, fx.main, spec, fx.rows)


@benchmark("views/build.sort_multi")
def _(env):
    fx = env.case()
    spec = _spec(fx.main, sort=[{"column": "Process", "dir": "asc"},
                                {"column": DATETIME_COLUMN, "dir": "desc"}])
    return _build(fx, fx.main, spec, fx.rows)


@benchmark("views/filter.equals_unindexed",
           note="sargable filter with no column index yet — a full scan")
def _(env):
    fx = env.case()
    spec = _spec(fx.nofts, filters=[{"column": EQ_COLUMN, "op": "equals",
                                     "value": EQ_VALUE}])
    return _build(fx, fx.nofts, spec, fx.rows)


@benchmark("views/filter.equals_indexed",
           note="same filter, same row count, with the lazy B-tree index built")
def _(env):
    fx = env.case()
    ensure_column_index(fx, fx.main, EQ_COLUMN)
    spec = _spec(fx.main, filters=[{"column": EQ_COLUMN, "op": "equals",
                                    "value": EQ_VALUE}])
    return _build(fx, fx.main, spec, fx.rows)


@benchmark("views/filter.in_list")
def _(env):
    fx = env.case()
    spec = _spec(fx.main, filters=[{"column": "Process", "op": "in",
                                    "value": "cmd.exe\npowershell.exe"}])
    return _build(fx, fx.main, spec, fx.rows)


@benchmark("views/filter.contains_column",
           note="per-column LIKE — no index can help this one, by design")
def _(env):
    fx = env.case()
    spec = _spec(fx.main, filters=[{"column": "CommandLine", "op": "contains",
                                    "value": TERM_COMMON}])
    return _build(fx, fx.main, spec, fx.rows)


@benchmark("views/filter.numeric_gt")
def _(env):
    fx = env.case()
    spec = _spec(fx.main, filters=[{"column": "Bytes", "op": ">", "value": "450000"}])
    return _build(fx, fx.main, spec, fx.rows)


@benchmark("views/filter.regex_column")
def _(env):
    fx = env.case()
    spec = _spec(fx.main, filters=[{"column": "User", "op": "regex",
                                    "value": r"^ACME.(admin|jsmith)$"}])
    return _build(fx, fx.main, spec, fx.rows)


@benchmark("views/filter.time_range_all_columns",
           note="column=None: every datetime column OR'd, via TS_NORMALIZE")
def _(env):
    fx = env.case()
    start, end = time_window(fx.rows)
    spec = _spec(fx.main, time_range={"enabled": True, "start": start,
                                      "end": end, "column": None})
    return _build(fx, fx.main, spec, fx.rows)


@benchmark("views/filter.time_range_one_column")
def _(env):
    fx = env.case()
    start, end = time_window(fx.rows)
    spec = _spec(fx.main, time_range={"enabled": True, "start": start,
                                      "end": end, "column": DATETIME_COLUMN})
    return _build(fx, fx.main, spec, fx.rows)


@benchmark("views/filter.tag_any")
def _(env):
    fx = env.case()
    spec = _spec(fx.main, tags=["__any__"])
    return _build(fx, fx.main, spec, fx.rows)


@benchmark("views/filter.tree_nested",
           note="guided filter builder: a 2-level group/cond tree")
def _(env):
    fx = env.case()
    tree = {"type": "group", "op": "AND", "children": [
        {"type": "cond", "column": "Channel", "op": "equals", "value": "Sysmon"},
        {"type": "group", "op": "OR", "children": [
            {"type": "cond", "column": "Process", "op": "equals", "value": "cmd.exe"},
            {"type": "cond", "column": "User", "op": "contains", "value": "admin"},
        ]},
    ]}
    return _build(fx, fx.main, _spec(fx.main, filter_tree=tree), fx.rows)


@benchmark("views/filter.raw_sql_fragment")
def _(env):
    fx = env.case()
    tree = {"type": "group", "op": "AND", "children": [
        {"type": "raw", "sql": '"Bytes" > 100000 AND "Channel" IS NOT NULL'},
    ]}
    return _build(fx, fx.main, _spec(fx.main, filter_tree=tree), fx.rows)


@benchmark("views/merge.unfiltered",
           note="3 members UNION ALL'd into one view")
def _(env):
    fx = env.case()
    total = 3 * (fx.rows // 4)
    return _build(fx, fx.merge_id, _spec(fx.merge_id), total)


@benchmark("views/merge.filter_equals",
           note="the filter is compiled and scanned once per member, serially")
def _(env):
    fx = env.case()
    total = 3 * (fx.rows // 4)
    spec = _spec(fx.merge_id, filters=[{"column": EQ_COLUMN, "op": "equals",
                                        "value": EQ_VALUE}])
    return _build(fx, fx.merge_id, spec, total)


@benchmark("views/merge.sorted",
           note="sort across members — the UNION has to be ordered as a whole")
def _(env):
    fx = env.case()
    total = 3 * (fx.rows // 4)
    spec = _spec(fx.merge_id, sort=[{"column": DATETIME_COLUMN, "dir": "asc"}])
    return _build(fx, fx.merge_id, spec, total)
