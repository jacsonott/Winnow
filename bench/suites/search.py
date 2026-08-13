"""Substring search — the trigram FTS index and its LIKE fallback.

CLAUDE.md's `detail=none` note is the reason this suite is shaped the way it
is: dropping per-occurrence position lists cut the index by 84%, at the cost
of verifying candidates against the real text, which makes **query time
scale with result count**. So a single "search benchmark" would be
meaningless — a rare IOC and a term matching every row are opposite ends of
that tradeoff. All three shapes (rare / matches-everything / miss) are
measured separately, and any future index change has to be read across all
three at once plus `footprint/fts_index`.

`*_fallback` runs the identical term against a same-sized source with no
index built, which is what keeps the index's actual value visible as a
number rather than an assumption.
"""

from __future__ import annotations

from ..fixtures import (TERM_COMMON, TERM_EXCLUDE, TERM_MISS, TERM_RARE)
from ..harness import Timed, benchmark


def _search(fx, source_id: int, term: str, *, mode: str = "contains",
            terms: list[dict] | None = None) -> Timed:
    spec = {"source_id": source_id, "filters": [], "sort": [],
            "search": term, "search_mode": mode}
    if terms is not None:
        spec["search_terms"] = terms
    held: dict = {}

    def run():
        held["v"] = fx.store.build_view(source_id, spec)

    def after():
        v = held.pop("v", None)
        if v:
            fx.store.close_view(v["view_id"])

    return Timed(run, after=after, items=fx.rows)


@benchmark("search/contains.rare_fts", note="~0.1% of rows — the IOC case")
def _(env):
    fx = env.case()
    return _search(fx, fx.main, TERM_RARE)


@benchmark("search/contains.common_fts",
           note="matches every row — detail=none's worst case, candidate "
                "verification dominates")
def _(env):
    fx = env.case()
    return _search(fx, fx.main, TERM_COMMON)


@benchmark("search/contains.miss_fts", note="matches nothing")
def _(env):
    fx = env.case()
    return _search(fx, fx.main, TERM_MISS)


@benchmark("search/contains.rare_fallback",
           note="same term, same row count, no index — the blob LIKE scan")
def _(env):
    fx = env.case()
    return _search(fx, fx.nofts, TERM_RARE)


@benchmark("search/contains.common_fallback")
def _(env):
    fx = env.case()
    return _search(fx, fx.nofts, TERM_COMMON)


@benchmark("search/contains.short_term_fallback",
           note="under TRIGRAM_MIN_LEN, so it can't use the index either way")
def _(env):
    fx = env.case()
    return _search(fx, fx.main, "4c")


@benchmark("search/contains.wildcard_term_fallback",
           note="contains a LIKE wildcard, so _fts_like_pattern refuses the "
                "unescaped pushdown; measures the escaped blob-LIKE route "
                "(the wildcard is escaped, so this matches literally)")
def _(env):
    fx = env.case()
    return _search(fx, fx.main, "netsvcs_id")


@benchmark("search/advanced.include_exclude_fts",
           note="one required term AND one NOT term")
def _(env):
    fx = env.case()
    return _search(fx, fx.main, "", mode="advanced", terms=[
        {"term": TERM_RARE},
        {"term": TERM_EXCLUDE, "exclude": True, "connector": "AND"},
    ])


@benchmark("search/advanced.include_exclude_fallback")
def _(env):
    fx = env.case()
    return _search(fx, fx.nofts, "", mode="advanced", terms=[
        {"term": TERM_RARE},
        {"term": TERM_EXCLUDE, "exclude": True, "connector": "AND"},
    ])


@benchmark("search/regex.whole_row",
           note="REGEXP over the concatenated row — a Python callback per row")
def _(env):
    fx = env.case()
    return _search(fx, fx.main, r"base64\s+IEX", mode="regex")


@benchmark("search/search_all_sources", min_size="quick",
           note="the sweep behind 'Search all tables' — every source, "
                "counts capped at SEARCH_ALL_COUNT_CAP")
def _(env):
    fx = env.case()
    return Timed(lambda: fx.store.search_all_sources(TERM_RARE))


@benchmark("search/search_all_sources.common",
           note="every source hits the cap immediately — measures how early "
                "the LIMIT cap+1 short-circuits")
def _(env):
    fx = env.case()
    return Timed(lambda: fx.store.search_all_sources(TERM_COMMON))


@benchmark("search/search_all_job.start_latency",
           note="the POST the modal waits on; the sweep itself is backgrounded")
def _(env):
    fx = env.case()
    held: dict = {}

    def run():
        held["job"] = fx.store.start_search_all_job(TERM_RARE)

    def after():
        # Cancel and drain, so the worker for one repetition isn't still
        # scanning (and competing for the connection) during the next.
        fx.store.cancel_search_all_job()
        fx.store.wait_for_search_all_job(timeout=120)

    return Timed(run, after=after)
