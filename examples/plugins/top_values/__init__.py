"""Top values — a toolbar panel listing the most common values of one
column for exactly the rows the grid is showing.

The reference example for `register_toolbar_panel`: a toggle button in
the table toolbar drops the plugin's own UI in between the toolbar and
the grid, and the panel follows the grid — it subscribes to
`winnow.onViewChange`, so every filter, search, timeframe or table switch
re-asks for the top values of the CURRENT view. There is no backend here
at all: the app's own `/api/group_summary` already answers "which values,
how often" for a view, so the panel calls it through `winnow.api` the way
the header value picker does. The built-in histogram strip
(static/js/histogram.js) is the same shape with a canvas and a route.
"""

PLUGIN = {
    "name": "top-values",
    "version": "1.0.0",
    "description": "The ten most common values of a column for the rows in the current view, between the toolbar and the grid.",
}

WINNOW_API_VERSION = 9


def register(api):
    api.register_toolbar_panel(
        id="top_values",
        label="Top values",
        entry="ui/panel.js",
        description="The ten most common values of a column for the rows the grid is showing; click one to copy it.",
    )
