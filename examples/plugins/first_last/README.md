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

Drag fields from the list into the zones (or into the description), or click a field (or Tab to it
and press Enter — the keyboard/trackpad path) and pick a zone from the
small **Place in** menu under it. A zone the field can't go into is greyed
out with the reason — "Already there", "Not a number column" (Total up),
"Full — 6 columns at most" (Group rows on) — and Ordered by says which
column it replaces:

| | |
| --- | --- |
| **Group rows on** | Any columns — one group per distinct combination. A column dropped here is **added to Include columns too**, since a result you cannot tell the groups apart in is not one; take it back out of Include columns if you only want it in the description |
| **Ordered by** | One column that defines first/last (defaults to the first datetime column); ties break by file order, deterministically |
| **Include columns** | Carried into the output next to the description, valued from each bookend's own row — drag the chips **or the preview's headers** to set their order. Grouped columns arrive here on their own; removing one here leaves the grouping alone, and dragging a chip from here into *Group rows on* moves it rather than copying it |
| **Total up** | Number columns summed over each group's **whole** set of rows, not just its two bookends — bytes moved across a session, events in a burst. Both bookends of a group carry the same total. A non-number column is refused rather than summed to zero |
| **Count unique** | Any column, as a `Unique count of <column>` output column: how many **distinct** values the group has across its whole set of rows (users seen on a host, hosts a user logged on to), blanks not counted — the same values List values would list, counted. It rides the grouping pass, like Total up |
| **List values** | Any column, listed per group as a `Values of <column>` output column: the group's **distinct** values across its whole set of rows (every user seen on a host, not just the two bookends' users), blanks left out, sorted — a number column by value. A cell names at most 50 and says how many more there were (`… (+12 more)`) |
| **Filters** | Scope which rows participate — same operators as the grid. The value list behind *is any of* / *is none of* has a search box; **All** and **None** act on what the search leaves (so "type `jsmith`, All, Apply" works), and a note says when the list is the 500 most common values rather than every one. The tag list under *Only rows with tags* has the same search |
| **Description** | Free text + placeholders: `{which}` → First/Last (or Only, for a one-row group), `{which:number}` → the row's position in its group instead (1 for the first, the group size for the last, so `{which:number} of {count}` reads `1 of 40` / `40 of 40`), `{count}` → group size, `{Column}` → that row's own value — **any** column of the table, whether or not it is grouped, ordered or included (the bookend query fetches whatever the template names), `{sum:Column}` / `{min:Column}` / `{max:Column}` → a **number** column's group total, smallest and largest, formatted like the Sum column (the column need not be under Total up; they ride the same grouping pass, and only Total up's sums are output columns), `{values:Column}` → the group's distinct values of **any** column, as List values lists them (the column need not be under List values), `{unique:Column}` → how many there are, as Count unique counts them. A new sheet starts with the box empty, showing `{which} of {count}` greyed — what an empty description renders as. Under the box are the **functions**, not the columns: click `{which}`, `{which:number}` or `{count}` to insert it, click `{sum:…}` (or min, max, values, unique) to start one and pick its column from the completion, or **drop a field on one** to fill it in (sum, min and max take only a number column). **Drag a field from the list into the box** and it lands as `{Column}` where you drop it (after a placeholder, never inside one). Or type `{` in the box to complete a name: what follows the brace narrows the list, ↑/↓ pick, Enter or Tab inserts, Escape closes, Ctrl+Space opens it anywhere. A typo'd placeholder fails the preview by name, never ships garbage. The colon form is the namespace for computed values: a plain name is always a field, so a column called `count` or `sum` can't collide with a function |
| **Layout** | Drag the rail's right edge to resize it (remembered on this machine; double-click resets). Drag a preview header's right edge to resize that column (per sheet; double-click fits) |
| **Auto-update** | On by default: the preview re-runs ~350 ms after any change. Off (remembered on this machine), changes mark the preview *Changed — press Refresh* and nothing runs until you do — easier on a very large table |

Several **sheets** live as sub-tabs (like the SQL pane's queries), so two
groupings can sit side by side without saving either. They come back when
the case is reopened: the sheet DEFINITIONS (zones, description, column
widths, which sheet was on top) are kept in the case file through
`winnow.tabState`, and the rows are re-run against the case as it is then
— a preview from three weeks ago would be a picture of the evidence rather
than the evidence. A line at the top says the sheets were restored, when
they were saved, and offers **Start fresh**; a sheet whose table or columns
are gone restores without them and names what it dropped. The preview's rows
select like a table tab's — click, Shift extends, Ctrl toggles — and
Ctrl+C copies the selection as TSV.

A one-event group emits a single row whose `{which}` is `Only` — a story
with one event has no separate ending, and `First of 1` read as if a Last
had gone missing.

Totals and unique counts ride the grouping pass that already counts each
group (the same one behind `{count}`), so asking for them costs no extra
pass over the table. Values that do not parse as numbers are skipped rather than read as
zero, so one `n/a` in a byte column cannot quietly deflate a total.

## Output

`[ordering column] [included columns…] [Sum of…] [Unique count of…] [Values of…] [Description] [Row (JSON)]`
— two rows per group. The description reads before the whole-row JSON
cell: it is the line, the JSON is the long cell opened when needed.
Three ways out, on the bar's right edge:

- **Copy result** — the *entire* result (not just the preview) as TSV to
  the clipboard, capped at 20,000 rows.
- **Create table…** — lands it via `ingest_rows`, so the result follows
  every ingest convention and the source table is never touched
  (invariant #1). It is named `First-Last of <table>` by default, using the
  table's nickname when it has one — the table picker shows nicknames too.
- **…with a timeline tag** — the create dialog can tag every row of the
  new table under a name you choose. The unified Timeline is exactly
  "every tagged row across the case", so that tag is what puts the
  bookends on it (and the write goes through the undoable tag path).

## Working in the background

On a big table a preview, a **Copy result** or a **Create table…** can take
a while — seconds per preview on a million rows. None of it holds up the
rest of Winnow: switch to a table, scroll, filter or search while it runs.

- **Preview.** Each change starts a fresh preview and stops the one it
  replaced on the server, so a run of quick edits never piles up work.
  Leave the tab while one runs and a card in the jobs panel says it is
  still going; when it lands the card reads `N groups — the preview is
  ready`, and **Show** brings you back to that sheet. Coming back to the
  tab yourself clears the cards, since the tab now says what they said.
- **Copy result.** The button reads *Copying…* while the result is worked
  out. Still on the tab when it lands, it goes straight to the clipboard;
  elsewhere, a card offers **Copy** instead, so nothing overwrites a
  clipboard you have used since.
- **Create table….** The dialog closes as soon as you press *Create
  table*, and a `Creating "<name>"` card carries the work, with
  **Cancel**. A quick create opens the new table as it always has; a slow
  one, or one that lands while you are elsewhere, leaves you where you
  are and turns its card into `Created "<name>"` with **Open table**.

## Files

```
first_last/
├── __init__.py   routes: meta, values, preview, rows, create,
│                 job/start, job, job/cancel (what the tab calls)
├── ui/tab.js     the tab
└── README.md
```

Backend tests live in `tests/test_plugins.py` (`-k first_last`),
`tests/test_firstlast_timeline.py`, `tests/test_firstlast_values.py`,
`tests/test_firstlast_unique.py` (Count unique) and
`tests/test_firstlast_jobs.py` (the background jobs); the
tab itself is driven by `tests/ui/test_firstlast_rework.py`,
`tests/ui/test_firstlast_autocomplete.py`,
`tests/ui/test_firstlast_functions.py` (the description's functions and drops),
`tests/ui/test_firstlast_filter_search.py` and
`tests/ui/test_firstlast_values_and_nicknames.py`, the saved sheets by
`tests/ui/test_firstlast_state.py`, and the work in the background by
`tests/ui/test_firstlast_background.py`.
