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

from store import q

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
    if source_id < 0:
        # A merge has no single backing table to aggregate — and a movement
        # graph across differently-shaped sources needs per-source column
        # picks anyway. Build it per source.
        raise ValueError("Pick a real table — merges aren't supported here")
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
    label_sel = f", COUNT(DISTINCT {q(label_col)}) AS labels" if label_col else ""
    sql = (
        f"SELECT {q(src_col)} AS src, {q(dst_col)} AS dst, COUNT(*) AS n{label_sel}"
        f" FROM {q(src['table_name'])}"
        f" WHERE {q(src_col)} NOT IN ('', '-') AND {q(dst_col)} NOT IN ('', '-')"
        f" AND {q(src_col)} != {q(dst_col)}"
        f" GROUP BY 1, 2 ORDER BY n DESC"
    )
    res = req.store.run_sql(sql, limit=limit)
    cols = res["columns"]
    return {
        "edges": [dict(zip(cols, row)) for row in res["rows"]],
        "truncated": res["truncated"],
    }
