"""The catastrophic-regex guard (winnow/rxguard.py) as a whole.

test_search.py and test_regex_named_groups.py check the guard where it
stands in front of a search, with the handful of patterns the bugs were
found on. Those leave most of the model unchecked: its fallback for what
the automaton can't model (backreferences, conditionals, bodies too big
to search) never ran in a test, and moving how a lookaround counts
towards a first character changed 144 of 6,000 random verdicts with no
test noticing. The corpus below is what notices that kind of change.

The named cases after it say what each fallback rule is for, one allowed
and one refused pattern each, so a corpus diff can be read against
them."""

from __future__ import annotations

import pytest

import rxguard_corpus
from winnow.rxguard import regex_hazard


def test_every_corpus_pattern_keeps_its_verdict():
    rows = rxguard_corpus.read()
    assert len(rows) == rxguard_corpus.SIZE
    moved = [(want, got, p) for want, p in rows if (got := rxguard_corpus.verdict(p)) != want]
    assert not moved, (
        f"{len(moved)} of {len(rows)} patterns changed verdict (was -> now):\n"
        + "\n".join(f"  {w} -> {g}  {p}" for w, g, p in moved[:25])
        + "\nIf that is intended, rewrite the file with `python -m tests.rxguard_corpus`"
          " and review the diff.")


def test_the_corpus_reaches_every_rule():
    """A corpus that never reaches a rule can't hold it. Each verdict is
    one rule's reason, so each has to turn up a fair number of times."""
    counts: dict[str, int] = {}
    for want, _ in rxguard_corpus.read():
        counts[want] = counts.get(want, 0) + 1
    assert set(counts) == {"ok", *rxguard_corpus.REASONS.values()}
    assert min(counts.values()) >= 25, counts


# The fallback, case by case: a backreference or a conditional in the
# body, so the automaton gives up and the shape rules decide. Refused
# means a rule could not rule out exponential backtracking. The rules are
# conservative on purpose (a refused pattern can be rewritten, a hung
# case can't be cancelled), so some refused shapes are fine in practice;
# those are marked.
@pytest.mark.parametrize("pattern, verdict", [
    # Nothing marks where one repetition ends: the digits can go to either.
    (r"(x)(?:(?(1)\d|z)\d+)+$", "undelimited"),
    # A `,` that none of the variable parts can match ends each one.
    (r"(x)(?:(?(1)y|z)\d+,)+$", "ok"),
    # Delimited, but two parts side by side can share the same digits.
    (r"(x)(?:(?(1)y|z)\d+\d*,)+$", "ambiguous"),
    (r"(x)(?:(?(1)y|z)\d+[a-f]*,)+$", "ok"),
    # Delimited and unambiguous, but two alternatives match the same text.
    (r"(x)(?:(?(1)y|z)(?:a|a),)+$", "alternatives"),
    (r"(x)(?:(?(1)y|z)(?:a|b),)+$", "ok"),
    # Alternatives are told apart by the character they start with, seen
    # through a group or a lookahead.
    (r"(x)(?:(?(1)y|z)(?:(a)|(b)),)+$", "ok"),
    (r"(x)(?:(?(1)y|z)(?:(?=a)a|b),)+$", "ok"),
    # Conservative: alternatives that only START alike are refused too.
    (r"(x)(?:(?(1)y|z)(?:a|ab),)+$", "alternatives"),
    # Conservative: a backreference could be any text, so no delimiter
    # pins one down under an open-ended repeat...
    (r"(\w)(?:\1,)+$", "undelimited"),
    # ...but a fixed count with nothing open-ended inside is a few ways
    # at most.
    (r"(\w)(?:\1,){3}$", "ok"),
])
def test_the_fallback_rules(pattern, verdict):
    assert rxguard_corpus.verdict(pattern) == verdict, regex_hazard(pattern)


@pytest.mark.parametrize("pattern, verdict", [
    # Two alternatives that can both match nothing are two ways to reach
    # the same `x`, every repetition.
    (r"(?:(?:a?|b?)x)+$", "splits"),
    (r"(?:(?:a|b)x)+$", "ok"),
])
def test_the_automaton_counts_every_way_to_a_character(pattern, verdict):
    assert rxguard_corpus.verdict(pattern) == verdict, regex_hazard(pattern)


def test_a_body_too_big_to_search_is_judged_by_shape():
    """Past the automaton's budget of 160 character atoms the shape rules
    decide, the same as for a backreference."""
    keywords = "|".join(c + "x" * 9 for c in "abcdefghijklmnopqrst")   # 200 characters
    assert regex_hazard(rf"(?:(?:{keywords})\s+,)+") is None
    assert rxguard_corpus.verdict(rf"(?:(?:{keywords})[a-z ]*\s+,)+") == "ambiguous"
    assert rxguard_corpus.verdict(rf"(?:(?:{keywords})\d+\d*,)+") == "ambiguous"
