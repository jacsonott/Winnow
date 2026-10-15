# Derived columns

A derived column is a new column Winnow computes from one that is already
in the table: a sortable time from a Unix epoch or a FILETIME, a field
lifted out of a JSON or XML document, a piece of a value cut out with a
regex, the first non-empty of three user-name columns, or a value looked
up in another table. The original column is never changed, and neither is
the evidence file. Once built, a derived column sorts, filters, groups and
exports like any other.

## What a derived column is

A derived column is marked with **ƒ** in its header; hover it to see what
it was derived from and how. It is stored in the case file, values
included, so it is there the next time you open the case and it goes
wherever the case file goes (see
[Derived columns travel with the case](#derived-columns-travel-with-the-case)).

Things to know before you rely on one:

- **The search box doesn't see it.** Search and **Search all** look at the
  columns the file came with. A derived value is computed from text that
  is already searchable, so search the source column, or filter the
  derived column through its filter box.
- **It works everywhere else.** Column filters, the value picker, sorting,
  grouping, CSV and Excel export and the [SQL pane](SQL-pane.md) all
  include it. A derived datetime column can be chosen in the timeframe
  filter, the histogram and the [Timeline](Timeline-search-and-watchlist.md)
  like any other datetime column.
- **It doesn't change what the table is.** Saved filters,
  [Timeline configurations](Timeline-search-and-watchlist.md#choosing-the-timestamp-type-and-body)
  and merging are matched on the file's own columns, so adding a derived
  column never stops a table matching its saved filters or its Timeline
  configuration, or merging with its siblings.
- **It can feed another one.** Once a derived column has finished
  building it appears in the **Parse column** list as `name · derived`,
  so you can chain steps, for example a JSON field that itself holds XML.

## Adding a derived column

| From | Do this |
| --- | --- |
| A column header | Right-click the header → **Derive a column from this…** |
| The table menu | Right-click the table's tab (or press `C`, that is Shift+C) → **Columns** → **Add derived column…** |
| The detail pane | Right-click a field inside a JSON or XML document → **Add as a column** |
| After an import | When a column looks like a timestamp Winnow can't already read, a notice says so; click **Add datetime column** |
| A whole document | Right-click the header → **Flatten JSON/XML into columns…**, or right-click a document node in the detail pane → **Flatten this document into columns…** |

**Add as a column** in the detail pane adds the column straight away with
no dialog, named after the field (`user.name`, `LogonType`); if that name
is taken it becomes `LogonType 2`.

### The Add derived column dialog

Work down the dialog from the top:

1. **Type** — what you want: **Timestamp**, **Extract part of a value**,
   **Join from another table**, **Compare (elapsed time)** or **Combine
   columns**.
2. **Operation** — the specific conversion, listed in the tables below.
3. **Parse column** — the column to read.
4. The operation's own settings, if any (required ones are marked `*`).
5. **New column name** — prefilled as `Column (parsed)`, `Column
   (extract)`, `Column elapsed` and so on. A name the table already has, in
   any capitalisation, is refused.
6. Check the **Preview**, then click **Add column**.

When the type is **Timestamp**, Winnow tries every format against the
column and picks the best one for you: *Suggested: Unix epoch — 100% of
sampled values parse.* Check the preview anyway. Some numbers are valid in
two formats (see [Ambiguous numbers](#ambiguous-numbers)).

The preview shows up to ten real values and what each becomes; a value
that won't convert shows *can't parse*. The verdict under it counts the
failures in a sample of up to 200 values. **This view** samples the rows
your current filters leave and **Whole table** samples from the top of the
table; the dialog starts on **This view** when you have filtered.
Operations that read several columns at once (Combine, Compare) always
preview against the whole table, and the dialog says so.

### While it fills in

The new column appears at once, right after the column it was derived
from, and fills in from the top as Winnow works through the table.
Progress shows in the jobs panel, the same place imports appear, and the ✕
on that row cancels. Cancelling an add removes the column; you can add it
again at any time.

When the job finishes, a notice gives the number of rows read and, if any
failed, how many values were not found. A row whose input was blank is not
a failure. If some values failed, the header's right-click menu offers
**Show N unparsed rows** (for a timestamp) or **Show N rows without this
field** (for an extracted or looked-up value), which filters the grid to
exactly those rows.

## Timestamps

Every timestamp operation writes the same shape: `YYYY-MM-DD HH:MM:SS`,
with a six-digit fraction (`.ffffff`) when the source carries sub-second
precision. That is what makes the column sort correctly as text and line
up with every other datetime column in the case. How it is *displayed* is
a separate choice: right-click the header to pick a display format, or set
the default in [Settings](Settings-and-shortcuts.md).

**Time zones.** Epoch-style values (Unix, FILETIME, WebKit, Mac, .NET) are
UTC by definition. A value that carries its own offset (`Z`, `UTC`, `GMT`,
`+02:00`, `+0200`, `+02`) is converted to UTC. A value with no zone is kept
exactly as written, unless you fill in **Source UTC offset** (`-05:00`,
`+0530`, `+5`), a fixed offset Winnow subtracts to reach UTC. An offset in
the value wins over the setting. Winnow never guesses daylight saving: in
the ISO 8601, dd Mmm yyyy, Mmm dd yyyy and US formats, a value ending in a zone it
can't convert, such as `EST` or `Europe/Paris`, counts as unparsed rather
than being read as if it were UTC. RFC 2822 dates convert the zones that
standard defines (`UT`, `GMT`, `Z`, `EST`/`EDT`, `CST`/`CDT`, `MST`/`MDT`,
`PST`/`PDT` and `+hhmm` offsets, plus `UTC`); any other zone, such as `CET`,
`AST` or a single military letter, counts as unparsed. `-0000` means the
time is UTC and the sender's own zone is unknown, so it is kept as UTC and
**Source UTC offset** doesn't shift it.

| Operation | Accepts | Example input → output |
| --- | --- | --- |
| **Unix epoch** | Seconds, milliseconds, microseconds or nanoseconds since 1970, with an optional fraction. **Unit**: `auto`, `s`, `ms`, `us`, `ns` | `1704461121` → `2024-01-05 13:25:21`<br>`1704461121000` → `2024-01-05 13:25:21.000000` |
| **Windows FILETIME** | 100-ns ticks since 1601: decimal, `0x` hex, or bare hex that contains a letter | `133489347210000000` → `2024-01-05 13:25:21.000000`<br>`0x01DA3FD5B5A3E000` → `2024-01-05 12:50:06.885068` |
| **WebKit/Chrome epoch** | Microseconds since 1601 (Chromium History, Cookies, Downloads) | `13348934721000000` → `2024-01-05 13:25:21.000000` |
| **BSD syslog (Mmm dd hh:mm:ss)** | Year-less syslog lines. **Year of first line** is required | `Dec 31 23:59:59 host sshd[2]: …` → `2023-12-31 23:59:59` |
| **ISO 8601** | `YYYY-MM-DDThh:mm[:ss]`, `T` or a space, fraction with `.` or `,`, optional zone | `2024-01-05T13:22:01+02:00` → `2024-01-05 11:22:01`<br>`2024-01-05T13:22:01.5Z` → `2024-01-05 13:22:01.500000` |
| **dd Mmm yyyy hh:mm:ss** | `05 Jan 2024 13:22:01` or `5-Jan-2024 …`, English month names, optional zone | `5-Jan-2024 13:22:01 +0200` → `2024-01-05 11:22:01` |
| **Mmm dd yyyy hh:mm:ss** | Month first, abbreviated or in full, the day plain or with `st`/`nd`/`rd`/`th`, optional comma, seconds and fraction optional, optional AM/PM and optional zone | `Jul 28th 2026 01:02:22` → `2026-07-28 01:02:22`<br>`July 28, 2026 1:02 PM` → `2026-07-28 13:02:00` |
| **Compact YYYYMMDDhhmmss** | 14 run-together digits | `20240105132201` → `2024-01-05 13:22:01` |
| **Mac absolute (Cocoa) time** | Seconds since 2001-01-01; negative for earlier dates; `0` counts as unparsed | `726153721` → `2024-01-05 13:22:01` |
| **Excel serial date** | Days since 1899-12-30, fraction = time of day, rounded to the second | `45296.5576` → `2024-01-05 13:22:57` |
| **.NET ticks** | 100-ns ticks since 0001-01-01 (`DateTime.Ticks`), decimal | `638400581210000000` → `2024-01-05 13:28:41.000000` |
| **Apache access log** | `dd/Mmm/yyyy:hh:mm:ss +zzzz`, brackets optional | `[05/Jan/2024:13:22:01 +0100]` → `2024-01-05 12:22:01` |
| **RFC 2822 (email date)** | Email and HTTP dates; two-digit years 50–99 are 19xx, 00–49 are 20xx; a zone RFC 2822 doesn't define (`CET`, `IST`…) counts as unparsed | `Fri, 05 Jan 2024 13:22:01 -0500` → `2024-01-05 18:22:01` |
| **US MM/DD/YYYY hh:mm:ss** | US dates, seconds optional, optional AM/PM in any common spelling (`PM`, `pm`, `p.m.`) and optional zone | `1/5/2024 1:22:01 PM` → `2024-01-05 13:22:01` |

The text formats, BSD syslog and Excel serial dates also take **Source UTC
offset**; Unix, FILETIME, WebKit, Mac and .NET values are UTC already and
don't. With it set to `-05:00`, the ISO value `2024-01-05 13:22:01` becomes
`2024-01-05 18:22:01`.

### Auto-ranging epochs

With **Unit** on `auto`, Winnow tries seconds, then milliseconds, then
microseconds, then nanoseconds, and keeps the one unit whose result falls
between 1990 and 2100. The units are a thousand times apart, so at most one
fits. The same 1990–2100 window applies to every numeric format (Unix,
FILETIME, WebKit, Mac, Excel, .NET) and to Compact and RFC 2822 values: a
value outside it counts as unparsed even if you pick the unit yourself.
That is deliberate. A `0`, a `-1` or a stray counter in a timestamp column
should show up as a failure, not as a date in 1970.

### Ambiguous numbers

Some numbers are valid in two formats. Unix seconds and Mac absolute time
overlap, and so do nanosecond Unix epochs and .NET ticks. Both formats
then report a high match rate, and the suggestion is only the first of
them. Compare the preview with a time you already know for one row before
you add the column.

### Year-less syslog

BSD syslog lines carry no year. Set **Year of first line** (it starts as
the current year) and Winnow works down the table in file order, adding a
year each time the month goes backwards, so `Dec 31` followed by `Jan 1`
lands in the next year. This assumes the lines are in the order they were
written: an out-of-order line whose month is earlier than the line before
it is put in the following year. `Feb 29` in a year that isn't a leap year
counts as unparsed. If the year was wrong, use **Re-derive…** (below) to
change it in place.

## Extracting part of a value

The **Extract part of a value** type pulls one piece out of each row:
**JSON field**, **XML field** or **Regex capture**. A row that doesn't
have the field, or doesn't match, is left empty and counted in **Show N
rows without this field**.

The easiest way to get a path right is not to type it: open the detail
pane (`d`), right-click the field inside the document, and choose **Add as
a column** or **Copy path**.

### JSON paths

| Path | Means | From `{"user":{"name":"alice"},"items":[{"id":7}],"odd.key":{"value":"x"}}` |
| --- | --- | --- |
| `$.user.name` or `user.name` | A key inside a key. The leading `$` is optional | `alice` |
| `items[0].id` | Array item by position, counting from 0; `[-1]` is the last | `7` |
| `["odd.key"].value` | A key that contains a dot, bracket or quote, quoted | `x` |
| `user` | A whole object or array comes out as JSON text | `{"name": "alice"}` |

A JSON `null` gives an empty cell; a missing key gives no value at all.
JSON paths can select an array item by position only. In an EvtxECmd
`Payload` column the item at a given position can be a different field for
a different event ID, so look at the `@Name` that sits beside each
`#text` (the flatten picker lists both) before you trust a positional
column.

### XML paths

| Path | Means |
| --- | --- |
| `Event/System/EventID` | Elements from the root down, separated by `/` |
| `Event/System/Provider@Name` | A trailing `@` reads an attribute instead of the text |
| `Event/EventData/Data[@Name='LogonType']` | The `Data` element whose `Name` attribute is `LogonType` |
| `Event/EventData/Data[1]` | The second `Data` element (positions count from 0) |

Paths start at the document's root element and are case-sensitive.
Namespaces are ignored, so you write `Event`, never
`{http://schemas.microsoft.com/…}Event`. A cell holding several top-level
elements is read too, and its paths start at the first of them.

The `[@Name='…']` form is what makes Windows event XML usable. Every field
in `EventData` is a `<Data Name="…">` element, and positions shift from one
event ID to the next, so `Data[@Name='LogonType']` gives a column that
means the same thing in every row, where `Data[8]` would not. When you
click or flatten, Winnow writes this form for you whenever a repeated
element has a `Name`, `Key` or `ID` attribute that tells its copies apart,
and names the column after the attribute value (`LogonType`).

A document containing `<!DOCTYPE` is never parsed and counts as having no
fields, and so does a cell over 8 MB.

### Flattening a whole document

**Flatten JSON/XML into columns…** samples 200 rows of the column and lists
every field it found, with its path, an editable column name, how many of
the sampled rows had it (**coverage**) and an example value. Fields present
and non-empty in every sampled row start ticked; use **Select all** and
**Select none** for the rest, then **Add columns**. All the ticked columns
are built in one pass over the table, and the batch is all-or-nothing: if
one name collides, nothing is added. The header menu offers Flatten only
when the cells on screen start like a document (`{`, `[` or `<`), and only
on a single table, not a merged one.

### Regex capture

**Regex** is searched for anywhere in the value, using Python syntax, and
is case-sensitive unless it starts with `(?i)`. What lands in the column
is the first capture group, or the whole match if there is no group.
**Capture group** picks a different numbered group; **Named group** picks
a `(?P<name>…)` group by name, which keeps working if you later edit the
pattern and the numbers shift. Patterns are limited to 512 characters.

| Regex | Input | Output |
| --- | --- | --- |
| `^\w+://([^/:]+)` | `https://evil.example.com:8443/x` | `evil.example.com` |
| `(?P<method>\w+) (?P<path>\S+) (?P<status>\d+)` with **Named group** `status` | `GET /index.html 200` | `200` |

When a pattern names its groups, the dialog offers *This pattern names 3
groups — make a column for each?*, with a box and a name per group and
sample values under each. Leave them ticked and the button becomes **Add 3
columns**: one pass over the table, one column per group.

### Patterns Winnow refuses

Some patterns can take minutes on a single unlucky value, and Python's
regex engine can't be interrupted once it starts. Winnow refuses those
before they run, with a message saying why. The shape to avoid is a
repeated group whose repetitions could divide the same text up in more
than one way:

| Refused | Use instead |
| --- | --- |
| `(\w+\s?)*` | `(\w+\s)*`, or one class: `[\w\s]*` |
| `(\w+\.?)+` | `(\w+\.)+` |
| `(\w+\s?){0,50}` | A fixed limit doesn't help; end each repetition at a character it can't match |

The common delimited shapes pass: `(\.[a-z]+)*`, `([a-z0-9-]+\.)+`,
`(?:[^,]*,)*`, `(\S+\s+){3}`. The same check applies to the regex filter,
search and `REGEXP` in the SQL pane.

## Combining columns

| Operation | What it does | Example |
| --- | --- | --- |
| **First non-empty value** | The **Parse column**, then each column under **Then try**, in order; the first that isn't blank wins. All blank stays blank | `SubjectUserName` blank, then `TargetUserName` = `alice` → `alice` |
| **Row as JSON** | The **Parse column** plus the listed **Columns** as one compact JSON object per row, for pasting into a ticket or a note. Every other column starts listed; remove what you don't want. **Skip empty cells** leaves blanks out | `{"Host":"WS01","EventId":4624,"Code":"007"}` |

In **Row as JSON**, a cell is written as a bare number only when that
number reads back exactly as the cell did, so `4624` is a number while
`007` and `1.10` stay strings.

## Comparing two times

**Duration between two columns** (type **Compare (elapsed time)**) stores
**Parse column** minus **Subtract column**, in seconds, negative when the
first is earlier. Both inputs should be datetime columns in ISO or US form,
which every derived timestamp is. Right-click the header to show it as `1h
23m 45s` or as plain **Seconds**: `2024-01-05 13:25:21` minus `2024-01-05
13:00:00` is `25m 21s`.

## Looking values up in another table

**Look up from another table** (type **Join from another table**) works
like VLOOKUP. The **Parse column** holds the key; choose the **Lookup
table**, **Its key column** and the **Value to bring back**.

| If several rows match | Host `SRV01`, with two `SRV01` rows in the lookup table |
| --- | --- |
| `first` — the earliest matching row | `carol` |
| `all` — every distinct match, comma-separated | `carol, dave` |

Keys match exactly, capitalisation included, and a blank key matches
nothing. A key with no match leaves the cell empty and counts in **Show N
rows without this field**. The lookup table must be a real table, not a
merge or the table you are adding the column to, and can have at most
2,000,000 rows.

## Changing and removing a derived column

All of these are on the derived column's header right-click menu.

- **Re-derive…** reopens the dialog with the column and operation fixed
  and the settings editable: the wrong syslog year, a forgotten UTC
  offset, another column to try. It is offered for every derived column
  except a JSON or XML field: timestamps, combined columns, durations, a
  **Regex capture**'s pattern and group, and a lookup's table and columns.
  The column keeps its name, position and width. Any
  derived column built from this one is recomputed after it, and the
  notice names them.
- **Change the field path…** takes the place of Re-derive for a JSON or
  XML field: edit the path and click **Recompute**.
- **Remove derived column…** asks first, then removes it. Winnow refuses
  while another derived column is built from this one; remove that one
  first.

There is no rename. To change a derived column's name, remove it and add
it again under the new name.

Cancelling a re-derive from the jobs panel keeps the column with whatever
it had computed. Its ƒ tooltip then says *incomplete — re-derive to
finish*.

## Derived columns on merged tables

A [merged table](Working-with-tables.md) shows a derived column when every
member table has a derived column of that name. Add one while the merge is
open and Winnow adds it to each member table, checking all of them first,
so a name clash on one member stops it everywhere. The preview samples the
first member, or the merged view under **This view**.

Two things work differently on a merge:

- **Flatten** isn't offered. Open a member table to flatten its documents.
- A lookup's **Lookup table** must be a real table, never a merge.

**Re-derive…**, **Change the field path…** and **Remove derived column…**
on a merge change every member table's copy. Winnow checks all of them
first: if another derived column on any member is built from it, or the
members compute it with different operations, nothing changes. Each member
is recomputed as its own job in the jobs panel; the merged column's ƒ
tooltip says *building…* until all of them finish, and its **Show N unparsed rows**
count covers every member. **Remove derived column…** says it removes the
column from all the tables in the merge.

## Derived columns travel with the case

Definitions and values both live in the case file, so a case handed to
another analyst opens with its derived columns built. A
[session file](Sessions-and-export.md) carries the definitions only, and
loading it rebuilds the columns from the receiving case's own data. A
column whose name is already taken there is skipped with a warning. A
lookup column is recreated only if the receiving case has a table that
matches the lookup table it was built from; for a lookup table made inside
Winnow, that means the same table or a copy of it.

Saving a view as a new table copies a derived column's values into it as
an ordinary column. Winnow refuses the copy while one of the table's
derived columns is still building, because its values would come across
blank.
