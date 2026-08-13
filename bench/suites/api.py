"""The HTTP layer itself, through FastAPI's TestClient.

Every one of these has a Store-level twin elsewhere in the suite. That
pairing is the point: the difference between `api/rows.page` and
`paging/fetch_rows.head` is what routing, pydantic validation and JSON
serialisation cost, and nothing else in the suite would show a regression
that lives there. `/api/rows` in particular is issued on every scroll, so
its serialisation cost is felt continuously rather than once.
"""

from __future__ import annotations

from ..fixtures import EQ_COLUMN, EQ_VALUE, GROUP_COLUMN, TERM_RARE
from ..harness import Timed, benchmark

WINDOW = 200


def _view_id(env, **kw) -> str:
    fx = env.case()
    spec = {"source_id": fx.main, "filters": [], "sort": []}
    spec.update(kw)
    return env.client().post("/api/view", json=spec).json()["view_id"]


@benchmark("api/sources", note="polled on every tab/sidebar refresh")
def _(env):
    c = env.client()
    return Timed(lambda: c.get("/api/sources"))


@benchmark("api/view.build_filtered")
def _(env):
    fx = env.case()
    c = env.client()
    spec = {"source_id": fx.main,
            "filters": [{"column": EQ_COLUMN, "op": "equals", "value": EQ_VALUE}],
            "sort": []}
    held: dict = {}

    def run():
        held["r"] = c.post("/api/view", json=spec).json()

    def after():
        r = held.pop("r", None)
        if r and "view_id" in r:
            c.delete(f"/api/view/{r['view_id']}")

    return Timed(run, after=after, items=fx.rows)


@benchmark("api/rows.page",
           note="issued on every scroll; compare against paging/fetch_rows.head "
                "to isolate serialisation cost")
def _(env):
    v = _view_id(env)
    c = env.client()
    return Timed(lambda: c.get("/api/rows", params={"view_id": v, "start": 0,
                                                    "count": WINDOW}),
                 items=WINDOW)


@benchmark("api/rows.page_deep")
def _(env):
    fx = env.case()
    v = _view_id(env)
    c = env.client()
    start = max(0, fx.rows - WINDOW - 1)
    return Timed(lambda: c.get("/api/rows", params={"view_id": v, "start": start,
                                                    "count": WINDOW}),
                 items=WINDOW)


@benchmark("api/rows.wide_window", note="the 2000-row cap api_rows enforces")
def _(env):
    v = _view_id(env)
    c = env.client()
    return Timed(lambda: c.get("/api/rows", params={"view_id": v, "start": 0,
                                                    "count": 2000}),
                 items=2000)


@benchmark("api/group_summary")
def _(env):
    v = _view_id(env)
    c = env.client()
    return Timed(lambda: c.get("/api/group_summary",
                               params={"view_id": v, "column": GROUP_COLUMN}))


@benchmark("api/column_values")
def _(env):
    fx = env.case()
    c = env.client()
    return Timed(lambda: c.get("/api/column_values",
                               params={"source_id": fx.main,
                                       "column": GROUP_COLUMN}))


@benchmark("api/search_all.start",
           note="returns immediately; the sweep runs on a background thread")
def _(env):
    fx = env.case()
    c = env.client()

    def run():
        c.post("/api/search_all/start", json={"query": TERM_RARE})

    def after():
        fx.store.cancel_search_all_job()
        fx.store.wait_for_search_all_job(timeout=120)

    return Timed(run, after=after)


@benchmark("api/row_tags.view",
           note="the bulk tag endpoint behind a select-all + hotkey")
def _(env):
    fx = env.case()
    v = _view_id(env, filters=[{"column": EQ_COLUMN, "op": "equals",
                                "value": EQ_VALUE}])
    c = env.client()

    def run():
        c.post("/api/row_tags/view",
               json={"view_id": v, "tag_id": fx.bench_tag_id, "on": True})

    def after():
        c.post("/api/row_tags/view",
               json={"view_id": v, "tag_id": fx.bench_tag_id, "on": False})

    return Timed(run, after=after, items=fx.rows // 6)
