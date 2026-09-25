# Winnow

**A fast, local viewer for the enormous CSVs that DFIR triage produces** —
EvtxECmd, MFTECmd, Amcache, a whole KAPE output folder — with the
row-tagging workflow that turns a million log lines into a findings list.
Think Timeline Explorer, rebuilt for big files: a 1.2-million-row CSV
imports in about eight seconds, filtering it takes milliseconds, and
scrolling stays smooth however deep you are.

![The grid mid-triage: an EvtxECmd table under the shipped Logons filter, tags on the rail](docs/screenshots/grid.png)

Everything runs on your machine — one Python server, no cloud, no build
step, works on an airgapped analysis box. Your work (tags, notes, saved
views, the watchlist) lives in a single SQLite case file you can hand to
another analyst. **The evidence files themselves are never modified.**

## Install

```bash
pip install -r requirements.txt
python server.py
```

Python 3.10 or newer and four packages — FastAPI, uvicorn,
python-multipart and openpyxl. Install from `requirements.txt` rather than
by hand: openpyxl is imported at startup for Excel support, and without it
the server stops before it binds.

That opens the home screen at <http://127.0.0.1:8777> in an app window,
falling back to an ordinary browser tab if no Chromium-family browser is
around. Create a case, then import from the UI, drag files onto the
window, or skip ahead with `python server.py --case case.db --open
timeline.csv`.

Would rather not touch a terminal? `launch/` has double-click launchers
for Linux, macOS and Windows ([launch/README.md](launch/README.md)), and
**Settings → File associations** puts Winnow in the OS's *Open With* menu
for the types it reads — per-user, no admin rights.

| flag | meaning |
| --- | --- |
| `--case FILE` | SQLite case file, created if missing |
| `--open A.csv B.csv` | Ingest files at startup |
| `--no-fts` | Skip the full-text index — search falls back to substring scanning |
| `--port`, `--host` | Defaults 8777 / 127.0.0.1 |
| `--force` | Open a case another Winnow still holds |

`python server.py --help` has the rest. A case file is meant to be open in
**one** Winnow at a time, and shouldn't live on a network share — SQLite's
journalling doesn't survive SMB/NFS. Two analysts on one investigation
work in separate case files and merge with session files.

**Updating in place** keeps your work, which lives *inside* the Winnow
folder: Settings → Updates, or `python update.py` (`--rollback` undoes,
`--from file.zip` is the airgapped route, `--dev` tracks the develop
branch). Nothing is ever fetched unless you ask.

## The loop

Filter down, tag what matters, read the findings back. That's the whole
model, and everything else is in service of it.

![The Excel-style value picker on a column header](docs/screenshots/value-picker.png)

**Filter** by typing into the box under any column header — `svchost`
contains, `!svchost` doesn't, `=4624` exact, `^C:\Users` starts with,
`>1000` numeric, `/re/` regex, `a|b|c` any-of, `""` empty. The `▾` opens
that column's distinct values with counts, Excel-style. Ready-made triage
filters ship in the box and appear when a matching table opens, so an
EvtxECmd export arrives with Logons, RDP, Defender tampering and
persistence one click away.

**Tag** with `1`–`9`. Counts live in the ribbon, a rail down the grid's
edge shows where findings cluster in the whole filtered view, and `Ctrl+Z`
steps back through exactly what each tag change touched. `?` lists every
key; the navigation keys rebind in Settings, and the tag digits are set
per tag in the tag editor.

**Read it back** on the case pages — Timeline, Search all, Watchlist,
Notes and dashboards.

![The unified Timeline: every tagged row across the case, one stream](docs/screenshots/timeline.png)

- **Timeline** — every tagged row across every table, one chronological
  stream. Tag it and it's here; that's the model.
- **Search all** — sweep every table, open or closed. Paste IOCs one per
  line for per-indicator, per-table hit counts.
- **Watchlist** — case-level indicators scanned across every table, with
  optional auto-tagging and a dot on the tab when new hits land.
- **Notes** — a Markdown scratchpad in the case file whose links navigate
  back into the evidence.
- **Dashboards** — boards of widgets built from what you're looking at;
  click a number and the table opens filtered to exactly those rows. A
  board plus your plugins saves as a **profile** for the next case of the
  same type. KAPE triage and ESXi/UAC ship ready to use.

## What else is in the box

- **Any log file** — a file no parser claims imports as raw text, one line
  per row. `.plaso` timelines, ESXi support bundles and UAC collections
  (`.zip`/`.tar`/`.tgz`) expand and import, nested archives included.
- **Real datetime columns** — derive a sortable column from Unix epochs,
  FILETIME, WebKit, Mac absolute, .NET ticks, Excel serials, ISO 8601 or
  year-less syslog. The original column is never touched.
- **Nested JSON and XML** — right-click a node in the detail pane to make
  it a column, or flatten a whole document at once. `Data[@Name='LogonType']`
  is why EVTX comes out useful.
- **Grouping** — drag a header into the Group by strip; nest levels, group
  by tag, tag a whole group without expanding it.
- **One view over several tables** — merge same-shaped exports from a
  fleet and filter, sort, tag and export them as one.
- **Keep what matters** — save a filtered view, or just the rows you
  picked, as a new table of the case.
- **Sessions and export** — hand your tags and notes to another analyst as
  a session file (tags merge by name), or compare two sessions row by row.
  Export offers four scopes: the current view or just its tagged rows as
  CSV, and every tagged row or every row of the whole case as XLSX, one
  worksheet per table.
- **A read-only SQL pane** for when a filter has done all it can:

  ```sql
  SELECT t.name, s.Process, count(*) n
  FROM row_tags rt
  JOIN tag_defs t ON t.id = rt.tag_id
  JOIN src_1 s ON s.rid = rt.rid
  GROUP BY 1, 2 ORDER BY n DESC;
  ```

## Speed

1.2M rows × 10 columns (169 MB CSV), on an ordinary laptop:

| | |
| --- | --- |
| Import | 8.2 s (~147k rows/s) |
| Filter + sort 171k matching rows | 0.6 s |
| Fetch a page 150,000 rows deep | 1 ms |
| Full-text search across all columns | 8 ms |

The trigram index builds in the background after import, so the table is
browsable while it finishes; search falls back to substring scanning until
it lands.

## Plugins

![The pivot plugin: a cross-tab of hosts against event descriptions](docs/screenshots/pivot.png)

**Settings → Plugins** turns them on and off per machine or per case, with
no restart. Seven examples ship switched off: **pivot** (Excel's
PivotTable over any table), **first_last** (collapse events into per-group
session bookends), **mft_usn** (raw NTFS `$MFT`/`$J` in pure Python, no
EZTools), **lateral_movement** (logon pairs as an offline graph),
**esxi_logs** (ESXi and UAC host logs into one schema), **top_values** (a
toolbar panel of a column's commonest values), and **claude_assistant** (a
Claude tab that sees the case *schema*, never row data — it needs network
and an API key, which is why it is opt-in).

A plugin is local Python running with Winnow's own privileges and nothing
is fetched from a network, so installing one is the consent step: only
install plugins you have read or trust. Writing your own is
[docs/writing-plugins.md](docs/writing-plugins.md) — a minimal ingest
format is about twenty lines.

## More

- [docs/writing-plugins.md](docs/writing-plugins.md) — the plugin guide.
- [docs/notes/](docs/notes/) — per-subsystem working notes: the read-only
  source tables, the materialised views that keep deep scrolling at 1 ms,
  the reader pool, the tokenizer that keeps paths and GUIDs searchable.
- `scripts/screenshots.py` regenerates the images above against the app as
  it is today.
