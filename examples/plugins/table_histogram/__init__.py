"""Table histogram — a toolbar panel showing WHEN the rows you're looking
at happened, and a way to narrow to a slice of it by dragging.

The reference example for `register_toolbar_panel`: a toggle button in
the table toolbar (beside search) drops the plugin's own UI in between
the toolbar and the grid. What makes this one useful rather than
decorative is that it follows the grid: the panel subscribes to
`winnow.onViewChange`, so every filter, search, sort, timeframe or table
switch re-queries the histogram against the CURRENT view — the bars
always describe exactly the rows in the table below them. Dragging a
range on the bars writes the case timeframe filter (`setTimeRange`),
which rebuilds the view, which redraws the histogram over the narrowed
range — the loop that makes "zoom into that spike" one gesture.

The counting is core (Store.time_histogram — two aggregate passes on the
reader pool over the view, merge-aware, bucket width picked to fit ~160
bars); this plugin is the route that exposes it and the UI on top.
"""

PLUGIN = {
    "name": "table-histogram",
    "version": "1.0.0",
    "description": "A time histogram of the current table view, between the toolbar and the grid; drag on it to set the timeframe filter.",
}

WINNOW_API_VERSION = 4

MAX_BUCKETS = 160


def register(api):
    api.register_toolbar_panel(
        id="histogram",
        label="Histogram",
        entry="ui/panel.js",
        description=(
            "Shows when the rows in the current view happened, as time buckets "
            "of a datetime column, and follows every filter you apply. Drag a "
            "range on the bars to set the timeframe filter."
        ),
    )
    api.register_api("histogram", histogram, methods=["POST"])


def histogram(req):
    """POST {view_id, column, max_buckets?} — buckets for the CURRENT view.
    A vanished view (the analyst rebuilt it meanwhile) is a 400 the panel
    treats as 'refetch', not an error worth showing."""
    if req.store is None:
        raise ValueError("Open a case first")
    body = req.body or {}
    view_id = body.get("view_id")
    column = body.get("column")
    if not view_id or not column:
        raise ValueError("view_id and column are required")
    try:
        max_buckets = max(20, min(int(body.get("max_buckets") or MAX_BUCKETS), 400))
    except (TypeError, ValueError):
        max_buckets = MAX_BUCKETS
    try:
        return req.store.time_histogram(view_id, column, max_buckets=max_buckets)
    except KeyError as e:
        raise ValueError(f"View expired or unknown column: {e}")
