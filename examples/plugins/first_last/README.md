# First/Last

Group a table's events and keep each group's bookends: the **first** and
**last** row per group, ordered by a column you choose, each carrying a
templated description and whichever columns you want alongside — landed as
a **new, ordinary Winnow table** you can tag, filter, put on the Timeline
and export.

The classic move: 40,000 logon events → group by `Host` + `User` → 200
session bookends reading

```
First of 312 | WKSTN-014 | user: jsmith
Last of 312 | WKSTN-014 | user: jsmith
```

Enable it in **Settings → Plugins** (it ships with Winnow, off by default),
then open the **First/Last** page tab.

## Controls

Drag fields from the list into the zones (clicking a field offers the same
placements — the keyboard/trackpad path):

| | |
| --- | --- |
| **Group rows on** | Any columns — one group per distinct combination |
| **Ordered by** | One column that defines first/last (defaults to the first datetime column); ties break by file order, deterministically |
| **Include columns** | Carried into the output next to the description, valued from each bookend's own row — drag the chips **or the preview's headers** to set their order |
| **Total up** | Number columns summed over each group's **whole** set of rows, not just its two bookends — bytes moved across a session, events in a burst. Both bookends of a group carry the same total. A non-number column is refused rather than summed to zero |
| **Filters** | Scope which rows participate — same operators as the grid |
| **Description** | Free text + placeholders: `{which}` → First/Last (or Only, for a one-row group), `{count}` → group size, `{Column}` → that row's own value, `{sum:Column}` → a Total-up column's group total, formatted like the Sum column. Click a chip to insert. A typo'd placeholder fails the preview by name, never ships garbage. The colon form is the namespace for computed values: a plain name is always a field, so a column called `count` or `sum` can't collide with a function |
| **Auto-update** | On by default: the preview re-runs ~350 ms after any change. Off (remembered on this machine), changes mark the preview *Changed — press Refresh* and nothing runs until you do — easier on a very large table |

Several **sheets** live as sub-tabs (like the SQL pane's queries), so two
groupings can sit side by side without saving either. The preview's rows
select like a table tab's — click, Shift extends, Ctrl toggles — and
Ctrl+C copies the selection as TSV.

A one-event group emits a single row whose `{which}` is `Only` — a story
with one event has no separate ending, and `First of 1` read as if a Last
had gone missing.

Totals ride the window that is already partitioned by the group (the same
one behind `{count}`), so asking for them costs no extra pass over the
table. Values that do not parse as numbers are skipped rather than read as
zero, so one `n/a` in a byte column cannot quietly deflate a total.

## Output

`[ordering column] [included columns…] [Description]` — two rows per group.
Three ways out, on the bar's right edge:

- **Copy result** — the *entire* result (not just the preview) as TSV to
  the clipboard, capped at 20,000 rows.
- **Create table…** — lands it via `ingest_rows`, so the result follows
  every ingest convention and the source table is never touched
  (invariant #1).
- **…with a timeline tag** — the create dialog can tag every row of the
  new table under a name you choose. The unified Timeline is exactly
  "every tagged row across the case", so that tag is what puts the
  bookends on it (and the write goes through the undoable tag path).

## Files

```
first_last/
├── __init__.py   routes: meta, values, preview, rows, create
├── ui/tab.js     the tab
└── README.md
```

Backend tests live in `tests/test_plugins.py` (`-k first_last`) and
`tests/test_firstlast_timeline.py`; the tab itself is driven by
`tests/ui/test_firstlast_rework.py`.
