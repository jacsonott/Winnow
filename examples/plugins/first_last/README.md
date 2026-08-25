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

| | |
| --- | --- |
| **Group by** | Any columns — one group per distinct combination |
| **ordered by** | The column that defines first/last (defaults to the first datetime column); ties break by file order, deterministically |
| **Only these rows** | Filters scoping which rows participate — same operators as the grid |
| **Description** | Free text + placeholders: `{which}` → First/Last, `{count}` → group size, `{Column}` → that row's own value. Click a chip to insert. A typo'd placeholder fails the preview by name, never ships garbage |
| **Columns to include** | Carried into the output next to the description, valued from each bookend's own row |

A one-event group emits a single row labelled `First` — a story with one
event has no separate ending.

## Output

`[ordering column] [included columns…] [Description]` — two rows per group,
via `ingest_rows`, so the result follows every ingest convention and the
source table is never touched (invariant #1). Preview shows the first few
groups and the total before you commit.

## Files

```
first_last/
├── __init__.py   routes: meta, values, preview, create
├── ui/tab.js     the tab
└── README.md
```

Backend tests live in `tests/test_plugins.py` (`-k first_last`).
