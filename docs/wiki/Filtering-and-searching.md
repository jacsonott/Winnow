# Filtering and searching

Filtering is how a table of two million rows becomes the forty that matter. Winnow gives you several ways to narrow a table: a filter box under every column, a value picker, a filter builder for AND/OR logic, saved and shipped triage filters, a timeframe, a search box that reads every column, and the tag filter. They all stack, and none of them touches your data. Clearing them brings every row back.

## How filters combine

Every filter you set narrows the rows left by the others. Column filters, the filter builder, the search box, the tag filter, the timeframe and **Hide empty rows** are all applied together (AND). The line beside the toolbar shows how many rows are left, for example `58 of 400 rows`.

If nothing is left, the grid shows **No rows match** with a **Clear filters** button.

## The filter row

By default every column has a filter box under its header. Type into one and the grid updates as you type. `Enter` commits the filter and puts the focus back on the grid. `Escape` clears that box.

If you prefer a less crowded header, turn off **Settings → Grid & layout → Always-on filter row**. A filter bar then appears above the grid. It shows only the filters you have set, as chips you can click to edit or remove with ✕. To filter a new column, click the ⌕ on its header, or use **+ filter a column…**, which lists every visible column and lets you type to find one.

To filter from a cell, select it and press `f` to filter its column to that value. `F` does the same but drops every other filter first. Right-click a cell for the same choices, worded **Narrow to this value**, **Reset to this value** and **Exclude this value**, plus **Filter by values…**, which opens the value picker. **Reset to this value** keeps the timeframe.

### Filter syntax

| Type this | Matches | Notes |
| --- | --- | --- |
| `svchost` | cells containing the text | Ignores case for A–Z only |
| `!svchost` | cells not containing the text | Empty cells match too |
| `=4624` | cells exactly equal to the value | Case-sensitive |
| `!=4624` | cells not equal to the value | Case-sensitive; empty cells match too |
| `^C:\Users` | cells starting with the text | Ignores case for A–Z only |
| `>1000` `>=1000` `<1000` `<=1000` | greater than, at least, less than, at most | Numeric on number columns only (see below) |
| `/psexe(c\|svc)/` | a regular expression | Python syntax; ignores case in every script |
| `""` or `''` | empty cells | |
| `*` | cells with any value | |
| `a.exe\|b.exe\|c.exe` | cells exactly equal to any of the values | Case-sensitive, like `=` |

Spaces at either end of what you type are ignored. An operator needs a value after it: a lone `=` or `!` is read as text to look for. There are no wildcards. A `%`, `_` or `*` inside a value is matched literally. An operator applies to everything after it, so `!a.exe|b.exe` means "does not contain the text `a.exe|b.exe`", not "neither of these"; use the filter builder for that. A value that itself contains `|` can't be typed as a list; tick it in the value picker instead.

**Settings → Filter & search syntax** has a short version of this table.

### Case sensitivity

The contains, does-not-contain and starts-with filters ignore case for the letters A–Z only. `alpha` finds `ALPHA`, but `älpha` does not find `ÄLPHA`, and Cyrillic or accented text is matched exactly. To ignore case in any script, use a regular expression, such as `/älpha/`.

Equals (`=`), not-equals (`!=`) and lists (`a|b|c`) compare the exact text, case included: `=SVCHOST.EXE` and `=svchost.exe` give different results. The value picker writes exact values for you, so this rarely matters when you tick values there.

### Numbers, dates and text

When a table is imported, Winnow decides each column's type from its first 500 rows. A column is a number column if every non-empty value in that sample is a plain number such as `-12` or `3.5`. It is a datetime column if at least 80% of the sampled values look like dates. Every other column is text. You can see each column's type in the table menu (`C`, or right-click a tab and choose **Columns…**).

The comparison filters (`>`, `>=`, `<`, `<=`) compare numbers only on number columns. On a text column they compare text, character by character, so `>1000` counts `30` and `N/A` as greater than `1000`. On a number column, a cell that isn't a number (such as `N/A` or a blank) never matches a comparison, and typing something that isn't a number after the operator shows a "Filter error" message.

When a filter can't run, such as a regular expression with a syntax error, Winnow shows a "Filter error" message that names the problem and keeps the rows you had.

## The value picker

The ▾ beside a column's filter box opens the value picker. It lists the column's distinct values with a count for each, most common first. Tick the values you want and click **Apply** (or press `Enter`). You can also open it with `v` for the column under the cell cursor.

![The value picker](../screenshots/value-picker.png)

| Control | What it does |
| --- | --- |
| **Find a value…** | Narrows the list as you type |
| **This view** / **Whole table** | Values among the rows the other filters leave, or every value in the column |
| **By count** / **A→Z** | Most common first, or alphabetical |
| **All** / **None** | Tick or untick every value shown |
| **Apply** / **Cancel** | Apply the selection, or close without changing anything |

The picker starts on **This view**, so the list reflects your other filters. If the column already has a value selection, it starts on **Whole table** so that you can widen the selection again. Empty cells are listed as `(empty)`.

The picker writes an ordinary filter: one value becomes `=value`, several become `a|b|c`. Ticking every value in a complete list removes the filter. Some selections can't be written in the box: a value containing `|`, a value with spaces at either end, or `(empty)` ticked alongside other values. Winnow applies those through the filter builder instead and tells you so ("applied under Filters ▾").

The list holds the 1,000 most common values at most. When a column has more, the picker says so. The **Find a value…** box only searches the values already listed, so to reach a rarer value, narrow the column first (for example with a contains filter) and reopen the picker.

The ▾ buttons appear automatically on tables under 250,000 rows, because reading a column's values means scanning it. To change that, open the table menu and use **Value filter dropdowns**: **Auto (on under 250,000 rows)**, **On for every column** or **Off**. Each column in the table menu's **Columns** list also has its own ▾ button to force the dropdown on or off for that column.

### Stacking a column

Right-click a column header and choose **Stack values (rarest first)…** to see the column's values as a bar chart of counts for the current view. **Rarest first** is where one-off command lines and odd hosts show up; **Most common** reverses it. It shows up to 60 bars. Click a bar to filter the grid to that value.

To take every value away as a list instead, **Copy unique values** in the same menu puts the column's distinct values for the current view on the clipboard, one per line (see [Copying](Working-with-tables.md#copying)).

## The filter builder

The filter builder is for logic the filter row can't express, such as "EventId is 4624 or 4625, and LogonType is not 3". Open it from **Filters ▾ → Filter builder…** or press `e`.

A filter is a group of conditions. In the **Match … of:** line at the top of each group, choose AND (every condition must hold) or OR (any one is enough). Use **+ condition** to add a condition and **+ group** to nest another group. Each condition takes a column, an operator and a value:

| Operator | Notes |
| --- | --- |
| contains, does not contain, starts with | Ignore case for A–Z only |
| equals, does not equal | Case-sensitive |
| matches regex | Python syntax; ignores case |
| > (numeric), >= (numeric), < (numeric), <= (numeric) | |
| is empty, is not empty | No value needed |
| is any of (one per line) | Exact values, one per line |

A datetime column gets a 📅 date picker next to the value. When you open the builder, any filters in the column boxes move into it as conditions, so the builder always shows the whole filter.

Under the conditions, **Equivalent SQL (editable):** shows the same filter as a SQL expression. You can edit either side and they stay in step. If you type SQL that the guided editor can't show, the builder switches to raw SQL mode and says so. **Start over with the guided editor** leaves raw mode. Raw SQL is checked when you click **Apply**, and an error is shown under the box. Only plain conditions are accepted: no `SELECT`, no `;` and no comments.

The **Group by** section lets a filter carry a grouping too (see [Grouping rows](Working-with-tables.md#grouping-rows)). **Apply** runs the filter; **Clear** empties the conditions.

To see the current filter as a full query, press `Q`. It opens in a new tab of the [SQL pane](SQL-pane.md).

## Saved filters

Click **Save filter…** in the filter builder to keep a filter. A saved filter stores the builder's conditions, the sort, the search box and any grouping. It is saved for the table's set of column names and lives outside the case, so it is available in every case you open.

When you open a table whose columns exactly match a saved filter, **Filters ▾** gets an accent ring (until you apply something) and lists the filter under **For this table**. Filters saved for a similar but not identical set of columns are listed with "(similar)" after the name. Nothing is applied until you pick one. While one is applied, the button reads **★** followed by the filter's name.

To step through the saved filters made for exactly this table's columns, press `]` or `w` for the next and `[` or `q` for the previous. Past either end the filters are cleared rather than wrapping round, so pressing `q` repeatedly always ends on the unfiltered table.

Applying a saved filter replaces the builder's conditions, the search and the grouping. It leaves the column boxes, the tag filter and the timeframe as they were, so a host filter in a column box keeps narrowing each saved filter you step through.

**Filters ▾ → Saved filters…** opens the full list, grouped by column set, with the open table's set first. Click a filter's name to apply it. Drag a filter up or down within its column set to change the order `[` and `]` step through. Each filter's **⋯** menu has:

- **Edit…**: applies the filter to the open table and opens it in the builder, where `Update "name"` saves over it and **Save as new…** keeps both. It needs a table open.
- **Rename…**
- **Move up** and **Move down**: the same reordering as dragging, from the keyboard.
- **Delete…**, which asks first.

**Export filters…** and **Import filters…** move them between machines as a JSON file. The same buttons are in **Settings → Saved filters**.

### Shipped triage filters

Winnow ships 28 ready-made triage filters for EvtxECmd, RECmd and MFTECmd output. They are added to your saved filters the first time Winnow runs, and they are offered like any other saved filter: nothing is applied until you pick one. If you delete or rename one, it stays that way.

| Column set | Filters |
| --- | --- |
| Event logs (EvtxECmd) | 20 filters named `EVTX — …`: log clearing and Defender off; Defender detections and tamper; Defender config changes; logons; incoming and outgoing RDP; Sysmon present; network share access (5140/5145); service installs; WMI; process create/exit (4688/4689); crashed applications; user and group changes; PowerShell; firewall rule changes; scheduled tasks; BITS transfers; Group Policy (client and DC); system shutdowns |
| Registry (RECmd batch) | 5 filters named `Registry — …`: basic system info; audit and PowerShell policy; TypedPaths / RecentDocs; UserAssist / BAM; Run keys (autoruns) |
| NTFS $MFT (MFTECmd) | 3 filters named `MFT — …`: executables in odd places; Startup folder and run-key paths; a suspicious tool sweep that uses Advanced search |

A few of them also group the result: `EVTX — PowerShell` and `EVTX — BITS transfers` group by `PayloadData1`, and `Registry — UserAssist / BAM` groups by `HivePath`.

## The timeframe filter

The timeframe filter keeps rows whose timestamp falls inside a range. Click **⏱ Timeframe** or press `R` to set it, then click **Apply**, which always turns it on:

- **Keep applied when switching tables** decides whether the timeframe follows you to the next table (see below). It is on by default and is saved as soon as you click it.
- **Column** is one datetime column, or **All datetime columns (catches a match on any of them)**. With all columns, a row is kept if any of its timestamps is in range, which catches a file whose creation time was changed but whose modified time wasn't.
- **Start** and **End** take `YYYY-MM-DD HH:MM:SS` in 24-hour time. A date alone, or a date with `HH:MM`, also works, and you can set only one end. The 📅 button opens a date picker.
- **From tagged rows** with **Fill range** sets the range to the earliest and latest timestamps among your tagged rows (all tags, or only the tags you toggle). It only fills the boxes; **Apply** still turns the filter on.

Both ends are inclusive. A date without a time means midnight at the start of that day, so an end of `2024-01-05` stops at `2024-01-05 00:00:00`. To include the whole day, end at `2024-01-05 23:59:59`.

The timeframe understands timestamps written as `2024-01-05 13:22:01` (or with a `T`), US-style `1/5/2024 1:22:01 PM`, and month names such as `Jan 5 2024` or `Jan 5th, 2024 1:22 PM`. Fractions of a second and time-zone offsets in the data are ignored: the comparison uses the date and time as written. Rows whose timestamp can't be read are left out while the timeframe is on.

The timeframe stays on when you clear filters, apply a saved filter or reset the view, and the button shows ⏱ followed by the column name while it is active. Press `r` to turn it off without losing the range, and `r` again to turn it back on; opened while it is off, the dialog says *Off right now — Apply turns it back on.* **Clear** in the dialog removes it. A table with no datetime column is not narrowed by it at all.

What happens when you switch tables depends on **Keep applied when switching tables**:

- **On** (the default): there is one timeframe for the whole case, and every table you open is narrowed by it.
- **Off**: each table keeps its own timeframe, the way it keeps its own filters. A table you have not set one on opens without one, and going back to a table puts back the timeframe it had.

The same switch is in Settings, under **Filter & search syntax**.

**Jump to timestamp…** in the same dialog (or `J`) scrolls to the row closest to a moment instead of filtering. Press `.` in any table to jump to the same moment again.

## The histogram

Click **Histogram** or press `h` to show a bar chart above the grid of when the rows in the current view happened. Choose which datetime column to chart from its list. The line beside it gives the row count, the bucket size and the tallest bar. Winnow picks a bucket size, from one second up to a year, that gives bars a few pixels wide across the strip, so narrowing the timeframe gives finer buckets. Hover a bar to see its time range and row count. **Tags** colours the bars by tag: each row counts under the first tag it carries, with untagged rows underneath.

Drag across the bars to set the timeframe filter to that range on the charted column. **Clear timeframe** removes it again. The histogram follows every filter, so it is a quick way to see where a search's hits cluster in time. It stays open when you open another case and charts that case's table once it has loaded; a case with no table open shows "Open a table to chart when its rows happened."

If the table has no datetime column, the histogram says so. You can make one from a text timestamp; see [Derived columns](Derived-columns.md).

## The search box

The search box looks for text anywhere in a row. Click the magnifier, or press `/` or `Ctrl+F`, to open it. It has three modes:

| Mode | How it matches |
| --- | --- |
| **Contains** | The text appears anywhere in the row. Ignores case for A–Z only. |
| **Regex** | A Python regular expression. Ignores case in every script. |
| **Advanced** | Several terms joined with AND or OR, each of which can be negated |

Search reads the row as its imported columns joined by single spaces. That means a search can match across a column boundary (`exe 1500` finds a row whose Process ends in `exe` and whose next column is `1500`), and a regex `^` anchors to the start of the first column, not of each cell. Derived columns are not searched; filter them with their column box instead.

`Ctrl+F` is taken over on purpose. The browser's own find only sees the rows currently drawn on screen, so it would report "not found" for rows that are there.

`Enter` runs the search and returns to the grid. `Escape` clears it.

### Advanced mode

In Advanced mode, each term is a separate box. Use **+ term** to add one, the AND/OR list to join it to the previous term, **NOT** to exclude rows containing it, and ✕ to remove it. NOT works on any term, including the first. AND binds tighter than OR, as in SQL, so `a AND b OR c` means "(a and b) or c". With more than six terms the bar collapses to a one-line summary; click it to expand, and **▴ minimize** to collapse it again.

### The search index and slow searches

Winnow builds a search index for each table in the background, and the Tables manager (`t`) shows whether it is ready. Contains and Advanced searches use it once it is ready, for terms of three characters or more that don't contain `%` or `_`. Other terms, and every search before the index exists, scan the whole table instead. The results are the same either way; only the speed differs. The hint at the right of the search box says which is happening: `substring`, `substring · index building`, `advanced · full-text`, or `regex · full scan`. Regex never uses the index.

If a search takes longer than five seconds, it carries on in the background and the rows you had stay on screen. When it finishes, a "Search finished" message offers **Apply**. Typing a new search, or `Escape` in the box, cancels it.

### Regular expressions that are refused

Winnow checks every regular expression before running it, in filters and in search. It refuses patterns whose repeated group can match the same text in more than one way, such as `(a+)+$` or `(a|aa)+`. On a hostile cell those can take practically forever, and nothing can stop them once started. The error message explains the problem. To fix such a pattern, make each repetition end at something it can't itself match, use a character class instead of alternatives, or use Contains.

## Filtering by tag

The tag chips above the grid also filter. Click a tag to show only rows carrying it. Clicking a second tag shows rows carrying either. **Any tag** shows every tagged row and **No tags** shows the rows nobody has tagged yet, which is what is left to go through. Right-click a tag and choose **Exclude *tag*** to hide the rows carrying it; the chip is struck through until you click it again. See [Tagging and notes](Tagging-and-notes.md) for tagging itself.

## Clearing filters

**Clear filters** in the toolbar, or `c`, removes the column filters, the filter builder's conditions, the search, the tag filter and any grouping. It keeps the timeframe, the sort and **Hide empty rows**. If the row under the cursor is still there afterwards, it stays selected, so you see it in context. Grouping cleared this way can be brought back with `X`.

**Reset view** in the table menu does the same and also restores the table's default sort.

To remove only the timeframe, use **Clear** in the timeframe dialog or **Clear timeframe** on the histogram.

## Keys

| Key | Action |
| --- | --- |
| `f` / `F` | Filter by the selected cell's value / the same, dropping other filters |
| `v` | Value picker for the cell's column |
| `e` | Filter builder |
| `]` or `w` / `[` or `q` | Next / previous saved filter |
| `c` | Clear filters, search and tag filter |
| `/` or `Ctrl+F` | Search box |
| `r` / `R` | Toggle the timeframe / open the timeframe dialog |
| `h` | Histogram |
| `J` or `a` / `.` | Jump to timestamp / jump again |
| `Q` | Open the current filter in the SQL pane |

Every key can be changed; see [Settings and shortcuts](Settings-and-shortcuts.md).
