# Cases and importing

A case is one investigation: a single file holding a copy of every row you
import and everything you do with them — tags, notes, dashboards. This page
covers the home screen where cases live, what a case file is, and every way
to get evidence into one. Importing never changes the evidence file; Winnow
reads it and copies the rows into the case.

## The home screen

The home screen is what Winnow shows when no case is open. **⚙ Settings**
and **⏻ Shut down** sit beside the Winnow wordmark at the top. Below them
is this machine's list of cases, in the same frame as the Tables manager:
sections down the left, the chosen one on the right. Along the bottom are
**Open existing case file…**, **Quick look at files…** and **+ New case**.

| Section | What it lists |
| --- | --- |
| **Recent** | Cases opened in the last 30 days, and cases never opened yet, most recently opened first. Each row shows its group as a badge. |
| **All cases** | Every case, under a heading for each group with its case count, total rows and when it was last opened. Its side entry says how many case files are missing. |
| *each group* | That group's cases. Groups are listed by when one of their cases was last opened. |
| **No group** | Cases with no group (shown once there is at least one group). |

Each row is one case: its name, with its notes (or *file not found*)
beside it, then **Tables**, **Rows**, **Tagged** (rows with at least one
tag) and **Opened** (*37 min ago*, *yesterday*, *3 days ago*, *Sep 5*).
Hover over the name for the case file's path, and over the opened time
for the exact time. Click a column heading to sort by it. Click a row to
open the case; with the keyboard, ↓ from the filter box walks the names
and Enter opens one.

**Filter cases, groups and notes…** filters the section you are in. When
the case you are after is in another section (an older case, while you
are in Recent), a button under the list says how many more match and
takes you to **All cases**. Escape clears the filter. A case whose file
has gone missing shows ⚠ and *file not found*, and can't be opened until
you fix or forget it.

Inside a case, the brand button at the top left shows the case's name and
takes you back here, to the section you were on. The case stays open in
the server until you open another one.

### Creating and opening cases

**+ New case** asks for a **Name**, an optional **Group** (an engagement
name, say; it gets its own section on the home screen and is offered again
as you type), the **Case file path** and a **Profile**. The path is filled
in from the name — `ACME Breach` becomes
`<cases folder>/acme-breach.db-winnow` — until you edit it; **Browse…** picks another folder and **New folder…** makes one. A
profile turns on its plugins and dashboards as the case opens, and asks for
any case variables it needs (required ones are marked `*`); see
[Dashboards](Dashboards.md). **Create case** writes the file and opens it.

**Open existing case file…** takes a path (typed, or **Browse…**) and adds
the case to this machine's list as it opens it — for a case someone handed
you, or one you removed from the list. Older cases saved as `.db` open the
same way. A file that is not a Winnow case, such as a browser `History`
database, is refused; open a case and import it instead. A path with no
file at it is refused too (*No case file at …*) rather than creating an
empty case there; **+ New case** makes new cases.

Each row's **⋯** button, or right-clicking the row, opens its menu:

| Menu item | What it does |
| --- | --- |
| **Open** | Opens the case. |
| **Edit name, group and notes…** | Changes the name, group and notes on this machine's list. The file keeps its name on disk. |
| **Copy case file path** | Copies the full path of the case file. |
| **Remove from list…** | Forgets the case on this machine; the file is left untouched. |
| **Delete case file…** | Permanently deletes the case file and everything in it. Refused for the case Winnow has open, or one open in another Winnow. |

### The cases folder

On first run Winnow asks where to keep case files. **Choose a folder…**
picks one; **Use the default** uses the `cases` folder inside the Winnow
folder, beside `server.py` and the launchers, wherever Winnow is started
from; the prompt shows its full path. New cases are proposed
there and quick looks go in its `quicklook` subfolder. No setting changes it
later; the `WINNOW_CASES_DIR` environment variable overrides it. See
[Installation](Installation.md) for where everything else lives.

## Case files

A case file ends in `.db-winnow` and is an ordinary SQLite database. It
holds a copy of every imported row and its search index; tags, row notes
and the Notes page; merges, derived columns and table folders;
the watchlist, dashboards, SQL tabs and sessions; and the case's own
settings, such as which plugins are on for it. Saved filters, header-set
names, folder-import profiles, analysis profiles, the case list and your
appearance and keyboard settings stay on the machine instead.

Because the case holds its own copy of the data, it grows with every import.
Removing a table does not shrink it; **Case ▾ → Compact case file…** does. On
a large case that takes several minutes, the app is unresponsive meanwhile,
and it needs as much free disk space as the case file uses.

**The evidence is never modified.** Files are opened read-only and their
rows copied. Nothing is written beside a SQLite database either: one with an
un-checkpointed `-wal` file is copied, with it, to a private temporary
folder and read there, so those rows are included. The only import that
writes near the evidence is an archive, which expands into a new folder
beside itself (see [Archives](#archives)).

**One Winnow per case.** Opening a case another Winnow has open shows who
has it (user, host, since when, last seen) and asks: **Open anyway** or
**Don't open**. Two servers on one case don't see each other's tags, notes or
imports until they reload, and a long write in one starts failing the other.
On the command line the open is refused unless you pass `--force`. A Winnow
that crashed stops counting as a holder about two and a half minutes after
its last heartbeat.

**Keep case files on a local disk.** SQLite's locking does not work over a
network share (SMB, NFS) and the file can be corrupted there. Two analysts
work in separate case files and exchange session files — see
[Sessions and export](Sessions-and-export.md).

**Two cases at once means two Winnows.** A Winnow has one case open, and
every window on it shows that case. To work on a second case side by side,
start Winnow again: the second one takes the next free port (8778, 8779, …)
and opens its own window. Opening another case from **Home** switches every
window on that Winnow instead, and any other window on it says **This window
is out of date** and saves nothing more until it is reloaded.

### Checking a case for misplaced tags

Before this version, two Winnows on one computer could mix up their cases.
A second Winnow started while the first was running opened its window on
the first Winnow's case. And a window left showing one case after another
window opened a different one went on saving: its tags, notes and imports
went into the case that was open then, onto the rows at the same row
numbers. If an analyst did either, check the cases they had open then.

Winnow keeps no record of which window made a tag, so it cannot pick the
misplaced ones out for you. What can:

- **A session saved before the mix-up.** In **Case ▾ → Sessions… → Compare
  two sessions**, compare that session (**From**) with **Current work
  (live)** (**to**). **Only in** *Current work* lists every row tagged
  since, and clicking the count opens those rows. Untag the ones that don't
  belong, or **Load** the session to put the whole case back as it was then
  (save the current work first if some of the later tags are real; see
  [Sessions and export](Sessions-and-export.md#comparing-two-sessions)).
- **Tags you know you applied then.** Filter the case by those tags and
  check each tagged row is one you meant. A misplaced tag sits on the row
  with the same **Line** number (the row number in the gutter) as the row
  it was meant for in the other case, usually in the table imported in the
  same order.
- **Tags, tables and notes that don't belong.** A tag name, an imported
  table or a note that belongs to the other case came from a window that
  was showing that case. Delete them here and add them to the right case.

## Quick-look cases

A quick look is a temporary case for looking at a file without filing it.
**Quick look at files…** on the home screen makes one and opens the Import
window to add files to it. Dropping files anywhere on the home screen
except a case row makes one too (dropping them on a case's row imports them
into that case), and so does opening a file through an OS file association
(Windows and Linux;
**Settings → File associations**, see
[Settings and shortcuts](Settings-and-shortcuts.md)).

An associated file imports with no questions: every readable table of a
SQLite database, with likely Chrome timestamps converted, and every sheet of
a workbook that has data. Files opened together share one quick look. A
Winnow on the home screen with no case open takes the file; otherwise a new
Winnow starts in its own window, so a case you have open is never switched
under you. Double-clicking a `.db-winnow` file opens that case.

A quick look never appears in the case list. Its banner reads *Quick look —
this case is temporary and isn't saved anywhere yet.*:

| Button | What it does |
| --- | --- |
| **Save as a case…** | Opens the same dialog as **+ New case** (**Name**, **Group**, **Case file path**, **Profile**), titled *Save as a case*. **Save case** moves the quick look's file to that path, lists it on the home screen and applies the profile. The path starts in the cases folder. Refused while an import is running, and while a download or a search started before your latest changes is still reading the case: save again when it finishes. |
| **Add tables to an existing case…** | Copies every table, with its tags and notes, into a case you pick. |
| **Discard…** | Deletes the quick look. |

The brand button asks the same question before leaving, since there is no
way back from the home screen. At startup Winnow deletes quick looks older
than seven days that are not open and hold no tags, notes or other work.

## Ways to import

| Where | What happens |
| --- | --- |
| **Case ▾ → Import…**, or **Import files…** in an empty case | Opens the Import window. |
| Drag files onto an open case | Queues them in the Import window. |
| Drag files onto a case row on the home screen | Opens that case and queues them there. |
| Drag files onto the rest of the home screen | Makes a [quick look](#quick-look-cases) and queues them there. |
| **Import a whole folder…** in the Import window | [Folder import](#folders-and-kape-output). |
| Double-click a file with an association | A quick look, imported straight away. |
| `--open` on the command line | Imports delimited files at startup. |
| **Import files…** on a format in **Settings → Plugins** | Forces files through that plugin's parser. See [Plugins](Plugins.md). |

Drag-and-drop takes files; for a folder use **Import a whole folder…**. If
the pointer turns into a no-entry sign instead, see
[Troubleshooting](Troubleshooting.md#dragging-a-file-in-shows-a-no-entry-cursor).

### The Import window

The Import window is a queue in two panes, like **Import a folder** and the
Tables manager: the queued files down the left, the chosen file's settings
and preview on the right, and the buttons along the foot. Add any mix of
files, set the ones that need it, then **Import N files**. The window closes
and the imports run in the background while you work (see
[Import jobs](#import-jobs)).

- **Add from this machine…** browses the disk Winnow runs on and imports in
  place, with no copy — the fast route for big files. Type a path or click
  down to it; **Filter this folder…** and **Sort** (Name, Size, Modified)
  help in a big folder. It only works when the browser runs on the same
  machine as Winnow.
- **Upload from browser…** is the browser's own picker. The file is copied
  to the server's temporary folder first, which needs the space and takes
  longer; a file over 1 GB gets a reminder to add it by path.
- **Import a whole folder…** is [folder import](#folders-and-kape-output).

Under each file's name the list says how it will be read: *Delimited ·
auto-detect*, *JSON · nested kept as JSON*, *SQLite · 2 tables*, *Text · one
line per row* and so on. **Filter queued files…** narrows a long queue, and
**✕** takes a file off it. The chosen file's pane, from the top:

| Part | What it holds |
| --- | --- |
| **File** | Its name, the folder it is in, its size, and *read in place* (added from this machine) or *uploaded when it imports*. |
| **Read as** | Only for a file that can be read more than one way: **Delimited** or **One line per row** for delimited and other text; **JSON** or **One line per row**; **IIS log**, **Delimited** or **One line per row**; a plugin's format or **One line per row** for an uploaded file a plugin claims. |
| Settings | The reader's own: **Delimiter** and **Header**, JSON's **Nested objects**, an IIS log's **Format**, a plugin format's options. Plaso files and archives have none. |
| **Preview** | The first 50 rows as they will import, a type picker in each column's heading where the type can be set, and a line under it saying what was detected. A SQLite file or workbook lists its **Tables** or **Sheets** here instead, to tick. |

A setting is kept as you change it, and choosing another file keeps it, so
there is nothing to confirm; a file whose pane you never open imports with
the detected settings. A SQLite file or workbook can't import until its
tables or sheets are ticked: the list says *SQLite · pick tables* in the
accent colour until then, and **Import N files** takes you to it.

An uploaded file previews from its first 1 MB, which is more than the
preview reads. JSON, SQLite and Excel previews need the whole file, so over
32 MB the pane asks first with **Load preview (uploads …)**. A SQLite file's
tables or a workbook's sheets are read once: choosing the file again shows
the same list without sending it again. A file added from this machine
previews in place at any size.

A file headed for the raw-text reader that looks binary (a NUL in its first
8 KB; a UTF-16 byte-order mark is exempt) is dropped from the queue with
*Skipped N binary files (not text)*, so an `.exe` or `.evtx` picked by
mistake is never uploaded. Added by path, it fails when its import starts.

### From the command line

```bash
python server.py --case acme.db-winnow --open Security.csv System.csv
```

`--case` opens the case, creating it if it doesn't exist and adding it to the
home screen. `--open` imports each file before the window opens and prints
row counts and warnings to the terminal; without `--case` it creates
`case.db-winnow` in the current folder. `--open` reads every file as
delimited text, whatever its extension — use the Import window for JSON,
SQLite, Excel, Plaso and the rest. Other options are in
[Installation](Installation.md).

## Supported formats

| File | Extensions | You get |
| --- | --- | --- |
| [Delimited text](#csv-tsv-and-other-delimited-text) | `.csv` `.tsv` `.psv` `.txt` | One table, columns from the header row. |
| [JSON](#json-and-json-lines) | `.json` `.jsonl` `.ndjson` | One table, one row per record. |
| [SQLite](#sqlite-databases) | `.db` `.sqlite` `.sqlite3` | One table per table you pick. |
| [Excel](#excel-workbooks) | `.xlsx` `.xlsm` | One table per sheet you pick. |
| [Plaso](#plaso-storage-files) | `.plaso` | One timeline table. |
| [IIS web server logs](#iis-web-server-logs) | named `u_ex…`, `ex…`, `nc…`, `in….log` | One table, columns from the log's own `#Fields:` lines. |
| [Archives](#archives) | `.zip` `.tar` `.tgz` `.gz` `.tbz2` `.txz` `.bz2` `.xz` | A folder, then folder import. |
| [Any other text](#plain-text-and-log-files) | `.log`, `.1`, no extension… | One row per line. |
| [Plugin formats](#plugin-formats) | raw `$MFT` and `$J`, ESXi and UAC logs | Whatever the plugin parses. |

Legacy Excel `.xls` and binary formats such as `.evtx` are refused as
binary. Run EVTX through EvtxECmd first; save an `.xls` as `.xlsx` in Excel.

Every column is stored as text. Winnow guesses a type for each from its
first 500 rows — text, number or datetime: number columns sort numerically,
and datetime columns are the ones the Timeframe filter, Histogram and
Timeline use. The type is fixed at import (the delimited-text preview can
change it); to get a real datetime out of an epoch or other encoded value,
add a [derived column](Derived-columns.md).

### CSV, TSV and other delimited text

**Delimiter.** The extension is believed first — `.csv` comma, `.tsv` tab,
`.psv` pipe — if the file agrees: that character on every sampled line, the
same number of times on at least 90% of them. Otherwise, and for `.txt` and
extensionless files, the content is sniffed for comma, tab, semicolon or
pipe. The line under the file's preview says what was found
(*Comma-delimited (detected) · 12 columns · first 50 rows*), and
**Delimiter** overrides it (**Auto-detect**, **Comma**, **Tab**,
**Semicolon**, **Pipe**). **Read as** → **One line per row** reads the file as
[raw text](#plain-text-and-log-files) instead. KAPE writes some delimited
output as `.txt`, so `.txt` comes here by default; a `.txt` log wants
**One line per row**.

**Header.** **First row is headers** is on by default, and the first
non-blank line is the header. Off, columns are named `col_1`, `col_2`… and
the first line is data. A blank header becomes `col_N`, a repeated one gets
`_1`, `_2`, and `rid`, `rank` or `rowid` get `_1` because those names are
taken. Each column's heading in the preview has a text / number / datetime
picker, and what you pick is kept as you pick it.

**Encodings.** Winnow decides from the first 1 MB:

| File | Read as |
| --- | --- |
| UTF-8, with or without a byte-order mark | UTF-8. |
| UTF-16 with a byte-order mark (Windows PowerShell 5.1's `Out-File` and `>`) | UTF-16. |
| Not UTF-8 and holding no UTF-8 at all (Excel "CSV" on Windows, older tools) | Windows-1252, said in the preview, the finished toast and the table's hover text. |
| UTF-8 with a few bad bytes | UTF-8; each bad byte is read as its Windows-1252 character rather than lost, and counted. |

UTF-16 without a byte-order mark is not detected; save it as UTF-8 first.

**Ragged rows.** The header sets the width. Short rows are padded with
blanks; long rows are cut to fit and their extra fields lost. No row is
dropped, and the toast counts them (*N ragged rows padded/trimmed*). If the
header itself is short, every longer row is trimmed to it — that count is
the only sign.

**Stray quotes.** An unbalanced `"` folds the following lines into one field
until the next quote, and those rows vanish without an error. Rows that
swallowed ten or more lines are counted: *N rows with very long multi-line
fields — check for a stray quote if the row count looks low*. Properly
quoted multi-line fields, such as EVTX XML payloads, stay under that.

**Large fields and bad lines.** One field can hold up to 64 Mi (67,108,864)
characters, so script blocks and base64 blobs import whole. A field past
that, or any line the reader can't parse, stops the import with an error
naming the line, and the rows before it are kept: *Line 1,010,002: … —
import stopped there; the 1,010,000 rows before it were kept*. If no row was
read, no table is left behind.

### JSON and JSON Lines

`.jsonl` and `.ndjson` hold one JSON value per line and stream. A `.json`
file is one document — an array of records, a single object (one row), or a
bare value — and is loaded whole, so memory grows with its size. A value
that isn't an object lands in a column called `value`. The import reads the
file twice, once to find every column, so it is slower than a CSV of the
same size.

**Nested objects**, in the file's pane, chooses how nested objects become
columns:

| Option | `{"user": {"name": "a", "id": 1}, "tags": ["x"]}` becomes |
| --- | --- |
| **Keep as JSON** (default) | `user` = `{"name": "a", "id": 1}`, `tags` = `["x"]` |
| **Flatten** | `user.name` = `a`, `user.id` = `1`, `tags` = `["x"]` |
| **Flatten to depth** N | Objects unfolded N levels; anything deeper stays JSON text. |

Arrays are never split, at any depth — their length varies record to record
— so they stay as JSON text in one column. Columns appear in the order keys
were first seen, and a record without a key gets a blank cell. To lift a
field out of a JSON cell later, use [derived columns](Derived-columns.md).

- Numbers are kept as written: `1.10` stays `1.10`.
- Two values for one column — a repeated key, or a literal `"process.name"`
  beside a nested `process.name` — are both kept; the second becomes
  `process.name_1`, and the preview and toast count the records affected.
- Past 1,000 columns, the first 999 keys stay columns and the rest go, per
  record, into one `Other fields (JSON)` column.
- A broken line in a `.jsonl` is skipped and counted (*N unreadable records
  skipped*); a truncated `.json` document fails the import.
- The preview reads the whole file, so an uploaded `.json` is uploaded in
  full just to preview it, and past 32 MB the pane asks first. Add big ones
  with **Add from this machine…**.

### SQLite databases

A SQLite file's pane lists every table with its row count where the preview
would be. Tick the ones you want (none are ticked at first; **All** and
**None** tick or clear the lot). Each becomes its own table, named
`<file name>.<table>` such as `History.urls`, and all of them import as one
job.

Chromium browsers store times as microseconds since 1601. A column whose
name contains `time`, `utc` or `date` and whose values look like that gets a
button under its table, after *Convert to dates:*, already pressed. Pressed
columns import as ISO 8601 UTC (`2024-05-01T13:07:00.123Z`), and a value
that doesn't convert keeps its number; unpress a button to keep the raw
number throughout.

The database is opened read-only (see [Case files](#case-files) for WAL
databases). BLOBs import as `<N bytes>`. Text that isn't valid UTF-8 gets
`�` in place of the bad bytes, and the toast counts the values. A table this
SQLite can't read, such as a virtual table whose module it lacks, is listed
with *can't be read: …* and can't be ticked; the others still import.

### Excel workbooks

A workbook's pane works the same way: tick sheets, and each becomes a table
(named `<file name>.<sheet>`). Its row counts are estimates from the sheet's
declared size; the import reads every row there is. `.xlsm` imports the same way, and macros never run.

- The header is the first non-empty row, so title rows above it are skipped.
  Data wider than the header lands in `col_N` columns.
- Date-formatted cells become text: `2024-05-01 13:07:00`, or `2024-05-01`
  at exactly midnight, or `13:07:00` for a time of day.
- Numbers lose float noise (`42`, not `42.0`); booleans read `TRUE` and
  `FALSE`.
- A formula imports as its last saved result — empty for a workbook written
  by a tool and never saved in Excel.
- Short rows are normal in Excel and pad silently; only rows wider than the
  table count as ragged.

Folder import skips SQLite files and workbooks, since which tables or sheets
to take is a choice per file.

### Plaso storage files

A `.plaso` file from log2timeline imports as one table, with no options and
no Plaso install. Old and new storage formats both work. The columns are
`Datetime`, `Timestamp desc`, `Data type`, `Parser`, `Source file`, `Host`,
`User` and `Attributes (JSON)`. `Datetime` is UTC to the microsecond and the
rows are in time order, so the unsorted table reads as a timeline.
Everything else an event carries is in `Attributes (JSON)`; pull fields out
with [derived columns](Derived-columns.md). An event that can't be decoded
still gets a row, with an error note in `Attributes (JSON)`.

### IIS web server logs

IIS names a site's logs after their format and how often they roll over, and
Winnow recognises them by that name, wherever they sit (usually
`inetpub\logs\LogFiles\W3SVC<n>\`). No other `.log` is read this way.

| Name | Format |
| --- | --- |
| `u_ex240501.log`, `ex240501.log` | W3C extended, one file a day. Hourly is `u_ex24050113.log`, monthly `u_ex2405.log`, weekly `u_ex240502.log` (the week of the month); `u_extend1.log` rolls over by size or never. |
| `u_nc240501.log`, `nc…`, `ncsa1.log` | NCSA common log format. |
| `u_in240501.log`, `in…`, `inetsv1.log` | IIS native format. |

`u_` means IIS wrote the file as UTF-8; without it the file is in the server's
code page, which reads as Windows-1252 and is said so.

**W3C columns come from the log itself.** Which fields a site logs is its own
setting, so each `#Fields:` line names the columns for the lines under it, and
the table has every field any of them names. IIS writes a new header whenever
the site restarts, and a changed setting changes the fields from that point
on: each line's values land under the field its own header names, and a field
its header doesn't have is blank on that line. The first column,
`Timestamp (UTC)`, joins `date` and `time` (always UTC in a W3C log) so the
Timeframe filter, Histogram and Timeline work straight away; `date` and `time`
stay too. Values are kept exactly as logged, including `-` for an empty field
and `+` for a space in `cs(User-Agent)`.

A line with more values than its header names, or one before any header,
keeps the extras in an `Unnamed values` column, which only appears when a file
needs it; a short line is padded. Both count as ragged rows in the import
toast.

**NCSA** lines become `Timestamp (UTC)` (the logged time with its offset
applied), `Remote host`, `Remote log name`, `User name`,
`Timestamp (as logged)`, `Request`, `Status` and `Bytes`. **IIS native** lines
become `Timestamp (local)` and the format's fifteen fields, `Client IP address`
to `Parameters`; native format logs the server's local time with no offset,
so that timestamp is not converted. In both, a line that isn't in the format
is kept whole in an `Unparsed line` column.

An IIS log's pane previews the first rows as they will import, from the
start of the file; for W3C the line under the preview says when a `#Fields:`
line further in can still add columns. **Format** picks **W3C extended**,
**NCSA common** or **IIS native**: the one the name says is picked, and the
other two are there for a log that was renamed or isn't what its name says.
**Read as** → **Delimited** or **One line per row** reads the file as any
other text file. Each column's type can be changed; a type set here sticks
to that column by name, so a column a later header adds keeps its detected
type.

A server that loses power mid-write leaves a run of zero bytes in its log,
and IIS carries on after them when it comes back. The text either side of the
gap imports as separate rows, and **Case ▾ → Log…** notes that the gap was
there. A log IIS is still writing can be imported in place: the lines it adds
during the import are left for next time.

Default-configured W3C tables, and NCSA and native ones, get a one-line
summary on the [Timeline](Timeline-search-and-watchlist.md): the URL, method,
status and client address.

### Plain text and log files

A file no other reader claims — `hostd.log`, `auth.log.1`, an extensionless
dump — imports as raw text: one row per line, the whole line in a single
`Message` column, no header, nothing split. The row number is the line
number: blank lines are kept as empty rows, and only a newline ends a line,
so a carriage return inside a logged value can't fake an extra row. Binary
files are refused. **Read as** → **One line per row**, in the pane of any
file that has a **Read as**, forces that file into this reader.

### Archives

`.zip`, `.tar`, `.tgz`/`.tar.gz`, `.tbz2`/`.tar.bz2` and `.txz`/`.tar.xz`
archives and single gzipped files (`hostd.0.gz`) expand, then import. A
`.bz2` or `.xz` that is a single compressed file, not a tarball, is not
supported.

**Import N files** expands the archive into a new folder named after it
(`bundle.tgz` → `bundle-extracted`) — beside the archive when added from
this machine, beside the case file when uploaded — and opens
[folder import](#folders-and-kape-output) on it so you choose what to import.
Archives inside are expanded in place, up to six levels deep, and a gzipped
rotated log becomes the plain file. The original is never touched. With
several archives queued, the folder window opens on the first and a toast
names the others' folders.

The toast names the first skipped and first renamed file; **Case ▾ → Log…**
lists them all.

- Members whose path would escape the folder, links and devices in a tar,
  and encrypted zip members are skipped.
- A damaged member is skipped and the rest expand; a truncated archive keeps
  what came before the damage. Large zips made by Windows Explorer can use
  Deflate64, which Winnow can't read — extract those with Explorer or 7-Zip
  and import the folder.
- Two members with one name are both kept; the later becomes
  `name (2).ext`.
- Expansion stops, leaving a partial tree, at 32 GB or 200,000 files.

**ESXi support bundles and UAC collections** are mostly `.log` files, and
folder import's default file types leave `.log` out. Turn the **esxi_logs**
plugin on first: creating the case with the **ESXi / UAC triage** profile
does it and adds that profile's dashboard, or switch it on in
**Settings → Plugins**. It claims `.log` and the ESXi log names, parses them
into one schema and adds a `.log` chip to folder import. Without it, turn on
**other text files** to bring the logs in as raw text.

### Folders and KAPE output

**Import a whole folder…** imports every matching file under a directory — a
KAPE triage output, an expanded archive. The window lists your profiles down
the left and shows the folder, its settings and what the scan found on the
right. It stays the same size whatever you pick or the scan finds.

1. **Browse…** to the folder.
2. Pick a profile on the left, or stay on **Custom (not saved)** to set
   things up yourself. A profile fills in everything below it.
3. Pick **File types**. `.csv`, `.tsv`, `.txt`, `.psv`, `.json`, `.jsonl`,
   `.ndjson`, `.plaso`, **IIS logs** and any extension an enabled plugin claims
   are on; **other text files** (off) adds every other non-binary file as raw
   text. **IIS logs** picks up [IIS web server logs](#iis-web-server-logs) by
   name, and a file named like one is read as one however it got in.
   **Include subfolders** (on) walks the tree.
4. Narrow with **Patterns**: **Include** and **Exclude**, one glob per line,
   case-insensitive. A pattern containing `/` matches the path from the
   chosen folder (`RegistryHives/*`); one without matches the file name
   anywhere (`*_Amcache_UnassociatedFileEntries.csv`). Include patterns only
   narrow what the file types let through.
5. Check the **Preview**, which updates as you type. **Will import** lists
   the matches with a box per file; **Excluded** lists the rest with each
   reason (`extension`, `IIS logs not selected`, `no include pattern matched`,
   `excluded by pattern: …`, `binary`, `unreadable`, `not a regular file`).
   A profile saved before **IIS logs** existed leaves them out until you tick
   it and **Update profile**.
   The line under the list counts them.
6. **Import checked (N)**.

Each table is named after its file. Files in subfolders are filed in table
folders mirroring the tree and arrive closed — open them from the sidebar —
so a hundred files don't become a hundred tabs; files at the top level open
as tabs. A file already imported from that path shows *already in case*
and starts unticked. Plugin-claimed files use the plugin's default options.
SQLite files and workbooks are never picked up and archives in the folder are
not expanded; import those on their own. The scan stops at 5,000 files and
says so.

**Profiles.** **Save as profile…** stores the file types, patterns and
subfolder choice under a name, for every case on this machine, and lists it
under **Profiles**. With a profile chosen, **Update profile** saves your
changes to it and **Delete profile** removes it; what it held stays on
screen under **Custom (not saved)**.

**Finding a profile.** Type in the **Search…** box at the top of the list to
narrow it. A profile is found by its name or by one of its patterns or file
types, and then says which one under its name: `unassociated` finds a
profile that excludes `*_Amcache_UnassociatedFileEntries.csv` and shows
*excludes \*_Amcache_UnassociatedFileEntries.csv*. The arrow keys move through the
list, and `Enter` in the search box picks the first match. `Esc` clears the
search; with the search empty, it closes the window.

**Pinning favourites.** Point at a profile and click the ☆ beside it to pin
it to a **Pinned** section at the top of the list. Click its ✕ to put it
back. Pins stay in this browser, like the ones in the
[derived column](Derived-columns.md) list.

### Plugin formats

Two bundled plugins add formats; both ship switched off (**Settings →
Plugins**). **mft_usn** reads a raw NTFS `$MFT` and `$UsnJrnl:$J`, recognised
by name (`$MFT`, `$J`, `*$UsnJrnl*`…), without EZTools. **esxi_logs** reads
ESXi support-bundle and UAC Linux host logs. A claimed file shows the
format's name in the queue, and its options, if it has any, in the file's
pane. Built-in extensions always win, so no plugin can take over `.csv` or
`.json`, and neither do built-in names: **esxi_logs** claims `.log`, but an
[IIS log](#iis-web-server-logs) is still read as one. Plugin
imports don't run as background jobs: they show an *Importing …* notice, not
a progress row, and can't be cancelled. See [Plugins](Plugins.md).

## Recognised tool output

Winnow knows the header sets common forensic tools write, and recognises a
table by its column names whatever the file was called (order and letter
case don't matter):

| Tool | Header sets |
| --- | --- |
| EvtxECmd | Event logs (EvtxECmd) |
| AmcacheParser | Amcache — file entries, device containers, device PnPs, driver binaries, driver packages, program entries, shortcuts |
| AppCompatCacheParser | Shimcache (AppCompatCache) |
| PECmd | Prefetch (PECmd), Prefetch timeline (PECmd) |
| RBCmd | Recycle Bin (RBCmd) |
| WxTCmd | Windows Timeline — activity, operations, package IDs |
| JLECmd | Jump lists — automatic, custom |
| LECmd | LNK files (LECmd) |
| SBECmd | Shellbags (SBECmd) |
| MFTECmd | NTFS $Boot, NTFS $MFT, USN journal $J, NTFS $SDS |
| RECmd (batch) | Registry (RECmd batch) |
| SrumECmd | SRUM — app resource use, network usage |
| esxi_logs plugin | ESXi / Linux host logs |

Recognition gets you:

- **A name** in place of a raw column list, in the **Filters** menu and in
  **Merge tables…**.
- **Shipped triage filters** that light up when a matching table opens:
  twenty for EvtxECmd (logons, RDP, Defender tampering, services, scheduled
  tasks…), five for RECmd batch output, three for MFTECmd `$MFT`. See
  [Filtering and searching](Filtering-and-searching.md).
- **Timeline summaries**: a tagged row reads as one line ("Special privileges
  assigned  svc_backup · WKSTN-4471 · id 4672") instead of the whole row.
  See [Timeline, Search all and the Watchlist](Timeline-search-and-watchlist.md).
- **Shipped dashboards**, such as the KAPE triage profile's, which find their
  tables by header set. See [Dashboards](Dashboards.md).

You can name your own header sets in the table menu's
**Name & merge** section, under **These N columns**.

## Import jobs

Every import runs in the background, tracked in the panel at the bottom
right — the same panel that shows search-index builds.

| Row | Shows | ✕ |
| --- | --- | --- |
| uploading | A browser upload's transfer, in MB. | Cancels the upload. |
| importing | Progress and rows so far; a multi-table job reads `name — 2/5: table`. | Cancels the import. |
| *N* queued | Imports waiting. Two run at once. | Cancels all of them. |
| done, error, cancelled | The result. Finished rows clear after a few seconds; errors stay. | Dismisses the row. |
| indexing | A search index being built. | — |

**Clear all** dismisses every finished row and leaves running work alone.
Reloading the page mid-import picks the jobs back up. Progress counts bytes
for delimited and raw text, records for JSON, and rows for SQLite, Excel and
Plaso.

**Cancelling drops the table entirely**, so a half-imported table never sits
in the list looking complete; a SQLite or Excel job keeps the tables it had
finished. An import that fails with an error keeps what it read before the
problem. Opening another case or shutting Winnow down cancels running
imports.

A finished import's toast says *name: N rows imported*, plus any of:

| Warning | Means |
| --- | --- |
| *N ragged rows padded/trimmed* | Rows with the wrong number of fields. |
| *N rows with very long multi-line fields — check for a stray quote…* | A possible unbalanced quote. |
| *N unreadable records skipped* | Broken JSON Lines. |
| *N records with two values for one key — the later kept as &lt;key&gt;_1* | JSON key clashes. |
| *not UTF-8 — read as Windows-1252* | The whole file was Windows-1252. |
| *N bytes not valid UTF-8, read as Windows-1252* | Stray bytes in a UTF-8 file. |
| *N text values were not valid UTF-8 (shown with �)* | SQLite text with bad bytes. |

**Case ▾ → Log…** keeps every import, failure and warning for the session.
New tables join the tab strip without moving you; turn on
**Settings → Imports → Open new tables when an import finishes** to jump to
the first table of each batch. A column that looks like an encoded timestamp
brings a toast offering **Add datetime column**
([Derived columns](Derived-columns.md)), and each new table is scanned
against the case's watchlist
([Timeline, Search all and the Watchlist](Timeline-search-and-watchlist.md)).

## The search index

After each import Winnow builds a full-text index of the table in the
background: an *indexing* row in the panel, then *Search index ready for …*.
The table is usable at once; until the index lands, search scans the table —
the same results, slower. The index is what makes **Contains** and
**Advanced** search and **Search all** fast
([Filtering and searching](Filtering-and-searching.md)), and it takes space
in the case file. **Case ▾ → Tables…** shows which tables have one.

The build works in short steps, so tagging, undo and grouping stay quick
while it runs; on a large event log it can take a while. Closing the case
before it finishes keeps what is done: the next build, on the first search
or from **Build** in the Tables manager, carries on from where it stopped.

`--no-fts` skips the index for files imported with `--open`; the first
Contains or Advanced search, or Search all, on such a table builds it anyway.
If the SQLite library in your Python is older than 3.45, no index is built
and every search scans.

## Importing the same file again

Importing a file again makes a second, independent table with the same name.
Tags and notes belong to the table they were made on, so the copy starts
clean, and only folder import flags the repeat. To replace a table, import
the new copy and remove the old one in **Case ▾ → Tables…**; renaming, folders,
removing and merging tables are covered in
[Working with tables](Working-with-tables.md).

Winnow records how each file was read — reader, delimiter, header, flatten
mode, table or sheet — so loading a session file into a case that lacks a
table can re-import it the same way, if the file is still at the path it was
imported from. That works for files added from this machine, not uploads:
an upload records only the file's name, so the load says so and you import
the file yourself.
See [Sessions and export](Sessions-and-export.md).
