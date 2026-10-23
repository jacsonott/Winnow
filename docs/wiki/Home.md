# Winnow wiki

Winnow is a local viewer for the very large CSV and log exports DFIR triage
produces: filter a table down, tag the rows that matter, and read the
findings back across the case.

## Your first triage

With an EvtxECmd export as the example:

1. **Install and start it.** `pip install -r requirements.txt`, then
   `python server.py`. ([Installation](Installation.md))
2. **Make a case.** **+ New case**, name it, **Create case**. Pick the
   **KAPE triage** profile to also get a dashboard and a starter watchlist.
   ([Cases and importing](Cases-and-importing.md))
3. **Import.** **Import files…**, **Add from this machine…**, then
   **Import 1 file**. For a whole KAPE output, **Import a whole folder…**.
4. **Filter.** **Filters ▾** → **EVTX — Logons**, then narrow it by typing
   under any column: `!svchost`, `=4624`, `^C:\Users`.
   ([Filtering and searching](Filtering-and-searching.md))
5. **Tag.** Select rows and press `1` for **TA**, `2` for **Suspicious**,
   `3` for **Benign**. `Shift`+`1` tags the whole view; `Ctrl+Z` undoes.
   ([Tagging and notes](Tagging-and-notes.md))
6. **Read it back.** The **Timeline** tab lists every tagged row from every
   table in time order. **Search all** (`s`) sweeps the case for a list of
   indicators. ([Timeline, Search all and the Watchlist](Timeline-search-and-watchlist.md))
7. **Hand it on.** **Case ▾ → Export…** writes the tagged rows to Excel.
   The case itself is one `.db-winnow` file you can copy.
   ([Sessions and export](Sessions-and-export.md))

## Pages

**Getting set up**

- [Installation](Installation.md): install, launch, update, uninstall.
- [Cases and importing](Cases-and-importing.md): case files and every
  import format.

**Working a case**

- [Filtering and searching](Filtering-and-searching.md): filter syntax,
  saved filters, time ranges, search.
- [Working with tables](Working-with-tables.md): columns, sorting,
  grouping, merging.
- [Tagging and notes](Tagging-and-notes.md): tags, keys, undo, notes.
- [Timeline, Search all and the Watchlist](Timeline-search-and-watchlist.md):
  reading across every table.
- [Derived columns](Derived-columns.md): timestamps from raw values, fields
  out of JSON and XML.

**Going further**

- [Dashboards](Dashboards.md): boards, widgets, profiles.
- [SQL pane](SQL-pane.md): read-only queries over the case.
- [Sessions and export](Sessions-and-export.md): snapshots, hand-offs,
  exports.
- [Plugins](Plugins.md): the plugin manager and the bundled plugins.

**Reference**

- [Settings and shortcuts](Settings-and-shortcuts.md): every setting and
  key.
- [Troubleshooting](Troubleshooting.md): common problems and the log.
- [Contributing](Contributing.md): changing Winnow itself.
- [Writing plugins](../writing-plugins.md): the plugin developer guide.

## Good to know

- **The evidence is never modified.** Tags, notes and derived columns live
  beside the imported rows.
- **One case, one Winnow.** Open a case in one Winnow at a time, from a
  local disk. Two analysts work in separate cases and swap session files.
- **Closing the window stops Winnow** a couple of minutes later, unless it
  was started with `--no-idle-shutdown`.
- **Nothing reaches the network** unless you check for an update or turn on
  a plugin that uses it, such as `claude_assistant`.
