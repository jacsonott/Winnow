# Winnow

**A fast, local viewer for the enormous CSVs that DFIR triage produces** —
EvtxECmd, MFTECmd, Amcache, the whole KAPE output folder — with the
row-tagging workflow that turns a million log lines into a findings list.
Think Timeline Explorer, rebuilt for big files: a 1.2-million-row CSV
imports in ~8 seconds, filtering it takes milliseconds, and scrolling never
stutters no matter how deep you are.

![The grid mid-triage: an EvtxECmd table under the shipped Logons filter, tagged rows on the rail](docs/screenshots/grid.png)

Everything runs on your machine — one Python server, no cloud, no CDN, no
build step. It works on an airgapped analysis box, and your work (tags,
notes, saved views) lives in a single SQLite case file you can hand to
another analyst. The evidence files themselves are **never modified**.

## What you get

- **Tags with hotkeys** — press `1` to mark a row TA, `2` suspicious.
  Tag counts live in the ribbon; a rail down the grid's edge shows where
  your findings cluster in the whole filtered view. Undo included.
- **Filters that keep up with you** — type `4624` or `!svchost` or
  `>1000` straight into a column header, pick values Excel-style from the
  header dropdown, or build AND/OR trees in the guided builder. A set of
  **ready-made triage filters ships in the box** (logon sweeps, RDP both
  directions, Defender tampering, persistence run keys, …) and appears
  automatically when a matching table opens.
- **Time navigation** — pin a timeframe that survives every filter change,
  jump to the same timestamp across tables, derive real sortable datetime
  columns from epochs/FILETIME/Excel serials/whatever your tool emitted.
- **A unified timeline** of every tagged row across every table in the
  case, in one chronological stream.
- **Pivot tables, session bookends, NTFS parsing** and more via drop-in
  plugins (five ship with the app), plus a read-only SQL pane when you
  want to write the query yourself.

## Run it

```bash
pip install fastapi "uvicorn[standard]" python-multipart
python server.py --case case.db --open timeline.csv
```

Prefer not to touch a terminal? The `launch/` folder has double-click
launchers — `winnow.sh` (Linux), `winnow.command` (macOS), `winnow.bat` /
`winnow.vbs` (Windows) — that find Python and start it for you. See
[launch/README.md](launch/README.md).

Opens http://127.0.0.1:8777 in an **app window** — a browser window with no
address bar, tab strip or bookmarks, and its own taskbar entry, so Winnow
looks like the application it is rather than one of your twenty tabs. It
uses whichever Chromium-family browser it finds (Edge first on Windows,
since it ships with the OS; Chrome first elsewhere) and falls back to an
ordinary tab in your default browser if there isn't one. You can also skip
`--open` and import from the UI.
The home screen's ⏻ button (or Session → "Shut down Winnow…") stops the server
again from the UI — everything is already saved in the case file.
A server whose browser windows have all been closed shuts itself down after
a couple of minutes on its own (once nothing is importing and no download is
still running) — so a forgotten Winnow no longer holds a case lock all
weekend. `--no-idle-shutdown` keeps it running for script-driven use.

| flag | meaning |
| --- | --- |
| `--case FILE` | SQLite case file, created if missing. Everything lives here. |
| `--open A.csv B.csv` | Ingest files at startup |
| `--no-fts` | Skip the full-text index. Roughly halves import time; search falls back to substring matching |
| `--port`, `--host` | Defaults 8777 / 127.0.0.1 |
| `--no-browser` | Don't auto-open a browser |
| `--browser-tab` | Open an ordinary tab instead of an app window |
| `--browser-profile DIR` | Give the app window its own browser profile — isolates Winnow from your extensions, but appearance, keybindings and panel sizes live in that profile, so a new one starts from defaults |
| `--force` | Open `--case` even if another Winnow already has it open |

A case file is meant to be open in **one** Winnow at a time. Each running
server leaves a `<case>.winnow-lock` marker beside its case file, and opening
a case another server still holds tells you who has it (user, host, when) and
asks before continuing — the CLI refuses outright, and `--force` overrides.
Going ahead anyway is supported but nothing merges the two: neither server
sees the other's tags, notes or imports until it reloads, and a long write in
one starts failing writes in the other. If the case file lives on a network
share, don't — SQLite's WAL journalling needs shared memory that SMB and NFS
don't provide. Two analysts on one investigation should work in separate case
files and merge with session files, which is what tag remap-by-name is for.

## Updating

Winnow keeps your work *inside its own folder* — saved filters, tag
templates, the case list, your `cases` folder setting, installed plugins,
session exports. So "download the new one and replace the folder" throws
all of that away. Update instead:

**From the app** — Settings → Updates → *Check for updates*. It tells you
what's available and installs it on request. Winnow never checks on its
own: no startup ping, no background poll.

**From a terminal**, in the Winnow folder:

```bash
python update.py --check      # what's available, changes nothing
python update.py              # check, show the plan, install
python update.py --rollback   # undo the last update
```

**On an airgapped analysis box**, fetch it on a machine that has network:

```bash
python update.py --download-only --dest /media/usb
```

then carry it over and apply it in place:

```bash
python update.py --from /media/usb/winnow-1.1.0.zip
```

**Beta testing, or working on Winnow itself?** `python update.py --dev`
syncs to the tip of the `develop` branch instead of the latest release —
unreleased code, with the same backup, the same protected paths and the
same `--rollback`. The install records that it came from develop (in
`.winnow-install.json`) and Settings → Updates shows the build it is on,
because a box running code no release was cut from is a thing to know
before quoting a version number in a report.

Either way, only the program files are replaced. `workspace/`, `plugins/`,
`sessions/` and every case file are never read, written or deleted, and
the version you were on is backed up first. Restart Winnow afterwards —
the running server keeps executing the code it already loaded.

## Measured on this machine

1.2M rows × 10 columns (169 MB CSV):

| | |
| --- | --- |
| Import | 8.2 s (~147k rows/s) |
| FTS5 index build | 8.8 s |
| Filter + sort 171k matching rows | 0.6 s |
| Fetch a page 150,000 rows deep | 1 ms |
| Full-text search across all columns | 8 ms |
| Tag all 171k rows in a view | instant |

## Using it

![Excel-style value picker on a column header](docs/screenshots/value-picker.png)

Filters are typed straight into the box under each column header:

| type | means |
| --- | --- |
| `svchost` | contains |
| `!svchost` | does not contain |
| `=4624` | exact |
| `^C:\Users` | starts with |
| `>1000` | greater than (numeric columns cast to REAL) |
| `/re/` | regular expression |
| `""` | empty |
| `*` | not empty |
| `a\|b\|c` | any of |

Keys: `↑↓`/`jk` move, `Shift+↑↓` extend selection, `PgUp`/`PgDn`, `g`/`G` for
top and bottom, `1`–`9` toggle a tag on the selection, `Shift+1`–`9` apply a tag
to **every** row in the current view, `/` search, `f` filters to the value in
the cell you're on (`Shift+F` does that *and* drops every other filter — the
timeframe filter stays), `q`/`w` cycle the saved filters (aliases `[`/`]`),
`e` the Filter builder, `v` the value picker for the cell you're on, `a`
toggles the timeframe filter (`A` configures it), `C` the table menu, `n` note,
`?` help. `Ctrl`/`⌘`+`Z` undoes the last tag you applied or removed — press it
again to keep stepping back. Undo reverses exactly the rows that op
*changed*, so tagging a selection that overlaps rows you'd tagged earlier
and then undoing leaves the earlier ones alone. `Alt`+`1`–`0` switches tabs: `Alt`+`1` is whichever table you were
last in, `Alt`+`2` onward are the page tabs in strip order (so they follow a
reorder rather than being nailed to SQL/Timeline). `J` jumps to the row nearest
a timestamp you type — the moment is remembered across tables, so `.` jumps
straight to it again in whichever table you're looking at. `X` toggles the
current grouping off and back on without touching the filters (`x` drops it
outright). `Q` opens the current
filter/sort/search as a ready-made query in the SQL pane.

Saved filters can carry a grouping: save while grouped and applying the filter
brings the grouping back; clearing filters (`c`) clears the grouping with them.
Reorder a header set's saved filters with ▲/▼ or by dragging rows in the Saved
filters list — that order is the `[` / `]` cycle order. The Timeframe filter
dialog can fill its range from your tagged rows — earliest to latest across any
tag, or just the tags you toggle on.

The `▾` on each filter box opens that column's values — every distinct value
with a count, ticked or unticked, the way Excel's header dropdown works — and
applies what you tick as an ordinary filter. Reading those values is a scan, so
it's on by default only under 250,000 rows; the table menu turns it on or off
for the whole table or one column, and a row's right-click menu offers it for
any column regardless.

**Right-click** does the obvious thing in five places: a row (tag it, filter to
or exclude the cell you clicked, copy), a group header (tag or untag every row
in the group in one go, without expanding it), a column header (display format,
add a datetime column from it, flatten JSON/XML out of it, the derived-column
actions), the detail pane (see below), a tab or a sidebar table name (the table
menu — columns, value dropdowns, layout defaults; also on `C`).

Click a column header to sort, `Shift`-click to add a secondary sort.

The narrow strip down the right edge of the grid is a rail showing where tagged
rows sit in the whole filtered view — so you can see clustering in a 200k-row
result without scrolling through it. The count on each tag in the ribbon is
scoped to what you're looking at — filter or search and it becomes "how many of
*these* are tagged", with the whole-table number in the tooltip.

## Grouping

Drag a column header into the **Group by** strip to bucket the view by it —
counts per value, expand a group to see its rows. Drag in a second column for a
nested breakdown (Process, then User within each process); drag the pills to
reorder the nesting, and the strip's Sort button orders groups by count or by
value. A datetime column groups by calendar day.

**+ Tag** adds a level that buckets by the tags on the rows instead: one group
per tag, plus everything untagged. It's the one grouping whose counts can add
up to more than the view holds — a row with two tags is in both groups, which
is the point. It nests either way round, so "Lateral movement, broken down by
Computer" and "each Computer, broken down by tag" are both a two-pill grouping.

Grouped rows are ordinary rows: click, `Ctrl`-click and `Shift`-click to
select, right-click for the row menu, tag with `1`–`9`, open the detail pane,
copy. A group header's own menu tags or untags the whole group server-side,
which works on a collapsed group and on an outer nesting level — neither needs
the rows paged in first.

## Tabs

![The unified Timeline: every tagged row across the case, one stream](docs/screenshots/timeline.png)

The header bar carries two strips: your open tables on the left, and the pages —
SQL, Timeline, anything a plugin pinned there — on the right. Both reorder by
dragging a tab along its strip, both scroll when there are more tabs than room,
and the divider between them sets how much of the bar each gets (double-click it
to go back to sizing itself). The sidebar down the left lists all of it as a
standing list — every table in the case, open or closed, and every page — with
▲/▼ on each row for when dragging a strip that's scrolled out of view is more
trouble than it's worth. Tab order and the divider are remembered per browser.
`Alt`+`1`–`0` switches between them from the keyboard — `Alt`+`1` back to the
table you were last in, `Alt`+`2` onward down the page strip.

## Timestamps

Logs arrive with whatever timestamp shape the tool that wrote them felt like.
Right-clicking a column header opens its options, including **Add datetime
column from this…**, which reads the column and adds a *new* one holding a
real, sortable datetime — the original is never modified, and neither is the
file on disk.

Winnow samples the column and suggests a format, with a live preview of what
each value becomes before you commit to it. It reads Unix epochs (seconds
through nanoseconds, auto-ranged), Windows FILETIME (decimal or hex),
WebKit/Chrome timestamps, Mac absolute/Cocoa time, .NET ticks, Excel serial
dates, ISO 8601 (with or without fraction and offset), `dd Mmm yyyy`,
`MM/DD/YYYY` with AM/PM, compact `YYYYMMDDhhmmss`, Apache access-log and
RFC 2822 dates — and old BSD syslog (`Mmm dd hh:mm:ss`), which carries no year:
you give it the year of the first line and it rolls forward on its own when the
file crosses New Year.

Values with an explicit offset are converted to UTC. Values without one are
kept exactly as written unless you set the source's fixed UTC offset. Anything
that can't be parsed is left empty and counted, and the column's menu offers
**Show N unparsed rows** so you can look at what didn't convert rather than
wonder. A second operation computes the **duration between two datetime
columns**, in case you want dwell time or clock skew as a sortable number.

Derived columns sort, filter, group, feed the timeframe filter and the
Timeline, and appear in exports, like any other column. They're marked `ƒ` in
the header. Session files carry the *definition*, not the values — importing
one against the same evidence recomputes it.

Display format is separate from all of that, and is presentation only: the
stored and exported value is always the text the file came with. Set it per
column by right-clicking its header, or set a default for the case and for
every case on this machine under **Settings → Timestamps**. The default is
`YYYY-MM-DD HH:MM:SS`; "As stored" is still there if you want the raw text.

## Nested JSON and XML

Plenty of logs put a whole document in one cell — EVTX `EventData`, cloud
audit `requestParameters`, EDR telemetry blobs. The grid can only show that
as one long unreadable string, and you can't sort, filter or group by
something buried inside it.

Double-click a row to open the **detail pane**, and any field holding JSON or
XML is pretty-printed and syntax-coloured. Every node in it is addressable:
right-click one and **Add as a column** builds a real column holding that
field from every row. No path syntax to learn — the path comes from the node
you clicked.

**Flatten JSON/XML into columns…** (a column header's right-click menu, or the
detail pane's node menu) does the whole document at once. It samples the
column, lists every field it finds with what fraction of the sample carried it
and an example value, and builds a column per field you tick — all in one pass
over the table rather than one pass each. Fields present in every sampled row
start ticked; ones that are present but always empty (a `<TimeCreated
SystemTime="…"/>` container, whose value is really on the attribute) sort to
the bottom and start unticked.

Extracted columns are ordinary derived columns: they sort, filter, group, feed
the Timeline, appear in exports, are marked `ƒ`, and travel in session files as
a *definition* that recomputes against the same evidence. The source table is
never touched. Rows where the field wasn't there are empty and counted — the
column's menu offers **Show N rows without this field**.

Paths are written the way you'd expect, and you can edit one by hand
(**Change the field path…** on the column's menu):

| | |
| --- | --- |
| `$.user.name` | a JSON field — the leading `$` is optional |
| `items[0].id` | into an array |
| `["odd.key"].v` | for a key containing a dot |
| `Event/System/EventID` | an XML element's text |
| `Provider@Name` | an XML attribute |
| `Data[@Name='LogonType']` | the repeated element with that attribute |
| `Data[2]` | the third same-named sibling |

That `[@Name='…']` form is why Windows event logs come out useful:
`EventData` is a run of identical `<Data Name="…">` elements, and addressing
them by name gives you a `LogonType` column that means the same thing in every
row, where addressing them by position would give you a `Data[4]` that doesn't.

XML that declares a `<!DOCTYPE>` is not parsed at all — evidence is untrusted
input and entity expansion isn't a risk worth taking for a shape no log field
has. Malformed or truncated XML still renders (unhighlighted), it just has
nothing addressable in it.

## The detail pane

Double-click a row (or press `d`) for the full-value view of every field, plus
the note box. Right-clicking in it gives you whichever of two things you're
pointing at, and often both:

- **Highlighted text** — copy it, filter the column to it, filter to it and
  drop everything else, exclude it, or search every column for it. A selection
  out of the middle of a document is a fragment, so it filters as *contains*
  rather than as an exact match.
- **A node of a parsed JSON/XML document** — add it as a column, filter the
  column to that value, copy the value or the path, or open the flatten picker.

## Sessions

**Session → Save session file** writes a small JSON containing tag definitions,
every tag assignment, notes, column layout and saved views, plus a hash of the
source file. Load it against the same evidence on another machine and it warns
you if the hash doesn't match. Tags are remapped by name on import, so two
analysts who both invented a "Lateral movement" tag end up merged rather than
duplicated.

**Export** writes the current view — filters, sort and search applied — as CSV
with `Line`, `Tags` and `Note` columns prepended, or just the tagged rows.

## SQL pane

Opens a read-only connection to the case file. `src_1`, `src_2`… are your data;
`row_tags`, `row_notes`, `tag_defs` are the sidecars. So this works:

```sql
SELECT t.name, s.Process, count(*) n
FROM row_tags rt
JOIN tag_defs t ON t.id = rt.tag_id
JOIN src_1 s ON s.rid = rt.rid
GROUP BY 1, 2 ORDER BY n DESC;
```

## Plugins

![The pivot plugin: hosts by event id](docs/screenshots/pivot.png)

Winnow can be extended without touching its source, Notepad++-style.
**Settings → Plugins** manages everything — effective immediately, no
restart. Each plugin has a scope: **on/off for all cases** (this
machine's default) or **on/off for this case only** — the per-case
choice is stored in the case file, so "this investigation needs the
pivot tab" survives handing the case to another analyst. A disabled
plugin's code is never even imported. "Install a plugin…" copies a
`.py` file or a plugin folder picked from anywhere on disk into
`plugins/` for you (and explains which of the two you need); dropping a
plugin into the folder by hand works too.

Plugins get three extension points:

- **Ingest formats** — parsers for file formats the app doesn't natively
  read, which then behave like built-ins everywhere: drag-and-drop,
  Import files…, folder import, and a per-format picker in the same
  Settings panel, with rows flowing into the same read-only `src_`
  tables as any CSV (so tagging, views, FTS, sessions and the SQL pane
  all just work).
- **Tabs** — a page tab alongside SQL and Timeline, reorderable with
  them like any other tab, whose content is entirely the plugin's own
  UI: an ES module the plugin ships, mounted into the main content area
  with a stable context object (API helpers, read-only SQL against the
  case, live source/tag state, the app's own theming).
- **API routes** — backend endpoints under `/api/plugin/<name>/…` for
  whatever the plugin's UI needs the server to do: query the case, run a
  computation, call an external service.

Five worked examples ship in `examples/plugins/` and are already listed
in Settings → Plugins — no install step, switched off until you enable
them:

- **`mft_usn/`** — raw NTFS `$MFT` and USN-journal (`$J`) parsing in
  pure stdlib Python (no MFTECmd/EZTools, no .NET — airgap-friendly):
  full paths reconstructed from parent references, `$SI`/`$FN`
  timestamps side by side with an `SI<FN Created` timestomp flag, USN
  reason flags decoded, and MFT entry/sequence numbers that let the two
  tables join in the SQL pane.
- **`lateral_movement/`** — a pinned tab that draws source→destination
  pairs from any table (4624s, firewall logs, netflow) as a
  force-directed graph: edge width is event count, arrows show
  direction, drag to untangle. Fully offline.
- **`first_last/`** — group events (host, user, anything) and keep each
  group's first and last row with a templated description — "First of
  312 | WKSTN-014 | user: jsmith" — as a new, taggable table. Turns tens
  of thousands of logon events into a page of session bookends.
- **`pivot/`** — Excel's PivotTable over any ingested table: drag
  fields into Rows, Columns, Values and Filters for a cross-tab with
  subtotals and grand totals, click a cell for the rows behind it, copy
  or export the result. Distinct counts stay correct at every subtotal
  level because each level is aggregated from the source rather than
  summed from the cells above it.
- **`claude_assistant/`** — a pinned Claude chat tab that sees the
  case's *schema* (never row data) and writes ready-to-paste SQL pane
  queries. Needs network + an Anthropic API key, which is exactly why
  it's an opt-in plugin rather than a feature — see its README.

**[docs/writing-plugins.md](docs/writing-plugins.md) is the developer
guide** — quickstart, all three hooks, the tab context object, testing,
and troubleshooting. The contract itself is also documented at the top of
[`plugin_api.py`](plugin_api.py); a minimal format is ~20 lines. Extra
plugin directories: `--plugins-dir DIR` or `$WINNOW_PLUGINS_DIR`. A
plugin that fails to load is listed with its error (Settings → Plugins
and the startup output) and skipped, never fatal.

A plugin is arbitrary local Python running with Winnow's own privileges —
installing it (from the UI or by hand) is the consent step, and nothing
is ever fetched from a network. Only install plugins you have read or
trust.

## Under the hood

For the curious (nothing here is needed to use it):

**Source tables are read-only.** Each import becomes `src_<id>` with an explicit
`rid INTEGER PRIMARY KEY`. Tags, notes, column layouts and saved views live in
sidecar tables keyed by `(source_id, rid)`. Re-import the same file and your
work is still there. Nothing ever writes back to the CSV.

**Scrolling stays O(window).** Naive `LIMIT/OFFSET` on a filtered sort degrades
badly once you're a few hundred thousand rows deep, because SQLite has to walk
every skipped row. Instead, changing a filter or sort materialises the result
once into a temp-attached table of `(pos, rid)`:

```sql
CREATE TABLE v.view_7 AS
SELECT ROW_NUMBER() OVER (ORDER BY "Timestamp" COLLATE NOCASE ASC, rid) AS pos, rid
FROM src_1 WHERE "Process" LIKE '%powershell%';
```

The grid then pages with `WHERE pos BETWEEN ? AND ?`, and the row count comes
free. Views live in a temporary database deleted when the server exits, so the
case file stays clean. Page reads (and every other pure-read path — grouping,
exports, search counts) run on pooled read-only connections, so a multi-second
view build or import never stalls scrolling in another tab. The pages either
side of the viewport are warmed while the browser is idle, so crossing a page
boundary is a cache hit rather than a visible stall.

**Search** uses an external-content FTS5 table over every column, tokenized to
keep `.`, `-`, `_`, `\`, `@` and `:` inside tokens so paths, GUIDs and account
names survive tokenization. Bare terms are quoted before they reach the FTS
parser; `AND` / `OR` / `NOT` / `prefix*` pass through.

**Search all tables** sweeps every table in the case, open or closed, in the
background — paste a list of IOCs, one per line, and you get a row per table
that matched *and* a row per indicator underneath it, so you can see which of
your 60 hashes hit and where rather than just that something did. Each row
opens that table filtered to that term. A mixed AND/OR/NOT query from the
Advanced builder gets the single per-table count instead: its terms constrain
each other, so a count for one of them alone would describe a query nobody ran.

