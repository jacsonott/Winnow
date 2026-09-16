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

import json
import re

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


# ------------------------------------------------------------- row as JSON

# What is emitted unquoted. Deliberately narrow: a plain integer of at most
# sixteen digits (a hash, a GUID fragment or a 20-digit record id must not
# lose precision in whatever reads the JSON next) and a plain decimal.
# Leading zeros, exponents, hex and signs other than a leading '-' stay
# strings — "007" is a string, and so is "1e5".
_JSON_INT = re.compile(r"^-?(?:0|[1-9]\d{0,15})$")
_JSON_DEC = re.compile(r"^-?(?:0|[1-9]\d{0,15})\.\d{1,12}$")


def _json_value(v):
    if v is None:
        return None
    s = v if isinstance(v, str) else str(v)
    if _JSON_INT.match(s):
        return int(s)
    if _JSON_DEC.match(s):
        return float(s)
    return s


def _row_json(values: list, params: dict, state: dict) -> str | None:
    """The row's columns as one compact JSON object: column names as keys,
    in the order listed; cells that look like plain numbers unquoted;
    NULL as null. With skip_empty, a cell that is NULL or blank (whitespace
    counts) is left out — an object of only what the row has. Never a
    parse failure: even an all-empty row is a (possibly empty) object."""
    names = state.get("inputs") or [f"col_{i + 1}" for i in range(len(values))]
    skip = bool(params.get("skip_empty"))
    obj = {}
    for name, v in zip(names, values):
        if skip and (v is None or not str(v).strip()):
            continue
        obj[name] = _json_value(v)
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


register_op({
    "id": "row_json",
    "label": "Row as JSON",
    "description": "This column and the listed ones as one JSON object per row — column names as keys, "
                   "one line, plain numbers unquoted. For copying a row into a ticket, a note or an LLM.",
    "params": [
        {
            "name": "extra_columns", "label": "Columns", "type": "columns", "required": False,
            "any_type": True, "prefill": "all",
            "help": "After the column above, in this order. Every other column is filled in — remove what you don't want.",
        },
        {
            "name": "skip_empty", "label": "Skip empty cells", "type": "bool", "default": False,
            "help": "Leave out columns whose cell is blank, so the object carries only what the row has.",
        },
    ],
    "value_type": "text",
    "family": "combine",
    "derived_kind": "combine",
    "hidden_from_detect": True,
    "parse": None,
    "parse_multi": _row_json,
})
