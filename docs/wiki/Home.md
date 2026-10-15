# Winnow wiki

Winnow is a local viewer for the very large CSV and log exports that DFIR
triage produces, built around one loop: filter a table down, tag the rows
that matter, and read the findings back across the whole case. It runs as a
Python server on your own machine with a browser window in front of it,
needs no network once installed, and never modifies the evidence files you
give it.

This wiki is the manual. The [README](../../README.md) is the short
version; start here when you want the detail.

## Your first triage

The fastest way to see how the pieces fit, using an EvtxECmd export as the
example:

1. **Install and start it.** `pip install -r requirements.txt`, then
   `python server.py`. A window opens on the home screen.
   ([Installation](Installation.md))
2. **Make a case.** **+ New case**, give it a name, and **Create case**.
   Picking the **KAPE triage** profile on the way also sets up a dashboard
   and a starter watchlist. ([Cases and importing](Cases-and-importing.md))
3. **Import.** **Import files…** in the empty case (later, **Case ▾ →
   Import…**), add the CSV with **Add from this machine…**, and
   **Import all queued**. The new table arrives as a tab, and its search
   index finishes building in the background. For a whole KAPE output, use
   **Import a whole folder…** instead.
4. **Filter.** Open **Filters ▾** and pick **EVTX — Logons** from the
   triage filters that ship for this kind of table, then narrow further by
   typing into the box under any column: `!svchost`, `=4624`, `^C:\Users`.
   ([Filtering and searching](Filtering-and-searching.md))
5. **Tag.** Select rows and press `1` to mark them **TA**, `2` for
   **Suspicious**, `3` for **Benign**. `Shift`+`1` tags every row in the
   view, and `Ctrl+Z` undoes. Double-click a row to read it in full and add
   a note. ([Tagging and notes](Tagging-and-notes.md))
6. **Read it back.** The **Timeline** tab shows every tagged row from every
   table in time order. **Search all** (`s`) sweeps the case for a list of
   indicators. ([Timeline, Search all and the Watchlist](Timeline-search-and-watchlist.md))
7. **Hand it on.** **Case ▾ → Export…** writes the tagged rows to Excel,
   one sheet per table, and **Case ▾ → Sessions…** saves a named snapshot
   of your tags and notes. The case itself is one `.db-winnow` file you can
   copy to a colleague. ([Sessions and export](Sessions-and-export.md))

## Pages

**Getting set up**

- [Installation](Installation.md): requirements, getting Winnow, first
  launch, the double-click launchers, command-line options, where files
  live, updating, rolling back and uninstalling.
- [Cases and importing](Cases-and-importing.md): the home screen, case
  files, quick looks, every supported format, folders and archives, import
  jobs and the search index.

**Working a case**

- [Filtering and searching](Filtering-and-searching.md): filter-row syntax,
  the value picker, the filter builder, saved and shipped triage filters,
  the timeframe filter, the histogram and the search box.
- [Working with tables](Working-with-tables.md): tabs and the sidebar,
  columns, sorting, the detail pane, selecting and copying, grouping,
  merging tables and saving a view as a table.
- [Tagging and notes](Tagging-and-notes.md): tags and their keys, tagging a
  view or a group, the rail, undo, row notes and the Notes page.
- [Timeline, Search all and the Watchlist](Timeline-search-and-watchlist.md):
  the three pages that work across the whole case.
- [Derived columns](Derived-columns.md): real timestamps from epochs and
  FILETIMEs, fields out of JSON and XML, regex captures, lookups.

**Going further**

- [Dashboards](Dashboards.md): boards, widgets, the Library and profiles,
  including the shipped KAPE and ESXi / UAC boards.
- [SQL pane](SQL-pane.md): read-only queries over the case, the tables you
  can query, and worked examples.
- [Sessions and export](Sessions-and-export.md): snapshots, handing work to
  another analyst, comparing two passes, and every export.
- [Plugins](Plugins.md): the plugin manager, the trust model, and each
  bundled plugin.

**Reference**

- [Settings and shortcuts](Settings-and-shortcuts.md): every Settings
  section and the full default keyboard map.
- [Troubleshooting](Troubleshooting.md): common problems, the log, and how
  to report a bug.
- [Contributing](Contributing.md): changing Winnow itself.
- [Writing plugins](../writing-plugins.md): the plugin developer guide.

## A few things worth knowing early

- **The evidence is never modified.** Importing copies rows into the case;
  tags, notes and derived columns live beside them.
- **One case, one Winnow.** A case file should be open in one Winnow at a
  time, and should sit on a local disk, not a network share. Two analysts
  work in separate cases and exchange session files.
- **Closing the window stops Winnow** a couple of minutes later, unless
  you started it with `--no-idle-shutdown`.
- **Nothing reaches the network** unless you check for an update or turn on
  a plugin that uses it, such as `claude_assistant`.
