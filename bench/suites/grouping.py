"""group_summary and expand_group.

Two things here are worth a benchmark each rather than one average:

* the **fast path** — when `_grouping_covers_whole_source` proves the view
  holds every row, the view join is skipped and the member table is
  aggregated directly (measured 56ms -> 30ms on 120k rows). `covers_source`
  and `via_view_join` are the same aggregation with and without that proof,
  so the gap between them is the optimisation's actual value.
* **cardinality** — a column with 8 distinct values and one distinct per row
  are different query plans (the second builds a real temp b-tree and then
  throws away all but `limit` groups).
"""

from __future__ import annotations

from ..fixtures import (DATETIME_COLUMN, EQ_COLUMN, EQ_VALUE,
                        GROUP_COLUMN, GROUP_HIGH_CARD_COLUMN)
from ..harness import Timed, benchmark


def _view(fx, source_id: int, **kw):
    spec = {"source_id": source_id, "filters": [], "sort": []}
    spec.update(kw)
    return fx.store.build_view(source_id, spec)["view_id"]


@benchmark("grouping/summary.covers_source",
           note="unfiltered view — takes the direct-aggregate fast path")
def _(env):
    fx = env.case()
    v = _view(fx, fx.main)
    return Timed(lambda: fx.store.group_summary(v, GROUP_COLUMN), items=fx.rows)


@benchmark("grouping/summary.via_view_join",
           note="filtered view — must join through v.view_N, where a column "
                "index provably can't help (verified with EXPLAIN QUERY PLAN)")
def _(env):
    fx = env.case()
    # ~99% of rows, deliberately: the fast path's proof is a row-count
    # comparison, so *any* filter matching every row takes the fast path and
    # this benchmark would silently become a duplicate of the one above. It
    # has to exclude a reliably non-zero number of rows while staying close
    # enough in size that the two are worth reading side by side.
    v = _view(fx, fx.main, filters=[{"column": "Bytes", "op": ">", "value": "9000"}])
    return Timed(lambda: fx.store.group_summary(v, GROUP_COLUMN), items=fx.rows)


@benchmark("grouping/summary.high_cardinality",
           note="one distinct value per row, capped at `limit` groups")
def _(env):
    fx = env.case()
    v = _view(fx, fx.main)
    return Timed(lambda: fx.store.group_summary(v, GROUP_HIGH_CARD_COLUMN),
                 items=fx.rows)


@benchmark("grouping/summary.datetime_day_bucket",
           note="DAY_BUCKET(): a Python SQL function called once per row")
def _(env):
    fx = env.case()
    v = _view(fx, fx.main)
    return Timed(lambda: fx.store.group_summary(v, DATETIME_COLUMN), items=fx.rows)


@benchmark("grouping/summary.merged",
           note="aggregated per member, unioned, then re-summed")
def _(env):
    fx = env.case()
    v = _view(fx, fx.merge_id)
    return Timed(lambda: fx.store.group_summary(v, GROUP_COLUMN),
                 items=3 * (fx.rows // 4))


@benchmark("grouping/summary.sorted_by_value")
def _(env):
    fx = env.case()
    v = _view(fx, fx.main)
    return Timed(lambda: fx.store.group_summary(v, GROUP_COLUMN, order="value"),
                 items=fx.rows)


@benchmark("grouping/expand_group.large",
           note="a group holding ~1/8 of the view — gets a real indexed sub-view")
def _(env):
    fx = env.case()
    v = _view(fx, fx.main)
    held: dict = {}

    def run():
        held["g"] = fx.store.expand_group(v, GROUP_COLUMN, "svchost.exe")

    return Timed(run, items=fx.rows // 8)


@benchmark("grouping/expand_group.nested",
           note="second level of a nested grouping — `path` narrows first")
def _(env):
    fx = env.case()
    v = _view(fx, fx.main)
    path = [{"column": GROUP_COLUMN, "value": "svchost.exe"}]
    return Timed(lambda: fx.store.group_summary(v, EQ_COLUMN, path=path),
                 items=fx.rows // 8)


@benchmark("grouping/expand_group.nested_expand")
def _(env):
    fx = env.case()
    v = _view(fx, fx.main)
    path = [{"column": GROUP_COLUMN, "value": "svchost.exe"}]
    return Timed(lambda: fx.store.expand_group(v, EQ_COLUMN, EQ_VALUE, path=path))
