# Working notes

The long-form "why is it like this" notes that used to live in one
`## Things that bite` section of [CLAUDE.md](../../CLAUDE.md). Same text,
same entries — split by subsystem so a session working on one part of the
app reads that part's traps instead of all 1,300 lines of them.

CLAUDE.md still carries what applies everywhere: the layout, the seven
invariants, testing and the backlog. **Read the note for whatever you're
about to touch before you touch it** — most of these entries exist because
something plausible was tried and measured, and the measurement is the
part you can't re-derive from the code.

| note | read it before touching |
| --- | --- |
| [store.md](store.md) | `store.py` — view building/paging, FTS and column indexes, grouping, tag counts, search-all, the reader pool, cancellation, compact |
| [ingest.md](ingest.md) | any ingest path (CSV/JSON/SQLite/folder/drag-drop) or the background job machinery |
| [grid.md](grid.md) | the virtualized grid in `static/app.js` — rendering, the page cache, selection, scroll geometry |
| [ui.md](ui.md) | the rest of the frontend — menus, filters and the value picker, saved filters, timeframe, tabs/sidebar, Settings, keybindings |
| [server.md](server.md) | `server.py` — routes, middleware, case open/shutdown, the 400-vs-500 split |
| [derived.md](derived.md) | derived datetime columns (`timeparse.py`, `drv_<id>`, `/api/derived/*`) |
| [plugins.md](plugins.md) | `plugin_api.py`, the plugin host, or the example plugins |

## Adding to these

New entry goes in the file for the subsystem it bites, not here and not
back in CLAUDE.md. Keep the shape the existing ones have: what the code
does, **why** (usually a measurement, a failure that actually happened, or
a spec detail that isn't obvious), and what would break if someone
"simplified" it back.

Two sessions working on different subsystems then don't collide in the
same file, which is the other reason this was split.
