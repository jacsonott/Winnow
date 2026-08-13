# Performance suite

Tracks how long each part of the backend takes, and tells you when a change
made it slower or faster. Stdlib only — no pytest-benchmark, no new
dependency (see CLAUDE.md: nothing may become a runtime dependency on the
airgapped target, and a perf suite with its own toolchain is a perf suite
nobody runs).

```
python3 -m bench --size quick          # ~2 min, 20k rows — "did I break something obvious"
python3 -m bench                       # ~10 min, 200k rows — the tier to baseline at
python3 -m bench --size large          # 1.2M rows
```

## The one command worth remembering

```
python3 -m bench --vs-ref HEAD --only-changed
```

Runs the suite twice — once against your working tree, once against a git
worktree checked out at `HEAD` — and prints only what moved. No baseline file
to remember to update, no stale numbers from a different machine, and it
answers the actual question ("did the change I'm about to commit cost
anything?") directly.

Both sides run **today's** benchmark code: `bench/` is copied into the
worktree over whatever was there. Only `store.py`/`server.py`/`workspace.py`
come from the ref. Otherwise you'd be comparing two different workloads and
calling the difference a regression.

Any revision works: `--vs-ref main`, `--vs-ref HEAD~5`, `--vs-ref b254039`.

## Saved baselines

```
python3 -m bench --save-baseline        # record this run
python3 -m bench                        # subsequent runs compare against it
```

Baselines live in `bench/baselines/<hostname>-<size>.json` — per machine and
per size tier, because comparing across either is meaningless. The directory
is gitignored by default; commit one if you want a machine's numbers tracked
over time.

Two saved runs can be compared without running anything:

```
python3 -m bench --json today.json --no-compare
python3 -m bench --diff yesterday.json today.json
```

## When is a change a regression?

A result has to clear three independent bars at once, or it's reported as
"same":

1. **7%** relative change (`--threshold`),
2. **0.5ms** absolute change (`--min-abs`) — so a 176µs → 215µs shuffle in a
   microbenchmark is never called a 22% regression,
3. **2x the larger run's standard deviation** — so a benchmark that swings
   between repetitions can't cross the line on one unlucky sample.

This is deliberately conservative. The failure mode that kills a perf suite
is crying wolf until people stop reading it. Two back-to-back runs of
identical code produce zero flagged changes.

A `~` next to a timing means its relative standard deviation exceeded 10% —
treat that benchmark's delta with suspicion, and re-run before believing it.

`--fail-on-regression` exits 1, for a pre-commit hook or CI.

## What's measured

99 benchmarks across `bench/suites/`, one module per area. Some deliberate
pairings, where the *gap between two numbers* is the real measurement:

| pair | what the gap means |
|---|---|
| `paging/fetch_rows.head` vs `.deep` | invariant #2 — paging cost must not grow with depth |
| `search/contains.*_fts` vs `.*_fallback` | what the trigram index is actually buying |
| `search/contains.rare_fts` vs `.common_fts` | `detail=none` makes query time scale with result count; a change can trade these off against each other |
| `views/filter.equals_indexed` vs `.equals_unindexed` | what the lazy column index is buying |
| `grouping/summary.covers_source` vs `.via_view_join` | the direct-aggregate fast path |
| `api/rows.page` vs `paging/fetch_rows.head` | routing + pydantic + JSON serialisation cost |

`footprint/` is metrics, not timings: source-table bytes, FTS index bytes,
column index bytes, and the case-file-to-CSV ratio. It's in the suite because
file size is a first-class property here — CLAUDE.md's index decisions are
recorded as sizes ("892MB → 143MB"), and a change that buys 10% query speed
by tripling the index is a regression that every timing in the suite would
miss.

`python3 -m bench --list` prints all of them with their notes.

## Fixtures

Generated from a fixed seed, so every filter's selectivity is identical run
to run. Input files (CSV/JSONL/SQLite) are cached in `bench/.cache` — they're
pure data, independent of `store.py`, so reuse across code changes and across
`--vs-ref` worktrees is safe. The **case file** is always rebuilt, since
building it is what several benchmarks measure.

Nothing touches the repo's real `case.db`, `sample.csv` or `workspace/`: the
case is built in a temp dir and `workspace.WORKSPACE_DIR` is redirected, the
same isolation `tests/conftest.py` applies.

Background FTS and column-index builds are disabled on the fixture store
after setup (`_quiet_background`). They're fire-and-forget threads in the
app, which is right there and wrong here — the LIKE-fallback benchmarks would
build the very index they exist to measure the absence of, and everything
running alongside a build would be timed against a busy core.

## Adding a benchmark

Do setup, return the callable to time. Everything outside `run` is excluded,
including the per-repetition `before`/`after` hooks.

```python
@benchmark("views/filter.my_thing", note="why this one is worth watching")
def _(env):
    fx = env.case()
    spec = {"source_id": fx.main, "filters": [...], "sort": []}
    held = {}
    return Timed(
        lambda: held.update(v=fx.store.build_view(fx.main, spec)),
        after=lambda: fx.store.close_view(held["v"]["view_id"]),
        items=fx.rows,
    )
```

Repetition count is adaptive (~1s per benchmark, 3–25 reps); pass `reps=` for
anything that mutates state expensively. If a benchmark changes the case,
undo it in `after` — the fixture's ~1% tagged baseline is what the timeline
and export suites are measured against.

`tests/test_bench_harness.py` covers the harness itself (significance rules,
setup/teardown exclusion, fixture determinism) so this doesn't rot silently.
