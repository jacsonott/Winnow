# Winnow

A local web app for reading very large delimited files out of SQLite, with the
row-tagging workflow that Timeline Explorer gets right and generic SQLite GUIs
don't. FastAPI + SQLite on the back, vanilla JS virtualized grid on the front.
No cloud, no CDN, no build step — it runs on an airgapped analysis box.

## Run it

```bash
pip install fastapi "uvicorn[standard]" python-multipart
python server.py --case case.db --open timeline.csv
```

Opens http://127.0.0.1:8777. You can also skip `--open` and import from the UI.

| flag | meaning |
| --- | --- |
| `--case FILE` | SQLite case file, created if missing. Everything lives here. |
| `--open A.csv B.csv` | Ingest files at startup |
| `--no-fts` | Skip the full-text index. Roughly halves import time; search falls back to substring matching |
| `--port`, `--host` | Defaults 8777 / 127.0.0.1 |
| `--no-browser` | Don't auto-open a browser |
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

## How it's put together

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
view build or import never stalls scrolling in another tab.

**Search** uses an external-content FTS5 table over every column, tokenized to
keep `.`, `-`, `_`, `\`, `@` and `:` inside tokens so paths, GUIDs and account
names survive tokenization. Bare terms are quoted before they reach the FTS
parser; `AND` / `OR` / `NOT` / `prefix*` pass through.

## Using it

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
to **every** row in the current view, `/` search, `f` first filter, `n` note,
`?` help. `J` jumps to the row nearest a timestamp you type — the moment is
remembered across tables, so `.` jumps straight to it again in whichever table
you're looking at. `X` toggles the current grouping off and back on without
touching the filters (`x` drops it outright). `Q` opens the current
filter/sort/search as a ready-made query in the SQL pane.

Saved filters can carry a grouping: save while grouped and applying the filter
brings the grouping back; clearing filters (`c`) clears the grouping with them.
Reorder a header set's saved filters with ▲/▼ or by dragging rows in the Saved
filters list — that order is the `[` / `]` cycle order. The Timeframe filter
dialog can fill its range from your tagged rows — earliest to latest across any
tag, or just the tags you toggle on.

Click a column header to sort, `Shift`-click to add a secondary sort.

The narrow strip down the right edge of the grid is a rail showing where tagged
rows sit in the whole filtered view — so you can see clustering in a 200k-row
result without scrolling through it.

## Timestamps

Logs arrive with whatever timestamp shape the tool that wrote them felt like.
Any column's `▾` menu has **Add datetime column from this…**, which reads the
column and adds a *new* one holding a real, sortable datetime — the original is
never modified, and neither is the file on disk.

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
column from its `▾` menu, or set a default for the case and for every case on
this machine under **Settings → Timestamps**. The default is
`YYYY-MM-DD HH:MM:SS`; "As stored" is still there if you want the raw text.

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

Winnow can be extended without touching its source, Notepad++-style.
**Settings → Plugins** manages everything: it lists every plugin in
`plugins/`, a checkbox per plugin toggles it on or off (a disabled
plugin's code is never even imported), and the install buttons copy a
`.py` file or a plugin folder picked from anywhere on disk into
`plugins/` for you — all effective immediately, no restart. Dropping a
plugin into the folder by hand works too.

Plugins get three extension points:

- **Ingest formats** — parsers for file formats the app doesn't natively
  read, which then behave like built-ins everywhere: drag-and-drop,
  Import files…, folder import, and a per-format picker in the same
  Settings panel, with rows flowing into the same read-only `src_`
  tables as any CSV (so tagging, views, FTS, sessions and the SQL pane
  all just work).
- **Tabs** — a pinned tab next to SQL/Timeline whose content is entirely
  the plugin's own UI: an ES module the plugin ships, mounted into the
  main content area with a stable context object (API helpers, read-only
  SQL against the case, live source/tag state, the app's own theming).
- **API routes** — backend endpoints under `/api/plugin/<name>/…` for
  whatever the plugin's UI needs the server to do: query the case, run a
  computation, call an external service.

Three worked examples ship in `examples/plugins/` — install any of them
from Settings → Plugins → "Install a plugin folder…", or `cp -r` into
`plugins/`:

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
