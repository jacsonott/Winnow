"""Lateral-movement graph plugin for Winnow.

The reference example for the plugin system's **custom UI** hooks: a
pinned tab (register_tab) whose content is a plugin-shipped ES module — a
force-directed graph of "who logged on where", drawn on a canvas — fed by
a plugin backend route (register_api) that aggregates edges out of any
ingested source with source/destination columns (4624s from an EvtxECmd
export, firewall logs, netflow, ...).

The backend goes through Store.run_sql on purpose: it opens its own
read-only connection, so a big aggregation never holds the shared
connection's lock (invariant #4), and it inherits the SQL pane's
defense-in-depth statement checks for free.
"""

from winnow.store import q

PLUGIN = {
    "name": "lateral-movement",
    "version": "1.0.0",
    "description": "Visualize lateral movement: a force-directed graph of source→destination pairs from any ingested table.",
}

WINNOW_API_VERSION = 1

MAX_EDGES = 2000


def register(api):
    api.register_tab(
        id="graph",
        label="Lateral movement",
        entry="ui/tab.js",
        description=(
            "Pick a table and its source/destination columns (logon events, "
            "firewall logs, netflow) and see the movement between hosts as a "
            "graph — edge width is event count, node size is degree."
        ),
    )
    api.register_api("edges", edges, methods=["POST"])


def _derived_names(src):
    return {c["name"] for c in src["columns"] if c.get("derived")}


def _from_clause(src):
    """The table plus its derived sidecar — bare q(col) references used to
    miss the sidecar entirely, and SQLite's double-quoted-string fallback
    made a derived src/dst column fail *silently* (one edge named after
    the column) rather than loudly."""
    base = f"{q(src['table_name'])} s"
    if not _derived_names(src):
        return base
    return f"{base} LEFT JOIN {q('drv_' + str(src['id']))} d ON d.rid = s.rid"


def _col(src, name):
    if src.get("is_merge"):
        return f"s.{q(name)}"
    return f"{'d' if name in _derived_names(src) else 's'}.{q(name)}"


def _scope(req, src):
    """A plain source is its table (plus sidecar); a merge is a UNION ALL
    of its members aliased `s` (invariant #9). Same shape as the
    first_last/pivot examples — copied, not imported: plugins are
    deliberately standalone."""
    if not src.get("is_merge"):
        return _from_clause(src)
    cols = [c["name"] for c in src["columns"]]
    branches = []
    for mid in src["member_source_ids"]:
        m = req.store.get_source(mid)
        sel = ", ".join(f"{_col(m, c)} AS {q(c)}" for c in cols)
        branches.append(f"SELECT s.rid AS rid, {sel} FROM {_from_clause(m)}")
    return "(" + " UNION ALL ".join(branches) + ") s"


def edges(req):
    """POST /api/plugin/lateral_movement/edges
    body: {source_id, src_col, dst_col, label_col?, limit?}
    -> {edges: [{src, dst, n, labels?}], truncated}
    """
    if req.store is None:
        raise ValueError("Open a case first")
    b = req.body or {}
    try:
        source_id = int(b.get("source_id"))
    except (TypeError, ValueError):
        raise ValueError("source_id is required")
    try:
        src = req.store.get_source(source_id)
    except KeyError:
        raise ValueError(f"No source {source_id}")

    names = {c["name"] for c in src["columns"]}
    src_col, dst_col = b.get("src_col"), b.get("dst_col")
    label_col = b.get("label_col") or None
    for col in (src_col, dst_col) + ((label_col,) if label_col else ()):
        if col not in names:
            raise ValueError(f"No column {col!r} in {src['name']}")
    if src_col == dst_col:
        raise ValueError("Source and destination must be different columns")

    limit = min(int(b.get("limit") or 500), MAX_EDGES)
    # '' and '-' are how EVTX exports spell "not present" (e.g. 4624s with
    # no workstation name); self-loops say nothing about movement.
    label_sel = f", COUNT(DISTINCT {_col(src, label_col)}) AS labels" if label_col else ""
    sc, dc = _col(src, src_col), _col(src, dst_col)
    sql = (
        f"SELECT {sc} AS src, {dc} AS dst, COUNT(*) AS n{label_sel}"
        f" FROM {_scope(req, src)}"
        f" WHERE {sc} NOT IN ('', '-') AND {dc} NOT IN ('', '-')"
        f" AND {sc} != {dc}"
        f" GROUP BY 1, 2 ORDER BY n DESC"
    )
    res = req.store.run_sql(sql, limit=limit)
    cols = res["columns"]
    return {
        "edges": [dict(zip(cols, row)) for row in res["rows"]],
        "truncated": res["truncated"],
    }
