"""Derived-column operations that read SEVERAL columns of the same row.

Registered into timeparse.OPERATIONS through register_op, the same seam
structparse.py and enrich.py use — a derived column needs the same
materialisation, sidecar and lifecycle whatever produced it. Stdlib only;
imports timeparse and nothing else from the app. store.py imports this
module for the registration side effect.

The first of these is coalesce — "the first of these columns that has
anything in it". Tool output is full of the same fact under three names
(SubjectUserName / TargetUserName / User; SourceIp / IpAddress / Client),
and a filter or a group-by wants one column, not three.
"""

from __future__ import annotations

from .timeparse import register_op


def _coalesce(values: list, params: dict, state: dict) -> str | None:
    """First value that is non-NULL and not whitespace-only, in the order
    the analyst listed the columns. Returned untrimmed: the derived column
    is a copy of what the source held, not a tidied version of it. All
    empty is NULL — not a parse failure (those count non-empty inputs that
    produced nothing), just a row no column had the fact for."""
    for v in values:
        if v is None:
            continue
        s = v if isinstance(v, str) else str(v)
        if s.strip():
            return s
    return None


register_op({
    "id": "coalesce",
    "label": "First non-empty value",
    "description": "The value of the first column in the list that isn't empty — this column first, "
                   "then the others in order. Rows where every listed column is blank stay blank.",
    "params": [{
        "name": "extra_columns", "label": "Then try", "type": "columns", "required": True,
        "help": "Checked in order after the column above. The first one with a value wins.",
    }],
    "value_type": "text",
    "family": "combine",
    "derived_kind": "combine",
    "hidden_from_detect": True,
    "parse": None,
    "parse_multi": _coalesce,
})
