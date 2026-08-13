"""Export and session round-trips.

`export_view_csv` returns a generator; the benchmark drains it, because the
route streams it and a benchmark that only built the generator would measure
nothing at all (the function body doesn't even run until first iteration).
Every cell goes through `_csv_safe`'s formula-injection prefixing, which is
per-cell Python — the reason CSV export is the slowest read path here.
"""

from __future__ import annotations

from ..harness import Timed, benchmark


def _view(fx, source_id: int, **kw):
    spec = {"source_id": source_id, "filters": [], "sort": []}
    spec.update(kw)
    return fx.store.build_view(source_id, spec)["view_id"]


def _drain(gen_factory):
    def run():
        total = 0
        for chunk in gen_factory():
            total += len(chunk)
        return total

    return run


@benchmark("export/csv.whole_view", reps=3,
           note="drains the streaming generator; _csv_safe runs per cell")
def _(env):
    fx = env.case()
    v = _view(fx, fx.main)
    return Timed(_drain(lambda: fx.store.export_view_csv(v)), items=fx.rows)


@benchmark("export/csv.tagged_only",
           note="same view, ~1% of rows — the 'export my findings' path")
def _(env):
    fx = env.case()
    v = _view(fx, fx.main)
    return Timed(_drain(lambda: fx.store.export_view_csv(v, tagged_only=True)),
                 items=len(fx.tagged_rids))


@benchmark("export/csv.merged_view", reps=3,
           note="rows resolved per member source_id while streaming")
def _(env):
    fx = env.case()
    v = _view(fx, fx.merge_id)
    return Timed(_drain(lambda: fx.store.export_view_csv(v)),
                 items=3 * (fx.rows // 4))


@benchmark("export/xlsx.tagged",
           note="openpyxl, one worksheet per source with tagged rows")
def _(env):
    fx = env.case()
    return Timed(lambda: fx.store.export_tagged_xlsx(),
                 items=len(fx.tagged_rids))


@benchmark("export/session.export",
           note="tags/notes/layout for one source, as portable JSON")
def _(env):
    fx = env.case()
    return Timed(lambda: fx.store.export_session(fx.main),
                 items=len(fx.tagged_rids))


@benchmark("export/session.import",
           note="remaps tag ids by name, creating any that are missing")
def _(env):
    fx = env.case()
    session = fx.store.export_session(fx.main)
    return Timed(lambda: fx.store.import_session(fx.main, session, merge=True),
                 items=len(fx.tagged_rids))


@benchmark("export/session.export_case",
           note="every source in the case at once")
def _(env):
    fx = env.case()
    return Timed(lambda: fx.store.export_case_session())
