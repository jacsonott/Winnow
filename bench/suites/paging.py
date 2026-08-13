"""Paging a materialised view — invariant #2's payoff.

The promise these benchmarks exist to defend: fetching a window costs the
same whether it's at row 0 or row 1,199,800. `head` and `deep` measure the
same call at opposite ends of the same view, so a regression that
reintroduces LIMIT/OFFSET behaviour shows up as the two numbers diverging,
not just as both getting slower.
"""

from __future__ import annotations

from ..fixtures import DATETIME_COLUMN
from ..harness import Timed, benchmark

WINDOW = 200        # what the grid actually asks for on a scroll
WIDE_WINDOW = 2000  # api_rows' own cap


def _view(fx, source_id: int, **kw):
    spec = {"source_id": source_id, "filters": [], "sort": []}
    spec.update(kw)
    return fx.store.build_view(source_id, spec)


@benchmark("paging/fetch_rows.head")
def _(env):
    fx = env.case()
    v = _view(fx, fx.main)["view_id"]
    return Timed(lambda: fx.store.fetch_rows(v, 0, WINDOW), items=WINDOW)


@benchmark("paging/fetch_rows.deep",
           note="same call at the far end of the view — must match .head")
def _(env):
    fx = env.case()
    view = _view(fx, fx.main)
    v, total = view["view_id"], view["row_count"]
    start = max(0, total - WINDOW - 1)
    return Timed(lambda: fx.store.fetch_rows(v, start, WINDOW), items=WINDOW)


@benchmark("paging/fetch_rows.deep_sorted",
           note="paging a sorted view — the sort is materialised, not re-run")
def _(env):
    fx = env.case()
    view = _view(fx, fx.main, sort=[{"column": "Bytes", "dir": "desc"}])
    v, total = view["view_id"], view["row_count"]
    start = max(0, total - WINDOW - 1)
    return Timed(lambda: fx.store.fetch_rows(v, start, WINDOW), items=WINDOW)


@benchmark("paging/fetch_rows.wide_window",
           note="a 2000-row window — the copy/export path's fetch size")
def _(env):
    fx = env.case()
    v = _view(fx, fx.main)["view_id"]
    start = max(0, fx.rows // 2)
    return Timed(lambda: fx.store.fetch_rows(v, start, WIDE_WINDOW), items=WIDE_WINDOW)


@benchmark("paging/fetch_rows.all_tagged",
           note="every row in the window carries a tag — worst case for the "
                "per-row tag/note resolution")
def _(env):
    fx = env.case()
    v = _view(fx, fx.main, tags=["__any__"])["view_id"]
    return Timed(lambda: fx.store.fetch_rows(v, 0, WINDOW), items=WINDOW)


@benchmark("paging/fetch_rows.merged_deep",
           note="rows resolved per member source_id, not one constant")
def _(env):
    fx = env.case()
    view = _view(fx, fx.merge_id, sort=[{"column": DATETIME_COLUMN, "dir": "asc"}])
    v, total = view["view_id"], view["row_count"]
    start = max(0, total - WINDOW - 1)
    return Timed(lambda: fx.store.fetch_rows(v, start, WINDOW), items=WINDOW)


@benchmark("paging/tag_positions",
           note="every tagged position in the view, for the scrollbar marks")
def _(env):
    fx = env.case()
    v = _view(fx, fx.main)["view_id"]
    return Timed(lambda: fx.store.tag_positions(v), items=len(fx.tagged_rids))


@benchmark("paging/find_position",
           note="'jump to this row' — a lookup into a materialised view")
def _(env):
    fx = env.case()
    v = _view(fx, fx.main, sort=[{"column": "Bytes", "dir": "desc"}])["view_id"]
    rid = max(1, fx.rows - 7)
    return Timed(lambda: fx.store.find_position(v, fx.main, rid))
