# Sessions and export

A session is a named snapshot of your analysis: the tags and notes on
every table in the case at the moment you saved it. Sessions are stored
inside the case file, so they travel with it. You use them to keep a
checkpoint before changing direction, to start a second pass over the same
evidence without losing the first, to hand your work to another analyst,
and to compare two passes row by row. Exports are the other way work
leaves Winnow: files of what you are looking at (as CSV, TSV, Excel, JSON
Lines, JSON, Markdown or HTML), and Excel workbooks of your findings or of
the whole case.

Everything here starts from the **Case ▾** button on the app bar:
**Case ▾ → Sessions…** and **Case ▾ → Export…**.

## Sessions

### What a session holds

For each table in the case (open in a tab or not), a session records:

- the tags on each row, by tag name, plus the tag palette itself (names,
  colours and tag keys);
- the note on each row;
- the table's column layout: order, hidden and pinned columns, widths and
  sort;
- the definitions of its derived columns, which are recomputed when the
  session is loaded rather than carried as values;
- what identifies the table: its name, the original file's path, a
  fingerprint of that file, its row count and its columns, and how it was
  imported (delimiter, header row, sheet or SQLite table).

A session does **not** hold the rows themselves. It names each row by its
Line number in the table, so it is only useful in a case that has the same
evidence imported the same way. It also leaves out the Notes page, merges,
dashboards, the watchlist and SQL tabs; those stay in the case file and are
untouched by saving or loading.

**Case ▾ → Sessions…** has two sections down its left: **Saved sessions**
and **Compare**. **Saved sessions** lists each session with how many rows
it has tagged (a row with two tags counts once), how many notes, the
number of tables it covers and when it was saved, newest first. A session
you received from someone else is listed under **Received from another
analyst**, below your own. **Filter sessions…** narrows the list by name.
Click a session's name to compare it with your current work (see
[Comparing two sessions](#comparing-two-sessions)). Everything else you can
do with a session is in its **⋯** menu, or in the menu you get by
right-clicking its row: **Compare with current work**, **Load…**,
**Download**, **Rename…** and **Delete…**.

### Saving a session

**Case ▾ → Sessions… → Save current work…**, then give it a name. Any text
works except `.` and `..`, and a name can contain `/` (`before 12/05` is
fine).

Saving under a name that already exists replaces that session without
asking. Use a new name if you want to keep the old one.

### Loading a session

Choose **Load…** from a session's **⋯** menu. Winnow asks first, because loading
**replaces** the tags and notes currently in the case: every table's, not
only the tables the session mentions. Save your current work first if you
want to keep it.

Loading also puts back each table's column layout from the session, and
recreates its derived columns. A derived column whose name is already
taken in the table is skipped with a warning; the tags and notes still
load.

Two things to know:

- `Ctrl+Z` can't step back past a load. Loading clears the undo history,
  since the changes it remembers describe tags that are no longer there.
  The session you saved before loading is your undo.
- A table that was made inside Winnow, by the SQL pane's
  **Save as table…** or by a plugin tab such as first_last, has no source
  file to fingerprint, so Winnow gives it an identity of its own when it is
  made. A load finds it by that identity, and so does a copy of it in
  another case. A table another analyst made the same way, with the same
  name and rows, is a different table and gets none of these tags. A
  session saved by an older version of Winnow didn't record that identity,
  so a load can't match its entries for such tables: it leaves those
  tables' current tags and notes as they are, and says so.

Any table the session couldn't place is reported in a message after the
load; see [How a session finds its tables](#how-a-session-finds-its-tables).

### Starting a fresh pass

**Start a fresh pass…** saves what you have under a name (it suggests a
free one, `pass 2`, `pass 3`…) and then clears every tag and note in the
case. Column layouts, derived columns, SQL tabs, the Notes page and the tag
palette all stay: a second pass starts with no conclusions, not with no
setup.

If you clear the name, Winnow asks whether to clear everything **without**
saving it first. That can't be undone. If you type the name of an existing
session, it asks before replacing it.

### Renaming and deleting

**Rename…**, in a session's **⋯** menu, refuses a name another session
already has. **Delete…** deletes the session from the case after asking; it
doesn't touch the live tags and notes.

## Handing work to another analyst

Two analysts on one investigation each work in their own case file. A
session file is how one passes their tags and notes to the other. That is
the only time you need one: a saved session is already inside the case
file and goes wherever the case goes.

### Sending a session file

Save a session, then choose **Download** from its **⋯** menu. You get a
`.winnow_case.json` file named after the session. It holds the session
described above: tags, notes, layouts and derived-column definitions, and
the original path of each evidence file on your machine (for a file you
uploaded through the browser, just its name). It holds no
evidence rows.

### Receiving a session file

**Case ▾ → Sessions… → Receive a session file…**, pick the `.json` file
and name it. This only stores the session in your case, under **Received
from another analyst**; nothing in your work changes yet. From there you
can click its name to compare it with your current work (below), or
**Load…** it from its **⋯** menu. As with saving, a name that is already
taken replaces that session without asking.

Before loading someone else's session, import the same evidence files they
worked from, the same way: the same file, with the same delimiter, header
setting, sheet or table. Their tags land on row numbers, so a table read
differently would put every tag on the wrong row, and Winnow refuses
rather than let that happen.

### How a session finds its tables

Tables are matched by the evidence file's fingerprint, never by Winnow's
internal numbering, so a session from another analyst's case lines up with
yours. A table made inside Winnow is matched by the identity it was given
when it was made. When several tables come from one file (every table of one SQLite
database, every sheet of one workbook, or a file imported twice), the
table's name, columns and row count pick the right one. The table name
doesn't have to match: a reviewer can name their tables differently.

For each table in the session, loading does one of these:

| Situation | What happens |
| --- | --- |
| One table in your case matches | Its tags and notes are applied. |
| The file is in your case, but no single table is unambiguously the session's | Skipped with a warning rather than guessed. Those tables keep their own tags and notes. |
| The table matches but has a different row count or columns (imported with other options) | Skipped with a warning. The table keeps its own tags and notes. |
| The file isn't in your case, but it is still at the path the session recorded | Winnow checks the file's fingerprint, re-imports it the way it was originally imported, then applies the tags. A table that came in through a plugin's importer can't be re-imported this way. |
| The file isn't in your case and isn't at the recorded path, or has changed | Skipped with a warning. Import it yourself, then load again. |
| The table was made inside Winnow, and your case has neither it nor a copy of it | Skipped with a warning: there is no file to re-import it from. A table of the same name that you made yourself is a different table and gets none of these tags. |
| A session from an older Winnow, for a table made inside Winnow | Skipped with a warning. The table it could be keeps its own tags and notes. |

A recorded path that isn't a local absolute path (a network share, for
example) is never followed. Each warning appears as a message after the
load.

Tags are matched by **name**. A tag in the session that you also have is
the same tag, even if its colour or key differs. A name differing only in
capitals (`Malware` and `malware`) counts as the same tag when that is
unambiguous on both sides. A tag you don't have is created, with the
session's colour and key.

A name that two tags share can't be matched that way, so which tag a row
goes on would be a guess. Loading a session you saved in this case still
works: it puts each row back on the tag it was on. If one of those tags has
since been deleted or renamed, its rows go onto a new tag with a name of its
own ("New tag 2", say), and a message says so. A session you received, or
one loaded from a file, is not loaded at all: the message names the tag and
says where it is repeated. If it's your case, give each tag its own name
under **Edit tags** and load again. If the session repeats the name, the
analyst who sent it has to rename the tags in their case and send the
session again.

### Replace, not merge

**Load** always replaces the case's tags and notes with the session's.
The Sessions dialog has no option to add a session on top of your own work.
To fold a colleague's findings into yours, compare the two first and carry
over the rows you agree with, or load their session into a copy of your
case.

## Comparing two sessions

Comparing is how a review gets checked: what one side tagged that the
other didn't, and where both tagged a row differently.

1. Open **Case ▾ → Sessions…** and choose **Compare** on the left.
   Clicking a session's name under **Saved sessions** does the same, with
   that session against your current work.
2. Pick **From · A** and **To · B**. Either side can be **Current work
   (live)**, your case as it is now. Once a session is saved, **From · A**
   starts on the newest one and **To · B** on **Current work (live)**: what
   has changed since you last saved. The usual review is the session that
   was handed over as A and your current work as B. **⇄** swaps the two.

The comparison runs as soon as either side changes. The totals over the
list spell the two names out (**Only in** *A*, **Only in** *B*, **Tagged
differently**, **Notes changed**), and under them is one line per table,
with a count in each column:

| Column | Meaning |
| --- | --- |
| **A only** | Rows tagged on the A side and untagged on the B side. |
| **B only** | Rows tagged on the B side and untagged on the A side. |
| **Differ** | Rows tagged on both sides, with different tags. |
| **Notes** | Rows whose note differs. |
| **All** | Every differing row in the table. |

A count of nothing shows as **—**.

Tags are compared by name and tables by their evidence file, so two
analysts' sessions from different cases compare correctly. Two tags that
share a name are told apart when both sides come from this case (a session
you saved here, or your current work), and named after their tag number
in the marks, such as `New tag #4`. Between a received session and your
work they can't be, so the section names them above the counts: a row moved
from one of them to the other is not counted as a difference. If the two
sessions cover different tables, the section says so above the counts. A
table that only one side has still gets a line, with every tagged row in
it counted as a difference. A table that isn't in your case is listed as
"not in this case" and its counts can't be opened, and so is a table made
inside Winnow that an older session recorded, since it can't be matched.

Click a count to open those rows in the table. The grid is filtered to
exactly those rows, and each carries a mark in the gutter: **A** for A
only, **B** for B only, **A→B** for both sides, differently. Hover over
a mark to see each side's tags and notes. A banner above the grid names the
two sessions and offers **All differences** and **Done**; **Done** removes
the marks and puts back the filter the table had before. While the rows are
open, **Case ▾ → Sessions…** opens on **Compare**, which says
**Comparison applied** at the top with the same **All differences** and a
**Clear** that does what **Done** does. Opening a comparison turns the
timeframe filter off, since the counts were worked out over the whole
table.

The counts are exact, but the rows you can open are capped at 2,000 per
kind of difference. A count past the cap opens its first 2,000 rows, and
the section says the comparison hit its row cap.

## Exporting

**Case ▾ → Export…** lists what you can export down its left, each with
its size under its name. The one you choose says on the right what goes
into the file, and the button at the bottom exports it, saying how much.

| Section | Format | What it contains | File name |
| --- | --- | --- | --- |
| **Current view** | Your choice of CSV, TSV, Excel, JSON Lines, JSON, Markdown or HTML (see [Formats for the current view](#formats-for-the-current-view)) | Every row of the open table's view, with its filters, sort, search and timeframe applied, in that order, and without the rows [Hide duplicate rows](Working-with-tables.md#hiding-duplicate-rows) hides. **Tagged rows only** limits it to the rows with at least one tag | `timeline-export.csv`, `.tsv`, `.xlsx`, `.jsonl`, `.json`, `.md` or `.html` |
| **Tagged rows** | Excel | Every tagged row in the case, one worksheet per table that has any | `tagged-export.xlsx` |
| **Every row** | Excel | Every row of every table, one worksheet per table | `all-tables.xlsx` |

**Current view** has a **Format** row of buttons under its row counts.
It says what narrows the view and how it is sorted, describes the chosen
format, and lists the file's columns in order, where you can choose which
go in the file and move them (see
[Choosing the columns](#choosing-the-columns)). It exports only while a
table is open. The two workbook exports ignore filters entirely and list
rows in Line order; each lists its worksheets with the rows each gets.
With nothing tagged in the case, **Tagged rows** has nothing to export and
its button is off.

### Formats for the current view

Pick the format on the **Format** row, and the button at the bottom names
it: **Export 2,400 rows (.xlsx)**, for example. The format you exported
to last is the one chosen the next time you open the dialog. Every format
holds the same rows in the same order, with the same columns: the ones
ticked in the column list, in its order.

| Format | Button | What you get | Use it for |
| --- | --- | --- | --- |
| CSV | **CSV** | Comma-separated text, UTF-8. | Any spreadsheet, other tools, or importing back into Winnow. |
| TSV | **TSV** | Tab-separated text, UTF-8. A cell that holds a tab, a quote or a line break is put in quotes, the way Excel writes a tab-delimited file. | Tools that want tabs rather than commas. |
| Excel | **Excel** | A workbook with one worksheet named after the table. | Handing the view to someone who works in Excel. |
| JSON Lines | **JSON Lines** | One JSON object per row, one row per line. | `jq`, scripts, and loading into a log platform. |
| JSON | **JSON** | The same objects as JSON Lines, in a single array. | Tools that want one JSON document. |
| Markdown | **Markdown** | A table in GitHub-flavored Markdown. | Pasting into a report, a ticket or a wiki. |
| HTML | **HTML** | A web page with the table on it. | Opening in a browser, printing, or attaching to a report. |

Some details of each:

- **CSV, TSV and Excel** get the [formula protection](#formula-protection)
  described below. Excel also keeps to [Excel's limits](#excels-limits): a
  view of more than 1,048,575 rows continues on sheets named `Name (2)`,
  `Name (3)` and so on.
- **JSON Lines and JSON** write each row as an object: `Line` (a number),
  `Table` (on a merge), `Tags` (a list of tag names, `[]` for none),
  `Note` (`null` for none), then each column under its own name. Values
  are written exactly as the case holds them, with no apostrophes added.
  If a table has a column with the same name as one of these, such as its
  own `Note` column, the table's column is written as `Note_1`, the name it
  would get if you imported a CSV export back into Winnow. That holds
  wherever you put either column; leave Winnow's **Note** out and the
  table's is plain `Note` again. The dialog's column list shows the name
  each one gets.
- **Markdown** is the table and nothing else, so what you paste is what
  renders. Characters Markdown would turn into formatting (`*`, `_`, `` ` ``,
  `|`, `<` and others) are escaped with a backslash, so a command line
  reads exactly as it is. A line break in a cell is written as `<br>`.
  Backslashes in paths such as `C:\Windows\System32` are left alone.
- **HTML** is one file that opens in any browser. At the top it shows the
  table's name, whether these are every row or the tagged rows, when it
  was exported, and the dialog's line saying what narrows the view and how
  it is sorted. Under that are the table, with each tag in its color, and
  the row count. The page follows the viewer's light or dark setting and
  prints on white. It has no scripts and loads nothing from the network,
  and every value is escaped, so evidence text can't act as markup.
- In **Markdown and HTML**, control characters show as escapes such as
  `\x07`, as they do in Excel. JSON keeps them, escaped the way JSON does.

Markdown and HTML suit a report-sized set of rows. Above 10,000 rows the
dialog warns that the file may be too big to open comfortably, and suggests
narrowing the view or choosing CSV, Excel or JSON Lines. It still exports
if you go ahead.

### Choosing the columns

The column list under **Current view** is the file's columns, top to
bottom. It starts the way the grid has them: **Line**, **Tags** and
**Note** first (and **Table** on a merge), then the table's columns in
the order you arranged them, each ticked, except a column hidden in the
grid, which is unticked and says "hidden in the grid".

- **Untick a column** to leave it out of the file, or tick a hidden one to
  put it in. That includes **Line**, **Tags** and **Note**.
- **Drag a row** to move the column. **⤒** and **⤓** at the end of a row
  send it to the start or the end of the file, and with a row's tick box
  focused, **Alt+↑** and **Alt+↓** move it one place.
- **Filter columns…** above the list narrows it to the columns whose names
  match. **All** and **None** tick or untick every column the list is
  showing, so filtering for `Payload` and pressing **None** leaves out all
  the PayloadData columns at once.
- **Reset to the grid** starts again from the grid's columns and order.

The **columns** count at the top and the line under the list follow what
you choose. With nothing ticked, the button reads **Choose a column to
export** and is off.

Your choice is for this export only. The grid's columns, their order and
what is hidden stay as they are. It lasts while the dialog is open, so you
can change the format without choosing again; the next time you open the
dialog, it starts from the grid again.

### What an export contains

Every export starts with three columns: **Line** (the row's number in its
table, as the grid's Line column shows it), **Tags** (tag names separated
by `; `; a list in JSON, colored labels in HTML) and **Note**. The table's
columns follow in the order you arranged them, and columns you have hidden
are left out. Visible derived columns are included. The text formats are
UTF-8. For **Current view**, that is where the column list starts, and
the file has the columns you leave ticked, in the order you leave them.

Exporting a merged view adds a **Table** column after **Line**, naming the
member table each row came from; without it, two rows could both read
"Line 1". The workbook exports have no sheet for a merge: every merged row
belongs to one of its member tables and appears on that table's sheet. A
derived column added to the merge itself is the one thing that doesn't
appear there.

### Formula protection

A cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return
could run as a formula when the file is opened in Excel or another
spreadsheet. Evidence text such as command lines and file names is
attacker-controlled, so every CSV, TSV and Excel export puts an apostrophe
in front of such a value: `=HYPERLINK(...)` is written as
`'=HYPERLINK(...)`. Column names, tag names and notes get the same
treatment. Only the exported copy changes; the value in the case is
untouched.

JSON, Markdown and HTML exports aren't opened as spreadsheets, so they
keep every value exactly as it is, with no apostrophe.

Expect this on negative numbers too. Imported values are stored as text,
so `-20` exports as `'-20`.

### Excel's limits

The workbook exports, including **Current view** as Excel, keep inside
what Excel can open:

- **Rows per sheet.** A worksheet holds 1,048,576 rows including the
  header, so 1,048,575 rows of data. A table with more continues on sheets
  named `Name (2)`, `Name (3)` and so on, with the header repeated, and
  every row is exported. **Every row** checks first and asks
  before starting when a table is over the limit; the tagged export and
  **Current view** continue the same way without asking.
- **Sheet names.** A sheet is named after its table, cut to 31 characters,
  with `\ / ? * [ ] :` and control characters replaced by `_` and
  apostrophes trimmed from the ends. Two tables whose names collide get
  `_1`, `_2` added, and a table called `History` (a name Excel reserves)
  becomes `History_1`.
- **Cell length.** A cell holds at most 32,767 characters. A longer value
  is cut to fit and ends with a marker giving its full length, for example
  `…[cut: the value is 40,000 characters, a cell holds 32,767 — all of it is
  in the case]`.
- **Control characters.** Characters a worksheet can't store are written as
  visible escapes such as `\x07` instead of being dropped. Tabs and line
  breaks stay as they are.

The CSV, TSV, JSON Lines, JSON, Markdown and HTML exports have no row
limit and keep values whole.

### Large exports

Every **Current view** format except Excel streams: the download starts at
once and grows as rows are written. An Excel file, whether **Current view**
or **Every row**, is written whole first and then downloads, so on a big
view or case nothing seems to happen for a while. **Case ▾ → Log…** records
when each export started and finished, how many rows it wrote, its size and
how long it took, and notes a download that was cut off. Winnow doesn't
shut itself down while a download is still in progress.

### Other things you can export

| From | What | Where it's described |
| --- | --- | --- |
| SQL pane result | **Copy**, **CSV…** and **Excel…** of the result as shown, in its displayed order | [SQL pane](SQL-pane.md) |
| Grid right-click → **Copy** | The selected rows as tab-separated text, with or without headers, up to 20,000 rows | [Working with tables](Working-with-tables.md) |
| Watchlist **Export…** | The indicators as `watchlist.csv` | [Timeline, Search all and the Watchlist](Timeline-search-and-watchlist.md) |
| **Export filters…** | Saved filters as `winnow-filters.json` | [Filtering and searching](Filtering-and-searching.md) |
| Profile **Export…** | A profile (its plugins, boards, watchlist and variables) as one JSON file | [Dashboards](Dashboards.md) |
| Pivot plugin **CSV** | The pivot table as shown | [Plugins](Plugins.md) |

## Handing over a whole case

A case file (`.db-winnow`) is self-contained: the imported rows, tags,
notes, sessions, the Notes page, dashboards, the watchlist, derived
columns and SQL tabs are all inside it. Copy the file and the other analyst
opens it with **Open existing case file…** on their home screen. They don't
need the original evidence files.

Shut Winnow down before copying (**Case ▾ → Shut down Winnow…**). While a
case is open, recent changes can sit in two files beside it,
`<case>.db-winnow-wal` and `<case>.db-winnow-shm`, and a copy of the main
file alone can miss them. A clean shutdown folds them back in. If Winnow
was killed or crashed and a `-wal` file is still there, keep it with the
case file. Going back to the home screen doesn't close the case.

A few things live on your machine rather than in the case and don't go
with it: saved filters (send them with **Export filters…**), keyboard
shortcuts, appearance, and the plugins themselves. A case that uses a
plugin's tab needs the same plugin installed on the other side.

### Compacting a case file

Removing a table, or dropping a filter index, frees space inside the case
file but doesn't make the file smaller. **Case ▾ → Compact case file…**
rewrites the file to give that space back, which is worth doing before
you send a case to someone.

Compacting a large case can take several minutes, during which Winnow
doesn't respond, and it needs about as much free disk space as the case
file uses (plus 256 MB) in the case's folder. Winnow checks first and
refuses if there isn't enough. When it finishes it reports the size before
and after. There is no command-line version.
