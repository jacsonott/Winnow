"""Whether a regular expression can backtrack catastrophically.

The one check every analyst-written pattern meets before it runs on
evidence: the regex filter and search, REGEXP literals in the SQL pane
(store._compiled_regex), and the regex_extract derived column, both its
preview and its backfill (structparse._compile_analyst_regex). One
module so those can't draw the line in different places. A second,
shape-only check in structparse used to stand in for the derive path,
because structparse cannot import store.

`regex_hazard(pattern)` is the entry point. The rest is the model it
decides with, described in the long comment below. Overlaps are judged
case-insensitively, as REGEXP runs. For a case-sensitive pattern (a
derived column's) that can only refuse more, never less.

Stdlib only, and imports nothing from the app.
"""

from __future__ import annotations

import functools
import re

# Catastrophic backtracking, and why it is refused rather than bounded.
# Cells are attacker-controlled evidence, and `re` backtracks: `(a+)+$`
# against forty a's and a "!" is 2^40 steps. That runs inside the REGEXP
# callback, which holds the GIL for the whole of one cell's match — every
# thread in the server (paging, the event loop, the cancel request itself)
# stops until it finishes, and nothing in Python can interrupt re.search
# (it only polls for signals, and those reach the main thread alone). A
# timeout needs another regex engine (a dependency) or another process per
# match; truncating cells would make a hit past the cut a silent miss. So
# the pattern is checked instead, and the two shapes that blow up are
# refused with a reason. One: a repeated group whose repetitions can match
# the same text more than one way — open-ended over anything variable, or
# a count of two or more over something open-ended (`(a+){10}`). That is
# decided exactly (_rx_splits: a Thompson automaton of the body, and a
# search for two paths that leave one point of the loop and come back to
# it over the same text), not by shape — the shape rules it replaced both
# refused patterns analysts type (`(\S+\s+){3}`, the field-N-of-a-command-
# line shape) and passed ones that blow up (`(,a+.\s+)+$`, where the `.`
# can take the last `a` or the first space). What the automaton does not
# model (backreferences, conditionals, possessive and atomic groups, very
# large bodies) falls back to those shape rules, _rx_delimited and
# _rx_ambiguous. Two, in that fallback only: an open-ended repeat of
# alternatives that can START the same way (`(a|a)*b`) — a cruder test the
# automaton makes exactly everywhere else, which is what lets a keyword
# list like `((?:lsadump|logonpasswords)\s+)+` through while `(a|a)*b` and
# `(a|aa)+` still fail it. Conservative on purpose: a refused
# pattern can be rewritten (end each part of the repetition at something
# it cannot match, a class instead of an alternation, or Contains), a hung
# case cannot be cancelled. NOT covered: polynomial blow-ups from adjacent
# repeats over the same characters (`.*a.*b` on a megabyte cell) — those
# are slow, not exponential, and refusing `.*x.*y` would refuse ordinary
# searches.
try:   # Python 3.11+
    from re import _constants as _sre_c, _parser as _sre_parse
except ImportError:   # Python 3.10
    import sre_constants as _sre_c  # type: ignore[no-redef]
    import sre_parse as _sre_parse  # type: ignore[no-redef]

_RX_REPEATS = (_sre_c.MAX_REPEAT, _sre_c.MIN_REPEAT)
_RX_POSSESSIVE = getattr(_sre_c, "POSSESSIVE_REPEAT", None)
_RX_ATOMIC = getattr(_sre_c, "ATOMIC_GROUP", None)


def _rx_union(a: set | None, b: set | None) -> set | None:
    return None if a is None or b is None else a | b


def _rx_class(items) -> set | None:
    """The characters a `[...]` (or `\\d`-style) item can match, both cases,
    or None for "too many to say" — a category, a negation, a wide range."""
    out: set = set()
    for op, av in items:
        if op == _sre_c.LITERAL:
            out |= {chr(av), chr(av).lower(), chr(av).upper()}
        elif op == _sre_c.RANGE and av[1] - av[0] < 256:
            for o in range(av[0], av[1] + 1):
                out |= {chr(o), chr(o).lower(), chr(o).upper()}
        else:
            return None
    return out


def _rx_first(items) -> tuple[set | None, bool]:
    """(the characters a match of `items` can start with — None for could
    be anything — and whether it can match the empty string). Errs towards
    None, which only ever makes the overlap check more cautious."""
    first: set | None = set()
    for op, av in items:
        if op == _sre_c.LITERAL:
            return _rx_union(first, {chr(av), chr(av).lower(), chr(av).upper()}), False
        if op == _sre_c.IN:
            return _rx_union(first, _rx_class(av)), False
        if op in (_sre_c.AT, _sre_c.ASSERT, _sre_c.ASSERT_NOT):
            continue   # zero-width
        if op == _sre_c.SUBPATTERN or (_RX_ATOMIC is not None and op == _RX_ATOMIC):
            sub, nullable = _rx_first(av[-1] if op == _sre_c.SUBPATTERN else av)
        elif op == _sre_c.BRANCH:
            sub, nullable = set(), False
            for alt in av[1]:
                f, n = _rx_first(alt)
                sub, nullable = _rx_union(sub, f), nullable or n
        elif op in _RX_REPEATS or (_RX_POSSESSIVE is not None and op == _RX_POSSESSIVE):
            sub, nullable = _rx_first(av[2])
            nullable = nullable or av[0] == 0
        else:
            return None, False   # ANY, NOT_LITERAL, a backreference: anything
        first = _rx_union(first, sub)
        if first is None or not nullable:
            return first, False
    return first, True


def _rx_alternatives_overlap(alts) -> bool:
    firsts = [_rx_first(a) for a in alts]
    for i, (fa, na) in enumerate(firsts):
        for fb, nb in firsts[i + 1:]:
            if na or nb or fa is None or fb is None or fa & fb:
                return True
    return False


# A `[...]` item's category back as the escape that wrote it, so an atom
# can be rebuilt as a one-character pattern and asked about directly.
_RX_CATEGORIES = {
    _sre_c.CATEGORY_DIGIT: r"\d", _sre_c.CATEGORY_NOT_DIGIT: r"\D",
    _sre_c.CATEGORY_SPACE: r"\s", _sre_c.CATEGORY_NOT_SPACE: r"\S",
    _sre_c.CATEGORY_WORD: r"\w", _sre_c.CATEGORY_NOT_WORD: r"\W",
}


@functools.lru_cache(maxsize=1)
def _rx_bmp() -> str:
    """Every BMP character but the surrogates — the practical alphabet of
    a log, for asking whether two classes share a character."""
    return "".join(chr(o) for o in range(0x10000) if not 0xD800 <= o <= 0xDFFF)


class _RxChar:
    """One single-character atom, rebuilt as a one-character pattern
    (`cls`, matched case-insensitively as REGEXP runs it) — plus, when it
    is a short list of characters (a literal, `[.,;]`), that list, so a
    check against it needs no scan."""

    def __init__(self, cls: str, explicit: frozenset | None = None):
        self.rx = re.compile(cls, re.IGNORECASE)
        self.explicit = explicit

    def overlaps(self, other: "_RxChar") -> bool:
        if self.explicit is not None:
            return any(other.rx.fullmatch(ch) for ch in self.explicit)
        if other.explicit is not None:
            return any(self.rx.fullmatch(ch) for ch in other.explicit)
        # Two classes (\w against \s, [^,] against \d): what one matches
        # across the BMP, searched with the other.
        return other.rx.search(_rx_members(self.rx.pattern)) is not None


@functools.lru_cache(maxsize=512)
def _rx_members(cls: str) -> str:
    """Every BMP character the one-character pattern `cls` matches. The
    scan is the few ms an overlap check costs, and a pattern asks about
    the same few classes many times over, so it is done once per class."""
    return "".join(re.compile(cls, re.IGNORECASE).findall(_rx_bmp()))


_RX_ANYTHING = _RxChar(r"(?s:.)")


def _rx_char(op, av) -> _RxChar | None:
    """The _RxChar for a single-character atom, or None for anything that
    is not one (a group, a repeat, a zero-width assertion)."""
    if op == _sre_c.LITERAL:
        c = chr(av)
        return _RxChar(re.escape(c), frozenset({c, c.lower(), c.upper()}))
    if op == _sre_c.NOT_LITERAL:
        return _RxChar(f"[^\\U{av:08x}]")
    if op == _sre_c.ANY:
        return _RxChar(".")
    if op != _sre_c.IN:
        return None
    negate = bool(av) and av[0][0] == _sre_c.NEGATE
    parts = av[1:] if negate else av
    body = []
    for pop, pav in parts:
        if pop == _sre_c.LITERAL:
            body.append(f"\\U{pav:08x}")
        elif pop == _sre_c.RANGE:
            body.append(f"\\U{pav[0]:08x}-\\U{pav[1]:08x}")
        elif pop == _sre_c.CATEGORY and pav in _RX_CATEGORIES:
            body.append(_RX_CATEGORIES[pav])
        else:
            return _RX_ANYTHING
    if not body:
        return _RX_ANYTHING
    explicit = None if negate else _rx_class(parts)
    return _RxChar(("[^" if negate else "[") + "".join(body) + "]",
                   frozenset(explicit) if explicit is not None else None)


def _rx_variable_chars(items, variable: bool = False) -> tuple[list[_RxChar], bool]:
    """(the atoms in `items` that can match a varying amount of text, and
    whether there is any variable-length part at all). `variable` says
    everything here is already inside one — a repeat whose count can vary,
    or an ambiguous alternation. A possessive repeat or an atomic group
    never gives back what it took, so nothing inside one counts."""
    chars: list[_RxChar] = []
    any_variable = variable
    for op, av in items:
        ch = _rx_char(op, av)
        if ch is not None:
            if variable:
                chars.append(ch)
            continue
        if op in _RX_REPEATS:
            sub, v = _rx_variable_chars(av[2], variable or av[0] != av[1])
        elif op == _sre_c.SUBPATTERN:
            sub, v = _rx_variable_chars(av[-1], variable)
        elif op == _sre_c.BRANCH:
            # Alternatives that each start differently are one choice the
            # first character makes (`(foo|bar)+`, `(a|bc)+`): only what is
            # variable inside them counts. Ones that can match the same
            # text are a choice backtracking can revisit, so all of it does.
            ambiguous = variable or _rx_alternatives_overlap(av[1])
            sub, v = [], ambiguous
            for alt in av[1]:
                alt_chars, alt_v = _rx_variable_chars(alt, ambiguous)
                chars += alt_chars
                v = v or alt_v
        elif op == _sre_c.GROUPREF_EXISTS:
            sub, v = [], True
            for alt in (av[1], av[2]):
                if alt is not None:
                    chars += _rx_variable_chars(alt, True)[0]
        elif op == _sre_c.GROUPREF:
            sub, v = [_RX_ANYTHING], True   # a backreference can be any text
        else:
            continue   # zero-width, possessive, atomic
        chars += sub
        any_variable = any_variable or v
    return chars, any_variable


def _rx_open_ended(items) -> bool:
    """Whether `items` holds a quantifier with no upper bound that can
    backtrack (so not a possessive one, nor one inside an atomic group)."""
    for op, av in items:
        if op in _RX_REPEATS and (av[1] == _sre_c.MAXREPEAT or _rx_open_ended(av[2])):
            return True
        if op == _sre_c.SUBPATTERN and _rx_open_ended(av[-1]):
            return True
        if op == _sre_c.BRANCH and any(_rx_open_ended(alt) for alt in av[1]):
            return True
    return False


def _rx_sequence(items) -> list:
    """`items` with plain groups opened up: the run of atoms one match of
    them steps through, in order."""
    out: list = []
    for op, av in items:
        if op == _sre_c.SUBPATTERN:
            out += _rx_sequence(av[-1])
        else:
            out.append((op, av))
    return out


def _rx_delimited(body) -> bool:
    """Whether every repetition of `body` ends (or starts) at a character
    none of its variable-length parts can match: a mandatory single
    character with all of the variable parts on ONE side of it — a `.`
    after `\\w+`, a `,` after `[^,]*`, a `\\` before `[^\\\\]+`. Then the
    repetitions cannot trade text, so a cell splits between them only one
    way. Both sides fails: in `(\\w+\\.\\w+)+` the word characters
    between two dots can go to either repetition, 2^n ways. An OPTIONAL
    delimiter (`\\s?`) is itself a variable part and delimits nothing."""
    seq = _rx_sequence(body)
    variable_chars, _ = _rx_variable_chars(body)
    varies = [_rx_variable_chars([item])[1] for item in seq]
    for i, item in enumerate(seq):
        d = _rx_fixed_char(item)
        if d is None or any(d.overlaps(v) for v in variable_chars):
            continue
        if not any(varies[:i]) or not any(varies[i + 1:]):
            return True
    return False


def _rx_fixed_char(item) -> _RxChar | None:
    """The one character a fixed single-character atom matches (bare, or
    `\\.{1}`), or None when `item` is not one."""
    op, av = item
    d = _rx_char(op, av)
    if d is None and op in _RX_REPEATS and av[0] == av[1] >= 1 and len(av[2]) == 1:
        d = _rx_char(*av[2][0])
    return d


def _rx_ambiguous(body) -> bool:
    """Whether one repetition of `body` can match the same text more than
    one way: two variable-length parts that can match the same character
    with nothing fixed between them that the first cannot match
    (`a?[ab]?`, `\\d+\\.?\\d*`, `\\w+x\\w+`). A delimiter does not
    save that — two ways through each repetition is 2^n over n of them."""
    seq = _rx_sequence(body)
    parts = []   # (variable chars, or None for a fixed item; the item)
    for item in seq:
        op, av = item
        if op == _sre_c.BRANCH and any(_rx_ambiguous(alt) for alt in av[1]):
            return True
        if op in _RX_REPEATS and len(_rx_sequence(av[2])) > 1 and _rx_ambiguous(av[2]):
            return True
        varies = _rx_variable_chars([item])[1]
        parts.append((_rx_variable_chars([item], True)[0] if varies else None, item))
    for i, (chars_i, _) in enumerate(parts):
        if chars_i is None:
            continue
        for chars_j, item_j in parts[i + 1:]:
            if chars_j is None:
                sep = _rx_fixed_char(item_j)
                if sep is not None and not any(sep.overlaps(c) for c in chars_i):
                    break   # part i cannot run past this character
                continue
            if any(a.overlaps(b) for a in chars_i for b in chars_j):
                return True
    return False


class _RxUnsupported(Exception):
    """A repeat body _rx_splits cannot model; the structural rules above
    (_rx_delimited, _rx_ambiguous) judge it instead."""


# Counted repeats past this many copies are modelled as E+ (or E* when
# the count can be 0): every count still has exactly one path, so any two
# ways through the original are two ways through the model, and the
# check only gets more cautious. Past _RX_MAX_ATOMS character atoms, or
# _RX_MAX_PAIRS product states, the structural rules decide instead.
_RX_FOLD = 8
_RX_MAX_ATOMS = 160
_RX_MAX_PAIRS = 20000


class _RxNfa:
    """A Thompson automaton for one repeat body: one state per point
    between atoms, an epsilon edge per choice the backtracking engine can
    make (which alternative, one more repetition or stop), a character
    edge per atom. Paths through it are exactly the ways `re` can match,
    which is what makes counting them the right question."""

    def __init__(self):
        self.eps: list[list[int]] = []
        self.chars: list[list[tuple[_RxChar, int]]] = []
        self.atoms = 0

    def state(self) -> int:
        self.eps.append([])
        self.chars.append([])
        return len(self.eps) - 1

    def seq(self, items) -> tuple[int, int]:
        start = cur = self.state()
        for op, av in items:
            a, b = self.item(op, av)
            self.eps[cur].append(a)
            cur = b
        return start, cur

    def item(self, op, av) -> tuple[int, int]:
        ch = _rx_char(op, av)
        if ch is not None:
            self.atoms += 1
            if self.atoms > _RX_MAX_ATOMS:
                raise _RxUnsupported
            s, t = self.state(), self.state()
            self.chars[s].append((ch, t))
            return s, t
        if op == _sre_c.SUBPATTERN:
            return self.seq(av[-1])
        if op in (_sre_c.AT, _sre_c.ASSERT, _sre_c.ASSERT_NOT):
            s = self.state()   # zero-width; a lookaround's own body is _rx_hazard's
            return s, s
        if op == _sre_c.BRANCH:
            s, t = self.state(), self.state()
            for alt in av[1]:
                a, b = self.seq(alt)
                self.eps[s].append(a)
                self.eps[b].append(t)
            return s, t
        if op in _RX_REPEATS:
            lo, hi, body = av
            if lo > _RX_FOLD or (hi != _sre_c.MAXREPEAT and hi > _RX_FOLD):
                lo, hi = min(lo, 1), _sre_c.MAXREPEAT
            s = cur = self.state()
            for _ in range(lo):
                a, b = self.seq(body)
                self.eps[cur].append(a)
                cur = b
            t = self.state()
            if hi == _sre_c.MAXREPEAT:
                loop = self.state()
                a, b = self.seq(body)
                self.eps[cur].append(loop)
                self.eps[loop] += [a, t]   # once more, or stop
                self.eps[b].append(loop)
            else:
                for _ in range(hi - lo):
                    a, b = self.seq(body)
                    self.eps[cur] += [a, t]
                    cur = b
                self.eps[cur].append(t)
            return s, t
        # A backreference, a conditional, a possessive repeat or an atomic
        # group: not something this models.
        raise _RxUnsupported


def _rx_splits(body) -> bool:
    """Whether a repeat of `body` can match some text more than one way
    in a way that compounds — two different paths from one point of the
    loop back to that same point over the same text. That is exponential
    ambiguity: n trips round the loop are 2^n paths, and on a cell that
    fails at the end `re` walks every one of them. Exact, rather than a
    rule about shapes, which matters in both directions:

    - `(\\S+\\s+){3}` and `^(\\S+\\s+)*\\S+$`, the "field N of a command
      line" shapes, pass: \\S and \\s share no character, so every
      boundary is where the text changes from one to the other.
    - `(\\w+\\s?)+` fails: the optional space lets a word end anywhere.
    - `(,a+.\\s+)+` and `(.\\s+[ab]+)+` fail, though the `,` pins each
      repetition of the first and `\\s+` and `[ab]+` share no character
      in the second: the `.` can take the last letter or the first space,
      two ways per repetition.

    The search runs over pairs of states, two paths in step reading the
    same character: a strongly connected set of pairs holding one where
    the paths agree, (p, p), and one where they do not means a second
    way round. Raises _RxUnsupported for what the automaton cannot model,
    and for a body big enough that the pairs would cost more than the
    check is worth."""
    nfa = _RxNfa()
    entry, end = nfa.seq(body)
    nfa.eps[end].append(entry)   # the loop: the next repetition
    n = len(nfa.eps)

    # Epsilon closure with path counts: from each state, the character
    # edges reachable without reading anything, and by how many distinct
    # routes (capped at 2 — one or several is all that matters). A cycle
    # of epsilon edges is a repetition that can match nothing at all,
    # round and round: refused outright.
    order: list[int] = []
    colour = [0] * n
    for root in range(n):
        if colour[root]:
            continue
        stack = [(root, iter(nfa.eps[root]))]
        colour[root] = 1
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                colour[node] = 2
                order.append(node)
                stack.pop()
            elif colour[nxt] == 1:
                return True
            elif colour[nxt] == 0:
                colour[nxt] = 1
                stack.append((nxt, iter(nfa.eps[nxt])))
    closure: list[dict] = [{} for _ in range(n)]
    for node in order:   # successors first
        c = closure[node]
        for k, _edge in enumerate(nfa.chars[node]):
            c[(node, k)] = 1
        for nxt in nfa.eps[node]:
            for key, m in closure[nxt].items():
                c[key] = min(2, c.get(key, 0) + m)

    seen_overlap: dict = {}

    def overlap(a: _RxChar, b: _RxChar) -> bool:
        key = (a.rx.pattern, b.rx.pattern)
        if key not in seen_overlap:
            seen_overlap[key] = a.overlaps(b)
        return seen_overlap[key]

    def step(x: int, y: int):
        """((x', y'), divergent) for the pairs one character on."""
        for e1, m1 in closure[x].items():
            c1, d1 = nfa.chars[e1[0]][e1[1]]
            for e2, m2 in closure[y].items():
                c2, d2 = nfa.chars[e2[0]][e2[1]]
                if overlap(c1, c2):
                    yield (d1, d2), (x == y and e1 == e2 and m1 > 1)

    # Tarjan's strongly connected components over the pairs reachable from
    # (entry, entry), iteratively.
    index: dict = {}
    low: dict = {}
    on_stack: set = set()
    comp_stack: list = []
    divergent: list = []
    counter = 0
    start = (entry, entry)
    work = [(start, None)]
    edges_of: dict = {}
    while work:
        node, parent = work[-1]
        if node not in index:
            index[node] = low[node] = counter
            counter += 1
            if counter > _RX_MAX_PAIRS:
                raise _RxUnsupported
            comp_stack.append(node)
            on_stack.add(node)
            edges_of[node] = iter(list(step(*node)))
        advanced = False
        for target, div in edges_of[node]:
            if div:
                divergent.append((node, target))
            if target not in index:
                work.append((target, node))
                advanced = True
                break
            if target in on_stack:
                low[node] = min(low[node], index[target])
        if advanced:
            continue
        work.pop()
        if parent is not None:
            low[parent] = min(low[parent], low[node])
        if low[node] == index[node]:
            comp = set()
            while True:
                w = comp_stack.pop()
                on_stack.discard(w)
                comp.add(w)
                if w == node:
                    break
            if any(a == b for a, b in comp):
                if any(a != b for a, b in comp):
                    return True
                if any(u in comp and v in comp for u, v in divergent):
                    return True
    return False


def _rx_hazard(items, open_outer: bool = False) -> str | None:
    """Why this parsed pattern can backtrack catastrophically, or None.
    `open_outer`: these items sit inside an open-ended repeat that can be
    backtracked into and that _rx_splits could not model — the one place
    the alternation rule below still has to stand in for it."""
    unbounded = _sre_c.MAXREPEAT
    for op, av in items:
        found = None
        if op in _RX_REPEATS:
            lo, hi, body = av
            exact = False
            if hi > 1:
                # A repeated group whose repetitions can split the same text
                # more than one way. Open-ended, any variable-length part
                # does it (`(a+)+`, `(a{1,2})+`); a bounded count of two or
                # more needs an open-ended one (`(a+){10}`; `(\d{1,3}){3}`
                # is a few dozen ways at most).
                risky = _rx_variable_chars(body)[1] if hi == unbounded else _rx_open_ended(body)
                try:
                    if risky and _rx_splits(body):
                        return "a repeated group that can split the same text more than one way"
                    exact = risky
                except _RxUnsupported:
                    if risky and not _rx_delimited(body):
                        return "a repeated group with nothing marking where one repetition ends"
                    if risky and _rx_ambiguous(body):
                        return "a repeated group whose parts can share out the same text more than one way"
            # A body the automaton passed has had its alternations weighed
            # exactly — (a|a)*b and (a|aa)+ fail it, a keyword list whose
            # words share a first letter does not — so the shape rule below
            # only applies under a repeat it could not model.
            found = _rx_hazard(body, open_outer or (hi > 1 and hi == unbounded and not exact))
        elif _RX_POSSESSIVE is not None and op == _RX_POSSESSIVE:
            found = _rx_hazard(av[2])
        elif _RX_ATOMIC is not None and op == _RX_ATOMIC:
            found = _rx_hazard(av)
        elif op == _sre_c.SUBPATTERN:
            found = _rx_hazard(av[-1], open_outer)
        elif op == _sre_c.BRANCH:
            # Alternatives that can match the same text give every
            # repetition two ways through — `(a|a)*b` is 2^n even though a
            # delimiter would pin the repetitions down, so this one holds
            # regardless of _rx_delimited. It judges by first characters,
            # so it also refuses alternatives that only START alike
            # (`lsadump|logonpasswords`); that is why it is the fallback's
            # rule and not the automaton's.
            if open_outer and _rx_alternatives_overlap(av[1]):
                return "alternatives that can match the same text inside an open-ended repeat"
            for alt in av[1]:
                found = found or _rx_hazard(alt, open_outer)
        elif op in (_sre_c.ASSERT, _sre_c.ASSERT_NOT):
            found = _rx_hazard(av[1])
        elif op == _sre_c.GROUPREF_EXISTS:
            found = _rx_hazard(av[1], open_outer) or (av[2] is not None and _rx_hazard(av[2], open_outer)) or None
        if found:
            return found
    return None


def regex_hazard(pattern: str) -> str | None:
    """Why `pattern` can backtrack catastrophically, as a phrase that
    finishes "… has ___", or None when it can't. The caller compiles it
    first: a pattern that doesn't compile is re.error's to report."""
    return _rx_hazard(_sre_parse.parse(pattern, re.IGNORECASE))
