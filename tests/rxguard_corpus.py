"""The regex guard's verdict corpus: a few thousand generated patterns
and what winnow.rxguard.regex_hazard says about each, frozen in
tests/data/rxguard_verdicts.txt and checked by tests/test_rxguard.py.

The guard is a model of how `re` backtracks, and a change anywhere in it
can move a verdict without any hand-written test noticing: flipping how
_rx_first treats a lookaround moved 144 of 6,000 random patterns, some
from refused to allowed. The corpus is what notices.

When a verdict is meant to change, re-judge the same patterns and
review the diff before committing it:

    python -m tests.rxguard_corpus            # rewrite the verdicts
    git diff tests/data/rxguard_verdicts.txt  # every line here is a pattern
                                              # whose verdict moved

`--generate` draws a fresh set of patterns instead, which is only for
widening the corpus; a regenerated file is a different test.

The grammar sticks to syntax every supported Python parses the same way
(no possessive repeats or atomic groups, which arrived in 3.11), so one
file holds on 3.10 and on 3.12."""

from __future__ import annotations

import random
import re
import sys
from pathlib import Path

CORPUS = Path(__file__).parent / "data" / "rxguard_verdicts.txt"
SEED = 20261016
SIZE = 3000

# regex_hazard's reasons, by the rule that gives each, so the file says
# which rule fired rather than repeating its wording.
REASONS = {
    "a repeated group that can split the same text more than one way": "splits",
    "a repeated group with nothing marking where one repetition ends": "undelimited",
    "a repeated group whose parts can share out the same text more than one way": "ambiguous",
    "alternatives that can match the same text inside an open-ended repeat": "alternatives",
}

_CHARS = ["a", "b", "x", ",", " ", "-", r"\.", ":"]
_CLASSES = [r"\d", r"\s", r"\S", r"\w", r"\W", ".", "[ab]", "[a-c]", "[^,]", "[0-9a-f]", r"[\s,]"]
_WORDS = ["lsadump", "logonpasswords", "mimikatz", "cmd", "powershell", "psexec", "net", "user",
          "admin", "temp", "system32", "rundll32", "reg", "query", "add", "delete"]
_REPEATS = ["*", "+", "?", "{2}", "{1,3}", "{2,}", "{0,9}", "{3,12}", "*?", "+?"]
# Parts that leave the delimiters below alone, for bodies the delimiter
# rule passes.
_NARROW = ["a", "b", "x", r"\d", "[ab]", "[0-9a-f]", "[a-c]", "ab", "xa"]
# Three alphabets with no character in common: parts drawn from different
# ones can't be ambiguous with each other, which leaves an alternation
# whose branches start alike as the only thing to judge.
_ALPHABETS = [["a", "b", "ab", "[ab]", "ba"], [r"\d", "[0-9]", "1", "23", "[1-3]"], ["x", "y", "xy", "[xy]", "yx"]]
_NARROW_REPEATS = ["+", "*", "?", "{1,3}", ""]
_DELIMS = [",", ":", "-", r"\.", ";"]


def verdict(pattern: str) -> str:
    from winnow.rxguard import regex_hazard

    reason = regex_hazard(pattern)
    if reason is None:
        return "ok"
    if reason not in REASONS:
        raise ValueError(f"regex_hazard gave a reason this corpus has no name for: {reason!r} "
                         "(add it to REASONS in tests/rxguard_corpus.py)")
    return REASONS[reason]


class _Gen:
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.groups = 0

    def atom(self, depth: int) -> str:
        r = self.rng.random()
        if depth > 0 and r < 0.30:
            return self.group(depth - 1)
        if r < 0.55:
            return self.rng.choice(_CLASSES)
        if r < 0.80:
            return self.rng.choice(_CHARS)
        if r < 0.85:
            return self.rng.choice(["^", "$", r"\b"])
        if r < 0.90 and depth > 0:
            look = self.rng.choice(["(?=", "(?!", "(?<="])
            # A lookbehind needs a fixed width.
            body = self.rng.choice(_CHARS) if look == "(?<=" else self.seq(depth - 1)
            return f"{look}{body})"
        if r < 0.94 and self.groups:
            return f"\\{self.rng.randint(1, self.groups)}"
        return self.rng.choice(_WORDS)

    def piece(self, depth: int) -> str:
        a = self.atom(depth)
        if a in ("^", "$", r"\b") or a.startswith("(?=") or a.startswith("(?!") or a.startswith("(?<="):
            return a
        if self.rng.random() < 0.45:
            a += self.rng.choice(_REPEATS)
        return a

    def seq(self, depth: int) -> str:
        return "".join(self.piece(depth) for _ in range(self.rng.randint(1, 4)))

    def group(self, depth: int) -> str:
        r = self.rng.random()
        if r < 0.15:
            # A long keyword alternation: past the automaton's atom budget,
            # so the structural fallback decides.
            words = self.rng.sample(_WORDS, self.rng.randint(8, 16))
            return "(?:" + "|".join(words) + r"\s+)"
        alts = "|".join(self.seq(depth) for _ in range(self.rng.choice([1, 1, 2, 3])))
        if r < 0.22 and self.groups:
            n = self.rng.randint(1, self.groups)
            return f"(?({n}){self.seq(depth)}|{self.seq(depth)})"
        if r < 0.60:
            self.groups += 1
            return f"({alts})"
        return f"(?:{alts})"


    def unmodelled(self, depth: int) -> str:
        """A group the automaton gives up on: alternatives with a
        backreference or a conditional among them. Under an open-ended
        repeat these are what the structural rules alone decide."""
        alts = [self.seq(depth) for _ in range(self.rng.choice([1, 2, 2, 3]))]
        i = self.rng.randrange(len(alts))
        if self.rng.random() < 0.7:
            cut = self.rng.randint(0, len(alts[i]))
            # A cut inside an escape or a class would not compile; those
            # draws are simply dropped by generate().
            alts[i] = alts[i][:cut] + f"\\{self.rng.randint(1, self.groups)}" + alts[i][cut:]
        else:
            alts[i] += f"(?({self.rng.randint(1, self.groups)}){self.seq(0)}|{self.seq(0)})"
        return "(?:" + "|".join(alts) + ")"


    def narrow(self, alphabet=_NARROW) -> str:
        return "".join(self.rng.choice(alphabet) + self.rng.choice(_NARROW_REPEATS)
                       for _ in range(self.rng.randint(1, 3)))

    def delimited(self) -> str:
        """A body the automaton gives up on (it holds a conditional), made
        of narrow parts and one delimiter, so the delimiter rule passes it
        and the ambiguity and alternation rules have to decide."""
        if self.rng.random() < 0.5:
            plain, alts, cond = self.rng.sample(_ALPHABETS, 3)
        else:
            plain = alts = cond = _NARROW
        parts = [self.narrow(plain)]
        if self.rng.random() < 0.7:
            parts.append("(?:" + "|".join(self.narrow(alts) for _ in range(self.rng.randint(2, 3))) + ")")
        parts.append(f"(?({self.rng.randint(1, self.groups)}){self.narrow(cond)}|{self.narrow(cond)})")
        self.rng.shuffle(parts)
        d = self.rng.choice(_DELIMS)
        return "(?:" + ("".join(parts) + d if self.rng.random() < 0.7 else d + "".join(parts)) + ")"


def generate(seed: int = SEED, size: int = SIZE) -> list[str]:
    rng = random.Random(seed)
    out: list[str] = []
    seen: set[str] = set()
    while len(out) < size:
        gen = _Gen(rng)
        # Most hazards need a repeat around a group, so put one there often.
        r = rng.random()
        repeat = rng.choice(["*", "+", "{2,}", "{3}", "{10}", "{1,20}"])
        if r < 0.15:
            gen.groups = 1
            body = f"({gen.narrow()})" + gen.delimited() + rng.choice(["*", "+", "{2,}"]) + gen.seq(1)
        elif r < 0.35:
            first = gen.group(2)
            gen.groups = max(gen.groups, 1)
            body = (first if first.startswith("(") and not first.startswith("(?") else f"({first})")
            body += gen.unmodelled(2) + repeat + gen.seq(1)
        elif r < 0.75:
            body = gen.group(3) + repeat + gen.seq(1)
        else:
            body = gen.seq(3)
        if body in seen:
            continue
        try:
            re.compile(body, re.IGNORECASE)
        except (re.error, OverflowError, RecursionError):
            continue
        seen.add(body)
        out.append(body)
    return out


def read() -> list[tuple[str, str]]:
    rows = []
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        key, pattern = line.split(" ", 1)
        rows.append((key, pattern))
    return rows


def write(patterns: list[str]) -> None:
    CORPUS.parent.mkdir(exist_ok=True)
    CORPUS.write_text("".join(f"{verdict(p)} {p}\n" for p in patterns), encoding="utf-8")


if __name__ == "__main__":
    write(generate() if "--generate" in sys.argv[1:] else [p for _, p in read()])
