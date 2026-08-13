"""Comparing one run against another, and rendering the report.

The whole point of the suite is this file: a wall of timings nobody diffs by
eye is not a regression detector. A result is only called a regression when
it clears three independent bars at once —

  1. a percentage threshold (default 7%), so a change has to be worth caring
     about in relative terms;
  2. an absolute floor (default 0.5ms), so a 2.1µs → 2.4µs shuffle in a
     microbenchmark never gets reported as "14% slower";
  3. a noise band of 2x the larger of the two runs' standard deviations, so a
     benchmark that swings by 20% between repetitions can't cross the line on
     one unlucky sample.

Anything failing any of those is "same". That's deliberately conservative:
the failure mode that kills a perf suite is crying wolf until people stop
reading it.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from .harness import env_mismatches

DEFAULT_THRESHOLD_PCT = 7.0
DEFAULT_MIN_ABS_S = 0.0005  # 0.5ms
NOISE_SIGMAS = 2.0
NOISY_RSD = 0.10  # relative stdev above this earns a ~ marker in the report

REGRESSION = "regression"
IMPROVEMENT = "improvement"
SAME = "same"
NEW = "new"
MISSING = "missing"
ERROR = "error"


@dataclass
class Delta:
    name: str
    group: str
    kind: str            # "time", or a metric name
    unit: str            # "s" for time, else the metric's unit
    base: float | None
    cur: float | None
    pct: float | None
    verdict: str
    detail: str = ""


def _significant(base: float, cur: float, *, stdev: float, threshold_pct: float,
                 min_abs: float) -> bool:
    delta = abs(cur - base)
    if base <= 0:
        return False
    if delta < min_abs:
        return False
    if delta < NOISE_SIGMAS * stdev:
        return False
    return (delta / base) * 100.0 >= threshold_pct


def _verdict(base: float, cur: float, *, higher_is_better: bool, **kw) -> str:
    if not _significant(base, cur, **kw):
        return SAME
    slower = cur < base if higher_is_better else cur > base
    return REGRESSION if slower else IMPROVEMENT


def compare_runs(baseline: dict, current: dict, *, threshold_pct: float = DEFAULT_THRESHOLD_PCT,
                 min_abs: float = DEFAULT_MIN_ABS_S) -> list[Delta]:
    """Pair up two result files by benchmark name. Benchmarks that only
    exist on one side are reported as new/missing rather than skipped — a
    silently-dropped benchmark is how a regression hides."""
    base_by_name = {r["name"]: r for r in baseline.get("results", [])}
    deltas: list[Delta] = []

    for cur in current.get("results", []):
        name, group = cur["name"], cur.get("group", "")
        base = base_by_name.pop(name, None)

        if cur.get("error"):
            deltas.append(Delta(name, group, "time", "s", None, None, None, ERROR,
                                cur["error"]))
            continue
        if base is None:
            t = cur.get("seconds", {}).get("median")
            deltas.append(Delta(name, group, "time", "s", None, t, None, NEW))
            continue
        if base.get("error"):
            t = cur.get("seconds", {}).get("median")
            deltas.append(Delta(name, group, "time", "s", None, t, None, NEW,
                                "baseline errored"))
            continue

        if "seconds" in cur and "seconds" in base:
            b, c = base["seconds"]["median"], cur["seconds"]["median"]
            stdev = max(base["seconds"]["stdev"], cur["seconds"]["stdev"])
            deltas.append(Delta(
                name, group, "time", "s", b, c, _pct(b, c),
                _verdict(b, c, higher_is_better=False, stdev=stdev,
                         threshold_pct=threshold_pct, min_abs=min_abs),
            ))

        for mname, m in (cur.get("metrics") or {}).items():
            bm = (base.get("metrics") or {}).get(mname)
            if not bm:
                deltas.append(Delta(f"{name}:{mname}", group, mname, m["unit"],
                                    None, m["value"], None, NEW))
                continue
            b, c = bm["value"], m["value"]
            deltas.append(Delta(
                f"{name}:{mname}", group, mname, m["unit"], b, c, _pct(b, c),
                # Metrics have no repetitions to derive noise from — they're
                # counts and file sizes, which are exact. The declared floor
                # is the only guard they need.
                _verdict(b, c, higher_is_better=m.get("higher_is_better", False),
                         stdev=0.0, threshold_pct=threshold_pct,
                         min_abs=m.get("floor", 0.0)),
            ))

    for name, base in base_by_name.items():
        deltas.append(Delta(name, base.get("group", ""), "time", "s",
                            base.get("seconds", {}).get("median"), None, None, MISSING))
    return deltas


def _pct(base: float, cur: float) -> float | None:
    return None if not base else (cur - base) / base * 100.0


# --------------------------------------------------------------------------
# rendering


def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


class C:
    def __init__(self, on: bool):
        self.on = on

    def _w(self, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self.on else s

    def red(self, s): return self._w("31", s)
    def green(self, s): return self._w("32", s)
    def yellow(self, s): return self._w("33", s)
    def cyan(self, s): return self._w("36", s)
    def dim(self, s): return self._w("2", s)
    def bold(self, s): return self._w("1", s)


def fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 1e-3:
        return f"{seconds * 1e6:.0f}us"
    if seconds < 1.0:
        return f"{seconds * 1e3:.1f}ms"
    return f"{seconds:.2f}s"


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def fmt_metric(value: float | None, unit: str) -> str:
    if value is None:
        return "—"
    if unit == "bytes":
        return fmt_bytes(value)
    if unit in ("rows", "count", "matches"):
        return f"{value:,.0f}"
    if unit == "ratio":
        return f"{value:.2f}x"
    return f"{value:,.2f}{unit}"


def fmt_value(d: Delta, which: str) -> str:
    v = d.base if which == "base" else d.cur
    return fmt_time(v) if d.kind == "time" else fmt_metric(v, d.unit)


def render_run(run: dict, c: C) -> list[str]:
    """The no-baseline report: what each benchmark costs right now."""
    out = []
    by_group: dict[str, list[dict]] = {}
    for r in run["results"]:
        by_group.setdefault(r.get("group", ""), []).append(r)

    width = max((len(r["name"]) for r in run["results"]), default=20)
    for group, rows in by_group.items():
        out.append(c.bold(group))
        for r in rows:
            if r.get("error"):
                out.append(f"  {r['name']:<{width}}  {c.red('ERROR')} {r['error']}")
                continue
            line = f"  {r['name']:<{width}}"
            if "seconds" in r:
                s = r["seconds"]
                rsd = s["stdev"] / s["median"] if s["median"] else 0.0
                marker = c.yellow(" ~") if rsd > NOISY_RSD else "  "
                line += f"  {fmt_time(s['median']):>9}{marker}"
                line += c.dim(f" +/-{rsd * 100:4.1f}%  n={r.get('reps', 0):<2}")
                if r.get("items"):
                    rate = r["items"] / s["median"] if s["median"] else 0
                    line += c.dim(f"  {rate:,.0f} {r.get('item_unit', 'rows')}/s")
            else:
                line += "  " + c.dim("(metrics only)")
            out.append(line)
            for mname, m in (r.get("metrics") or {}).items():
                out.append(c.dim(f"      {mname:<{max(8, width - 4)}}  "
                                 f"{fmt_metric(m['value'], m['unit']):>9}"))
        out.append("")
    return out


def render_comparison(deltas: list[Delta], c: C, *, base_label: str,
                      cur_label: str, show_same: bool = True) -> list[str]:
    out = []
    by_group: dict[str, list[Delta]] = {}
    for d in deltas:
        by_group.setdefault(d.group, []).append(d)

    shown = [d for d in deltas if show_same or d.verdict != SAME]
    width = max((len(d.name) for d in shown), default=20)
    width = max(width, 24)

    header = (f"  {'benchmark':<{width}}  {base_label:>9}  {cur_label:>9}  "
              f"{'delta':>8}")
    out.append(c.dim(header))

    for group, ds in by_group.items():
        visible = [d for d in ds if show_same or d.verdict != SAME]
        if not visible:
            continue
        out.append(c.bold(group))
        for d in visible:
            pct = "—" if d.pct is None else f"{d.pct:+.1f}%"
            tag = {
                REGRESSION: c.red("SLOWER"),
                IMPROVEMENT: c.green("faster"),
                NEW: c.cyan("new"),
                MISSING: c.yellow("gone"),
                ERROR: c.red("ERROR"),
                SAME: c.dim("same"),
            }[d.verdict]
            if d.verdict in (REGRESSION, IMPROVEMENT) and d.kind != "time":
                tag = c.red("WORSE") if d.verdict == REGRESSION else c.green("better")
            line = (f"  {d.name:<{width}}  {fmt_value(d, 'base'):>9}  "
                    f"{fmt_value(d, 'cur'):>9}  {pct:>8}  {tag}")
            if d.detail:
                line += c.dim(f"  {d.detail}")
            out.append(line)
        out.append("")
    return out


def render_summary(deltas: list[Delta], c: C) -> list[str]:
    counts = {v: 0 for v in (REGRESSION, IMPROVEMENT, SAME, NEW, MISSING, ERROR)}
    for d in deltas:
        counts[d.verdict] += 1
    parts = [
        c.red(f"{counts[REGRESSION]} slower") if counts[REGRESSION] else "0 slower",
        c.green(f"{counts[IMPROVEMENT]} faster") if counts[IMPROVEMENT] else "0 faster",
        f"{counts[SAME]} unchanged",
    ]
    for label, key in (("new", NEW), ("gone", MISSING), ("errors", ERROR)):
        if counts[key]:
            parts.append(c.yellow(f"{counts[key]} {label}"))
    return ["  ".join(parts)]


def render_env(env: dict, c: C, label: str = "environment") -> list[str]:
    return [
        c.dim(f"{label}: {env.get('host')} | {env.get('cpu', '')[:44]} | "
              f"py{env.get('python')} sqlite{env.get('sqlite')} | "
              f"size={env.get('size')} ({env.get('rows', 0):,} rows) | "
              f"{env.get('git_branch')}@{env.get('git_commit')}"
              f"{'+dirty' if env.get('git_dirty') else ''}")
    ]


def render_env_warning(base_env: dict, cur_env: dict, c: C) -> list[str]:
    bad = env_mismatches(base_env, cur_env)
    if not bad:
        return []
    lines = [c.yellow("! baseline was recorded in a different environment — "
                      "the comparison below is not trustworthy:")]
    for f in bad:
        lines.append(c.yellow(f"    {f}: {base_env.get(f)!r} -> {cur_env.get(f)!r}"))
    return lines
