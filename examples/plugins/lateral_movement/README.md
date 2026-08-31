# lateral-movement — a custom-UI tab plugin for Winnow

Adds a pinned **Lateral movement** tab (next to SQL/Timeline): a directed
force-graph of hosts touching hosts, built from **movement events** you
select out of your ingested tables.

A *movement event* maps a source-host column to a destination-host column,
optionally within conditions (`EventId = 4624 AND LogonType 3`). The
plugin ships a set of defaults shaped against real KAPE/EvtxECmd triage
output — remote logons (4624 type 3/10), failed logons (4625), explicit
credentials (4648), RDP sessions (21/24/25 and 1149), and share access
(5140/5145) — and each binds automatically to any table carrying the
columns it needs.

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
  panel header) — the editor pre-guesses the source/destination/label/time
  columns from your table's names, so you usually just name it and add a
  condition (e.g. `EventId equals 5985`). Saved on this machine (via
  `req.storage`), surviving case switches and updates like the app's own
  saved filters. **Events…** manages the full set.
- **Collapse the event list** with the header caret when the graph needs
  the room — the header keeps a count of what's selected.

Edge width is event count, arrows show direction, node size is total
events; drag nodes to untangle, scroll to zoom, drag the background to
pan. Fully offline — the aggregation runs through Winnow's read-only SQL
path (`Store.run_sql`), so it never blocks the app or touches the case
file's shared connection.

## Install

**Settings → Plugins → "Install a plugin folder…"** and pick this folder,
or `cp -r examples/plugins/lateral_movement plugins/`. No restart needed.

## As a reference for writing your own UI plugin

The most complete demonstration of the plugin custom-UI hooks documented
in [`plugin_api.py`](../../../plugin_api.py):

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
