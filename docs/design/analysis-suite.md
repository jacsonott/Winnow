# Design plan — analysis suite: watchlist, stack, case notes, entity pivot, dashboards

Five related additions that push Winnow past per-table triage into
correlation and output. They share machinery deliberately: the watchlist
feeds the dashboard, the dashboard hangs off plugin bundles (now
"profiles"), stack and entity-pivot reuse the value/grouping counting
paths, and everything queryable becomes a dashboard widget.

Grounding — what already exists and is reused:
- `Store.search_all_sources` / `start_search_all_job` — the cross-table
  scan the watchlist auto-runs.
- `Store.set_tags` + `upsert_tag` — auto-tagging watchlist hits.
- `Store.run_sql(sql, limit)` — read-only own-connection queries; the
  dashboard's SQL widgets and the SQL pane already ride this.
- Page-tab registration (`sources.js` tab list: 'sql' | 'timeline' |
  'plugin:<id>') — the new tabs slot in the same way.
- Ingest-complete hook (`jobs.js`, `for sid of j.source_ids`) — where a
  new source announces itself; the watchlist auto-scan triggers here.
- `PluginBundles` (workspace/plugin_bundles.json, "case types") — the
  thing dashboards attach to, conceptually promoted to **profiles**.
- Workspace-vs-case split (invariant #8): analyst workflow → workspace/;
  evidence + findings → the case `.db`.

---

## 1. IOC watchlist  (own tab)

**What.** A case-level set of indicators (value + kind + optional note).
Every source, on import and on demand, is scanned; matches are counted,
listed, and optionally auto-tagged. A **Watchlist** page tab shows
indicators, per-indicator hit counts, and where each landed.

**Where it lives.** Indicators are *evidence-adjacent findings*, so they
travel in the case `.db` (`watchlist(id, value, kind, note, auto_tag_id,
created_at)` + a materialized `watchlist_hits(watchlist_id, source_id,
rid)` so hits survive and are taggable/exportable). This is the one place
we diverge from search-all, which is ephemeral.

**Auto-scan.** On ingest completion (`jobs.js` source-done hook) the
client POSTs `/api/watchlist/scan?source_id=…`; the server runs each
indicator through the same substring/FTS path `search_all_sources` uses,
writes `watchlist_hits`, and — if the indicator names an `auto_tag_id` —
routes the hit rids through `set_tags` (undoable, shows on the rail like
any tag). A "Scan all tables" button re-runs everything.

**Matching.** v1: case-insensitive substring / exact per kind (hash =
exact, ip/domain/filename = contains), reusing FTS where present. Kinds
set the match rule + color, not format validation. Future: CIDR,
defanged normalization (`hxxp`, `[.]`), regex indicators.

**Import.** Reuse the Search-all term-file importer shape — one indicator
per line, `#` comments, optional `value,kind` — plus paste. A watchlist
can be **saved to workspace** and re-applied to any case (a standing IOC
set), the same save/apply pattern bundles use.

**Tab UI.** Left: indicator list with hit counts + color swatch. Right:
hits for the selected indicator (table, time, matched field, context),
each double-clickable to open that source filtered to the row (the
`openFiltered` seam from the plugin work generalizes here). Top: import /
paste / scan-all / "auto-tag hits as…".

**Routes.** `GET/POST/DELETE /api/watchlist`, `POST /api/watchlist/scan`,
`GET /api/watchlist/hits?watchlist_id=…`, `POST /api/watchlist/import`.

---

## 2. Stack / long-tail analysis  (+ a visualization surface)

**What.** Least-frequency-of-occurrence as a first-class tool: pick a
column, get `value → count` sorted **ascending** (rarest first), because
rare is where evil hides. Bigger than the value picker (a filter control)
— an analysis view with charts.

**Where.** Two entry points: (a) a **"Stack column"** item in the column
header / table menu; (b) a reusable **visualization panel** the stack view
is the first tenant of. The panel takes `{source_id/view_id, column, agg}`
and renders sorted bars (top/bottom N), a value table with counts + %, or
a time histogram — the same histogram the lateral-movement plugin uses,
lifted to a shared module so the main Timeline and dashboards reuse it.

**Backend.** Mostly exists: `group_summary` / `column_values` already
count distinct values under a view. Add `order=count_asc` and an optional
second dimension for stacked/cross-tab counts. Rendering is client-side
canvas (no chart dependency — airgap rule).

**Payoff beyond stack.** This is the "more visualization options" ask
delivered as infrastructure: once a chart module eats `{rows, x, y,
kind}`, the stack view, the timeline scrubber, and every dashboard widget
draw through it.

---

## 3. Case notes  (Markdown, own tab, travels in the .db)

**What.** A free-form Markdown scratchpad per case — the running
narrative, distinct from per-row notes. A **Notes** page tab with an
edit/preview toggle.

**Where.** In the case file (`case_notes(id, body, updated_at)` — one row,
read/written whole, same shape as session documents). Evidence-adjacent
analyst work that must travel with the `.db` to whoever receives the case
— the session-portability argument (invariant #10), so NOT workspace/.

**Rendering.** A tiny dependency-free Markdown subset (headings, bold,
code, lists, links) rendered client-side — airgap-safe. Auto-saves on a
debounce like `saveLayout`. Feeds the eventual report generator's
narrative section.

**Nice touch.** "Insert reference" — drop a link to the current
view/filter or a tagged row, so the narrative points back into evidence.

---

## 4. Entity pivot  (see docs/mockups/entity-pivot.html)

**What.** Pick any value — host, user, hash, IP — and get *everywhere it
appears across every table at once*: per-source hit counts, a combined
activity timeline, a merged evidence stream, and its direction (source vs
destination, reusing the lateral-movement src/dst notion). The question
analysts ask constantly ("what do we know about WKS07?") that today means
filtering each table by hand.

**Where it's reachable — three ways in:**
1. **Right-click any cell → "Pivot on 'WKS07'"** (rowmenu.js gains one
   item next to filter-by-value). Primary path — always one cell from a
   full-case view of that value.
2. **From the watchlist** — an indicator's "pivot" action.
3. **From the entity-pivot tab's own search box.**

Opens as a page tab (like SQL/Timeline). See the mockup: left rail =
"appears in" (sources + counts) and "seen as" (source/destination); right
= a per-entity time histogram (reusing the chart module) over a merged,
chronological evidence table; double-click a row opens that source
filtered to it.

**Backend.** `POST /api/entity/pivot {value, columns?}` that, per source,
runs a counted scan across its text columns (or a chosen set), returns
`{source_id, count, sample_rows, time_bounds}`, plus a merged timeline
built the same way the super-timeline would — so entity pivot and the
super-timeline share their normalization code. Column resolution ("which
columns hold hosts") reuses header-set knowledge (RemoteHost/Computer/
Workstation for EvtxECmd, etc.).

**Why high-value + low-risk.** Mostly assembly of parts that exist (search
counting, chart module, `openFiltered`, header sets), and it's the
connective tissue the dashboard and report both lean on.

---

## 5. Custom case dashboards  (see docs/mockups/dashboard.html)

**What.** A grid of widgets that summarize a case at a glance — watchlist
hit counts, tag totals, host facts from Registry, a logging-posture panel
("Sysmon on, ScriptBlock off — blind to PowerShell content"), event-volume
charts, stack results, distinct-peer counts. A **Dashboard** page tab,
editable (add/move/remove), saved as part of a **profile**.

**Key architectural bet: every widget is a saved query + a render kind.**
`{title, source: sql|watchlist|tags|stack|filter, query, render:
stat|kv|bar|histogram|chips|list}`. SQL widgets run through `run_sql`
(read-only, own connection — already safe and airgap-clean). A dashboard
is therefore *data, not code*: build a query in the SQL pane or a filter
in the grid, click "→ add to dashboard," pick a render, done. No plugin
authoring to make Winnow show new things.

**How it ties to profiles (the missing specifics for plugin bundles).**
Extend a bundle into a **profile**: `{name, plugins, dashboard:
[widgets], watchlist?, default_filters?}`. Applying "KAPE triage" enables
its plugins AND lays out its dashboard AND (optionally) loads a standing
watchlist. The profile becomes "how I analyze *this kind of case*," and —
because widgets are portable SQL — analysts **ship and share profiles**
for BEC, ransomware, insider, etc. New analysis types = new profiles =
mostly SQL, no code. That's the adaptability described.

**Widget kinds (v1):**
- `stat` — one number + sub-label (watchlist hits, tag totals, distinct
  count). Query → one value.
- `kv` — label/value pairs (host facts). Query → name/value rows.
- `chips` — boolean posture (logging on/off), green/red. Query → name +
  0/1.
- `bar` / `histogram` — the chart module (event volume, stack top-N).
- `list` — top rows (rarest command lines). Query → rows + a count.

**Where dashboards & profiles live.** The dashboard *definition* is
workspace-level (part of the profile — "how I work," a machine/analyst
property, like bundles). The *data* is queried live from the open case. A
profile exports to JSON and imports on another box — the portability the
watchlist and sessions have.

**Routes.** `GET/POST /api/dashboard` (the open case's active layout),
profiles extend `/api/plugin_bundles` with `dashboard`/`watchlist` fields,
`POST /api/dashboard/widget/preview` runs a widget's query → render-ready
data.

---

## Build order (each useful shipped alone)

1. **Chart module** — extract the plugin histogram to a shared canvas
   `{rows, x, y, kind}`. Unblocks stack, entity timeline, dashboards.
2. **Stack view** — smallest immediate win; exercises the chart module.
3. **Case notes tab** — self-contained, no dependencies, pure win.
4. **Watchlist tab** — auto-scan + auto-tag; first cross-source
   correlation feature.
5. **Entity pivot** — assembles search-counting + chart + header sets +
   openFiltered; also builds the merged-timeline normalization the
   super-timeline will reuse.
6. **Dashboards + profiles** — the capstone; consumes all of the above as
   widget sources and gives plugin-bundle "profiles" their purpose.

Each ships as its own PR against develop with tests (backend counting /
scan / auto-tag / dashboard-preview at Store+route level; UI tests for tab
mount, pivot-from-cell, widget rendering). Mockups for the two visual/novel
pieces live in `docs/mockups/`.
