# lateral-movement — a custom-UI tab plugin for Winnow

Adds a pinned **Lateral movement** tab (next to SQL/Timeline) that follows
movement between hosts through a case: who reached which host, as which
account, in what order. It reads **movement events** out of your ingested
tables and draws them five ways:

- **Chain over time** — a lane per host and an arrow per link (two hosts
  and an account) where it first happens; RDP sessions as bars from logon
  to logoff; **credential switches** as diamonds. A switch is an account
  leaving a host soon after a different account arrived there, without
  ever having logged on to it itself, and on no other day (after Hopper,
  USENIX Security 2021); an explicit-credentials event (4648) states the
  same thing outright.
- **Host matrix** — sources down the side, destinations across the top.
- **By account** — accounts ranked by what stands out about them
  (switches, sweeps, failures then a success, explicit credentials,
  workstation-to-workstation hops, one day only), each with its activity
  across the case.
- **Pivot** — from a host's menu: where its access came from and where
  it went next, in time order.
- **Path** — from a host's menu: time-ordered routes between two hosts
  (or to the hosts you mark as crown jewels), each step where the account
  changes explained by a credential switch or flagged as unexplained.

Under every view, the rows over the whole case: drag across it to zoom
every view into a window.

A *movement event* maps a source-host column to a destination-host column,
optionally with an account column, an acting-account column (who used
whose credentials), a time column and conditions (`EventId = 4624 AND
LogonType 3`). It is **tied to a header set** — one of the app's named
column sets (`Event logs (EvtxECmd)`, `NTFS $MFT (MFTECmd)`, …) — or to
any table with its columns, so an EVTX event never shows up on a firewall
table. The plugin ships defaults shaped against real KAPE/EvtxECmd triage
output — remote logons (4624 type 3/10), failed logons (4625), explicit
credentials (4648), RDP sessions (21/24/25, paired into sessions) and
attempts (1149), and share access (5140/5145) — all tied to the EvtxECmd
set. The first time the tab opens on a case it ticks every event that
fits a table there.

What else it does:

- **Folds every spelling of a host into one.** `WKSTN-17.corp.local`,
  `WKSTN-17 (10.1.0.117)`, `- (10.1.0.117)` and a bare `10.1.0.117` are
  one host, and an address takes the name rows write beside it across
  every ticked table, so a netflow export's addresses land on the event
  log's hosts. A flow row with no account that lines up with a logon on
  the same two hosts within a minute backs that hop up rather than
  drawing it twice.
- **Honours the case timeframe.** The timeframe filter you set on the
  tables tab applies here too ("Apply case timeframe").
- **Jumps to the evidence.** A host's or a link's menu opens the table it
  came from, filtered to every spelling of the host and the event's own
  conditions.
- **Defines your own events** with **+ New event type** (in the panel
  header). The builder reads the columns of one table in the case and
  guesses the source host, destination host, user and time columns from
  their names (never the acting account); add a condition (`EventId is
  5985`) by picking from the column's commonest values. The right-hand
  side shows what the event finds in that table as you go: rows matched,
  links, hosts, the busiest links, which addresses fold to names, which
  links nothing else draws, and how many account-less rows back up a hop
  another event already draws. Saved on this machine (via
  `req.storage`), surviving case switches and updates like the app's own
  saved filters. **Events…** lists every event, edits and deletes yours,
  and copies a shipped one into the builder, in two panes: **Yours** and
  **Shipped**.
- **Remembers the case's setup** — which events are ticked, the view,
  crown jewels and accounts marked as expected — with the case
  (`winnow.tabState`).

Fully offline — the aggregation runs through Winnow's read-only SQL path
(`Store.run_sql`), so it never blocks the app or touches the case file's
shared connection. The tab reads up to 250,000 (event, hosts, account,
minute) rows; past that it keeps the earliest and says so.

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
- `register_api(route, handler, methods)` — the backend the tab calls
  (`feed` is every hop the ticked events draw, folded and to the minute,
  which all five views read; `preview` is the builder's live count over
  the same SQL; `values` fills its value chips; `defs` reads and saves
  definitions); the handler gets a plain `PluginRequest` and uses
  `req.store.run_sql` for reads and `req.storage` for its own persistent
  state. A merge is read as a union of its members built with
  `merge_select_list` and `union_all`.
- **Shipping defaults** — `defaults.json` beside the module, read by the
  `defs` route and merged with the analyst's saved definitions, the same
  pattern the app uses for its filter defaults.
- The `winnow` context object: `el`/`post`/`api`/`toast`,
  `winnow.state.sources`/`timeRange`, `winnow.openFiltered(id, pairs,
  {where})` to jump to evidence, `winnow.tabState` to keep the tab's setup
  with the case, and `onShow` for refresh-on-activation.
- The app's own dialog pieces: `winnow.modal` for the builder,
  `winnow.sectionPanes` for the **Events…** dialog's two panes (plugin
  API 14), `winnow.dropdownMenu` for each event's **⋯** menu and for the
  views' right-click menus (anchored on a point), and
  `winnow.confirmDialog` before a delete.
- Big views on a canvas: one canvas kept in view with `position: sticky`
  over a spacer as tall as what is drawn, painted a screen at a time, so
  a thousand hosts never need a canvas a thousand lanes tall.
- Theming: every canvas color is read from Winnow's CSS tokens
  (`--accent`, `--series-N`, `--line-2`, …) at draw time, and `ui/tab.css`
  uses the same tokens, so all styles and both themes just work.
