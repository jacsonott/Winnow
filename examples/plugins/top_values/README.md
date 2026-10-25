# Top values

A toolbar panel: click **Top values** in the table toolbar and the ten
most common values of a column — for exactly the rows the grid is
showing — drop in between the toolbar and the grid.

- **It follows the grid.** Every filter, search, timeframe or table
  switch re-asks for the top values of the current view, so the list
  always describes the rows in the table below it.
- **Pick the column.** The dropdown lists the open table's columns
  (derived ones included); it starts on the first.
- **Click a value to copy it** — for a filter box, a search, a report.
- No backend of its own: the counts come from Winnow's `/api/group_summary`,
  the same query the header value picker runs, scoped to the view.

Enable it in **Settings → Plugins** (it ships with Winnow, off by
default). The state of the toggle is remembered per browser.

## Files

```
top_values/
├── __init__.py    register_toolbar_panel — a dozen lines of code, no routes
├── ui/panel.js    the panel: column picker, the list, copy on click
└── README.md
```

This is the reference example for `register_toolbar_panel` — see
docs/writing-plugins.md, *Hook: toolbar panels*. The built-in histogram
strip (`static/js/histogram.js`) is the same shape with a canvas and a
route of its own. Tests: `tests/test_top_values_plugin.py`,
`tests/ui/test_top_values_panel.py`.
