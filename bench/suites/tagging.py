"""Tagging — the workflow the whole tool exists for.

Everything here mutates the case, so every benchmark undoes its own work in
the untimed `after` hook. That isn't just tidiness: the fixture's ~1% tagged
baseline is what `paging/fetch_rows.all_tagged`, the timeline suite and the
tagged exports are all measured against, and a benchmark that left a million
extra rows tagged behind it would quietly change every number that ran after
it. A scratch tag (`bench_tag_id`) is used throughout so the baseline tag's
row set is never touched.
"""

from __future__ import annotations

from ..fixtures import EQ_COLUMN, EQ_VALUE
from ..harness import Timed, benchmark


def _view(fx, source_id: int, **kw):
    spec = {"source_id": source_id, "filters": [], "sort": []}
    spec.update(kw)
    return fx.store.build_view(source_id, spec)["view_id"]


@benchmark("tagging/set_tags.10k_rows",
           note="an explicit rid list — the 'selected some rows' path")
def _(env):
    fx = env.case()
    rids = list(range(1, min(fx.rows, 10_000) + 1))

    def run():
        fx.store.set_tags(fx.main, rids, fx.bench_tag_id, True)

    def after():
        fx.store.set_tags(fx.main, rids, fx.bench_tag_id, False)

    return Timed(run, after=after, items=len(rids))


@benchmark("tagging/tag_view.whole_view",
           note="select-all then hit a hotkey — one server-side set operation, "
                "never a client-side list of positions")
def _(env):
    fx = env.case()
    v = _view(fx, fx.main)

    def run():
        fx.store.tag_view(v, fx.bench_tag_id, True)

    def after():
        fx.store.tag_view(v, fx.bench_tag_id, False)

    return Timed(run, after=after, items=fx.rows)


@benchmark("tagging/tag_view.with_exclusions",
           note="select-all-then-uncheck-a-few: exclusions resolved in SQL so "
                "an excluded row that already had the tag keeps it")
def _(env):
    fx = env.case()
    v = _view(fx, fx.main)
    exclude = [[fx.main, r] for r in range(1, min(fx.rows, 200) + 1)]

    def run():
        fx.store.tag_view(v, fx.bench_tag_id, True, exclude=exclude)

    def after():
        fx.store.tag_view(v, fx.bench_tag_id, False)

    return Timed(run, after=after, items=fx.rows)


@benchmark("tagging/tag_view.filtered",
           note="'filter to it, then mark the lot' — ~1/6 of the source")
def _(env):
    fx = env.case()
    v = _view(fx, fx.main, filters=[{"column": EQ_COLUMN, "op": "equals",
                                     "value": EQ_VALUE}])

    def run():
        fx.store.tag_view(v, fx.bench_tag_id, True)

    def after():
        fx.store.tag_view(v, fx.bench_tag_id, False)

    return Timed(run, after=after, items=fx.rows // 6)


@benchmark("tagging/tag_view.merged",
           note="one INSERT across members, each row carrying its own source_id")
def _(env):
    fx = env.case()
    v = _view(fx, fx.merge_id)

    def run():
        fx.store.tag_view(v, fx.bench_tag_id, True)

    def after():
        fx.store.tag_view(v, fx.bench_tag_id, False)

    return Timed(run, after=after, items=3 * (fx.rows // 4))


@benchmark("tagging/set_tags_pairs",
           note="merged-view selection: each row's own (source_id, rid)")
def _(env):
    fx = env.case()
    per = max(1, min(fx.rows // 4, 3000))
    pairs = [[sid, r] for sid in fx.members for r in range(1, per + 1)]

    def run():
        fx.store.set_tags_pairs(pairs, fx.bench_tag_id, True)

    def after():
        fx.store.set_tags_pairs(pairs, fx.bench_tag_id, False)

    return Timed(run, after=after, items=len(pairs))


@benchmark("tagging/tag_counts",
           note="runs after every tag change, and inside get_source")
def _(env):
    fx = env.case()
    return Timed(lambda: fx.store.tag_counts(fx.main))


@benchmark("tagging/tag_counts.merged")
def _(env):
    fx = env.case()
    return Timed(lambda: fx.store.tag_counts(fx.merge_id))


@benchmark("tagging/set_note")
def _(env):
    fx = env.case()
    counter = {"n": 0}

    def run():
        counter["n"] += 1
        fx.store.set_note(fx.main, 1, f"bench note {counter['n']}")

    def after():
        fx.store.set_note(fx.main, 1, "")

    return Timed(run, after=after)
