"""CLI for the benchmark suite.

    python3 -m bench                          run + compare against the saved baseline
    python3 -m bench --size quick             smaller fixture, faster run
    python3 -m bench -k search -k paging      only benchmarks matching these
    python3 -m bench --vs-ref HEAD            measure HEAD too, and diff against it
    python3 -m bench --save-baseline          record this run as the baseline
    python3 -m bench --diff a.json b.json     compare two saved runs, run nothing

See bench/README.md for the workflow this is meant to support.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import compare as C
from .compare import (DEFAULT_MIN_ABS_S, DEFAULT_THRESHOLD_PCT, REGRESSION,
                      compare_runs, render_comparison, render_env,
                      render_env_warning, render_run, render_summary)
from .harness import REGISTRY, SIZE_ORDER
from .runner import REPO_ROOT, load_suites, run, run_against_ref, select

BASELINE_DIR = Path(__file__).resolve().parent / "baselines"


def default_baseline_path(size: str) -> Path:
    """Baselines are per (machine, size) — comparing across either is
    meaningless, so they can't share a file and accidentally overwrite
    each other."""
    import socket

    return BASELINE_DIR / f"{socket.gethostname()}-{size}.json"


def _load(path: str | Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _save(path: str | Path, data: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=1)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m bench", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--size", choices=SIZE_ORDER, default="standard",
                   help="fixture row count tier (default: standard)")
    p.add_argument("-k", dest="patterns", action="append", metavar="SUBSTR",
                   help="only run benchmarks whose name contains this (repeatable)")
    p.add_argument("--group", dest="groups", action="append", metavar="GROUP",
                   help="only run this group (repeatable)")
    p.add_argument("--reps", type=int, help="force a fixed repetition count")
    p.add_argument("--list", action="store_true", help="list benchmarks and exit")

    p.add_argument("--baseline", metavar="PATH",
                   help="compare against this results file "
                        "(default: bench/baselines/<host>-<size>.json)")
    p.add_argument("--save-baseline", action="store_true",
                   help="write this run to the default baseline path")
    p.add_argument("--no-compare", action="store_true",
                   help="don't compare against any baseline")
    p.add_argument("--vs-ref", metavar="REF",
                   help="also run the suite against this git revision (with "
                        "today's benchmark code) and compare against it")
    p.add_argument("--diff", nargs=2, metavar=("BASE", "CUR"),
                   help="compare two saved result files and exit")

    p.add_argument("--json", metavar="PATH", help="write this run's results here")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD_PCT,
                   metavar="PCT", help="percent change worth reporting (default: 7)")
    p.add_argument("--min-abs", type=float, default=DEFAULT_MIN_ABS_S * 1000,
                   metavar="MS", help="absolute change floor in ms (default: 0.5)")
    p.add_argument("--only-changed", action="store_true",
                   help="hide benchmarks whose result didn't move")
    p.add_argument("--fail-on-regression", action="store_true",
                   help="exit 1 if anything regressed (for CI / pre-commit use)")
    p.add_argument("--quiet", action="store_true", help="no per-benchmark progress")
    a = p.parse_args(argv)

    c = C.C(C._color_enabled())
    out: list[str] = []

    if a.diff:
        base, cur = _load(a.diff[0]), _load(a.diff[1])
        deltas = compare_runs(base, cur, threshold_pct=a.threshold,
                              min_abs=a.min_abs / 1000)
        out += render_env(base["environment"], c, "baseline")
        out += render_env(cur["environment"], c, "current ")
        out += render_env_warning(base["environment"], cur["environment"], c)
        out.append("")
        out += render_comparison(deltas, c, base_label="baseline", cur_label="current",
                                 show_same=not a.only_changed)
        out += render_summary(deltas, c)
        print("\n".join(out))
        return 1 if (a.fail_on_regression and
                     any(d.verdict == REGRESSION for d in deltas)) else 0

    if a.list:
        load_suites()
        for bd in select(a.size, a.patterns, a.groups):
            note = f"  — {bd.note}" if bd.note else ""
            print(f"{bd.group:<10} {bd.name}{note}")
        print(f"\n{len(select(a.size, a.patterns, a.groups))} of {len(REGISTRY)} "
              f"benchmarks selected at size={a.size}")
        return 0

    ref_run = None
    if a.vs_ref:
        ref_run = run_against_ref(a.vs_ref, a.size, patterns=a.patterns,
                                  groups=a.groups, reps=a.reps)
        print("", flush=True)

    if not a.quiet:
        print(f"running {a.size} suite", flush=True)
    cur = run(a.size, patterns=a.patterns, groups=a.groups, reps=a.reps,
              verbose=not a.quiet)
    print("", flush=True)

    if a.json:
        _save(a.json, cur)

    baseline = None
    base_label = "baseline"
    if ref_run is not None:
        baseline, base_label = ref_run, a.vs_ref[:9]
    elif not a.no_compare:
        path = Path(a.baseline) if a.baseline else default_baseline_path(a.size)
        if path.exists():
            baseline = _load(path)
        elif a.baseline:
            print(f"no such baseline: {path}", file=sys.stderr)
            return 2

    out += render_env(cur["environment"], c)
    deltas = []
    if baseline is None:
        out.append("")
        out += render_run(cur, c)
        out.append(c.dim("no baseline yet — run with --save-baseline to record "
                         "this run as one, or --vs-ref <rev> to diff against a "
                         "git revision"))
        print("\n".join(out))
    else:
        deltas = compare_runs(baseline, cur, threshold_pct=a.threshold,
                              min_abs=a.min_abs / 1000)
        out += render_env(baseline["environment"], c, "baseline   ")
        out += render_env_warning(baseline["environment"], cur["environment"], c)
        out.append("")
        out += render_comparison(deltas, c, base_label=base_label[:9],
                                 cur_label="current",
                                 show_same=not a.only_changed)
        out += render_summary(deltas, c)
        print("\n".join(out))

    if a.save_baseline:
        path = Path(a.baseline) if a.baseline else default_baseline_path(a.size)
        _save(path, cur)
        print(f"\nbaseline written to {path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path}")

    if a.fail_on_regression and any(d.verdict == REGRESSION for d in deltas):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
