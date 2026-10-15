# SQL pane

The SQL pane runs read-only SQLite queries against the open case file. It is
for the questions a filter can't answer: a count per user, a join between
two tables, every tagged row with its note. Your tables appear as you see
them in the grid, with your derived columns, tags and notes, and no query
you run there can change the case.

## Opening the SQL pane

Click **SQL** in the page tabs at the top of the window. If you turned on
**Settings → Appearance → Pages as a dropdown**, it is in the **Pages ▾**
menu instead. If you closed the tab, reopen it from the **Pages** section of
the tables sidebar.

The first time you open the pane in a case it holds one query tab, **Query
1**, filled in to show the first 50 rows of the table you had open:

```sql
-- Security.csv (src_1)
SELECT * FROM src_1 LIMIT 50;
```

From a table, `Q` (Shift+Q) opens the table's current filter, sort and
search as a `SELECT` in a new query tab, named **Filter:** and the table's
name. Use it when a filter has got you most of the way and the next step
needs a `GROUP BY` or a join.

## Running a query

Type the query and click **Run**, or press `Ctrl+⏎` (`⌘⏎` on a Mac). The
pane runs one statement at a time; two statements separated by `;` give
*You can only execute one statement at a time.* Drag the bar under the query
box to make it taller or shorter.

Until something has run in a tab, the results area offers up to three
starter queries for the table you had open: its first 50 rows, the rows you
tagged in it (when it has tagged rows), **Rows per table** (when the case has
more than one table) and the most common values of one of its columns. Each
button names the table and column it uses. Hover over one to see its SQL;
click it to run it. A starter fills the current tab when the tab is empty or
holds another starter, and otherwise opens in a new tab, so it never
overwrites a query you were writing.

## What read-only means

The pane opens the case file read-only and allows only reading. A statement
that would change anything, such as `INSERT`, `UPDATE`, `DELETE`, `CREATE` or
`DROP`, fails with *not authorized*. `ATTACH`, `DETACH`, `PRAGMA` and
`VACUUM` are refused before the query runs, with *ATTACH, DETACH, PRAGMA and
VACUUM aren't allowed in the SQL pane*; the words only count as SQL, so
`WHERE Host = 'pragma'` is fine. `WITH` (including `WITH RECURSIVE`) and
`EXPLAIN QUERY PLAN` work.

Your queries never write. The pane itself saves the text of your query tabs,
and changes the case only when you ask it to: **Save as table…** adds a
table, and tagging rows in a result tags them.

## Query tabs

Each query lives in its own tab above the editor. Tabs are saved in the case
file as you type, so they are there when you reopen the case and they travel
with the case file. A session file does not carry them.

| To | Do this |
| --- | --- |
| Add a query | Click **+**. New tabs are named **Query 2**, **Query 3** and so on |
| Rename one | Double-click or right-click the tab, then type the name at **Query name:** |
| Reorder | Drag a tab along the strip |
| Close one | Click its **✕**. A tab with a query in it asks first: *Close "…"? Its query is deleted from the case file.* The last tab can't be closed |

The last result of each tab stays on screen while the case is open, so
switching tabs doesn't lose it. Results are not saved in the case file; run
the query again after reopening the case.

## Finding your tables

A table's SQL name is `src_` plus its id, and a merged table's is `merge_`
plus the merge's id. The name in the sidebar is not used in SQL. To find the
right one:

- Click **Tables ▾**. It lists every table as *name — src_N (N rows)*, and
  clicking one inserts its SQL name at the cursor.
- Start typing the table's name or `src_`: autocomplete offers *name — src_N*.
- Query the `sources` and `merges` tables (see
  [Which table is which](#which-table-is-which)).

**Copy schema** copies a description of every table (names, `src_N`
mapping, columns and their types) to the clipboard, ready to paste into
notes or a chat with an LLM. It leaves merged tables out.

### Autocomplete

Autocomplete opens once you have typed two characters of a word, or at any
time with `Ctrl+Space`. It offers table names, the columns of the tables the
query already names (or of the table open in the grid, before you have named
one), **Tags** and **Note**, and common SQL keywords and functions. Each
suggestion is labelled *table*, *column*, *derived* (one of your derived
columns), *winnow* (Tags and Note) or *keyword*. Use the arrow keys to move,
`Tab` or `Enter` to accept, `Esc` to close.

## Tables and columns you can query

| Name | What it holds |
| --- | --- |
| `src_N` | One per table, as you see it in the grid: `rid`, the file's columns, your [derived columns](Derived-columns.md), then **Tags** and **Note** |
| `merge_N` | One per merged table: `source_id`, `rid`, the merge's columns, **Tags** and **Note**. A derived column appears here only if every member table has it |
| `main.src_N` | The raw import of table N: `rid` and the file's columns, with no derived columns, Tags or Note |
| `sources` | One row per table: `id`, `name` (the imported file's name), `nickname` (the name you gave it, if any), `row_count`, `columns` (JSON) |
| `merges` | One row per merged table: `id`, `name`, `source_ids` (a JSON list of its member table ids) |
| `tag_defs` | One row per tag: `id`, `name`, `color`, `hotkey` |
| `row_tags` | One row per tag on a row: `source_id`, `rid`, `tag_id` |
| `row_notes` | One row per row note: `source_id`, `rid`, `note` |
| `case_notes` | The text of the Notes page, in `body`, and when it was last saved, in `updated_at` |
| `derived_columns` | The definition of each derived column: `source_id`, `name`, `input_column`, `op_id`, `params`, `status` |

The case file's other bookkeeping tables can be read too; `SELECT name FROM
sqlite_master` lists them all.

Points that catch people out:

- **`rid` is the row's number within its table**, starting at 1 in every
  table. The same `rid` in two tables means two different rows, so a join to
  `row_tags` or `row_notes` needs `source_id` as well as `rid`. In a
  `merge_N`, use `source_id` and `rid` together for the same reason.
- **Tags** is the row's tag names joined with `, ` (for example `TA,
  Suspicious`), and **Note** is its row note. They are a convenience;
  `Tags LIKE '%Suspicious%'` also matches a tag named *Not Suspicious*, so
  use `row_tags` and `tag_defs` for an exact tag. If the file already has a
  column called Tags or Note, the pane's ones are named **Winnow Tags** and
  **Winnow Note**.
- **Every value is stored as text.** The number or date type Winnow gives a
  column is only a label. `EventId = 4624` and `EventId = '4624'` both
  match, but ordering compares text: `LogonType > 5` finds no logon type 10,
  because `'10'` sorts before `'5'`. Use `CAST(LogonType AS INTEGER) > 5`,
  and cast in `ORDER BY`, `MIN` and `MAX` too. Derived datetime columns are
  written as `YYYY-MM-DD HH:MM:SS`, so they compare and sort correctly as
  they are.

### Functions Winnow adds

| Function | What it does |
| --- | --- |
| `x REGEXP 'pattern'` | True when the pattern matches anywhere in `x`. Python regular-expression syntax, case-insensitive. A pattern that can backtrack catastrophically is refused before the query runs, as in the [search box](Filtering-and-searching.md#regular-expressions-that-are-refused) |
| `DAY_BUCKET(x)` | The calendar day, `YYYY-MM-DD`, of a timestamp written ISO-style (`2024-01-05 13:22:01`), US-style (`1/5/2024 1:22 PM`) or with a month name (`Jan 5, 2024`, `5th Jan 2024`). NULL for anything else, including epoch numbers |
| `TS_NORMALIZE(x)` | The same three shapes as `YYYY-MM-DD HH:MM:SS`, so they sort and compare correctly. It reads the time as written: a `+02:00` or `Z` on the end is ignored, not converted |

## Results

The result appears as a table under the editor. The line above it reads
*N rows · M ms*, followed by *tags joined via rid* when the rows can be
tagged (below). That line and the buttons beside it stay in place while
you scroll the result, down or across.

**The pane has no row limit.** While a query runs, the result area reads
*Running… N rows so far*. Winnow keeps the result on disk rather than in
the browser, and fetches the rows as you scroll to them, so a result of
millions of rows scrolls like a small one; rows that are still being
fetched show empty for a moment. The disk is the temporary folder: if it
runs low, the query stops early and the status line says *stopped — the
disk Winnow keeps results on is nearly full*. A result is kept until you
run the tab's query again or close the tab; after a long session the
oldest results are let go, and the status line says *this result is no
longer held — run the query again* if you scroll back into one.

Click a column header to sort the whole result by that column; click again
to reverse. Sorting doesn't run the query again. Numbers sort as numbers,
including numbers stored as text, and NULLs come first.

### Tagging rows from a result

A result that names real rows gets a live **Tags** column, and its rows can
be selected and tagged. That needs either a `rid` column and a query that
reads exactly one `src_N`, or both a `source_id` and a `rid` column (as a
`merge_N` gives). A `GROUP BY` result names no rows, so none of this applies
to it.

| To | Do this |
| --- | --- |
| Select a row | Click it. Click the only selected row again to clear it |
| Add or remove one row | `Ctrl`+click |
| Select a range | `Shift`+click |
| Tag the selected rows | Press the tag's key, `1` to `9` ([tag keys](Tagging-and-notes.md#tag-keys)). If the first selected row already has the tag, the key removes it from all of them |
| Copy the selected rows | `Ctrl+C`, as tab-separated text with a header row |
| Clear the selection | `Esc` |
| Open a row in its table | Double-click it |

Tagging here is the same as tagging in the grid: the rows' tags change in
their tables, the counts update, and the change can be
[undone](Tagging-and-notes.md#undo).

## Stopping a runaway query

A query has no time limit, because a large `GROUP BY` can rightly take
minutes. While it runs, **Stop** sits beside the row count; if it runs for
more than about a second, a **✕ Cancel** chip appears as well, and either
one stops it. A query stopped after it had returned rows keeps them: they
are the result, and the status line adds *stopped — these are the rows it
had returned*. One stopped before it returned any says *Query cancelled*.
Running the query again in the same tab, closing its tab, opening another
case or closing the Winnow window also stop it.

## Exporting results

The buttons on the right of the status line act on every row of the
result, in the order you sorted it, not only the rows you have scrolled
past.

| Button | What it does |
| --- | --- |
| **Copy** | Copies the selected rows, or the whole result when none are selected, as tab-separated text with a header row. A result too big for the clipboard is refused with *Too much to copy (N rows) — use CSV… to save it to a file instead* |
| **CSV…** | Asks for a file name (the query tab's name to start with) and saves a `.csv` |
| **Excel…** | Asks for a file name and saves a `.xlsx`, one sheet named after the query tab. A result longer than a worksheet holds (1,048,576 rows) carries on onto a second sheet, *Name (2)*, and so on. Over 200,000 rows it says *Building the workbook — the download starts when it is ready*, because a workbook can only be saved once it is finished |
| **Save as table…** | Runs the query again and saves the result as a new table in the case |

**CSV…** and **Excel…** guard against formulas: a text value that starts
with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading `'`, so a
spreadsheet shows it rather than running it. Column names get the same
treatment; numbers stay numbers, so `-5` is not changed. **Excel…** also
writes control characters that a worksheet can't hold as escapes such as
`\x07`, and cuts a value longer than 32,767 characters (the most a cell
holds) with a note saying how long it was. The values in the case are not
changed.

**Save as table…** asks for the new table's name at **New table name:**. If
the result is over 500,000 rows it asks *You're about to save N rows as a
table. Are you sure?* first. It saves the query that produced the result on
screen, even if you have edited the query since. The new table opens when it
is done, and you can filter, tag and export it like any other. With
`SELECT *`, the pane's own columns come along as data: `rid` (as `rid_1`),
Tags, Note and `rowid` (as `rowid_1`). Name the columns you want instead.
For other ways to export, see [Sessions and export](Sessions-and-export.md).

### Putting a query on a dashboard

**To dashboard…** puts the query on a board. Pick a board from its menu, or
**＋ New dashboard…**, and the widget editor opens with the query filled in.
See [Dashboards](Dashboards.md#from-the-sql-pane).

## Example queries

These were run against a case with a Security event export as `src_1`.
Change the table numbers and column names to match yours.

### Which table is which

```sql
SELECT id, 'src_' || id AS query_as, name, nickname, row_count
FROM sources
ORDER BY id;

SELECT id, 'merge_' || id AS query_as, name, source_ids
FROM merges;
```

Run them one at a time.

### Tag counts per table

```sql
SELECT s.name AS "Table", t.name AS "Tag", COUNT(*) AS "Rows"
FROM row_tags rt
JOIN tag_defs t ON t.id = rt.tag_id
JOIN sources s ON s.id = rt.source_id
GROUP BY rt.source_id, rt.tag_id
ORDER BY "Table", "Rows" DESC;
```

### Top processes among tagged rows

```sql
SELECT Process, COUNT(*) AS n
FROM src_1
WHERE rid IN (SELECT rid FROM row_tags WHERE source_id = 1)
GROUP BY Process
ORDER BY n DESC
LIMIT 20;
```

### Tags by process

The `rt.source_id = 1` line matters: without it, a tag on row 5 of any other
table is counted against row 5 of `src_1`.

```sql
SELECT t.name AS tag, s.Process, COUNT(*) AS n
FROM row_tags rt
JOIN tag_defs t ON t.id = rt.tag_id
JOIN src_1 s ON s.rid = rt.rid
WHERE rt.source_id = 1
GROUP BY 1, 2
ORDER BY n DESC;
```

### Logons per user

```sql
SELECT TargetUserName, LogonType, COUNT(*) AS logons
FROM src_1
WHERE EventId = '4624'
GROUP BY TargetUserName, LogonType
ORDER BY logons DESC;
```

If the user and logon type are inside an XML or JSON column, extract them
first as [derived columns](Derived-columns.md#extracting-part-of-a-value),
then query them by name the same way.

### Tagged rows with their tag

This result keeps `rid` and reads one table, so its rows can be selected,
tagged and opened from the result.

```sql
SELECT t.name AS tag, s.*
FROM row_tags rt
JOIN tag_defs t ON t.id = rt.tag_id
JOIN src_1 s ON s.rid = rt.rid
WHERE rt.source_id = 1
  AND t.name = 'Suspicious'
ORDER BY s.Time;
```

### Rows with a note, and tagged rows across a merge

Again, two queries to run one at a time.

```sql
SELECT rid, Time, Host, Tags, Note
FROM src_1
WHERE Note IS NOT NULL;

SELECT source_id, Host, COUNT(*) AS n
FROM merge_1
WHERE Tags IS NOT NULL
GROUP BY source_id, Host;
```

### Shell processes per day

```sql
SELECT DAY_BUCKET(Time) AS day, COUNT(*) AS n
FROM src_1
WHERE Process REGEXP '(cmd|powershell)\.exe$'
GROUP BY day
ORDER BY day;
```

## SQL Copilot

The bundled claude_assistant plugin adds a **Copilot** panel beside the
editor that writes queries and can insert or run them. It needs network
access and is off by default; see [Plugins](Plugins.md#claude_assistant).
