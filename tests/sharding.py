"""`pytest --shard K/N`: run the K-th of N disjoint slices of the suite.

CI spreads the browser suite across N runners this way
(.github/workflows/ci.yml) — ~800 tests that each open a fresh browser
context are wall-clock bound, not CPU bound, so N runners finish in about
1/N of the time. Without the option nothing changes. The hooks that read
the option live in tests/conftest.py; this file is the part worth testing
on its own (tests/test_sharding.py).

Slices are whole test FILES, never a file cut in two. The UI modules lean
on module-scoped fixtures (a plugin switched on for the module,
clean_plugin_tab_state), and half a file would pay for that setup twice and
run its tests in an order nobody has run them in.

Files are dealt heaviest first, each to whichever slice is lightest so far
(greedy longest-processing-time), weighed by test count. Counting tests
rather than reading recorded durations is deliberate: a durations file goes
stale with every test added, and the UI tests are close enough to one
another in cost — most of each one is opening the page — that the count
does well enough. Measured on CI when this went in: six UI shards ran
157-200s of pytest against an even split of 177s. The backend suite is
lumpier (four tests in test_view_jobs.py are ~105s of its ~270s), so its
two shards balance by luck more than design; if one runs long, that file
is why.

A file's slice depends only on what was collected, so every runner on a
given commit agrees on the split without talking to the others, and a
failed shard re-runs the same tests.
"""

from __future__ import annotations


def parse(spec: str) -> tuple[int, int]:
    """'2/6' -> (2, 6). K counts from 1, the way the jobs are labelled."""
    try:
        k, n = (int(x) for x in spec.split("/"))
    except ValueError:
        raise ValueError(f"--shard wants K/N, e.g. 2/6 (got {spec!r})") from None
    if not 1 <= k <= n:
        raise ValueError(f"--shard {spec}: K must be between 1 and N")
    return k, n


def deal(sizes: dict[str, int], n: int) -> dict[str, int]:
    """Assign each file (by node-id path) to a slice 0..n-1, balancing the
    summed `sizes`. Ties break on the path, so the result never depends on
    collection order or hash seeds."""
    load = [0] * n
    owner: dict[str, int] = {}
    for path in sorted(sizes, key=lambda p: (-sizes[p], p)):
        slot = load.index(min(load))
        owner[path] = slot
        load[slot] += sizes[path]
    return owner
