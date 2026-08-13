"""Suite discovery, the shared Env every benchmark receives, and the run loop."""

from __future__ import annotations

import importlib
import json
import os
import pkgutil
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import fixtures
from .fixtures import CaseFixture, build_case
from .harness import REGISTRY, SIZE_ORDER, SIZES, Metric, environment, run_benchmark

REPO_ROOT = Path(__file__).resolve().parent.parent

# Groups run in this order regardless of import order, so two runs on the
# same machine hit the same benchmarks in the same sequence — page cache
# and SQLite's own cache state are part of what's being measured, and
# reshuffling the order changes the numbers.
GROUP_ORDER = ["ingest", "views", "paging", "search", "grouping", "tagging",
               "timeline", "export", "sql", "meta", "api", "footprint"]


class Env:
    """Handed to every benchmark. Owns the shared case fixture (built once,
    lazily, on first use) and collects any non-time metrics a benchmark
    wants recorded."""

    def __init__(self, size: str, verbose: bool = True):
        self.size = size
        self.rows = SIZES[size]
        self.verbose = verbose
        self._case: CaseFixture | None = None
        self._client = None
        self._metrics: dict[str, Metric] = {}
        self._workspace_dir = tempfile.mkdtemp(prefix="tl-bench-ws-")
        # Same isolation the pytest suite's `isolate_workspace` fixture does:
        # nothing here may read or write the developer's real workspace/.
        import workspace as WS

        WS.WORKSPACE_DIR = Path(self._workspace_dir)

    def case(self) -> CaseFixture:
        if self._case is None:
            if self.verbose:
                print(f"building fixture case ({self.rows:,} rows) — untimed setup",
                      flush=True)
            t0 = time.perf_counter()
            self._case = build_case(self.rows, verbose=self.verbose)
            if self.verbose:
                print(f"  ready in {time.perf_counter() - t0:.1f}s\n", flush=True)
        return self._case

    def client(self):
        """A TestClient bound to the fixture's store, for the api suite —
        the same monkeypatch of server.STORE the pytest suite uses."""
        if self._client is None:
            import server
            from fastapi.testclient import TestClient

            server.STORE = self.case().store
            self._client = TestClient(server.app,
                                      headers={"X-Timeline-Lite-Client": "1"})
        return self._client

    def metric(self, name: str, value: float, unit: str = "",
               higher_is_better: bool = False, floor: float = 0.0) -> None:
        self._metrics[name] = Metric(value, unit, higher_is_better, floor)

    def close(self) -> None:
        if self._case is not None:
            self._case.close()
            self._case = None
        shutil.rmtree(self._workspace_dir, ignore_errors=True)


def load_suites() -> None:
    """Import every module under bench/suites, which is what populates the
    registry via the @benchmark decorator."""
    from . import suites

    for mod in pkgutil.iter_modules(suites.__path__):
        importlib.import_module(f"{suites.__name__}.{mod.name}")


def select(size: str, patterns: list[str] | None = None,
           groups: list[str] | None = None):
    """Registry entries that apply, in GROUP_ORDER."""
    tier = SIZE_ORDER.index(size)
    picked = []
    for bd in REGISTRY:
        if SIZE_ORDER.index(bd.min_size) > tier:
            continue
        if groups and bd.group not in groups:
            continue
        if patterns and not any(p.lower() in bd.name.lower() for p in patterns):
            continue
        picked.append(bd)
    picked.sort(key=lambda b: (GROUP_ORDER.index(b.group) if b.group in GROUP_ORDER
                               else len(GROUP_ORDER), REGISTRY.index(b)))
    return picked


def run(size: str, *, patterns=None, groups=None, reps=None, verbose=True) -> dict:
    load_suites()
    todo = select(size, patterns, groups)
    env = Env(size, verbose=verbose)
    results = []
    try:
        for i, bd in enumerate(todo, 1):
            if verbose:
                print(f"[{i:>3}/{len(todo)}] {bd.name}", end="", flush=True)
            t0 = time.perf_counter()
            rec = run_benchmark(bd, env, reps_override=reps)
            results.append(rec)
            if verbose:
                took = time.perf_counter() - t0
                if rec.get("error"):
                    print(f"  ERROR: {rec['error']}", flush=True)
                elif "seconds" in rec:
                    from .compare import fmt_time
                    print(f"  {fmt_time(rec['seconds']['median'])}"
                          f"  ({took:.1f}s wall)", flush=True)
                else:
                    print(f"  ok ({took:.1f}s wall)", flush=True)
    finally:
        env.close()
    return {"environment": environment(size), "results": results}


# --------------------------------------------------------------------------
# running the same suite against another git revision


def run_against_ref(ref: str, size: str, *, patterns=None, groups=None,
                    reps=None) -> dict:
    """Measure `ref` with *this* checkout's benchmark code.

    A git worktree at `ref` gets today's bench/ copied over it before the run.
    That matters: comparing HEAD's benchmarks against whatever benchmarks
    existed at `ref` would compare two different workloads and call the
    difference a regression. Only store.py/server.py/workspace.py — the code
    under test — come from `ref`.

    The fixture cache is shared through BENCH_CACHE_DIR so both sides ingest
    byte-identical input files.
    """
    commit = subprocess.run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
                            cwd=REPO_ROOT, capture_output=True, text=True)
    if commit.returncode != 0:
        raise SystemExit(f"not a git revision: {ref}")

    worktree = tempfile.mkdtemp(prefix="tl-bench-ref-")
    # Outside the worktree — it gets removed before we read the results back.
    fd, out_path = tempfile.mkstemp(prefix="tl-bench-ref-", suffix=".json")
    os.close(fd)
    try:
        subprocess.run(["git", "worktree", "add", "--detach", worktree, ref],
                       cwd=REPO_ROOT, check=True, capture_output=True, text=True)
        shutil.rmtree(os.path.join(worktree, "bench"), ignore_errors=True)
        shutil.copytree(Path(__file__).resolve().parent,
                        os.path.join(worktree, "bench"),
                        ignore=shutil.ignore_patterns("__pycache__", ".cache",
                                                      "baselines", "results"))
        cmd = [sys.executable, "-m", "bench", "--size", size,
               "--json", out_path, "--no-compare", "--quiet"]
        for p in patterns or []:
            cmd += ["-k", p]
        for g in groups or []:
            cmd += ["--group", g]
        if reps:
            cmd += ["--reps", str(reps)]
        env = dict(os.environ, BENCH_CACHE_DIR=str(fixtures.cache_dir()))
        print(f"measuring {ref} in a temporary worktree (this runs the whole "
              f"suite twice)…", flush=True)
        proc = subprocess.run(cmd, cwd=worktree, env=env)
        if proc.returncode not in (0, 1):
            raise SystemExit(f"reference run failed (exit {proc.returncode})")
        with open(out_path) as f:
            data = json.load(f)
        data["environment"]["ref"] = ref
        return data
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", worktree],
                       cwd=REPO_ROOT, capture_output=True, text=True)
        shutil.rmtree(worktree, ignore_errors=True)
        if os.path.exists(out_path):
            os.unlink(out_path)
