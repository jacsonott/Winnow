# lateral-movement — a custom-UI tab plugin for Winnow

Adds a pinned **Lateral movement** tab (next to SQL/Timeline): pick any
ingested table and its source/destination columns — 4624 logon events
from an EvtxECmd export, firewall logs, netflow — and see the movement
between hosts as a force-directed graph. Edge width is event count,
arrows show direction, node size is total events touching that host, and
an optional label column (typically the user/account) adds a distinct-
value count per edge. Drag nodes to untangle; hover for totals.

Fully offline — the aggregation runs through Winnow's read-only SQL path
(`Store.run_sql`), so it never blocks the app and never touches the case
file's shared connection.

## Install

**Settings → Plugins → "Install a plugin folder…"** and pick this folder,
or `cp -r examples/plugins/lateral_movement plugins/`. No restart needed.

## As a reference for writing your own UI plugin

This is the smallest complete demonstration of the custom-UI hooks
documented in [`plugin_api.py`](../../../plugin_api.py):

- `register_tab(id, label, entry)` — a pinned tab whose content is
  `ui/tab.js`, an ES module served from the plugin's own folder and
  mounted with `export default function mount(container, winnow)`.
- `register_api(route, handler)` — the backend the tab calls at
  `winnow.base + '/edges'`; the handler gets a plain `PluginRequest` and
  uses `req.store.run_sql` for reads.
- The `winnow` context object: `el`/`post`/`toast` helpers,
  `winnow.state.sources` for live case state, and `onShow` for refresh-
  on-activation.
- Theming: every canvas color is read from Winnow's CSS tokens
  (`--accent`, `--line-2`, …) at draw time, so all four styles and both
  themes just work.
