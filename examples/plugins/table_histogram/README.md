# Table histogram

A toolbar panel: click **Histogram** in the table toolbar (beside the
search icon) and a histogram of *when the rows in the current view
happened* drops in between the toolbar and the grid.

- **It follows the grid.** Every filter, search, sort, timeframe or
  table switch re-queries the histogram against the current view, so
  the bars always describe exactly the rows in the table below them.
- **Drag to narrow.** Drag a range across the bars and it becomes the
  case **timeframe filter** (the same ⏱ filter in the toolbar) — the view
  rebuilds and the histogram redraws over the slice. *Clear* in the panel
  removes it again.
- **Pick the column.** The dropdown lists the table's datetime columns
  (derived ones included); it defaults to the timeframe filter's column,
  else the first datetime column.
- Bucket width is chosen automatically (seconds up to years) so the span
  fits in ~160 bars, aligned to clock boundaries; the panel says which.

Enable it in **Settings → Plugins** (it ships with Winnow, off by
default). The state of the toggle is remembered per browser.

## Files

```
table_histogram/
├── __init__.py    register_toolbar_panel + the `histogram` route (Store.time_histogram)
├── ui/panel.js    the panel: canvas bars, brush, column picker
└── README.md
```

This is the reference example for `register_toolbar_panel` — see
docs/writing-plugins.md, *Hook: toolbar panels*. Tests:
`tests/test_table_histogram_plugin.py`, `tests/ui/test_table_histogram.py`.
