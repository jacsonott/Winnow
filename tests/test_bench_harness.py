"""The benchmark harness itself (bench/), not the code it measures.

A perf suite that silently stops measuring correctly is worse than no perf
suite — it reports "no change" forever and everyone believes it. These tests
cover the parts that could rot without anyone noticing: that setup and
teardown really are excluded from the timing, that the significance rules
suppress noise without suppressing real regressions, and that the generated
fixtures are actually deterministic (a filter whose selectivity drifted
between runs would report regressions that aren't).

Deliberately fast: the row counts here are tiny, since none of this is
measuring performance — it's checking the machinery that does.
"""

from __future__ import annotations

import json
import time

import pytest

from bench import compare as C
from bench.harness import REGISTRY, Timed, _measure_once, benchmark, run_benchmark


# --------------------------------------------------------------------------
# measurement


class _FakeEnv:
    """run_benchmark only needs the metric sink."""

    def __init__(self):
        self._metrics = {}


def test_before_and_after_hooks_are_not_timed():
    # The whole reason a benchmark returns a callable instead of just being
    # one: an ingest benchmark's fresh-database setup must not land inside
    # the number it reports.
    calls = []
    spec = Timed(
        run=lambda: (calls.append("run"), time.sleep(0.02)),
        before=lambda: (calls.append("before"), time.sleep(0.05)),
        after=lambda: (calls.append("after"), time.sleep(0.05)),
    )
    elapsed = _measure_once(spec)
    assert calls == ["before", "run", "after"]
    assert 0.015 < elapsed < 0.045  # the run(), not the 120ms of hooks


def test_run_benchmark_records_stats_and_samples():
    def bench_fn(env):
        return Timed(lambda: time.sleep(0.001), reps=5, warmup=1)

    bd = type("BD", (), {"name": "t/x", "group": "t", "fn": staticmethod(bench_fn),
                         "reps": None, "note": ""})()
    rec = run_benchmark(bd, _FakeEnv())
    assert rec["reps"] == 5
    assert len(rec["samples"]) == 5
    assert rec["seconds"]["min"] <= rec["seconds"]["median"] <= rec["seconds"]["max"]
    assert "error" not in rec


def test_a_failing_benchmark_is_recorded_not_raised():
    # One broken benchmark must not take the other 98 down with it.
    def bench_fn(env):
        raise RuntimeError("boom")

    bd = type("BD", (), {"name": "t/x", "group": "t", "fn": staticmethod(bench_fn),
                         "reps": None, "note": ""})()
    rec = run_benchmark(bd, _FakeEnv())
    assert rec["error"] == "RuntimeError: boom"
    assert "seconds" not in rec


def test_metrics_only_benchmark_has_no_timing():
    def bench_fn(env):
        env.metric = lambda *a, **k: None  # not used here
        return None

    bd = type("BD", (), {"name": "t/x", "group": "t", "fn": staticmethod(bench_fn),
                         "reps": None, "note": ""})()
    rec = run_benchmark(bd, _FakeEnv())
    assert "seconds" not in rec and "error" not in rec


# --------------------------------------------------------------------------
# significance rules — the part that decides what gets called a regression


def _run(name: str, median: float, stdev: float = 0.0) -> dict:
    return {"environment": {}, "results": [{
        "name": name, "group": "g", "reps": 5,
        "seconds": {"min": median, "median": median, "mean": median,
                    "stdev": stdev, "max": median},
    }]}


def _verdict(base_s: float, cur_s: float, stdev: float = 0.0, **kw) -> str:
    deltas = C.compare_runs(_run("g/x", base_s, stdev), _run("g/x", cur_s, stdev), **kw)
    return deltas[0].verdict


def test_real_regression_is_flagged():
    assert _verdict(0.010, 0.020) == C.REGRESSION


def test_real_improvement_is_flagged():
    assert _verdict(0.020, 0.010) == C.IMPROVEMENT


def test_change_under_the_percent_threshold_is_same():
    assert _verdict(0.100, 0.104) == C.SAME  # +4%, under the 7% bar


def test_large_percent_on_a_tiny_absolute_change_is_same():
    # 176us -> 215us is +22%, and means nothing. Without this rule the
    # report fills up with microbenchmark jitter and stops being read.
    assert _verdict(0.000176, 0.000215) == C.SAME


def test_change_inside_the_noise_band_is_same():
    # +40% on paper, but both runs swing by +/-30% between repetitions.
    assert _verdict(0.010, 0.014, stdev=0.004) == C.SAME


def test_same_change_outside_the_noise_band_is_flagged():
    assert _verdict(0.010, 0.014, stdev=0.0001) == C.REGRESSION


def test_thresholds_are_configurable():
    assert _verdict(0.100, 0.104, threshold_pct=2.0) == C.REGRESSION


def test_new_and_missing_benchmarks_are_reported_not_dropped():
    # A benchmark that vanishes is how a regression hides.
    base = _run("g/gone", 0.01)
    cur = _run("g/added", 0.01)
    verdicts = {d.name: d.verdict for d in C.compare_runs(base, cur)}
    assert verdicts == {"g/added": C.NEW, "g/gone": C.MISSING}


def test_errored_benchmark_surfaces_as_error():
    cur = {"environment": {}, "results": [
        {"name": "g/x", "group": "g", "error": "KeyError: nope"}]}
    d = C.compare_runs(_run("g/x", 0.01), cur)[0]
    assert d.verdict == C.ERROR and "KeyError" in d.detail


def test_metric_direction_is_respected():
    # A byte count going up is a regression; a rows/s figure going up isn't.
    def one(value, higher_is_better):
        return {"environment": {}, "results": [{
            "name": "g/x", "group": "g",
            "metrics": {"m": {"value": value, "unit": "bytes",
                              "higher_is_better": higher_is_better, "floor": 0}},
        }]}

    lower = C.compare_runs(one(100, False), one(200, False))
    assert [d.verdict for d in lower] == [C.REGRESSION]
    higher = C.compare_runs(one(100, True), one(200, True))
    assert [d.verdict for d in higher] == [C.IMPROVEMENT]


def test_metric_floor_suppresses_trivial_size_changes():
    def one(value):
        return {"environment": {}, "results": [{
            "name": "g/x", "group": "g",
            "metrics": {"m": {"value": value, "unit": "bytes",
                              "higher_is_better": False, "floor": 65536}},
        }]}

    assert [d.verdict for d in C.compare_runs(one(100_000), one(120_000))] == [C.SAME]


def test_environment_mismatch_is_surfaced():
    base = {"host": "a", "cpu": "x", "python": "3.12", "sqlite": "3.45", "size": "quick"}
    cur = dict(base, host="b", size="large")
    lines = C.render_env_warning(base, cur, C.C(False))
    assert lines and any("host" in ln for ln in lines)
    assert any("size" in ln for ln in lines)
    assert C.render_env_warning(base, dict(base), C.C(False)) == []


# --------------------------------------------------------------------------
# fixtures and registry


@pytest.fixture
def bench_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("BENCH_CACHE_DIR", str(tmp_path / "cache"))


def test_generated_csv_is_deterministic(bench_cache, tmp_path, monkeypatch):
    from bench import fixtures

    first = open(fixtures.events_csv(200), "rb").read()
    monkeypatch.setenv("BENCH_CACHE_DIR", str(tmp_path / "cache2"))
    second = open(fixtures.events_csv(200), "rb").read()
    assert first == second, "fixture data drifted — every filter's selectivity would too"


def test_generated_csv_has_the_selectivity_the_suites_assume(bench_cache):
    # The search suite's three shapes have to actually be those shapes.
    from bench import fixtures

    text = open(fixtures.events_csv(2000), encoding="utf-8").read()
    assert text.count(fixtures.TERM_COMMON) == 2000       # every row
    assert 0 < text.count(fixtures.TERM_RARE) < 100       # the IOC case
    assert text.count(fixtures.TERM_MISS) == 0


def test_benchmark_names_are_unique():
    # compare_runs pairs runs up by name; a duplicate would silently make one
    # of them unreportable.
    from bench.runner import load_suites

    load_suites()
    names = [bd.name for bd in REGISTRY]
    assert len(names) == len(set(names))


def test_every_benchmark_has_a_known_group():
    from bench.runner import GROUP_ORDER, load_suites

    load_suites()
    unknown = {bd.group for bd in REGISTRY} - set(GROUP_ORDER)
    assert not unknown, f"groups missing from GROUP_ORDER (they'd sort last): {unknown}"


def test_end_to_end_run_and_compare(bench_cache, monkeypatch, tmp_path):
    """The whole pipeline on a 400-row fixture: build the case, run a couple
    of real benchmarks, serialise, compare a run against itself."""
    from bench import harness, runner

    monkeypatch.setitem(harness.SIZES, "quick", 400)
    result = runner.run("quick", patterns=["meta/list_sources", "meta/get_source"],
                        reps=2, verbose=False)
    assert result["results"], "nothing ran"
    assert not [r for r in result["results"] if r.get("error")]
    assert all("seconds" in r for r in result["results"])

    # Round-trips through JSON the way --json / --baseline do.
    round_tripped = json.loads(json.dumps(result))
    deltas = C.compare_runs(round_tripped, result)
    assert deltas and all(d.verdict == C.SAME for d in deltas)
