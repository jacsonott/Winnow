"""The SQL pane, and the small per-source reads the UI makes constantly.

The `meta` group is short calls, but they're on the path of nearly every
interaction: `get_source` in particular runs a COUNT(DISTINCT rid) over
row_tags and is called by almost every other Store method, which is exactly
why `group_summary`'s first cut of the direct-aggregate optimisation came
out break-even — the extra lookups ate the whole saving. Anything that makes
these slower makes everything slower, diffusely enough that it wouldn't show
up anywhere else.
"""

from __future__ import annotations

from ..fixtures import EQ_COLUMN, GROUP_COLUMN
from ..harness import Timed, benchmark


@benchmark("sql/run_sql.limited_select",
           note="the pane's common case — a bounded SELECT")
def _(env):
    fx = env.case()
    table = fx.store.get_source(fx.main)["table_name"]
    sql = f'SELECT * FROM "{table}" LIMIT 5000'
    return Timed(lambda: fx.store.run_sql(sql), items=5000)


@benchmark("sql/run_sql.aggregate_scan",
           note="a full-table GROUP BY on its own read-only connection")
def _(env):
    fx = env.case()
    table = fx.store.get_source(fx.main)["table_name"]
    sql = f'SELECT "{GROUP_COLUMN}", count(*) FROM "{table}" GROUP BY 1'
    return Timed(lambda: fx.store.run_sql(sql), items=fx.rows)


@benchmark("sql/run_sql.join_two_sources",
           note="the join that makes the SQL pane worth having")
def _(env):
    fx = env.case()
    a = fx.store.get_source(fx.main)["table_name"]
    b = fx.store.get_source(fx.members[0])["table_name"]
    sql = (f'SELECT a."{EQ_COLUMN}", count(*) FROM "{a}" a '
           f'JOIN "{b}" b ON a."{EQ_COLUMN}" = b."{EQ_COLUMN}" '
           f'WHERE a.rid < 2000 AND b.rid < 2000 GROUP BY 1')
    return Timed(lambda: fx.store.run_sql(sql))


@benchmark("sql/sql_tabs.list")
def _(env):
    fx = env.case()
    fx.store.create_sql_tab("bench", "SELECT 1")
    return Timed(lambda: fx.store.list_sql_tabs())


@benchmark("meta/get_source",
           note="COUNT(DISTINCT rid) over row_tags; called by nearly "
                "every other Store method")
def _(env):
    fx = env.case()
    return Timed(lambda: fx.store.get_source(fx.main))


@benchmark("meta/get_source.merge")
def _(env):
    fx = env.case()
    return Timed(lambda: fx.store.get_source(fx.merge_id))


@benchmark("meta/list_sources",
           note="the sidebar's data — every source, open or closed")
def _(env):
    fx = env.case()
    return Timed(lambda: fx.store.list_sources())


@benchmark("meta/column_values",
           note="the filter box's value picker — the shape a plain "
                "single-column index is best at")
def _(env):
    fx = env.case()
    return Timed(lambda: fx.store.column_values(fx.main, GROUP_COLUMN, 200))


@benchmark("meta/column_values.high_cardinality")
def _(env):
    fx = env.case()
    return Timed(lambda: fx.store.column_values(fx.main, "CommandLine", 200))


@benchmark("meta/column_max_lengths",
           note="column auto-sizing on open; cached per source after the first")
def _(env):
    fx = env.case()

    def run():
        fx.store._maxlen_cache.pop(fx.main, None)
        fx.store.column_max_lengths(fx.main)

    return Timed(run, items=fx.rows)


@benchmark("meta/list_column_indexes",
           note="hashes each known column and probes for that index name")
def _(env):
    fx = env.case()
    return Timed(lambda: fx.store.list_column_indexes(fx.main))
