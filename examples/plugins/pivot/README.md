# Pivot table

Excel's PivotTable, over any ingested Winnow table. Drag fields into
**Rows**, **Columns**, **Values** and **Filters**; get a cross-tab with
subtotals and grand totals; click a cell to see the rows behind it.

It ships with Winnow, switched off: turn it on in **Settings → Plugins →
Manage plugins…**. To run a modified copy instead, copy this folder into
`plugins/` (or use **Install a plugin… → Pick a plugin folder…** there);
a copy in `plugins/` replaces the bundled one.

Then pick the **Pivot** page tab.

## What it does

- **Four field areas**, like Excel's. Drag a field from the list, or click
  it (or Tab to it and press Enter — drag isn't reachable from a
  keyboard) and pick an area from the small **Place in** menu under it.
  An area the field is already in is greyed out ("Already there"), and
  Rows/Columns say when picking one moves the field out of the other.
- **Nested rows and columns.** Several fields in Rows nest, with a subtotal
  after each group; several in Columns produce stacked column headers.
- **Aggregations**: Count, Distinct count, Sum, Average, Min, Max. One
  field can appear in Values more than once — "Count of Host" beside
  "Distinct count of Host" is a normal thing to want.
- **Filters** with the same vocabulary as the grid's header filter box
  (is any of / is none of / contains / starts with / >, < / is empty).
- **Tag chips** at the top of the Filters area — the grid's tag ribbon, with
  the same clicks: click a tag to count only rows carrying it (two tags mean
  rows carrying either), right-click it for **Exclude**, and **Any tag** /
  **No tags** for everything tagged or everything not looked at yet. Each
  chip shows how many rows of the pivot's table carry it. Coming back to the
  tab from the grid re-runs a pivot filtered on tags, since rows may have
  been tagged in the meantime.
- **Click any cell** for the underlying rows.
- **Copy** (TSV — pastes straight into a spreadsheet) and **CSV** download.
- Sort the rows by any value column: click its header, again to reverse,
  a third time for key order.
- **The pivots come back when the case is reopened.** Their DEFINITIONS
  (table, field areas, filters and tag filter, subtotal switches, which pivot was on top)
  are kept in the case file through `winnow.tabState`; the cross-tab itself
  is re-queried against the case as it is then. A line at the top says they
  were restored, when they were saved, and offers **Start fresh** — and a
  pivot whose table or columns are gone restores without them and names
  what it dropped.

## Three things it does deliberately

**It pivots the whole table, not the grid's current view.** A view lives in
Winnow's scratch database, which the supported plugin read path
(`Store.run_sql`, a read-only connection to the case file) can't see. So the
Filters area is the pivot's own filtering — which is how Excel works anyway.
Tags are the exception that isn't one: they live in the case file
(`row_tags`), so the tag chips compile into the same query as everything
else, in the grid's own spelling — a tag id, `"!<id>"` for an exclusion,
`"__any__"` and `"__none__"` — and select the rows the grid's ribbon would.

**Subtotals are worked out, not summed from the cells above them.** Add up
the per-host distinct user counts and you'll double-count anyone who appears
on two hosts. Every nesting level is its own grouping set, and all of them
come from one pass over the table: the rows are grouped once by every field
in Rows and Columns, keeping counts, sums, minimums and maximums that do add
up (an average as its sum and its count), and each level is grouped from
that. Distinct count is the one that doesn't add up, so its field is kept in
that first grouping and each level counts the distinct values under it. So
every aggregation is right, not just the associative ones, and the table is
read once per refresh rather than once per level; the level count is
capped.

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

Merged tables work too: a merge is read as the union of its member
tables, and its tag filter matches each row through the member it came
from, since that is where a merged row's tags live.

## Files

```
pivot/
├── __init__.py     PLUGIN, register(), and the four routes
├── ui/tab.js       the tab: field areas, the cross-tab, drill-down, export
└── README.md
```

Backend tests live with Winnow's own suite, in `tests/test_plugins.py`
(`-k pivot`); the tab itself is driven by `tests/ui/test_pivot_tags.py`
(the tag chips), `tests/ui/test_pivot_state.py` (the saved pivots),
`tests/ui/test_pivot_refresh.py` (the table kept, dimmed, while the next
one is worked out) and `tests/ui/test_field_place_menu.py` (the **Place
in** menu, shared with First/Last).
