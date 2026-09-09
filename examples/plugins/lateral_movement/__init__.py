"""Lateral-movement graph plugin for Winnow.

The reference example for the plugin system's **custom UI** hooks — and,
since v2, for `req.storage` (per-plugin workspace persistence) and for
shipping **defaults** the way the app ships filter defaults: a JSON file
of movement-event definitions shaped against real KAPE triage output,
bound to tables by required columns.

A *movement event* is one way a table describes hosts touching hosts:
{src_col, dst_col, label_col?, time_col?, conditions[]}. The graph is
built from any number of (source, event) selections at once — several
event types out of one EVTX export, plus a firewall table, plus a
netflow table, all on one canvas — and every edge row carries a time
bucket so the UI can draw the histogram and brush it without another
query.

The backend goes through Store.run_sql on purpose: it opens its own
read-only connection, so a big aggregation never holds the shared
connection's lock (invariant #4), and it inherits the SQL pane's
defense-in-depth statement checks — and its TS_NORMALIZE() — for free.
"""

import json
import os

from winnow import defaults
from winnow.store import q

PLUGIN = {
    "name": "lateral-movement",
    "version": "2.0.0",
    "description": "Visualize lateral movement over time: multiple movement events across multiple tables on one graph, with defaults for KAPE/EvtxECmd output.",
}

WINNOW_API_VERSION = 1

MAX_ROWS = 8000          # (src, dst, bucket) rows across every selection
MAX_SELECTIONS = 20
_DEFAULTS_FILE = os.path.join(os.path.dirname(__file__), "defaults.json")

OPS = {
    "equals": lambda col, ph: f"{col} = {ph}",
    "not_equals": lambda col, ph: f"{col} != {ph}",
    "contains": lambda col, ph: f"instr(lower({col}), lower({ph})) > 0",
    "not_contains": lambda col, ph: f"instr(lower({col}), lower({ph})) = 0",
}


def register(api):
    api.register_tab(
        id="graph",
        label="Lateral movement",
        entry="ui/tab.js",
        description=(
            "Movement events (logons, RDP, share access, netflow) from any "
            "number of tables on one directed graph, colored by event type, "
            "with a brushable timeline of when each hop happened."
        ),
    )
    api.register_api("edges", edges, methods=["POST"])
    api.register_api("defs", defs, methods=["GET", "POST"])


# ---------------------------------------------------------------- defaults


def _shipped_defaults():
    with open(_DEFAULTS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("events", [])


def _header_sets():
    """The app's shipped header-set nicknames (EvtxECmd, MFTECmd, ...) —
    a movement event ties to one by name, so it only offers itself on a
    table that actually IS that artifact, the same way a filter default
    binds to its header set. {name: [columns]}."""
    return {name: cols for name, cols in defaults.headers()["nicknames"]}


def defs(req):
    """GET  -> {shipped: [...], saved: [...]}
    POST {saved: [...]} -> same — replaces the analyst's saved definitions
    (machine-level, via req.storage; survives case switches and updates,
    exactly like the app's own saved filters)."""
    if req.method == "GET":
        saved = (req.storage.get() if req.storage else {}).get("saved", [])
        return {"shipped": _shipped_defaults(), "saved": saved,
                "header_sets": _header_sets()}
    saved = (req.body or {}).get("saved")
    if not isinstance(saved, list):
        raise ValueError("Body must be {saved: [event definitions]}")
    for d in saved:
        _validate_def_shape(d)
    if req.storage is None:
        raise ValueError("No storage available")
    req.storage.set({"saved": saved})
    return {"shipped": _shipped_defaults(), "saved": saved}


def _validate_def_shape(d):
    if not isinstance(d, dict) or not d.get("name"):
        raise ValueError("Every definition needs a name")
    for k in ("src_col", "dst_col"):
        if not d.get(k):
            raise ValueError(f"Definition {d.get('name')!r} needs {k}")
    hs = d.get("header_set")
    if hs and hs not in _header_sets():
        # Same failure mode as an unresolvable filter header_set — a
        # definition bound to a set that doesn't exist would never appear
        # on any table, silently.
        raise ValueError(f"Definition {d['name']!r} names an unknown header set {hs!r}")
    for c in d.get("conditions") or []:
        if c.get("op") not in set(OPS) | {"in", "not_in"}:
            raise ValueError(f"Unknown condition op {c.get('op')!r}")
        if not c.get("column"):
            raise ValueError("Every condition needs a column")


# ------------------------------------------------------------------ edges


def _derived_names(src):
    return {c["name"] for c in src["columns"] if c.get("derived")}


def _from_clause(src):
    base = f"{q(src['table_name'])} s"
    if not _derived_names(src):
        return base
    return f"{base} LEFT JOIN {q('drv_' + str(src['id']))} d ON d.rid = s.rid"


def _col(src, name):
    if src.get("is_merge"):
        return f"s.{q(name)}"
    return f"{'d' if name in _derived_names(src) else 's'}.{q(name)}"


def _scope(req, src):
    """A plain source is its table (plus derived sidecar); a merge is a
    UNION ALL of its members aliased `s` (invariant #9)."""
    if not src.get("is_merge"):
        return _from_clause(src)
    cols = [c["name"] for c in src["columns"]]
    branches = []
    for mid in src["member_source_ids"]:
        m = req.store.get_source(mid)
        sel = ", ".join(f"{_col(m, c)} AS {q(c)}" for c in cols)
        branches.append(f"SELECT s.rid AS rid, {sel} FROM {_from_clause(m)}")
    return "(" + " UNION ALL ".join(branches) + ") s"


def _lit(value):
    return "'" + str(value).replace("'", "''") + "'"


def _endpoint(expr, strip_prefix):
    """'TargetServerName: FILESRV' -> 'FILESRV' when asked — EVTX payload
    fields carry their field name as a prefix, and an edge to the prefix
    is an edge to noise."""
    if not strip_prefix:
        return expr
    return (f"CASE WHEN instr({expr}, ': ') > 0"
            f" THEN substr({expr}, instr({expr}, ': ') + 2) ELSE {expr} END")


def _conditions_sql(src, conditions):
    parts = []
    names = {c["name"] for c in src["columns"]}
    for c in conditions or []:
        col_name, op, value = c.get("column"), c.get("op"), c.get("value", "")
        if col_name not in names:
            raise ValueError(f"No column {col_name!r} in {src['name']}")
        col = _col(src, col_name)
        if op in ("in", "not_in"):
            vals = [v.strip() for v in str(value).split(",") if v.strip()]
            if not vals:
                raise ValueError(f"'{op}' needs a comma-separated list")
            parts.append(f"{col} {'NOT ' if op == 'not_in' else ''}IN ({', '.join(_lit(v) for v in vals)})")
        elif op in OPS:
            parts.append(OPS[op](col, _lit(value)))
        else:
            raise ValueError(f"Unknown condition op {op!r}")
    return parts


# Bucket sizes the histogram can draw sensibly, chosen from the time span.
_BUCKETS = [("minute", "%Y-%m-%d %H:%M", 60), ("hour", "%Y-%m-%d %H", 3600),
            ("day", "%Y-%m-%d", 86400), ("month", "%Y-%m", 2678400)]


def _pick_bucket(req, spans):
    lo = min((s for s, _ in spans if s), default=None)
    hi = max((e for _, e in spans if e), default=None)
    if not lo or not hi:
        return _BUCKETS[2]
    try:
        import datetime
        span = (datetime.datetime.fromisoformat(hi[:19]) - datetime.datetime.fromisoformat(lo[:19])).total_seconds()
    except ValueError:
        return _BUCKETS[2]
    for name, fmt, size in _BUCKETS:
        if span / size <= 400:
            return (name, fmt, size)
    return _BUCKETS[-1]


def edges(req):
    """POST /api/plugin/lateral_movement/edges
    body: {selections: [{source_id, src_col, dst_col, label_col?, time_col?,
                         strip_prefix?, conditions?: [{column, op, value}]}],
           time?: {start?, end?},   # the case timeframe filter, verbatim
           limit?}
    -> {edges: [{k, src, dst, t?, n, labels?}], bucket, truncated}
    k indexes into `selections` — it's how the UI colors by event type.
    t is the bucket key (present when that selection named a time_col)."""
    if req.store is None:
        raise ValueError("Open a case first")
    b = req.body or {}
    selections = b.get("selections") or []
    if not selections:
        raise ValueError("Pick at least one movement event")
    if len(selections) > MAX_SELECTIONS:
        raise ValueError(f"At most {MAX_SELECTIONS} selections per build")
    time_range = b.get("time") or {}
    start, end = time_range.get("start") or None, time_range.get("end") or None
    limit = min(int(b.get("limit") or MAX_ROWS), MAX_ROWS)

    prepared = []
    spans = []
    for sel in selections:
        try:
            src = req.store.get_source(int(sel.get("source_id")))
        except (TypeError, ValueError, KeyError):
            raise ValueError(f"No source {sel.get('source_id')!r}")
        names = {c["name"] for c in src["columns"]}
        sc_name, dc_name = sel.get("src_col"), sel.get("dst_col")
        label = sel.get("label_col") or None
        time_col = sel.get("time_col") or None
        for col in (sc_name, dc_name) + tuple(x for x in (label, time_col) if x):
            if col not in names:
                raise ValueError(f"No column {col!r} in {src['name']}")
        if sc_name == dc_name:
            raise ValueError("Source and destination must be different columns")
        where = _conditions_sql(src, sel.get("conditions"))
        sc = _endpoint(_col(src, sc_name), sel.get("strip_prefix"))
        dc = _endpoint(_col(src, dc_name), sel.get("strip_prefix"))
        # EVTX "not present" spellings, and loopback — a host talking to
        # itself (127.0.0.1, ::1, LOCAL, '- (127.0.0.1)') is not movement.
        noise = "('', '-', '- (-)', '- (127.0.0.1)', '- (::1)', 'LOCAL', 'localhost', '127.0.0.1', '::1')"
        where += [f"{sc} NOT IN {noise}", f"{dc} NOT IN {noise}", f"{sc} != {dc}"]
        ts = f"TS_NORMALIZE({_col(src, time_col)})" if time_col else None
        if ts and start:
            where.append(f"{ts} >= {_lit(start)}")
        if ts and end:
            where.append(f"{ts} <= {_lit(end)}")
        prepared.append((src, sc, dc, label, ts, where))
        if ts:
            span_sql = f"SELECT min({ts}), max({ts}) FROM {_scope(req, src)} WHERE {' AND '.join(where)}"
            row = req.store.run_sql(span_sql, limit=1)["rows"]
            spans.append((row[0][0], row[0][1]) if row else (None, None))

    bucket_name, fmt, _size = _pick_bucket(req, spans)

    branches = []
    for k, (src, sc, dc, label, ts, where) in enumerate(prepared):
        label_sel = f"COUNT(DISTINCT {_col(src, label)})" if label else "NULL"
        t_sel = f"strftime('{fmt}', {ts})" if ts else "NULL"
        branches.append(
            f"SELECT {k} AS k, {sc} AS src, {dc} AS dst, {t_sel} AS t,"
            f" COUNT(*) AS n, {label_sel} AS labels"
            f" FROM {_scope(req, src)}"
            f" WHERE {' AND '.join(where)}"
            f" GROUP BY 1, 2, 3, 4"
        )
    sql = " UNION ALL ".join(branches) + " ORDER BY n DESC"
    res = req.store.run_sql(sql, limit=limit)
    cols = res["columns"]
    return {
        "edges": [dict(zip(cols, row)) for row in res["rows"]],
        "bucket": bucket_name,
        "truncated": res["truncated"],
    }
