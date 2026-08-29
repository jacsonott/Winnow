"""Cross-table enrichment operations for derived columns.

`lookup` joins a value in from another table: the input column is the
local key ("the column you are joining on"), and the params name the
other table, its key column, and the column whose value lands here —
VLOOKUP, materialized. The whole mapping is loaded once per backfill
via the op's `prepare` hook (the registry's ops are otherwise pure
per-value functions; prepare is the narrow door for the ones that need
the Store), then each row is a dict hit.

Ties: `multi` decides, per column, at creation — 'first' keeps the
lowest-rid match (deterministic, VLOOKUP-like), 'all' comma-joins every
distinct match in rid order. Chosen by the analyst because both answers
are right for different questions.

Registered as family 'lookup' — its own group in the Add-derived-column
modal, never auto-suggested by format detection."""

from __future__ import annotations

from . import timeparse

# A lookup table bigger than this loaded into a dict is probably a data
# table being misused as a lookup — refuse with the reason rather than
# quietly eating RAM.
MAX_LOOKUP_ROWS = 2_000_000


def _params_ok(params: dict) -> tuple[int, str, str, str]:
    try:
        other = int(params.get("other_source_id"))
    except (TypeError, ValueError):
        raise ValueError("Pick the table to look values up in")
    match_col = (params.get("match_column") or "").strip()
    value_col = (params.get("value_column") or "").strip()
    if not match_col or not value_col:
        raise ValueError("Pick the key column and the value column in the lookup table")
    multi = params.get("multi") or "first"
    if multi not in ("first", "all"):
        raise ValueError("multi must be 'first' or 'all'")
    return other, match_col, value_col, multi


def _check(store, params: dict) -> None:
    """Create-time validation against the case — a bad reference should
    fail the modal, not the backfill job."""
    other, match_col, value_col, _ = _params_ok(params)
    if other < 0:
        raise ValueError("Look up against a real table — merge the result instead of the source")
    try:
        src = store.get_source(other)
    except KeyError:
        raise ValueError(f"No table {other} to look values up in")
    names = {c["name"] for c in src["columns"]}
    for col, label in ((match_col, "key"), (value_col, "value")):
        if col not in names:
            raise ValueError(f"The lookup table {src['name']!r} has no {label} column {col!r}")


def _prepare(store, params: dict, state: dict) -> None:
    from .store import q  # local import — store imports this module at load

    other, match_col, value_col, multi = _params_ok(params)
    src = store.get_source(other)
    mapping: dict[str, str] = {}
    seen: dict[str, set] | None = {} if multi == "all" else None
    with store._reader() as ro:
        n = 0
        for row in ro.execute(
            f"SELECT {store._col_ref(src, match_col)} AS k, {store._col_ref(src, value_col)} AS v "
            f"FROM {store._from_clause(src)} ORDER BY rid"
        ):
            n += 1
            if n > MAX_LOOKUP_ROWS:
                raise ValueError(
                    f"The lookup table has more than {MAX_LOOKUP_ROWS:,} rows — too big to load as a mapping")
            k = "" if row["k"] is None else str(row["k"])
            v = "" if row["v"] is None else str(row["v"])
            if multi == "first":
                mapping.setdefault(k, v)
            else:
                if v not in seen.setdefault(k, set()):
                    seen[k].add(v)
                    mapping[k] = f"{mapping[k]}, {v}" if k in mapping else v
    state["map"] = mapping


def _lookup(value, params: dict, state: dict):
    if value is None:
        return None
    return state.get("map", {}).get(str(value))


timeparse.register_op({
    "id": "lookup",
    "label": "Look up from another table",
    "description": "Join a value in from another table: this column's value is the key; pick the table, its key column, and the value column to bring back.",
    "params": [
        {"name": "other_source_id", "label": "Lookup table", "type": "lookup_source", "required": True},
        {"name": "match_column", "label": "Its key column", "type": "lookup_column", "required": True,
         "help": "The column in the lookup table your values match against."},
        {"name": "value_column", "label": "Value to bring back", "type": "lookup_column", "required": True},
        {"name": "multi", "label": "If several rows match", "type": "select",
         "options": ["first", "all"], "default": "first",
         "help": "first = the earliest match (like VLOOKUP); all = every distinct match, comma-joined."},
    ],
    "parse": _lookup,
    "prepare": _prepare,
    "check": _check,
    "value_type": "text",
    "derived_kind": "text",
    "family": "lookup",
    "stateful": True,
    "hidden_from_detect": True,
})
