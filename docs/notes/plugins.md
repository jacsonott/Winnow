# Plugins: the extension host and the example plugins

`plugin_api.py`, `plugins/`, `examples/plugins/`. The authoring contract
lives in `plugin_api.py`'s module docstring and
[docs/writing-plugins.md](../writing-plugins.md); this is what bites the
*host*.

Part of the working notes split out of [CLAUDE.md](../../CLAUDE.md) —
see [docs/notes/README.md](README.md) for the whole set.

---

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
  import()ed by `showPluginTab` in app.js with a `?v=<gen>` cache-buster
  — `gen` is the registry load sequence, bumped on every reload, so a
  toggle-off/on picks up changed JS. One mount per tab, kept across tab
  switches, torn down on case switch (`resetPluginTabMounts`) and gen
  change; the module's default export gets `(container, winnow)` where
  `winnow` is `buildPluginTabContext`'s stable surface (api/post/el/
  modal helpers, `sql()` → run_sql's own RO connection, `schemaText()`,
  live state getters); optional onShow/onHide exports fire per switch.
  `register_api` registers backend routes dispatched at request time by
  the one catch-all `/api/plugin/{fs}/{route}` handler — deliberately
  not real FastAPI routes, so a Settings toggle's registry reload is
  instantly authoritative with no stale route objects. Handlers get a
  plain `PluginRequest` (method/route/query/body/store — None when no
  case is open) and return JSON-ables; ValueError → 400, same split as
  api_view; the CSRF middleware already covers non-GET. Plugin backends
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
  precedence in app.js (`pluginFormatFor`): a built-in extension always
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
