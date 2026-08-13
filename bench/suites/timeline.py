"""The unified Timeline tab.

`build_timeline` unions every tagged row across every real source in the
case, normalising timestamps through TS_NORMALIZE so tables with different
timestamp formats sort against each other. It's rebuilt from scratch on
every tag-filter change, so its build cost is felt directly — and it grows
with how much of the case has been triaged, not with how big the case is.
"""

from __future__ import annotations

from ..fixtures import DATETIME_COLUMN
from ..harness import Timed, benchmark


def _configs(fx) -> dict:
    """What server.py's `_resolve_timeline_configs` would hand in after
    matching each source's header set against a saved template."""
    return {
        sid: {"timestamp_column": DATETIME_COLUMN,
              "body_columns": ["Process", "User", "CommandLine"],
              "type_label": f"src{sid}"}
        for sid in [fx.main, fx.nofts, *fx.members]
    }


def _timeline(fx, **kw) -> Timed:
    held: dict = {}

    def run():
        held["t"] = fx.store.build_timeline(**kw)

    return Timed(run)


@benchmark("timeline/build.all_tags",
           note="every tagged row in the case, configured bodies")
def _(env):
    fx = env.case()
    return _timeline(fx, configs=_configs(fx))


@benchmark("timeline/build.no_configs",
           note="the fallback path — first datetime column, every column, "
                "the source's own file name")
def _(env):
    fx = env.case()
    return _timeline(fx, configs=None)


@benchmark("timeline/build.one_tag",
           note="narrowed to a single tag id")
def _(env):
    fx = env.case()
    return _timeline(fx, configs=_configs(fx), tag_ids=[fx.tag_id])


@benchmark("timeline/fetch_rows",
           note="paging the timeline view — same pos-based paging as the grid")
def _(env):
    fx = env.case()
    t = fx.store.build_timeline(_configs(fx))
    start = max(0, t["row_count"] - 201)
    return Timed(lambda: fx.store.fetch_timeline_rows(t["view_id"], start, 200),
                 items=200)
