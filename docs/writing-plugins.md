# Writing Winnow plugins

A plugin is local Python that Winnow imports at startup and lets extend
the running app — new file parsers, new tabs with their own UI, new
backend endpoints — without touching Winnow's source. Drop it in
`plugins/`, and it's installed. Delete it, and it's gone.

This guide is the long form, and it stands alone — every hook, data
shape and example you need to write a plugin is in this file, and every
code sample in it was extracted verbatim and run before shipping. The
enforcing contract lives in [`plugin_api.py`](../winnow/plugin_api.py)'s module
docstring, and three fuller plugins live in
[`examples/plugins/`](../examples/plugins/), but neither is required
reading. Start with the Quickstart.

**Contents**

1. [The three extension points](#1-the-three-extension-points)
2. [Quickstart: a parser in 20 lines](#2-quickstart-a-parser-in-20-lines)
3. [Plugin anatomy](#3-plugin-anatomy)
4. [Hook: ingest formats](#4-hook-ingest-formats)
5. [Hook: tabs](#5-hook-tabs)
6. [Hook: API routes](#6-hook-api-routes)
7. [Talking to the case](#7-talking-to-the-case)
8. [Testing a plugin](#8-testing-a-plugin)
9. [Installing and sharing](#9-installing-and-sharing)
10. [Security model](#10-security-model)
11. [Troubleshooting](#11-troubleshooting)
12. [Reference](#12-reference)
13. [Writing a plugin with an LLM](#13-writing-a-plugin-with-an-llm)

> **This file is self-contained.** You do not need to read Winnow's
> source to write a plugin against it, and neither does an LLM you're
> working with — see [§13](#13-writing-a-plugin-with-an-llm).

---

## 1. The five extension points

Everything a plugin does, it does by calling methods on the `api` object
handed to its `register()` function:

| Method | Adds | Use it for |
| --- | --- | --- |
| `api.register_ingest_format(...)` | A file parser | Formats Winnow can't read: raw `$MFT`, EVTX, prefetch, a vendor's export |
| `api.register_tab(...)` | A pinned tab with your own UI | A whole feature surface: a graph, a dashboard, an assistant, a report builder |
| `api.register_api(route, handler)` | A backend endpoint | Whatever your tab (or a script) needs the server to do |
| `api.register_row_action(...)` | An entry in the row right-click menu | Anything that operates on the selected rows: a VirusTotal lookup on the highlighted hashes, an enrichment, a hand-off to another tool |
| `api.register_toolbar_panel(...)` | A toggle in the table toolbar + a strip above the grid | Something that follows the current view: a histogram of when its rows happened, a sparkline, a legend |

They compose: a tab usually pairs with one or more routes; a row action
often pairs with a tab that shows its results; a toolbar panel usually
pairs with a route that reads through the current view. Ingest formats
and row actions work in a single-file plugin; tabs and panels need a
folder plugin (there has to be somewhere to serve the JS from).

The shipped examples map onto these:

| Example | Demonstrates |
| --- | --- |
| [`mft_usn/`](../examples/plugins/mft_usn/) | Ingest formats — two of them, with options, streaming parsers, extension *and* bare-filename matching |
| [`lateral_movement/`](../examples/plugins/lateral_movement/) | A tab + a route — canvas UI, case queries, theming |
| [`claude_assistant/`](../examples/plugins/claude_assistant/) | A tab + a route that calls an external service, with credentials and dependencies |
| [`table_histogram/`](../examples/plugins/table_histogram/) | A toolbar panel + a route — following the grid with `onViewChange`, driving the timeframe filter with `setTimeRange` |

---

## 2. Quickstart: a parser in 20 lines

A Windows `hosts` file is a real triage artifact (a tampered one
redirects update or telemetry domains) and Winnow can't read it — the
format isn't delimited in a way the CSV sniffer handles. Let's teach it.

Create `plugins/hostsfile.py`:

```python
"""Parse a Windows/Unix hosts file into a Winnow table."""

PLUGIN = {"name": "hostsfile", "version": "1.0.0",
          "description": "Reads a hosts file: IP, hostname, comment, line number."}


def parse_hosts(path, options):
    def rows():
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for lineno, raw in enumerate(fh, 1):
                text, _, comment = raw.partition("#")
                parts = text.split()
                if not parts:
                    continue                      # blank or comment-only line
                ip, hostnames = parts[0], parts[1:]
                for host in hostnames or [""]:
                    yield [ip, host, comment.strip(), lineno]

    return {
        "columns": ["IP", "Hostname", "Comment", "Line"],
        "rows": rows(),
        "column_types": ["text", "text", "text", "number"],
    }


def register(api):
    api.register_ingest_format(
        id="hosts",
        label="hosts file",
        extensions=[".hosts"],
        filename_patterns=["hosts"],   # the real artifact has no extension
        description="IP/hostname pairs from a hosts file, one row per mapping.",
        parse=parse_hosts,
    )
```

Restart the server (or just open **Settings → Plugins** — installs and
toggles reload without a restart). You now have a `hosts file` format:
drag a `hosts` file onto the window, or use Settings → Plugins → the
format's own **Import files…** picker. Rows land in a normal `src_N`
table, so tagging, filtering, search, sessions and the SQL pane all work
on it immediately.

That's the whole loop. Everything below is detail.

---

## 3. Plugin anatomy

### File layout

A plugin is **either** a single `.py` file **or** a folder with an
`__init__.py`:

```
plugins/
  hostsfile.py              ← single-file plugin (ingest formats only)
  my_plugin/                ← folder plugin (everything)
    __init__.py             ← must define register(api)
    parser.py               ← helper modules; import with `from . import parser`
    ui/
      tab.js                ← ES module for a tab
      style.css             ← any other asset you want to fetch
```

Names starting with `.` or `_` are skipped entirely — handy for parking a
plugin without deleting it (`_wip_plugin.py`), though Settings → Plugins
is the better off switch since it keeps the plugin visible.

### The module contract

```python
PLUGIN = {                      # optional — all three keys optional
    "name": "my-plugin",        # display name; defaults to the file/folder name
    "version": "1.0.0",
    "description": "One line, shown in Settings → Plugins.",
}

WINNOW_API_VERSION = 1          # optional; refuse to load on an older Winnow

def register(api):              # REQUIRED
    ...
```

`register(api)` is called once at load. Raise anything inside it and the
plugin is recorded as failed (with your exception's message) and skipped
— it never takes the server or other plugins down.

### Two names, and which one to use where

A plugin has a **filesystem name** (the file/folder name) and a **display
name** (`PLUGIN["name"]`, defaulting to the filesystem name). They are
used in different places, and mixing them up is the single most common
authoring confusion:

| Identifier | Built from | Example |
| --- | --- | --- |
| Ingest format id | display name | `mft-usn.mft` |
| Tab id | display name | `lateral-movement.graph` |
| API route URL | **filesystem** name | `/api/plugin/lateral_movement/edges` |
| Asset URL | **filesystem** name | `/plugin_assets/lateral_movement/ui/tab.js` |

You rarely need to write either by hand: the frontend gets both from
`GET /api/plugins`, and your tab module gets ready-made prefixes as
`winnow.base` and `winnow.assets`. Just don't hardcode a URL built from
the display name.

The simplest way to avoid thinking about it: **name the folder and the
plugin the same thing.** The examples deliberately differ (`mft_usn` vs
`mft-usn`) to show that they can.

### Dependencies

Winnow's core is FastAPI + stdlib, deliberately, so it runs on an
airgapped analysis box. A plugin may depend on anything you like, but:

- **Import third-party modules lazily**, inside the function that needs
  them, not at module top level. A missing dependency then produces one
  broken *feature* with an actionable message instead of a plugin that
  won't load at all.
- Say so in your README, and raise a `ValueError` telling the analyst
  what to `pip install` (see `claude_assistant`).

```python
def ask(req):
    try:
        import anthropic
    except ImportError:
        raise ValueError("This plugin needs: pip install -U anthropic")
```

---

## 4. Hook: ingest formats

```python
api.register_ingest_format(
    id="mft",                          # required; [a-z0-9_-], unique in this plugin
    label="NTFS $MFT (raw)",           # required; shown in the UI
    parse=parse_mft,                   # required; callable
    extensions=[".mft"],               # optional
    filename_patterns=["$MFT", "*.mft"],  # optional
    description="…",                   # optional; shown under the format
    options=[...],                     # optional; see below
)
```

### Matching

A file routes to your format if **either** its extension is in
`extensions` **or** its bare filename fnmatches one of
`filename_patterns`. Both are case-insensitive, and matching uses only
the last path component (so a Windows path from a browser upload works).

`filename_patterns` exists because the artifacts plugins are for often
have no extension at all — `$MFT`, `$J`, `hosts`, `SYSTEM`. It also gets
those files past the **folder import** scan's extension gate
automatically; you don't have to do anything extra for directory
imports to pick them up.

**Built-in extensions always win.** If you register `.csv`, files still
route to Winnow's CSV parser by default — your format stays reachable
through its own picker in Settings → Plugins, but it won't hijack
existing behavior.

### The `parse` contract

```python
def parse(path, options) -> dict:
    return {
        "columns": ["Timestamp", "FileName"],   # required: list[str]
        "rows": iter_rows(path),                # required: iterable of sequences
        "column_types": ["datetime", "text"],   # optional: text|number|datetime
        "name": "custom table name",            # optional: defaults to the filename
    }
```

- **`rows` should be a generator.** It's consumed lazily and committed in
  batches, so memory stays flat on a multi-GB input. Returning a list
  works, and materializes the whole file first.
- **Cells may be `str`, `int`, `float`, or `None`.** Everything is
  stringified (`None` → `""`) because source columns are TEXT — the same
  evidence-fidelity rule the CSV path follows. Don't pre-format numbers
  you want to sort numerically; declare `column_types` instead.
- **Ragged rows are fine.** Short rows are padded, long rows trimmed, and
  the count is reported to the analyst. You don't need to normalize
  lengths yourself.
- **Raising mid-iteration is safe.** Everything committed before the
  error is kept, with an accurate row count, and the analyst sees your
  exception message. Parse defensively rather than aborting a
  million-row import over one torn record — carve past it, and consider
  surfacing a count of skipped records as its own column or in the table
  name.
- **`column_types`** only sets metadata (which drives numeric-aware
  sorting/filtering and datetime handling); every value is still stored
  as TEXT. Declare it when you *know* — a parser that produced the
  timestamps shouldn't leave typing to a 500-row sample.

Timestamps: emit `YYYY-MM-DD HH:MM:SS[.ffffff]` if you can. That's the
shape Winnow's timeframe filter, day-bucketing and timeline normalize
against, so your rows sort correctly next to every other table's.

### Options

Declared options render as a generic form in the import queue
("Options" next to the queued file) and arrive in `parse`'s `options`
dict, already defaulted and validated:

```python
options=[
    {"name": "records", "label": "Records", "type": "choice",
     "choices": ["all", "in-use", "deleted"], "default": "all"},
    {"name": "resolve_paths", "label": "Reconstruct full paths",
     "type": "bool", "default": True},
    {"name": "prefix", "label": "Path prefix", "type": "text", "default": ""},
]
```

Types are `bool`, `text`, `choice` (which requires `choices`). Values the
analyst didn't set come from `default`; keys you didn't declare are
dropped before your parser sees them; a `choice` outside its list is a
400 rather than something your parser has to guard.

---

## 5. Hook: tabs

```python
api.register_tab(
    id="graph",                 # required; [a-z0-9_-]
    label="Lateral movement",   # required; the tab caption
    entry="ui/tab.js",          # required; ES module, relative to the plugin folder
    description="…",            # optional; the tab's tooltip
)
```

Folder plugins only, and `entry` must exist **at registration time** — a
typo is a visible load error in Settings → Plugins, not a 404 the first
time someone clicks the tab.

### The module contract

```js
export default function mount(container, winnow) { /* build your UI */ }

export function onShow(container) { /* optional: tab activated */ }
export function onHide(container) { /* optional: tab deactivated */ }
```

`mount` is called **once**, the first time the tab is activated (tabs are
lazy — a plugin tab costs nothing until used). `container` is an empty
`<section class="pluginview">` filling the main content area; fill it
with whatever you like. `mount` may be `async`.

The mount then **persists across tab switches** — switching to the grid
and back does not rebuild it, so in-progress work survives. It is torn
down and rebuilt when:

- the case is switched (a view built from one case's data must not leak
  into another), or
- the plugin is reloaded — any Settings toggle or install. The entry URL
  carries a `?v=<gen>` cache-buster tied to the reload, so **toggling
  your plugin off and on is the reload button while you iterate on JS.**

`onShow`/`onHide` fire on *every* switch, including the first. Use
`onShow` to refresh anything that may have changed while you were hidden
(new sources imported, theme changed) and `onHide` to pause timers or
animation loops.

### The `winnow` context

The second argument to `mount` is Winnow's stable surface for plugins.
Prefer it to reaching into the app's globals — this is what's supported.

| Field | What it is |
| --- | --- |
| `apiVersion` | Contract version of this object (currently `1`) |
| `plugin` | Your plugin's display name |
| `base` | `/api/plugin/<fs_name>` — prefix for your own routes |
| `assets` | `/plugin_assets/<fs_name>` — prefix for your own files |
| `api(path, opts)` | `fetch` wrapper: JSON in/out, throws with `err.status`, sets the CSRF header |
| `post(path, body)` | JSON POST shorthand |
| `sql(sql, limit)` | Read-only query against the case file |
| `schemaText()` | The case's schema as CREATE TABLE-ish SQL (LLM-ready) |
| `toast(msg, ms)` | Transient status message |
| `el(tag, cls, text)` | Winnow's element helper |
| `modal(title, build, opts)` | Winnow's modal |
| `confirmDialog(msg, opts)` / `promptDialog(msg, initial)` | Async dialogs |
| `openSource(id)` | Switch the app to a source's grid tab |
| `refreshSources()` | Re-fetch the app's source list — call after your backend creates a table via `ingest_rows` (a sync ingest announces itself through no job), then `openSource(new_id)` |
| `state.sources` | Live source list (`{id, name, columns, row_count, is_merge, error}`) |
| `state.sourceId` | Currently selected source id |
| `state.tags` | Tag definitions |
| `state.variables` | The case's variables as `{name: value}` — see [Case variables](#case-variables) |
| `setVariable(name, value)` | Set one case variable (creates it if new) |

**Always call your backend through `winnow.api` / `winnow.post`.** A raw
`fetch()` won't carry the `X-Timeline-Lite-Client` header that Winnow's
CSRF middleware requires on non-GET `/api/*` calls, and will 403.

### Theming

Read colors from Winnow's CSS custom properties instead of hardcoding
them, and your UI works across all four styles and both light and dark:

```js
const accent = getComputedStyle(document.documentElement)
  .getPropertyValue('--accent').trim();
```

Useful tokens: `--ink` (background), `--panel`, `--panel-2`, `--panel-3`,
`--line`, `--line-2` (borders), `--text`, `--dim`, `--accent`,
`--danger`, `--mono`, `--ui`. For canvas work, read them **at draw time**
rather than caching at mount, so a theme switch repaints correctly.

For regular DOM, reuse Winnow's own classes — `btn`, `btn ghost`,
`note-status`, `row-actions`, `session-row` — and you inherit the app's
look for free.

### A complete tab plugin

Everything above, assembled — a tab that charts how many rows carry each
tag, across the whole case. Two files, and it's the smallest thing that
exercises all three hooks' interaction (tab + route + case query).

`plugins/tagchart/__init__.py`:

```python
"""Tag summary: how many rows carry each tag, case-wide."""

PLUGIN = {"name": "tagchart", "version": "1.0.0",
          "description": "Bar chart of tagged rows per tag."}


def summary(req):
    if req.store is None:
        raise ValueError("Open a case first")
    res = req.store.run_sql(
        "SELECT t.name AS tag, COUNT(*) AS n "
        "FROM row_tags rt JOIN tag_defs t ON t.id = rt.tag_id "
        "GROUP BY 1 ORDER BY n DESC",
        limit=200,
    )
    return {"rows": [dict(zip(res["columns"], r)) for r in res["rows"]]}


def register(api):
    api.register_tab(id="chart", label="Tag chart", entry="ui/tab.js",
                     description="Tagged rows per tag, across every table.")
    api.register_api("summary", summary, methods=["POST"])
```

`plugins/tagchart/ui/tab.js`:

```js
let refresh = null;   // module-level so onShow can re-run it

export default function mount(container, winnow) {
  const { el, post } = winnow;
  const box = el('div');
  box.style.cssText = 'padding:14px;overflow:auto;display:flex;flex-direction:column;gap:6px';
  container.append(box);

  refresh = async () => {
    box.replaceChildren(el('div', 'note-status', 'Loading…'));
    let rows;
    try {
      ({ rows } = await post(`${winnow.base}/summary`, {}));
    } catch (e) {
      box.replaceChildren(el('div', 'note-status', 'Failed: ' + e.message));
      return;
    }
    box.replaceChildren();
    if (!rows.length) {
      box.append(el('div', 'note-status', 'No tagged rows in this case yet.'));
      return;
    }
    const max = Math.max(...rows.map((r) => r.n));
    for (const r of rows) {
      const line = el('div', 'row-actions');
      const track = el('div');
      track.style.cssText = 'flex:1;background:var(--panel-3);height:14px';
      const bar = el('div');
      bar.style.cssText = `height:14px;width:${(r.n / max) * 100}%;background:var(--accent)`;
      track.append(bar);
      line.append(el('span', 'session-name', r.tag), track,
                  el('span', 'count', r.n.toLocaleString()));
      box.append(line);
    }
  };
  refresh();
}

// Tags change while you're on other tabs — recount on every activation.
export function onShow() { if (refresh) refresh(); }
```

Note what it does *not* do: no `fetch()` (that would miss the CSRF
header — `winnow.post` handles it), no hardcoded colors (`--accent` and
`--panel-3` follow the analyst's theme), and no direct SQLite access
(`run_sql` keeps it off the writer lock).

### Shipping more than one file

Anything inside your plugin folder is fetchable under `winnow.assets`:

```js
const { rules } = await (await fetch(`${winnow.assets}/data/rules.json`)).json();
await import(`${winnow.assets}/ui/graph.js`);   // split your JS up
```

For CSS, inject a `<link>` in `mount`. Scope your selectors — you're
sharing the document with the rest of the app.

---

## 6. Hook: toolbar panels

```python
api.register_toolbar_panel(
    id="histogram",        # unique within this plugin
    label="Histogram",     # the toolbar toggle's caption
    entry="ui/panel.js",   # ES module, relative to the plugin folder
    description="…",       # the toggle's tooltip
)
```

A toggle button appears in the table toolbar beside the search icon.
While it's on (the state persists per browser) and a table is showing,
your module's UI occupies a strip **between the toolbar and the grid**;
it hides with the toolbar on page tabs. Folder plugins only.

### The module contract

Identical to a tab's: `export default function mount(container, winnow)`,
plus optional `onShow(container)` / `onHide(container)` — called on every
toggle and grid/page switch. `container` is an empty `<section>` spanning
the grid's width; keep it short (a histogram is ~100px), it's above the
evidence.

### Following the grid

The `winnow` context is a tab's (see above) with three additions that
exist for exactly this hook:

```js
export default function mount(container, winnow) {
  const off = winnow.onViewChange(({ sourceId, viewId, rowCount }) => refresh());
  async function refresh() {
    const v = winnow.state.view;             // {view_id, row_count} — the grid's CURRENT view
    if (!v) return;
    const h = await winnow.post(`${winnow.base}/histogram`, { view_id: v.view_id, column });
    draw(h);
  }
  canvas.onmouseup = () => winnow.setTimeRange({ column, start: '2026-03-14 08:00:00', end: '2026-03-14 09:00:00' });
  clearBtn.onclick = () => winnow.clearTimeRange();
}
```

- `winnow.onViewChange(cb)` fires after **every** grid rebuild — filter,
  search, sort, timeframe, table switch. Returns an unsubscribe.
- `winnow.state.view` is what the table is showing right now. Hand its
  `view_id` to a route of yours that reads *through the view* (see
  `Store.time_histogram` for the shape: reader pool, both view kinds,
  merges unioned) and your panel describes exactly the rows on screen.
- `winnow.setTimeRange({column, start, end})` / `winnow.clearTimeRange()`
  write the case timeframe filter — the toolbar's ⏱ — as if typed into
  its dialog, so the button, the toggle key and every other consumer
  agree. The rebuild that follows fires `onViewChange` again.
- `winnow.onAppearanceChange(cb)` fires after every skin / theme / accent
  change. A canvas doesn't inherit CSS: read the tokens you paint with
  (`getComputedStyle(document.documentElement).getPropertyValue('--accent')`
  and friends) at draw time and redraw here, and your panel follows the
  analyst's look — including a custom accent — like the rest of the app.

`table_histogram/` is the worked example: bars per time bucket of a
datetime column, a drag on them becomes the timeframe filter, and the
whole thing re-queries on every view change.

---

## 7. Hook: API routes

```python
api.register_api("edges", edges_handler, methods=["POST"])
# -> POST /api/plugin/<fs_name>/edges
```

Routes may nest (`"chat/stream"`); each segment is `[a-z0-9_-]`. Methods
default to `("GET", "POST")` and must be a subset of GET/POST/PUT/DELETE.

### The handler

```python
def edges_handler(req):
    # req.method  "GET" | "POST" | ...
    # req.route   "edges"
    # req.query   dict[str, str] from the query string
    # req.body    parsed JSON, or None
    # req.store   the open Store, or None when no case is open
    if req.store is None:
        raise ValueError("Open a case first")
    return {"edges": [...]}          # anything JSON-able
```

`PluginRequest` is deliberately a plain object — no FastAPI types — so
handlers are trivially unit-testable and the contract survives framework
upgrades.

### Errors

- **`raise ValueError("…")` → HTTP 400** with your message shown to the
  analyst. Use it for everything they can act on: no case open, a column
  that doesn't exist, a missing dependency, a bad parameter.
- **Anything else → HTTP 500** with a traceback in the server console.
  That's the right outcome for a genuine defect; don't catch broadly just
  to convert bugs into 400s.

Always validate `req.body` — it comes from the browser. Check that a
`source_id` is an int, that column names exist in the source's real
column list, and never interpolate an unvalidated string into SQL (see
below).

---

## 8. Hook: row actions

```python
api.register_row_action(
    id="vt",                          # unique within this plugin
    label="Look up on VirusTotal",    # the menu entry
    handler=vt_lookup,
    description="Query VT for the selected cell's hash",
    max_rows=50,                      # the entry is disabled past this many rows
)
```

Right-clicking a row (or a selection) in any table view shows a
**Plugins** section listing every registered action. Choosing one has
the server resolve the selected rows to their full cells — by
`(source_id, rid)`, so a merged view hands you each member's own row —
and call your handler.

### The handler

```python
def vt_lookup(req):
    # req.body = {
    #   "source_id": int,            # the table (or merge) the analyst is in
    #   "column": str | None,        # the right-clicked cell's column…
    #   "value":  str | None,        # …and its value, when the click hit a cell
    #   "rows": [{"rid": int, "source_id": int, "cells": {col: val, ...}}, ...],
    # }
    hashes = {r["cells"].get(req.body["column"]) for r in req.body["rows"]}
    ...
    return {"message": f"{len(hashes)} hashes queued"}
```

The return value is JSON-able. Three optional keys drive the UI:

| key | effect |
| --- | --- |
| `message` | Shown as a toast (default: "*label*: done") |
| `open_url` | Opened in a new browser tab — hand off to a web console |
| `show_tab` | Activates one of this plugin's registered tabs (namespaced id, e.g. `"vt-plugin.results"`) — for results too rich for a toast |

`raise ValueError("…")` → 400 with the message, same as API routes.
`max_rows` is enforced server-side as well as greying the menu entry, so a
network-bound lookup can never be pointed at a million rows. `req.store`
and `req.storage` are available exactly as for API routes — an action can
land its results as a new table with `ingest_rows` (see *Writing tables
from a plugin*).

---

## 9. Talking to the case

`req.store` is Winnow's `Store`. The safe, supported way to read from it:

```python
res = req.store.run_sql("SELECT a, b FROM src_1 LIMIT 100", limit=5000)
# -> {"columns": [...], "rows": [[...], ...], "truncated": bool, "elapsed_ms": int}
```

**Use `run_sql` for reads.** It opens its own read-only connection and
never takes Winnow's writer lock, so a slow aggregation can't block
ingests, tagging, or anyone else's queries — and it inherits the SQL
pane's statement checks (no `ATTACH`/`PRAGMA`/`VACUUM`) for free.

Winnow's own hot read paths use an internal pooled-reader mechanism
(`Store._reader()`) instead. It's private and has preconditions a plugin
isn't positioned to guarantee — don't reach for it; `run_sql` is the
supported plugin read path.

Other useful methods:

| Call | Returns |
| --- | --- |
| `store.list_sources()` | `list[dict]` — every source, shape below |
| `store.get_source(source_id)` | One source dict; raises `KeyError` if absent |
| `store.ingest_rows(columns, rows, name=...)` | Create a new table from computed rows |
| `store.path` | The case file's path |

A source dict:

```python
{
  "id": 1,
  "name": "evtx-security.csv",       # display name (the tab caption)
  "table_name": "src_1",             # what you query — always src_<id>
  "columns": [{"name": "Timestamp", "type": "datetime"}, ...],
  "row_count": 331_642,
  "path": "/evidence/…",             # absolute source path, may be None
  "file_hash": "…", "imported_at": "2026-08-16T09:12:44",
  "has_fts": 1, "fts_building": False,
  "is_open": True,                    # has a visible tab
  "tagged_row_count": 118, "note_count": 4,
}
```

Two things to know about ids: a **negative** `source_id` is a *merge*
(a virtual union of several sources) with no single backing table — if
your feature needs `table_name`, reject negatives with a `ValueError`
rather than building broken SQL. And the sidecar tables are queryable
too: `row_tags(source_id, rid, tag_id)`, `row_notes(source_id, rid,
note)`, `tag_defs(id, name, color, hotkey)` — that's how you join
analyst findings to evidence rows.

### Quoting identifiers

Column and table names are **user data** — they come from CSV headers.
Never f-string one into SQL. Winnow hands you the same quoting helper it
uses internally, on the `api` object:

```python
def register(api):
    sql = f"SELECT {api.q(col)} FROM {api.q(src['table_name'])} WHERE {api.q(col)} != ''"
```

`api.NUM_RE` is there too — the same "does this look numeric" test the
grid uses for right-alignment and numeric sorting, so a plugin's tables
agree with the rest of the app.

Module-level helpers that never see `api` can import them directly
instead; the three bundled examples that build SQL this way do exactly
that, and it stays supported:

```python
from winnow.store import NUM_RE, q
```

Prefer `api.q` in handler code. It is the stable surface: it does not
depend on where Winnow's modules happen to live, and plugins load by file
path rather than as part of the package, so a direct import only resolves
because the server was started from the install directory.

Validate first, quote second: check the column is actually in
`src["columns"]` before using it, then quote it. Values (as opposed to
identifiers) should go through `run_sql`'s SQL as literals you built from
validated input, or be avoided entirely by filtering in Python.

### Writing tables from a plugin

To turn a computation into a browsable table, use `ingest_rows` — the
same path plugin parsers feed:

```python
req.store.ingest_rows(
    ["Host", "Score"], ((h, s) for h, s in results),
    name="Beacon scoring",
)
```

It follows every ingest convention automatically (TEXT columns,
contiguous row ids, batched commits, background search index), so the
result is a completely normal source.

**Never write to a `src_` table.** Source tables are immutable by
contract — that's what makes re-import non-destructive and sessions
portable. Derive a new table instead.

### Case variables

A case carries a small set of named values — the engagement name, the
base URL of a backend your plugin talks to, a link to the scoping
document — that the analyst edits under **Case ▾ → Case settings →
Variables**. Plugins read and write the same set:

```python
def report_handler(req):
    base = req.variables.get("report_api")        # {} when no case is open
    if not base:
        raise ValueError("Set the `report_api` variable in Case settings")
    req.set_variable("last_report", datetime.now().isoformat())
    ...
```

- `req.variables` is a plain `{name: value}` dict of strings; it never
  raises.
- `req.set_variable(name, value)` creates or updates one. Names are
  `[A-Za-z][A-Za-z0-9_.-]*` (up to 64 chars); values are capped at 4000
  characters. `ValueError` when no case is open or the name is bad.
- Tabs and toolbar panels get the same through `winnow.state.variables`
  and `winnow.setVariable(name, value)`.

A **profile** (case type) can declare the variables its plugins expect,
so the analyst is asked once, at case creation, rather than discovering
a missing setting mid-investigation:

```json
"variables": [
  {"name": "engagement", "label": "Engagement name", "required": true,
   "description": "Used in report titles"},
  {"name": "report_api", "label": "Report API base URL", "default": "http://reports.local"}
]
```

Required ones gate the **New case** dialog; applying a profile to an
existing case seeds any that are absent (never overwriting a value the
analyst already set) and prompts for the required ones still empty.
Saving a profile from a case carries its variable *definitions* along —
never the values.

**Variables live in the case file and travel with it.** They are for
configuration, not secrets: a token or password belongs in the
environment (see [Secrets: the `WINNOW_*` environment](#secrets-the-winnow_-environment)),
never in a variable.

---

## 10. Testing a plugin

Plugins are ordinary Python, so ordinary tests work. Two levels:

**Test the parser directly** — no Winnow, no server:

```python
from mft_usn import mft

out = mft.parse("tests/fixtures/tiny.mft", {"records": "all"})
rows = [dict(zip(out["columns"], r)) for r in out["rows"]]
assert rows[0]["FullPath"] == ".\\Users\\bob\\secret.txt"
```

**Test through the registry** to cover `register()` itself:

```python
from plugin_api import PluginRegistry

reg = PluginRegistry()
reg.load([Path("examples/plugins")])
rec = next(p for p in reg.describe() if p["fs_name"] == "my_plugin")
assert rec["error"] is None            # catches register() raising
fmt = reg.get_format("my-plugin.thing")
assert fmt.matches("evidence.thing")
```

**Test a route handler** by building a throwaway case. Handlers take a
plain `PluginRequest`, so no HTTP and no fixtures from Winnow's own
suite are involved — this file runs on its own with just `pytest`:

```python
# test_myplugin.py — put it next to your plugin folder
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "/path/to/winnow")             # the install root, not winnow/
from winnow.plugin_api import PluginRequest        # noqa: E402
from winnow.store import DEFAULT_TAGS, Store       # noqa: E402

import myplugin                               # your plugin package


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "case.db"), default_tags=DEFAULT_TAGS)
    # Give it something to query: ingest_rows is the same path a parser feeds.
    s.ingest_rows(["Host", "User"], [["WS1", "alice"], ["WS2", "bob"]], name="logons")
    yield s
    s.close()


def test_handler(store):
    out = myplugin.summary(PluginRequest("POST", "summary", {}, {"source_id": 1}, store))
    assert out["rows"] == [...]


def test_handler_without_a_case():
    with pytest.raises(ValueError):        # -> the 400 the analyst sees
        myplugin.summary(PluginRequest("POST", "summary", {}, {}, None))
```

Two habits worth copying from `tests/test_plugins.py`: fake an external
SDK with `monkeypatch.setitem(sys.modules, "anthropic", fake_module)` so
a network-dependent plugin is still testable offline, and build binary
fixtures in the test file rather than committing evidence.

For a tab's JS there's no browser test runner, but you can at least
syntax-check it the way the repo checks its own frontend modules — see the Testing
section of [`CLAUDE.md`](../CLAUDE.md).

---

## 11. Installing and sharing

**Install:** Settings → Plugins → *Install a plugin…* — pick the `.py`
file for a single-file plugin, or the folder that directly contains
`__init__.py` for a folder plugin (the dialog states the rule and checks
your pick before any bytes move). It copies into `plugins/` and loads
immediately, no restart. Copying in by hand works identically; the panel
picks it up next time it opens. The bundled examples in
`examples/plugins/` never need installing — they're always listed, with
a scope dropdown like any other plugin, just defaulting to off.

**Scopes:** each plugin is on/off *for all cases* (machine default,
`workspace/plugins.json`) or *for this case only* (an override stored in
the case file's `case_settings`, so it travels with the evidence). The
registry reloads on every case switch — per-case disablement really
unloads the code, same guarantee as the machine-level switch.

**Distribute** a folder plugin as a zip or a git repo containing the
plugin folder. Include a README saying what it does, what it needs
(`pip install …`), and whether it touches the network. Nothing is
fetched automatically — installation is always the analyst's explicit
act.

**Extra plugin directories:** `--plugins-dir DIR` (repeatable) or
`$WINNOW_PLUGINS_DIR`. Useful for developing out of a git checkout
without copying:

```bash
python server.py --plugins-dir ~/src/my-winnow-plugins
```

Installs from the UI always land in the first directory (`plugins/`).

**Versioning:** the current plugin API version is **4** (toolbar panels
and the view-change context arrived in 4; row actions in 3; `api.q`/
`NUM_RE` in 2). Set `WINNOW_API_VERSION` to the
API version you built against. If a future Winnow's API version is lower than yours, it
refuses to load your plugin with a "update Winnow" message rather than
failing mysteriously somewhere inside `register()`.

---

## 12. Security model

**A plugin is arbitrary Python running with Winnow's privileges.** It can
read any file the analyst can, open sockets, and touch the case. There is
no sandbox — the same trust model as a Notepad++ plugin, an Autopsy
module, or a Ghidra script.

What Winnow guarantees:

- Nothing is ever downloaded or auto-installed. Code runs only because
  someone put it in a plugin directory.
- A **disabled plugin is never imported** — its code does not run at all,
  which is why the off switch is meaningful rather than cosmetic.
- Asset serving is confined to the plugin's own folder, and installs
  reject path traversal.

What that leaves to you, as an author:

- Say plainly in your README if the plugin touches the network or reads
  outside the case. Analysts run these on evidence machines.
- Treat everything from `req.body` and `req.query` as hostile input.
- Don't log or persist secrets; read credentials from the environment
  rather than a file in the plugin folder — and never from a case
  variable, which is case data and travels with the file.
- Keep the airgap in mind — if your plugin needs the internet, make that
  the headline of your README, the way `claude_assistant` does.

### Secrets: the `WINNOW_*` environment

A token has exactly one supported home: an environment variable with the
`WINNOW_` prefix, which the analyst sets under **Settings → Environment**
(or exports from the shell — the shell wins). Winnow keeps it in the
user's own environment — `HKCU\Environment` on Windows, an owner-only
`~/.config/winnow/env` elsewhere — and loads it into the process at
startup. It is never in the case file, never in a Winnow setting, never
returned by an API, never shown again once saved.

```python
def lookup_handler(req):
    token = req.env("WINNOW_VT_API_KEY")
    if not token:
        raise ValueError("Set WINNOW_VT_API_KEY under Settings → Environment")
    ...
```

- `req.env(name, default=None)` reads one; only `WINNOW_*` names are
  readable through it (`ValueError` otherwise), so a plugin can't lift
  `AWS_SECRET_ACCESS_KEY` or anything else out of the process by way of
  Winnow.
- Read it server-side, in the handler that uses it. **Never send the
  value to the browser** — not in a response, not in a tab's HTML. A tab
  that needs a network call makes it through its own `register_api`
  route.
- Tell the analyst the name in your README, the way `claude_assistant`
  names `ANTHROPIC_API_KEY`: "set `WINNOW_VT_API_KEY` under Settings →
  Environment".
- For everything that is *not* secret — the engagement, a base URL, a
  document link — use a [case variable](#case-variables) instead, so it
  travels with the case.

---

## 13. Troubleshooting

| Symptom | Cause |
| --- | --- |
| Plugin missing from Settings → Plugins | Filename starts with `.` or `_`; or it's a folder with no `__init__.py`; or it's not in a scanned directory (check the paths listed in the panel) |
| Listed as "failed to load" | Your `register()` raised, or the module failed to import. The panel shows the message; the server console has the full traceback |
| "needs Winnow plugin API vN" | Your `WINNOW_API_VERSION` is newer than this Winnow |
| Format never triggers on a file | Extension collides with a built-in (built-ins win — use the format's own picker), or the name doesn't match your patterns. Patterns match the bare filename, not the path |
| Tab doesn't appear | Registration failed (see "failed to load"), or the plugin is toggled off. `register_tab` needs a **folder** plugin |
| Tab JS changes don't show up | Toggle the plugin off and on in Settings — that bumps the cache-buster and re-imports the module |
| Tab shows "failed to load: …" | Your module threw during import or `mount`. Open the browser console for the stack |
| Route returns 403 | Non-GET call that bypassed `winnow.api`/`winnow.post` and so lacks the CSRF header |
| Route returns 404 | Wrong name in the URL — routes use the **filesystem** name, not `PLUGIN["name"]`. Use `winnow.base` |
| Route returns 405 | Method not in the `methods=` list you registered |
| Every request 500s | Your handler raises something other than `ValueError`. Traceback is in the server console |
| Import produces one giant column | Your `rows` yielded strings instead of sequences — each row must be a list/tuple of cells |
| Numbers sort as text | Declare `column_types` (`"number"`), or the type comes from a 500-row sample |

---

## 14. Reference

### `register_ingest_format(*, id, label, parse, extensions=(), filename_patterns=(), description="", options=())`

`parse(path: str, options: dict) -> {"columns": list[str], "rows": Iterable[Sequence], "column_types"?: list[str], "name"?: str}`

Option spec: `{"name": str, "label"?: str, "type": "bool"|"text"|"choice", "default"?: Any, "choices"?: list[str]}`

### `register_tab(*, id, label, entry, description="")`

Module: `export default function mount(container, winnow)`, plus optional
`onShow(container)` / `onHide(container)`.

### `register_api(route, handler, methods=("GET", "POST"))`

`handler(req: PluginRequest) -> JSON-able`, where `PluginRequest` has
`.method`, `.route`, `.query`, `.body`, `.store`. `ValueError` → 400.

### `register_toolbar_panel(*, id, label, entry, description="")`

Module: `export default function mount(container, winnow)`, plus optional
`onShow(container)` / `onHide(container)`. Context additions:
`winnow.onViewChange(cb) -> unsubscribe`, `winnow.state.view`,
`winnow.setTimeRange({column, start, end, enabled=true})`,
`winnow.clearTimeRange()`, `winnow.onAppearanceChange(cb) -> unsubscribe`.

### `register_row_action(*, id, label, handler, description="", max_rows=1000)`

`handler(req: PluginRequest) -> JSON-able`; `req.body` is
`{"source_id", "column", "value", "rows": [{"rid", "source_id", "cells"}]}`.
Optional return keys `message` / `open_url` / `show_tab`. `ValueError` → 400.

### `PluginRequest`

What every API-route and row-action handler receives:

| Member | What it is |
| --- | --- |
| `method`, `route` | HTTP method and the route as registered |
| `query` | `dict[str, str]` from the query string |
| `body` | Parsed JSON body, or `None` |
| `store` | The open `Store`, or `None` when no case is open |
| `storage` | Plain-JSON dict persisted per plugin in the workspace (`plugin_data/<fs_name>.json`) — not case data, and readable on disk, so not for secrets |
| `variables` | The case's variables as `{name: value}` (`{}` with no case) |
| `set_variable(name, value)` | Create or update one case variable |
| `env(name, default=None)` | A `WINNOW_*` environment variable — the home for tokens; prefix-enforced, server-side only |

### HTTP surface

| Endpoint | Purpose |
| --- | --- |
| `GET /api/plugins` | Everything loaded: plugins, formats, tabs, directories |
| `POST /api/plugins/toggle` | `{fs_name, enabled}` — persists and reloads |
| `POST /api/plugins/install` | Multipart install; copies into `plugins/` |
| `POST /api/ingest/plugin/path` | `{path, format_id, name?, options?}` — ingest by server path |
| `POST /api/ingest/plugin/upload` | Multipart sibling of the above |
| `GET /plugin_assets/<fs_name>/<path>` | A plugin's own files |
| `* /api/plugin/<fs_name>/<route>` | A plugin's registered routes |
| `POST /api/plugins/row_action/<fs_name>/<id>` | Runs a row action on `{source_id, pairs: [[source_id, rid]…], column?, value?}` |
| `GET /api/case/variables` | `[{name, value, description, required}]` for the open case |
| `POST /api/case/variables` | `{name, value?, description?, required?}` — create or update one |
| `DELETE /api/case/variables/<name>` | Remove one |
| `GET /api/env` | `WINNOW_*` names and where each comes from — never values; loopback-only |
| `POST /api/env` | `{name, value}` — save one; loopback-only |
| `DELETE /api/env/<name>` | Remove one; loopback-only |

The path/upload ingest routes are also the scripting entry point — you
can drive a plugin parser from `curl` without touching the UI.

---

## 15. Writing a plugin with an LLM

**Paste this one file. That's the whole context budget.**

Everything an author needs is here: the contract for all three hooks,
the data shapes you'll consume, complete runnable examples of an ingest
format and a tab, and a standalone test recipe. It deliberately does not
assume you can read Winnow's source — every example in it was extracted
verbatim from this document and run against a live server before it
shipped.

Rough context cost (character estimate, not a tokenizer run):

| What you paste | Size | Use it when |
| --- | ---: | --- |
| **This guide alone** | **~8k tokens** | Anything described here — which is every hook |
| \+ `plugin_api.py` | ~14k tokens | You want the enforcing code beside the prose (validation rules, exact error text) |
| \+ one `examples/plugins/*` | ~12–23k tokens | You're building something close to that example and want a full working precedent |
| The whole codebase | ~190k tokens | You're changing Winnow itself, not writing a plugin |

So the guide is roughly **1/24th** the cost of loading the tool, and the
step up to guide + contract is still under a tenth.

### A prompt that works

> Here is the plugin development guide for Winnow, a local DFIR triage
> tool. Write a plugin that <what you want>. Follow the contract in the
> guide exactly — do not invent API surface that isn't documented in it.
> Include a test file using the standalone recipe in §8.
>
> <paste this file>

### What to hand it *instead of* the codebase

- **Don't paste `tests/test_plugins.py`.** It mostly tests Winnow's
  plugin *host* — the loader, installs, traversal rejection — none of
  which a plugin author implements. §8's recipe is the part that's
  actually about testing your own plugin.
- **Don't paste `store.py`.** The supported surface is the short list in
  §7; the rest is internals a plugin must not reach into anyway. If you
  paste it, an LLM will happily use a private method and you'll find out
  when Winnow refactors.
- **Do paste an example plugin** if you're building something in the
  same shape — a precedent is worth more than prose for the last 10%.

### The honest edges

Three things this guide can't do for you:

- **New hook types.** If you need an extension point that doesn't exist
  (a new export format, a right-click action), no amount of guide helps
  — that's a change to `plugin_api.py`, i.e. a Winnow PR.
- **Matching internal behavior exactly.** If your parser has to reproduce
  a Winnow-specific detail not spelled out here (say, precisely how the
  timeframe filter normalizes an odd timestamp shape), read the source
  for that one function.
- **Anything an LLM asserts that isn't in here.** The failure mode to
  watch for is a confidently invented method — `store.query()`,
  `api.register_command()`, `winnow.refresh()`. None of those exist.
  Cross-check any API call against §12; if it isn't listed, it's a
  hallucination, and the plugin will fail at load or at first click with
  a message that says so.
