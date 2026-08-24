# Pivot table

Excel's PivotTable, over any ingested Winnow table. Drag fields into
**Rows**, **Columns**, **Values** and **Filters**; get a cross-tab with
subtotals and grand totals; click a cell to see the rows behind it.

Install from **Settings → Plugins → "Install a plugin folder…"**, or:

```bash
cp -r examples/plugins/pivot plugins/
```

Then pick the **Pivot** page tab.

## What it does

- **Four field areas**, like Excel's. Drag a field from the list, or click
  it and pick an area (drag isn't reachable from a keyboard).
- **Nested rows and columns.** Several fields in Rows nest, with a subtotal
  after each group; several in Columns produce stacked column headers.
- **Aggregations**: Count, Distinct count, Sum, Average, Min, Max. One
  field can appear in Values more than once — "Count of Host" beside
  "Distinct count of Host" is a normal thing to want.
- **Filters** with the same vocabulary as the grid's header filter box
  (is any of / is none of / contains / starts with / >, < / is empty).
- **Click any cell** for the underlying rows.
- **Copy** (TSV — pastes straight into a spreadsheet) and **CSV** download.
- Sort the rows by any value column: click its header, again to reverse,
  a third time for key order.

## Three things it does deliberately

**It pivots the whole table, not the grid's current view.** A view lives in
Winnow's scratch database, which the supported plugin read path
(`Store.run_sql`, a read-only connection to the case file) can't see. So the
Filters area is the pivot's own filtering — which is how Excel works anyway.

**Subtotals are queried, not summed from the cells above them.** Add up the
per-host distinct user counts and you'll double-count anyone who appears on
two hosts. Each nesting level is aggregated from the source independently,
so every aggregation is right rather than just the associative ones. It
costs one `GROUP BY` per level; the level count is capped.

**Count means Excel's COUNTA.** "Count of Bytes" is how many rows *have* a
Bytes value, not how many rows exist — otherwise putting a specific field in
Values would tell you nothing. Sum, Average and numeric Min/Max go through
the same guarded cast the rest of Winnow uses, so a stray `N/A` in a numeric
column becomes NULL and drops out, instead of being counted as a real zero.

## Derived columns

Columns you added yourself (`Add datetime column from this…`) work
everywhere the file's own columns do — rows, columns, values, filters and
the drill-down. They live in the `drv_<id>` sidecar rather than `src_<id>`,
so the queries join it and qualify every reference. Naming one without that
join doesn't fail: SQLite's double-quoted-string fallback turns `"Day"` into
the *string* `'Day'`, and the pivot quietly reports one group named after
the column.

## Limits

| | |
| --- | --- |
| Grouping levels per refresh | 16 |
| Groups per level | 20,000 |
| Column groups rendered | 60 |
| Drill-down rows | 500 |

Merged sources aren't supported — a merge has no single backing table to
aggregate. Pivot the members individually.

## Files

```
pivot/
├── __init__.py     PLUGIN, register(), and the four routes
├── ui/tab.js       the tab: field areas, the cross-tab, drill-down, export
└── README.md
```

Backend tests live with Winnow's own suite, in `tests/test_plugins.py`
(`-k pivot`).
