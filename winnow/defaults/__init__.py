"""The defaults Winnow ships: header-set nicknames and triage filters.

Both used to be Python modules holding literal data, which meant editing
220 lines of nested dict literals to add a filter and reading a `_eids(...)`
helper to understand one. They are JSON now — the data is data, diffs are
legible, and nothing has to be executed to read it.

**headers.json** names the header sets common forensic tools emit
(EvtxECmd, MFTECmd, Amcache, ...), so an analyst's Nth case opens with the
same tables already labelled. Each set may also carry a `summary` block
saying how to render one of its rows in a line — the Timeline's Body reads
"Special privileges assigned  svc_backup · WKSTN-4471 · id 4672" rather
than the whole source row pipe-joined. See `_summary` for the shape and
why a wrong column name in one is a load-time error. **filters.json** is a working analyst's
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
PROFILES_FILE = HERE / "profiles.json"


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


def _summary(name: str, cols: list[str], rec: dict) -> dict | None:
    """One header set's `summary` block, validated and normalised to
    {"lead": [column, ...], "details": [(column, label), ...]}.

    This is how a Timeline row says what happened instead of printing the
    whole source row: `lead` is the columns that carry the event itself,
    best first (the first non-blank one wins on a given row — EvtxECmd
    leaves MapDescription empty for an event it has no map for, and the
    row still has to read as something), `details` the handful of fields
    that identify the subject and the peer, and `labels` a word in front
    of the ones whose bare value would be a mystery ("id 4624", not
    "4624").

    Every column named here has to be one of this set's own columns, and
    every label has to name one of its own details: a typo would simply
    render nothing on every row of that shape forever, and nobody would
    know to look here. Same reasoning as the filters/header_set binding
    below — fail at load, loudly, rather than silently produce a worse
    Timeline."""
    spec = rec.get("summary")
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise DefaultsError(f"headers.json entry {name!r}: summary must be an object")
    lead = spec.get("lead") or []
    details = spec.get("details") or []
    labels = spec.get("labels") or {}
    if not isinstance(lead, list) or not isinstance(details, list) or not isinstance(labels, dict):
        raise DefaultsError(
            f"headers.json entry {name!r}: summary needs lead/details lists and a labels object")
    known = set(cols)
    for c in [*lead, *details, *labels]:
        if c not in known:
            raise DefaultsError(
                f"headers.json entry {name!r}: summary names column {c!r}, which that "
                f"header set does not define — it would render nothing on every row")
    for c in labels:
        if c not in details:
            raise DefaultsError(
                f"headers.json entry {name!r}: summary labels {c!r}, which is not one of its details")
    if not lead and not details:
        raise DefaultsError(f"headers.json entry {name!r}: summary says nothing")
    return {"lead": list(lead), "details": [(c, labels.get(c, "")) for c in details]}


@lru_cache(maxsize=1)
def headers() -> dict:
    """{"version": int, "nicknames": [(name, [columns]), ...],
    "summaries": {name: {"lead": [...], "details": [(column, label), ...]}}}
    — tuples so callers read the same shape the Python module handed them.

    `summaries` is a separate key rather than a third element of each
    nickname tuple because half the callers do `dict(headers()
    ["nicknames"])`; a shape with a summary but no entry here is simply
    absent from the dict."""
    data = _load(HEADERS_FILE)
    out = []
    summaries = {}
    for i, rec in enumerate(data.get("nicknames") or []):
        name, cols = rec.get("name"), rec.get("columns")
        if not name or not isinstance(cols, list) or not cols:
            raise DefaultsError(f"headers.json entry {i} needs a name and a non-empty columns list")
        out.append((name, list(cols)))
        spec = _summary(name, cols, rec)
        if spec:
            summaries[name] = spec
    if not out:
        raise DefaultsError("headers.json lists no header sets")
    return {"version": int(data.get("version") or 0), "nicknames": out, "summaries": summaries}


@lru_cache(maxsize=1)
def profiles() -> list[dict]:
    """Shipped analysis profiles (plugins + optional watchlist + a
    dashboard). Read-only; surfaced alongside the analyst's saved bundles
    with negative ids. Widget SQL may use {{evtx}}-style placeholders the
    store resolves per case."""
    data = _load(PROFILES_FILE)
    out = []
    for i, rec in enumerate(data.get("profiles") or []):
        if not rec.get("name"):
            raise DefaultsError(f"profiles.json entry {i} needs a name")
        out.append(rec)
    return out


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
