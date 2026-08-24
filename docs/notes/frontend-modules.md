# The frontend's module layout

`static/js/` is one ES module per subsystem, loaded by
`<script type="module" src="/static/js/main.js">`. It was one 11,194-line
`static/app.js` until 2026-08-21.

Part of the working notes — see [docs/notes/README.md](README.md) for the set.

---

## Why it was split

Two costs, both measured rather than felt:

- **Every session read the whole file.** A change to the value picker loaded
  the timeline, the plugin host and the SQL pane along with it.
- **Concurrent sessions collided in it.** Three PRs opened within 25 minutes
  of each other all edited `app.js`, along with the same five other files;
  the branch history has four "merge main into branch" commits from working
  around that.

No build step was added and none is needed: `type="module"` is native, works
offline, and the plugin host already `import()`s plugin modules at runtime.

## The rules that make it safe

These are not style preferences. Each one is a bug that happened during the
split, found by loading the app:

1. **Modules hold declarations only.** Every top-level side effect from the
   old file lives in `main.js` — the per-module `wire*()` calls that attach
   DOM handlers, then the startup sequence. Nothing runs at module-evaluation
   time, so the import cycles between modules (there are many, and that's
   fine) can't produce a temporal-dead-zone error.
2. **`core.js` imports nothing.** It holds `$`, `el`, the constants, `api`/
   `post`, the toast/busy chrome and `debounce`. Three modules initialise a
   top-level `const x = debounce(...)`, which *does* run at evaluation time —
   so `debounce` has to be reachable before any other module's body runs, and
   the only way to guarantee that is for its module to be a leaf. When
   `debounce` lived in `view.js`, the app died on load with "Cannot access
   'debounce' before initialization"; when `el` was one line the wrong side of
   a module boundary, `core.js` stopped being a leaf and it died the same way.
   **If you add anything to `core.js` that imports from another module, you
   have reintroduced that crash.**
3. **A module can't assign to an imported binding.** Modules are strict, and
   imports are read-only. The two variables the old single scope reassigned
   across what are now module lines got explicit setters next to their
   declaration: `setRowH` in core.js (the density setting rebinds `ROW_H`)
   and `setSearchAllRepaint` in search.js (`modal()` clears the hook from
   ui.js). Reads stay live bindings, so importers see the new value.
4. **Everything is exported.** Every top-level name carries `export`, which
   is the honest translation of the single global scope it came from — and
   the import lists are then a real record of what actually crosses a
   boundary. `tests/test_static_syntax.py` proves each module imports
   everything it uses, so a function moved between modules without its
   import fails the suite instead of the analyst's next click.

## Where things live

| module | holds |
| --- | --- |
| `core` | `$`, `el`, constants, `api`/`post`, toast/busy, `debounce`. Imports nothing. |
| `state` | `S` and the `sel*` row-selection helpers |
| `view` | building/rebuilding the materialized view |
| `grid` | paging, prefetch, painting, cell-range selection |
| `columns` `tsformat` `derived` | column layout/widths; timestamp parsing and display formats; derived and extracted columns |
| `filters` `filterbuilder` `savedfilters` `timeframe` | quick filters and the value picker; the guided tree; saved filters and nicknames; timeframe and jump-to-timestamp |
| `grouping` `tags` `rowmenu` `detail` | group-by; tagging and undo; the row right-click menu; the detail pane |
| `sources` `importer` `merge` `tables` | sources/tabs/sidebar; every import path; the merge builder; the Tables manager |
| `search` `sql` `timeline` `session` `plugins` | toolbar + search-all; the SQL pane; the timeline tab; session files; the plugin host frontend |
| `ui` `keymap` `settings` `home` | modal/dialog/menu primitives; keybindings; the Settings modal; the home screen and `boot()` |
| `main` | imports everything, calls each `wire*()`, runs the startup sequence, exposes `window.__winnow` |

## `window.__winnow`

A flat, live view of every module's exports, built with getters (not a
spread, which would freeze a rebindable export like `ROW_H` at boot).
Collision-free by construction — these names all shared one scope until the
split. It exists for the browser console and for `tests/ui/`, which reaches
into app state through `page.evaluate` and has no other way in now that the
globals are gone. **Nothing in the app reads it**; it is not an API, and a
plugin should use the context object `buildPluginTabContext` hands it.

## Moving code between modules

Move the declaration, then run `pytest tests/test_static_syntax.py` — it will
name any import you didn't add. Then check the module you moved *from* still
compiles without it, and that you haven't given `core.js` an import (rule 2).
The browser tests in `tests/ui/` are the backstop, but they exercise a
fraction of the surface; the static check covers all of it.
