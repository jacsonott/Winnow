"""tests/sharding.py — how CI splits a suite across runners.

The property that matters is the one a broken split would hide: every test
runs on exactly one shard. A test dealt to no shard never runs and never
fails, which is the quiet way a required check stops covering anything.
"""

from __future__ import annotations

import pytest

import sharding

# Shaped like tests/ui: a few big files, a long tail of small ones.
SIZES = {f"tests/ui/test_{i:03d}.py": size
         for i, size in enumerate([40, 31, 22, 17, 12, 9, 9, 7, 5, 4, 3, 3, 2, 1, 1, 1])}


@pytest.mark.parametrize("n", [1, 2, 3, 6, 16, 40])
def test_every_file_lands_on_exactly_one_shard(n):
    owner = sharding.deal(SIZES, n)
    assert set(owner) == set(SIZES)
    assert all(0 <= slot < n for slot in owner.values())


@pytest.mark.parametrize("n, want", [(2, [84, 83]), (4, [42, 42, 42, 41]),
                                     # the 40-test file is a shard on its own
                                     (6, [40, 31, 25, 24, 24, 23])])
def test_shards_are_balanced(n, want):
    owner = sharding.deal(SIZES, n)
    assert [sum(s for p, s in SIZES.items() if owner[p] == k) for k in range(n)] == want


def test_split_ignores_collection_order():
    """Every runner computes the split on its own; they agree only because
    the answer depends on the files, not the order they were found in."""
    reordered = dict(reversed(list(SIZES.items())))
    assert sharding.deal(reordered, 6) == sharding.deal(SIZES, 6)


def test_more_shards_than_files_leaves_some_empty():
    owner = sharding.deal({"a.py": 3, "b.py": 1}, 4)
    assert sorted(owner.values()) == [0, 1]


@pytest.mark.parametrize("spec, want", [("1/1", (1, 1)), ("2/6", (2, 6)), ("6/6", (6, 6))])
def test_parse(spec, want):
    assert sharding.parse(spec) == want


@pytest.mark.parametrize("spec", ["0/6", "7/6", "2", "a/b", "2/6/1", ""])
def test_parse_rejects(spec):
    with pytest.raises(ValueError):
        sharding.parse(spec)
