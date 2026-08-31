"""The defaults Winnow ships: header-set nicknames and triage filters.

Both used to be Python modules holding literal data, which meant editing
220 lines of nested dict literals to add a filter and reading a `_eids(...)`
helper to understand one. They are JSON now — the data is data, diffs are
legible, and nothing has to be executed to read it.

**headers.json** names the header sets common forensic tools emit
(EvtxECmd, MFTECmd, Amcache, ...), so an analyst's Nth case opens with the
same tables already labelled. **filters.json** is a working analyst's
Timeline Explorer triage set, converted to filter trees.

The one thing the Python version got for free was the binding between
them: a filter's column list *was* the header set's list, the same object,
so they could not drift. In JSON a filter names its set (`header_set`) and
this module resolves it — which is stronger, because an unresolvable name
fails loudly here and in tests/test_filter_defaults.py, where the old
version would only have failed if someone happened to mistype a Python
identifier.

Both files carry a `version`. Bump it when you add entries: workspace's
`ensure_seeded` re-seeds only sets not already present, so an analyst's
renamed or deleted copy is never re-added beside itself. Never edit an
existing header set in place — files produced by an older release of the
tool are still out there and still have to match.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
HEADERS_FILE = HERE / "headers.json"
FILTERS_FILE = HERE / "filters.json"


class DefaultsError(RuntimeError):
    """A shipped defaults file is missing or malformed. Not an analyst's
    problem to solve, so it says which file and what is wrong with it."""


def _load(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError as e:
        raise DefaultsError(f"{path.name} is missing from this install ({e})") from e
    except ValueError as e:
        raise DefaultsError(f"{path.name} is not valid JSON ({e})") from e


@lru_cache(maxsize=1)
def headers() -> dict:
    """{"version": int, "nicknames": [(name, [columns]), ...]} — tuples so
    callers read the same shape the Python module handed them."""
    data = _load(HEADERS_FILE)
    out = []
    for i, rec in enumerate(data.get("nicknames") or []):
        name, cols = rec.get("name"), rec.get("columns")
        if not name or not isinstance(cols, list) or not cols:
            raise DefaultsError(f"headers.json entry {i} needs a name and a non-empty columns list")
        out.append((name, list(cols)))
    if not out:
        raise DefaultsError("headers.json lists no header sets")
    return {"version": int(data.get("version") or 0), "nicknames": out}


@lru_cache(maxsize=1)
def filters() -> dict:
    """{"version": int, "filters": [(name, [columns], payload), ...]}.

    `header_set` is resolved to that set's column list here, so a filter
    and the table it targets cannot describe different columns."""
    data = _load(FILTERS_FILE)
    by_name = dict(headers()["nicknames"])
    out = []
    for i, rec in enumerate(data.get("filters") or []):
        name, hs, payload = rec.get("name"), rec.get("header_set"), rec.get("payload")
        if not name or not payload:
            raise DefaultsError(f"filters.json entry {i} needs a name and a payload")
        cols = by_name.get(hs)
        if cols is None:
            raise DefaultsError(
                f"filter {name!r} targets header set {hs!r}, which headers.json "
                f"does not define — a filter bound to nothing would never apply")
        out.append((name, list(cols), payload))
    return {"version": int(data.get("version") or 0), "filters": out}
