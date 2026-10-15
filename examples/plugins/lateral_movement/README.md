# lateral-movement — a custom-UI tab plugin for Winnow

Adds a pinned **Lateral movement** tab (next to SQL/Timeline): a directed
force-graph of hosts touching hosts, built from **movement events** you
select out of your ingested tables.

A *movement event* maps a source-host column to a destination-host column,
optionally within conditions (`EventId = 4624 AND LogonType 3`), and is
**tied to a header set** — one of the app's named column sets
(`Event logs (EvtxECmd)`, `NTFS $MFT (MFTECmd)`, …). It only offers
itself on a table that IS that artifact, exactly the way a filter default
binds to its header set, so an EVTX event never shows up on a firewall
table. The plugin ships defaults shaped against real KAPE/EvtxECmd triage
output — remote logons (4624 type 3/10), failed logons (4625), explicit
credentials (4648), RDP sessions (21/24/25 and 1149), and share access
(5140/5145) — all tied to the EvtxECmd set.

What you can do:

- **Combine events across tables.** Tick several events from one EVTX
  export, plus a firewall table, plus netflow — every edge lands on one
  graph, colored by event type (see the legend).
- **See it over time.** Any event with a time column feeds a brushable
  timeline histogram under the graph; drag across it to show only the
  hops in that window.
- **Honour the case timeframe.** The timeframe filter you set on the
  tables tab applies here too (toggle "Apply case timeframe").
- **Jump to the evidence.** Double-click a host to open its source table
  filtered to that host's rows.
- **Define your own events** with **+ New event type** (right in the
  panel header). The builder reads the columns of one table in the case
  and guesses the source host, destination host, user and time columns
  from their names; add a condition (`EventId is 5985`) by picking from
  the column's commonest values. The right-hand side shows what the
  event finds in that table as you go (rows matched, links, hosts and
  the busiest links), so a wrong column shows up before you save. Start
  from a blank event or a copy of any existing one, and choose whether
  it is offered on every table of the same header set or on any table
  with its columns. Saved on this machine (via `req.storage`), surviving
  case switches and updates like the app's own saved filters.
  **Events…** lists every event, edits and deletes yours, and copies a
  shipped one into the builder.
- **Collapse the event list** with the header caret when the graph needs
  the room — the header keeps a count of what's selected.

Edge width is event count, arrows show direction, node size is total
events; drag nodes to untangle, scroll to zoom, drag the background to
pan. Fully offline — the aggregation runs through Winnow's read-only SQL
path (`Store.run_sql`), so it never blocks the app or touches the case
file's shared connection.

## Install

It ships with Winnow, switched off: turn it on in **Settings → Plugins →
Manage plugins…**. To run a modified copy instead, copy this folder into
`plugins/` (or use **Install a plugin… → Pick a plugin folder…** there);
a copy in `plugins/` replaces the bundled one. No restart needed.

## As a reference for writing your own UI plugin

The most complete demonstration of the plugin custom-UI hooks documented
in [`plugin_api.py`](../../../winnow/plugin_api.py):

- `register_tab(id, label, entry)` — a pinned tab whose content is
  `ui/tab.js` (+ `ui/tab.css`), ES modules served from the plugin folder,
  mounted with `export default function mount(container, winnow)`.
- `register_api(route, handler, methods)` — the backend the tab calls;
  the handler gets a plain `PluginRequest` and uses `req.store.run_sql`
  for reads and `req.storage` for its own persistent state.
- **Shipping defaults** — `defaults.json` beside the module, read by the
  `defs` route and merged with the analyst's saved definitions, the same
  pattern the app uses for its filter defaults.
- The `winnow` context object: `el`/`post`/`api`/`toast`,
  `winnow.state.sources`/`timeRange`, `winnow.openFiltered(id, pairs)` to
  jump to evidence, and `onShow` for refresh-on-activation.
- Theming: every canvas color is read from Winnow's CSS tokens
  (`--accent`, `--line-2`, …) at draw time, and `ui/tab.css` uses the same
  tokens, so all styles and both themes just work.
