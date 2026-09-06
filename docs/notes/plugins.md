# Plugins: the extension host and the example plugins

`plugin_api.py`, `plugins/`, `examples/plugins/` (mft_usn, lateral_movement,
pivot, claude_assistant). The authoring contract
lives in `plugin_api.py`'s module docstring and
[docs/writing-plugins.md](../writing-plugins.md); this is what bites the
*host*.

Part of the working notes split out of [CLAUDE.md](../../CLAUDE.md) —
see [docs/notes/README.md](README.md) for the whole set.

---

- **`_reload_plugins()` mutates the registry in place, so a shared
  `PluginRegistry` in a test is a landmine.** It calls
  `PLUGINS.load(...)` on the existing object rather than building a new
  one. A test that monkeypatches `server.PLUGINS` to a module-scoped
  fixture and then does anything that reloads — applying a profile,
  toggling a plugin, switching case — rewrites that fixture against the
  real plugin directories, where the bundled examples are default-OFF.
  Every later test in the module then gets 404s from example-plugin
  routes, far from the test that caused it. `tests/test_plugins.py`'s
  `example_registry` is function-scoped for exactly this reason; keep it
  that way.

- **A plugin can WRITE `WINNOW_*` environment variables, and that is
  deliberately not a privilege boundary.** `req.set_env` goes through
  `userenv.set_var`, so it inherits every rule the Settings panel has
  (prefix, RESERVED refused, a shell export still wins) — a plugin can do
  no more than the analyst can. It grants no capability a plugin lacked:
  it is arbitrary Python and could always write `~/.config/winnow/env` or
  poke the registry itself. What the API buys is that it lands in the
  right place with the right permissions and shows up in the panel the
  analyst manages. The one genuinely new exposure is by proxy: Winnow's
  own `/api/env` routes are loopback-only, and a plugin route is not, so a
  plugin that wraps `set_env` in a route hands a remote viewer (remote
  mode) the ability to trigger it. `req.is_loopback` exists so the plugin
  can make the same call Winnow makes; the guide says to use it.

- **A plugin route handler runs in a worker thread, and must keep
  doing so.** `api_plugin_dispatch` is `async def`, so for a while it
  called `entry["handler"](req)` straight from the event loop — one
  plugin waiting on an LLM completion froze the whole server, presence
  stream included, for as long as the call took. It now goes through
  `run_in_threadpool`. Row actions were always fine (their route is a
  plain `def`, which FastAPI threadpools for you); if you add another
  plugin entry point from an `async def`, thread it the same way.

- **Plugin tables are namespaced against ACCIDENTS, not against a plugin
  that means harm.** A plugin's own tables live in the case file as
  `plugin:<fs_name>:<table>` (`req.table("chat")`). The separator is a
  colon because `plugin_<fs>_<name>` was ambiguous exactly where this
  codebase lives — plugin `mft_usn` table `cache` and plugin `mft` table
  `usn_cache` both produced `plugin_mft_usn_cache`, and one plugin could
  read and drop the other's data by accident. Underscored folder names are
  the house style, so that was reachable, not theoretical.
  `{table}` substitution is a convenience so authors never quote an
  identifier; it is NOT a boundary, and the docs must not say it is — a
  plugin holds `req.store` and can do anything to the case file. The
  plugin half of the name is deliberately permissive (a `chat-gpt` folder
  installs and serves routes fine, so it must not get a permanent 400
  from `req.table()`); only `:` and `"` are refused.
  They carry no `sources` row, so the grid, the sidebar and merges never
  see them; use `ingest_rows` when an analyst should browse the result.

- **Plugins** (plugin_api.py, `plugins/`, Settings → Plugins) are
  Notepad++-style drop-in extensions, first loaded at server *import* (so
  `uvicorn server:app` gets them, not just `python server.py`;
  `--plugins-dir` / `$WINNOW_PLUGINS_DIR` add directories) and reloaded
  live by `_reload_plugins()` whenever Settings → Plugins toggles or
  installs one — `PluginRegistry.load` is written to be safely re-run on
  a live server (registry rebuilt wholesale; superseded modules linger in
  sys.modules under unique per-load names because Python can't truly
  unload code, but nothing references them again). Enabled/disabled state
  is `workspace/plugins.json` (`PluginPrefs`) — machine-level workflow
  state, stored as a *disabled* list so presence in plugins/ means on by
  default, keyed by filesystem name because that's the only identity that
  exists without importing (a disabled plugin is discovered for the
  listing but its code never runs — the point of the off switch).
  `POST /api/plugins/install` copies an uploaded .py or folder (from the
  panel's webkitdirectory picker) into `PLUGIN_DIRS[0]`, rejecting
  absolute/`..` paths so an upload can't write outside the plugins dir; a
  name collision is a 409 the frontend confirms into `overwrite=true`,
  and an install whose *load* then fails still keeps the files and
  reports the error (same standing as a hand-copied broken plugin).
  Three extension points on PluginAPI:
  `register_ingest_format` parses a file into columns + a row iterable
  that feeds `Store.ingest_rows` — the generic sibling of `ingest_csv`
  with every one of its conventions (all-TEXT via `sanitize_columns`,
  contiguous rid from 1, per-BATCH lock/commit, ragged pad-and-count,
  sampled types with an explicit `column_types` override, background FTS)
  — so invariants #1/#2 hold for plugin sources with no extra work, and a
  plugin source is a completely normal source afterward.
  `register_tab` adds a page tab (a true SQL/Timeline sibling — ordered
  among them by the analyst, not pinned after them; see the two-strips
  entry in [ui.md](ui.md)): the entry is
  an ES module in the plugin folder, served via `/plugin_assets/<fs>/…`
  (enabled folder plugins only; resolved-path containment blocks
  traversal; same no-cache middleware as /static/) and dynamically
  import()ed by `showPluginTab` (`static/js/plugins.js`) with a `?v=<gen>` cache-buster
  — `gen` is the registry load sequence, bumped on every reload, so a
  toggle-off/on picks up changed JS. One mount per tab, kept across tab
  switches, torn down on case switch (`resetPluginTabMounts`) and gen
  change; the module's default export gets `(container, winnow)` where
  `winnow` is `buildPluginTabContext`'s stable surface (api/post/el/
  modal helpers, `sql()` → run_sql's own RO connection, `schemaText()`,
  live state getters incl. state.timeRange, plus openFiltered(source_id, [{column, value}]) to jump to the evidence); optional onShow/onHide exports fire per switch.
  `register_api` registers backend routes dispatched at request time by
  the one catch-all `/api/plugin/{fs}/{route}` handler — deliberately
  not real FastAPI routes, so a Settings toggle's registry reload is
  instantly authoritative with no stale route objects. Handlers get a
  plain `PluginRequest` (method/route/query/body/store — None when no
  case is open — and `storage`, the plugin's own persistent dict) and return JSON-ables; ValueError → 400, same split as
  api_view; the CSRF middleware already covers non-GET. `req.storage.get()/set(dict)` is one JSON document per plugin under workspace/plugin_data/ (machine level, in updater.PROTECTED) — cross-case state like a plugin's saved definitions, parallel to the app's saved filters. Plugin backends
  should read via `req.store.run_sql` (own RO connection — never holds
  invariant #4's lock). A plugin that
  fails to import/register is recorded with its error and skipped, never
  fatal; `GET /api/plugins` carries the reason to the Plugins modal.
  Format matching is extension OR bare-filename fnmatch — the latter
  because the files plugins exist for ("$MFT", "$J") *have* no extension;
  that's also why `scan_import_directory` grew `filename_patterns` (a
  second way past its extension gate, marked kind `"plugin"`; kinds the
  frontend can't resolve to a loaded format fall back to the CSV path,
  the pre-plugin behavior for analyst-added extensions) and why the
  Plugins modal's per-format picker sets no `accept` attribute. Routing
  precedence (`pluginFormatFor` in `static/js/importer.js`): a built-in extension always
  wins over a plugin claiming the same one. Parse errors are 400s like
  every other ingest route. Security model is Notepad++'s: a plugin is
  arbitrary local Python with the app's privileges, installed by the
  analyst physically placing it — never fetched, so the airgap rule holds.
- The example mft_usn plugin's fixup handling encodes a real-world trap:
  extraction tools disagree about whether $MFT records arrive with NTFS's
  multi-sector fixups still stamped (KAPE/icat/RawCopy: yes; ntfscat:
  already un-applied — verified against a real mkntfs volume, where the
  strict all-stamped check silently produced 0 rows). `_apply_fixups`
  therefore distinguishes all-stamped (un-stamp), none-stamped (parse
  as-is), and mixed (genuinely torn → skip the record).

- **The pivot example is the one that exercises `run_sql`'s edges**, and
  three of them bit during its build — worth knowing before writing
  another aggregating plugin:
  - `run_sql` takes **no parameters**, so a plugin building a WHERE clause
    has to inline its own values, and inlining has to walk string literals.
    The numeric guard (`_numeric_expr`'s pattern, which any plugin
    aggregating a TEXT column needs to copy) embeds `NUM_RE`, and that
    regex contains two `?` of its own. A naive inliner reads them as
    placeholders and shifts every bound value one slot along — silently
    wrong rather than loudly broken. Winnow's own `_inline_sql_params`
    walks literals for exactly this reason.
  - **A derived column is not in the physical `src_<id>` table.** It's
    merged into `src["columns"]` at read time but materialised in the
    `drv_<id>` sidecar. Plugin SQL that names one must `LEFT JOIN` the
    sidecar **and qualify every reference through the aliases**
    (`s."col"` / `d."col"` — the `_from_clause`/`_col` pair the bundled
    examples copy). Qualifying is now load-bearing twice over: `run_sql`
    shadows `src_<id>` with a derived-including TEMP view when the source
    has derived columns, so a *bare* reference next to a manual sidecar
    join is `ambiguous column name` (loud), while a bare reference with
    *no* join on a source without derived columns falls into SQLite's
    double-quoted-string fallback and returns the literal column name
    (silent). Hand-written pane queries need none of this — bare
    `src_<id>` includes derived columns since the shadow views; `main.
    src_<id>` is the raw table. `_base_cols` exists in store.py for the
    paths that must *not* see derived columns.
  - **Anything a plugin inlines into SQL has to skip quoted spans — both
    kinds.** Single quotes because the numeric guard embeds a regex
    containing `?`; double quotes because a CSV header can be `Elevated?`
    and `q()` quotes it straight into the statement. Miss either and bound
    values shift one slot along, which is wrong rather than broken.
  - **A plugin can't see a view.** `run_sql` opens a read-only connection
    to the *case file*; `v.view_N` lives in the scratch database that only
    the reader pool attaches. Any plugin that wants "what the grid is
    showing" has to be handed the filter spec and recompile it, or do what
    the pivot does and own its filtering outright.
  - **Iterating on a plugin's Python needs a reload.** The module is
    imported once at server import; editing the file and re-requesting a
    route runs the *old* code with the *new* line numbers, which makes
    tracebacks point at unrelated lines. Toggle the plugin off and on in
    Settings → Plugins (that's what `_reload_plugins` is for) or restart.

- **Plugin enablement is two-layer, and the defaults point opposite ways.**
  Machine level (workspace/plugins.json): installed plugins are on unless
  in the `disabled` list; bundled examples (examples/plugins/, always in
  PLUGIN_DIRS) are off unless in `enabled_bundled` — presence-means-on is
  right for something the analyst placed and wrong for what we shipped.
  Case level (case_settings `plugin_overrides`, JSON {fs_name: bool}):
  wins where set, travels with the case file on purpose. Three rules keep
  it honest: `_reload_plugins()` runs on every case open, because "a
  disabled plugin's code never runs" is a per-case statement now; the
  "everywhere" scopes clear the open case's override, so the dropdown
  can't show a state a leftover override silently exempts; and
  `PluginRegistry.load` is first-directory-wins on fs_name, so an
  analyst's installed copy of an example shadows the bundled one instead
  of both loading and fighting over tab ids.

- **The `esxi_logs` example and the {{all:...}} widget placeholder.**
  The ESXi / UAC triage profile (winnow/defaults/profiles.json) needs to
  see across MANY log tables at once — a support bundle is a pile of files
  (hostd.log, vmkernel.log, …) plus rotated copies, all sharing the one
  "ESXi / Linux host logs" header set the esxi_logs plugin emits. The
  ordinary `{{header_set:Name}}` placeholder binds to the FIRST matching
  source, which would silently ignore every other log. So the store grew a
  sibling: `{{all:header_set:Name}}` expands to a parenthesised
  `SELECT * FROM src_a UNION ALL SELECT * FROM src_b …` over every matching
  source (schemas identical by construction, so the union lines up by
  position), and the dashboard slices it back apart on the plugin's `Log`
  column ('hostd'/'auth'/'shell'/…, set from the filename). A profile still
  can't create a merge, and this isn't one — it's a read-time union, so it
  needs no new object in the case file and picks up rotated logs imported
  later with no re-wiring. The plugin parses two line shapes per line, not
  per file (ESXi ISO and year-less Linux syslog both appear in one
  syslog.log), and folds timestamp-less continuation lines onto the
  previous row rather than emitting orphans.

- **Bundles are profiles.** A plugin bundle (PluginBundles, workspace/plugin_bundles.json) now carries an optional `dashboard` (a list of widget definitions) alongside its plugins. Applying a bundle whose profile has a dashboard also sets the open case's dashboard (Store.set_dashboard). So a profile is 'how I analyze this kind of case' — plugins + a dashboard — one saveable, shareable JSON thing. See docs/design/analysis-suite.md.
