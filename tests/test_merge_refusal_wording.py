"""One refusal written twice: keep the two copies saying the same thing.

Removing a table a merge is built on is refused in two places. The server
raises it from `Store.drop_source` (surfaced as a 409), and the Tables
manager checks for it first in `static/js/tables.js` so the analyst gets
the reason and the merge's NAME without a round trip, and so the Remove
button never looks like it did nothing. The client cannot import the
server's string — fetching it would be the round trip the pre-check
exists to avoid — so the sentence is duplicated verbatim by hand.

Neither copy's WORDING is pinned anywhere else. `tests/ui/
test_remove_table_in_a_merge.py` reads the dialog and
`tests/test_drop_merged_table.py` reads the ValueError, but what each of
them asserts is that the merge's NAME appears — so both stay green
through any rewording of either copy, and the result is an app that
explains the same refusal two different ways depending on whether the
click went through the pre-check or raced past it. Nothing else
notices.

So this is the mechanical guard: read both files and compare the clause
they share, character for character. What deliberately DIFFERS is the
opening — the client has the table's display name to hand and uses it,
the server only knows "This table" — so the comparison starts after that
and covers the part that is genuinely one sentence written twice.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TABLES_JS = ROOT / "static" / "js" / "tables.js"
STORE_PY = ROOT / "winnow" / "store.py"

# The tail of the refusal, as a single-line string literal in either
# language. Anchored on "first —" rather than on the whole sentence so
# that a reworded clause is still FOUND and then compared, instead of
# vanishing from the scan and taking the check with it.
CLAUSE = re.compile(r"""(['"])(\s*first — [^'"]*)\1""")


def _clause(path: Path) -> str:
    """The shared tail, or a failure naming the file it went missing from.

    A scan that matches nothing passes forever, so "exactly one" is
    asserted rather than assumed: if the sentence is reflowed across
    lines, or the em dash is retyped as a hyphen, this test fails and says
    where — which is the right outcome, because a guard that can silently
    stop guarding is no better than not having one.
    """
    hits = [m.group(2) for line in path.read_text(encoding="utf-8").splitlines()
            if (m := CLAUSE.search(line))]
    assert len(hits) == 1, (
        f"expected exactly one merge-refusal clause in {path.name}, found {len(hits)}: {hits}")
    return hits[0]


def test_the_scan_finds_the_clause_in_both_files():
    """Before comparing them, check there is something to compare.

    Deliberately not a check on the current wording — that belongs to the
    two suites that read the message an analyst actually sees, and pinning
    it here as well would mean one rewording failing in three places. All
    this asks is that what the scan pulled out is a sentence about merges
    rather than an empty match, so the comparison below is doing work.
    """
    for path in (TABLES_JS, STORE_PY):
        clause = _clause(path)
        assert "merge" in clause and len(clause) > 40, (path.name, clause)


def test_the_client_and_the_server_refuse_in_the_same_words():
    """Edit one copy without the other and this is what fails."""
    assert _clause(TABLES_JS) == _clause(STORE_PY), (
        "the Tables manager's pre-check and Store.drop_source no longer word the "
        "refusal the same way — change both, or the message the analyst sees "
        "depends on which path the click took")


def _assembly(path: Path) -> str:
    """The few lines that build the message, not the whole file.

    Searching the file for a piece like "that merge" is the obvious
    version and it does not guard: the phrase occurs in `drop_source`'s
    own docstring too, so the server's singular pick could drift to "the
    merge" and the scan would still find a match somewhere above it. The
    window is anchored on the clause line, which `_clause` has already
    proved is unique.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    at = next(i for i, line in enumerate(lines) if CLAUSE.search(line))
    return "\n".join(lines[max(0, at - 5):at + 1])


@pytest.mark.parametrize("piece", [". Delete ", "that merge", "those merges"])
def test_the_pieces_around_the_clause_match_too(piece):
    """The sentence is assembled from a lead-in, a singular/plural pick
    and the shared tail. The tail is compared above; these are the joins
    on either side of the pick, where "Delete that merge first" can drift
    into "Delete the merge first" on one side only — a difference the
    clause comparison would never see, since it starts after the pick."""
    for path in (TABLES_JS, STORE_PY):
        assert piece in _assembly(path), (
            f"{piece!r} is gone from the refusal {path.name} builds")
