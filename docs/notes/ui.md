# UI surfaces: menus, filters, settings, tabs, keybindings

Everything in `static/js/` and `static/style.css` that isn't the grid
itself: the right-click menus, the filter bar (and the classic filter row
behind a setting) with its value picker, saved filters, the timeframe
filter, tab strips and the sidebar, Settings, and the keymap.

Part of the working notes split out of [CLAUDE.md](../../CLAUDE.md) —
see [docs/notes/README.md](README.md) for the whole set.

---

- **Shift+<tag key> asks before it decides.** The whole-view tag
  hotkey reads `/api/tag_view_coverage` first: anything in the view still
  untagged means tag the lot, everything already tagged means take it
  off. The direction is settled by the WHOLE view, never by a sample row
  — the unshifted key is the one that toggles from the row under the
  cursor, and mixing the two rules would make a keystroke's meaning
  depend on where the cursor happened to be. The coverage is asked fresh
  rather than read from `S.tagCounts`, which is refreshed
  fire-and-forget and is a ribbon's number, not a number to write from.
  The confirm names the direction in its own button ("Tag them" /
  "Remove the tag"), so a test that clicks it should find it by role
  (`.confirm-actions .btn:not(.ghost)`), not by the label.

- **A background refresh must not navigate.** `loadSources()` re-opens
  the current source as a side effect of picking a tab, and `openSource()`
  switches to the grid and resets that source's filters and search. That
  is right for a real navigation and wrong for "an import finished": with
  a batch of files, the jobs poll called it once per completion, so the
  analyst was dragged back to whichever table the first file opened —
  every few seconds, losing the filter they had just set, and tearing down
  and refetching the grid each time (the "it locks up" in the report).
  `loadSources(select, { navigate: false })` refreshes the tab strip,
  sidebar and dashboards without moving anyone; it still opens a table
  when nothing is on screen, which is what makes the FIRST import land.
  Any new background caller wants that option — `refreshSourcesQuietly()`
  in tables.js exists for the same reason on the polling path.

- **Theming rule for controls.** `<select>` has a global themed base rule in
  `static/style.css` (dark `--ink`/`--line-2`, accent focus), and each theme
  sets `color-scheme` on `<html>` so the parts CSS can't reach — the native
  option popup, date pickers, the scrollbar fallback — follow the theme
  instead of flashing a white OS menu on a dark app. **Any new dropdown/menu
  inherits this by default; never ship a bare `el('select')` that looks
  unthemed** (the widget editor did before this). Contextual rules
  (`.fb-cond select`, `.wl-add select`, …) still win where they set more.

- **Bar buttons never wrap their label.** `.toolbar .btn`, `.sql-head .btn`,
  `.detail-head .btn`, `.dash-bar .btn` and `.wl-head .btn` are
  `white-space: nowrap; flex: 0 0 auto`. Under width pressure (tag counts,
  the open search box, a 1280px laptop, the wider Phosphor/Blueprint faces)
  the things that give way are, in order, the group strip (`flex-shrink: 6`,
  its hint ellipsizes, 150px floor keeps the label and `+ Tag`) and then the
  tag ribbon, whose chips wrap onto a second line — the designed behaviour.
  Before this every flex item shared the squeeze evenly and each button
  folded into a two-line pill. Two more rules since a walk through the app
  at 1024px: **the ribbon's floor is `min-content`, not 0** (at 0 the
  chips spilled out of the ribbon's box and under the row count, and each
  chip — a `.tag-chip`, not a `.btn` — broke its own label, so they are
  `nowrap` too), and **the toolbar itself `flex-wrap`s**, so once even
  that isn't enough (the search box open on a laptop) the stats and
  buttons move to a second line as a whole instead of the ribbon folding
  into a one-chip column. The header bar has the same shape:
  `.bar-actions` never shrinks and its buttons never wrap, `#sourceTabs`
  keeps `SOURCE_TABS_MIN` (140px) and it is `#pageTabs` — which scrolls —
  that shrinks (`flex: 0 1 auto`, 60px floor). A pressed segment (`.vp-seg`, `.segmented`)
  is the filled accent with `--accent-fg` text; accent text on the dim
  accent fill measured 1.4–2.7:1 across the skins, and accent-on-panel
  fails in Phosphor light.

- The **timeframe filter** (`S.timeRange`, `static/js/timeframe.js`, `time_range` on
  `ViewSpec`, compiled in `_compile_where` via the registered SQL function
  `TS_NORMALIZE`) is deliberately a separate piece of state from every
  other filter mechanism, and every place that resets "the filters" —
  `clearAllFilters()`, `applyPreset()`, opening a different source — is
  written to skip it on purpose (see the comments at each site). It's
  meant to stay pinned while everything else changes underneath it, and
  toggles on/off via its own keybind (`toggleTimeRange`) rather than
  needing its config modal reopened. `column: null` means "every datetime
  column on whichever table is open, OR'd together" — the MFT case this
  exists for: a timestomped Created date shouldn't hide a row whose
  Modified date is genuinely in range. `TS_NORMALIZE(x)` (a zero-padded
  `"YYYY-MM-DD HH:MM:SS"`, same ISO/US shapes as `DAY_BUCKET`/
  `parseTimestamp`) is what both the column values and the start/end
  bounds get compared through — a bare text/numeric comparison on the raw
  stored value sorts the US `M/D/YYYY` shape wrong.
- **The histogram strip re-asks; it does not wait to be told.**
  `/api/histogram` names a view id, and a view is evicted the moment the
  next rebuild lands — which is what a burst of filter changes is. The
  409 that comes back was swallowed on the reasoning that "the rebuild's
  own view change refetches", and it does not: that change fired before
  the 409 came back. One lost answer and the chart went on describing the
  previous filter until something else rebuilt the view. It now compares
  the view the answer was about with the view the grid has and asks again
  when they differ, bounded so that a view that is current and keeps
  failing is re-asked a few times rather than polled.

- **The histogram strip** (`static/js/histogram.js`, `GET /api/histogram`
  over `Store.time_histogram`, toggled by `#btnHistogram` or `h`) was the
  `table_histogram` example plugin until 2026-09 and is built in now;
  three things from its life as a plugin are worth knowing before touching
  it. **A 409 from the route means mid-rebuild, not an error**: the strip
  fetches 150 ms after `winnow:viewchange`, and a second rebuild in that
  window evicts the view it asked about — so on a 409 it keeps what is
  drawn and lets that rebuild's own view change refetch. Only an
  'expired' KeyError is a 409; an unknown column (a derived column just
  removed) or a non-datetime one is a 400 the strip shows as text in
  `.th-empty`, because waiting for a view change would never fix it. It
  listens only while it is on screen: open but hidden behind a page tab,
  a view change is left for the show edge in `syncHistogramPanel` to
  refetch (keying on the pref alone aggregated a view rebuilt behind the
  SQL tab twice), and the first ask after opening measures the section
  rather than the canvas, which "Loading…" has hidden — measuring the
  canvas fell through to a 600px fallback and an 85-bar first chart.
  **The drag snaps to a unit chosen from the drag, not from the bar
  width**: rounding outwards to the current bucket made any drag inside
  one 6h bar that whole bar, so the view never narrowed enough for the
  server to re-bucket and "zoom in" did nothing; the unit is fine enough
  that the rounding adds ≤ ~8 % per end, never finer than a second, never
  coarser than the bar (`snapUnit`). And **the canvas redraws on
  `winnow:appearance`** — a canvas does not inherit CSS, so the tokens are
  read at draw time and a skin/accent change is one redraw, or the bars
  keep the old colour until the next view change. Two host rules: the
  strip shares `#pluginPanels` with plugin toolbar panels, and
  `syncPluginPanels()` (plugins.js) stays the only writer of the host's
  `hidden` — it asks `histogramOpen()`, which is what keeps a plugin
  toggled off from hiding an open histogram; and `#btnHistogram` sits
  AFTER `#pluginToolbarButtons`, never inside it, because
  `renderPluginPanelButtons` wipes that span on every plugin reload (boot
  and every case switch). Prefs are `winnow.histogram` `{open, column}`
  per browser; the plugin's `winnow.panels['table-histogram.histogram']`
  is migrated to `open` once and deleted.
- There's no separate "preset" concept anymore — a preset is just a saved
  filter (`workspace.SavedFilters`, cross-case) whose `col_names` happens to
  match (exactly, or "similar" per the same Jaccard/subset heuristic the old
  case-scoped `filter_presets` table used) the table just opened. The banner
  (`checkPresets`/`matchingSavedFilters` in `static/js/savedfilters.js`) computes this entirely
  client-side against the already-loaded `S.savedFilters` — no request. A
  case file saved before this change may still have rows in the old
  `filter_presets` SQLite table; `Store.pop_legacy_presets()` reads and
  clears it once on open, and server.py folds whatever it finds into
  `WS.filters`. Nothing writes to `filter_presets` anymore — it stays in the
  schema purely as a one-way migration source for old case files.
- A header-set **nickname** (`workspace.HeaderNicknames`, `header_nicknames.json`)
  is a separate tiny store from `SavedFilters`, not a field on it — several
  saved filters commonly share one header set (e.g. five different EVTX
  filters), and should all pick up the same nickname rather than needing it
  set per-filter. Keyed the same way as `ColumnLayouts` (sorted, lowercased
  column names) — saving again for the same set overwrites in place.
- **The group header's own menu** (`groupMenuItems`) tags or untags every
  row in a group in one `/api/row_tags/view` call against a view id from
  `groupRowsView` — the group's own expanded sub-view when it has one,
  otherwise a throwaway `expand_group` that gets DELETEd afterwards. So it
  works on a collapsed group and on an outer nesting level, where the client
  has never seen a single one of the rows. Untagging is a mode flip on the
  same menu rather than a ✓ toggle: a group is a set of rows with mixed
  tags, so there's no single row to read a checkmark off the way
  `rowMenuTagList` does. The throwaway view is safe to drop immediately
  because undo records the *rows* (invariant #7's `v.undo_<n>` delta table),
  not the view they were found through. Tagging while grouped *by tag* —
  undoing, the SQL pane's tag hotkey and the tag editor's Delete included
  — calls `regroupIfGroupedByTag()`: the tag just changed which
  group those rows belong to, and the expanded sub-views are server-side
  with nothing here to patch them with. Either way it ends with
  `clearRowCaches()`, not `clearGroupPageCache()` alone: the flat page
  cache is still alive under the grouping and would otherwise paint the
  pre-tag rows back on Ungroup (grid.md, "Grouped mode's rows are ordinary
  rows").
- **The sidebar tree's indent guides are a background-image, so a
  highlight must set background-COLOR.** Rows in the tree are a flat list
  — `renderSidebar` appends folder headers and table rows to one
  container, which is what lets a filter force-expand without rebuilding
  the nesting — so there is no nested element to hang a left border on.
  Each row paints one vertical rule per level it sits inside, clipped to
  its own indent by `background-size`. `background: var(--ink)` on
  `.drop-into` or `.active` drops the guides exactly when the row is
  being dragged onto or is the current one; `background-color` doesn't.
  `tests/ui/test_sidebar_tree.py` pins it.

- **The sidebar** (`renderSidebar`, replacing the old `openTabJumpMenu`
  dropdown) is a *persistent* list of every table, open or closed — the
  horizontal tab strip (`.tabs`/`renderTabs`) is untouched and still the
  primary way to switch between what's currently open; the sidebar exists
  for the same reason the dropdown used to (reaching a table that isn't
  open, or is scrolled out of the strip's view), just without having to
  reopen a menu for every click — the case that actually forced this: a
  directory import can open 30+ tabs in one pass (every ingest auto-opens
  its tab), and a dropdown you reopen per click doesn't scale to that.
  `#app`'s CSS grid grew a column rather than a wrapper div — `#sidebar` is
  `grid-column: 1; grid-row: 1 / -1`, the four rows that used to be `#app`'s
  only direct children (`.bar`/`.toolbar`/`#presetBanner`/`.main-area`) all
  moved to `grid-column: 2` — so hiding it (`[hidden]`) collapses that
  column to zero width for free, nothing else occupies it. `renderSidebar`
  is called from inside `renderTabs()` itself (both of `renderTabs`'s
  callers — `loadSources` and the tab strip's own drag-drop handler —
  mean `S.sources`/`S.tabOrder` just changed), not from a
  parallel set of call sites that could drift out of sync. The table list
  has two parts. An **Open** section at the top is the working set — the
  tables with a tab open, in `S.tabOrder`, reorderable by ▲/▼ or drag
  (`openSidebarRow`, which still uses `wireDragReorder`/`moveTab`); drag a
  table from the tree onto it (`wireOpenDrop`) to open one. Below it, **All
  tables** is a **folder tree**: every table sits at the root or inside a
  folder (`source_folders`/`source_folder_map` in the case file — see
  store.py), open or not — so an open table appears in *both* (the Open
  section is your tabs, the tree is the whole library). This replaced a
  first cut that had folders *replace* the old Open/Closed split entirely;
  the working-set section came back because reordering and closing tabs
  from the sidebar is worth keeping. Folders are created/renamed/
  reordered/deleted from their header rows (`.sidebar-folder`, a class
  distinct from `.sidebar-row` — so its actions need their own
  `:hover .sidebar-row-actions` rule, which is easy to forget) and from the
  header's ＋; a table is filed by dragging its row onto a folder (or the
  row's "Move to a folder" button) and dragged back out via the root drop
  zone. A directory import reproduces the on-disk tree here (importer.js
  sends the file's subfolder as `folder_path`, the ingest job creates the
  folders via `ensure_folder_path`). Folder membership is keyed by the
  *signed* source id, so a merge folds like any table. Folder collapse
  state is per-browser `localStorage` (`FOLDERS_KEY`), but the folders
  themselves are case data that travels with the `.db`. Page tabs still get
  their own Pages section below the tree — same rows, same drag/▲/▼
  reorder against the other strip; see the two-strips entry below. Its
  active-row highlight isn't simply `s.id === S.sourceId`:
  `S.sourceId` is never cleared while a page tab is showing (there's no
  single "a source is open" flag to unset), so `sidebarRow` also requires
  `S.activeTab === 'grid'` — the same condition `syncTabSelection` applies
  to the strip itself, which is why both now live in that one function
  rather than as a block repeated in every `show*Tab`. Collapse state persists in
  `localStorage` (`winnow.sidebar`) like `winnow.keymap`/`winnow.appearance` — a
  per-browser UI preference, not `workspace/` state. `dropdownMenu` lost
  its `actions`/`forceReopen` support in the same change — `openTabJumpMenu`
  was their only caller, and dead generic capability isn't worth carrying;
  its rows' `.menu-item`/`.menu-item-action` classes live on, reused as-is
  by the sidebar's own rows. Drag-to-reorder (`wireDragReorder`, factored
  out of what used to be `wireTabDrag` alone) is shared by the horizontal
  strip and the SQL pane's sub-tab strip — same native-HTML5-DnD technique
  and `S.tabOrder`/`S.sqlTabs`. The sidebar's table rows once used it too,
  but they drag differently now: a sidebar drag *files a table into a
  folder* (`wireTableDrag` + `wireFolderDrop`, its own `draggedTableId`),
  not reorders the strip.
- **Editing a saved filter** goes through the real grid, not a
  self-contained dialog: the Saved filters modal's "Edit" applies that
  filter (`applyPreset`) and *then* opens `openFilterBuilder(f)` with the
  record, so the row count behind the modal is live feedback on the change
  being made. The only thing the `editing` argument adds is an `Update
  "<name>"` button; everything else, including "Save as new…", is the
  normal builder. That button deliberately sends **only `payload`** —
  never `col_names`. A filter's header set is its identity for `[` / `]`
  cycle order and the suggested-filter banner (see the saved-filters
  entries above), so re-binding it to whatever table happened to be open
  during an edit would silently move it out of the group it was saved
  for; "Save as new…" is the rebind path. `workspace.SavedFilters.update`
  replaced the old name-only `rename` with the same
  None-means-leave-alone partial-update convention `CaseRegistry.update`
  already used, so one method serves both a rename and a conditions
  re-save. Edit needs a table open (it applies the filter to preview it),
  hence the disabled button and its explanatory title when none is.
- **Two tab strips share the header bar**: `#sourceTabs` (tables, ordered
  by `S.tabOrder`) and `#pageTabs` (SQL, Timeline and plugin tabs, ordered
  by `S.pageTabPrefs.order`), split by the `#tabSplit` divider. Page tabs
  were three loose `.tab-sql` buttons sitting directly in `.bar` before —
  fixed order, no scrolling of their own, and free to squeeze the table
  strip to nothing once a couple of plugin tabs existed. Now:
  - A page tab is identified by a **string key** — `'sql'`, `'timeline'`,
    `'plugin:<id>'` — where a table tab is a numeric source id. That's
    what lets the one shared `wireDragReorder` (and its one shared
    `draggedTabId`) span both strips safely: a tab dragged from one strip
    to the other resolves to no index in the target's own `currentIds()`
    and the drop no-ops — the same guard the SQL sub-tabs already relied
    on. `S.activeTab` holds that key verbatim (or `'grid'`), which is what
    makes `syncTabSelection` one comparison per node rather than a branch
    per tab. It is now the only thing that writes `aria-selected` on
    either strip, and it ends with the sidebar re-render for the same
    reason `renderTabs()` does — every caller has just changed what's
    active.
  - A strip with tabs scrolled out of view **fades that edge**
    (`data-overflow="left|right|left right"`, a `mask-image` in the
    stylesheet), set by `syncTabOverflow()` from the strip's own scroll
    geometry — after every `renderTabs`/`applyPageTabsSize` (through
    `requestAnimationFrame`, since there is no geometry before layout), on
    each strip's `scroll`, and on window resize. Without it a second table
    tab cut mid-word beside a row of plugin tabs read as a broken tab, not
    as "more this way".
  - `renderPageTabs` **moves** `#tabSql`/`#tabTimeline` into place rather
    than rebuilding them (a dozen places reach them by id) and builds the
    plugin ones. Each node is drag-wired exactly once
    (`dataset.dragWired`): the two reused ones would otherwise accumulate
    a listener set per render, and one drop would then apply the same
    reorder once per set.
  - **The pages are compact, and each badge sits on the page it counts.**
    A walk with eight tables open found the table strip overflowing
    (1,212px of tabs in 1,171px) and the page strip collapsed to one 83px
    button reading "SQL" with a "99+" badge on it — where the 99+ counted
    *watchlist* hits. Two things follow from that. The page tabs render in
    the SQL sub-tab register (`.page-tabs .tab-sql`: mono, 11px, tight
    padding, no rules between them), which took the four of them from
    300px to 238px and handed the difference to the tables; a glyph per
    page was tried and dropped, since a glyph plus its gap costs more than
    the padding saves. And `pagesMenu` ("Pages as a dropdown") is OFF by
    default now — four labelled pages that are always there beat one
    button that is not, and the preference stays for a case carrying
    enough plugin or pinned-dashboard tabs that the strip really is the
    problem it was added for. **In the collapsed mode the badge does not
    follow the button**, which is labelled with whichever page is up and
    therefore owns none of these numbers: `paintWatchlistBadge` gives it
    an ownerless dot (the sentence goes in the button's own `title`, since
    a dot has nothing to announce) and the count itself rides the Watchlist
    row inside the menu, via `menuItemNode`'s `badge` slot. The strip is
    still what scrolls when a case has too many tables for it — this buys
    room, it does not remove the limit. tests/ui/test_page_tabs_badges.py.
  - Order and divider position persist in `localStorage`
    (`winnow.pagetabs`), unlike `S.tabOrder`, which is in-memory and
    resets per case. "SQL" means the same thing in every case, and a
    plugin tab belongs to this machine's `plugins/` rather than to any one
    case file — neither has a reason to jump back on a case switch.
  - **`Alt`+`1`–`0`** (`activateTabSlot`) addresses both strips as one key
    row: 1 is the last-selected table, 2…0 the page tabs *in strip order*,
    so the digits follow a reorder. Slot 1 calls `showGridTab()` rather
    than `openSource()` when the target is already `S.sourceId` —
    re-opening resets that table's filters/sort/search, which is not what
    "back to where I was" means. It's handled before `matchAction` and the
    tag hotkeys in the keydown listener because neither of those checks
    modifiers (`'0'` is `resetColumnWidths`, `1`–`9` are tag hotkeys), and
    it reads `e.code` rather than `e.key` since `Alt`+digit isn't a digit
    in `e.key` on every layout. `Shift`+digit — the obvious row — was
    already taken by apply-tag-to-view. Sitting above that pair also puts
    it outside their `S.activeTab === 'grid'` gate, which is correct and
    not incidental: switching tabs is the one thing that has to work
    *from* a non-grid tab, the same carve-out `TAB_AGNOSTIC_ACTIONS`
    makes for Settings/Tables/Search-all. It's above the `typing` guard
    for the same reason — the SQL pane focuses its editor on arrival, so a
    shortcut that stopped at that guard could get you into that tab and
    never back out — but below a check for an open dialog (`#modal` *or* a
    spawned `.confirm-overlay`; `_spawnDialog` builds its own, so one
    check doesn't cover the other).
  - The divider stores **the width the analyst dragged to** and applies
    that width *clamped* to what the bar can currently give it
    (`clampPageTabsWidth`); the clamped value is never written back, so a
    narrower window squeezes the strip without forgetting the setting.
    Below the width where both strips' minimums fit, the space is halved
    rather than honouring either — starving the table strip to hold a
    60px page strip is the worse failure, and both strips scroll. The
    clamp is deliberately a no-op while `#app` is `[hidden]`: every rect
    is 0 before a case is open, which would otherwise pin the strip at 0px
    for the whole session, since only `showApp()` and the window `resize`
    handler re-run it.
- **`syncTabChrome` is the registry of grid-only chrome.** The toolbar,
  a plugin's toolbar panels, the session-comparison banner, the "N
  selected" tag bar and the row detail pane all describe one table's
  grid, and every writer of `S.activeTab` — `showGridTab`/`showSqlTab`/
  `showTimelineTab`, `showNotesTab`, `showWatchlistTab`, `showDashboard`
  and `showPluginTab` — ends in `syncTabChrome()`, which is what makes it
  the one place. A new grid-only surface hides itself there, **not in
  `showMainView`**: `showPluginTab` swaps views with `hideMainViews`/
  `hidePluginViews` directly and never passes through `showMainView`, so a
  hide put there works for the built-in pages and leaves the surface
  standing beside every plugin tab. That is how the detail pane was left
  open next to the SQL editor — `#detail` and `#detailResize` are siblings
  of `.main-content`, not children of the grid, so the view swap never
  touched them, and with `d` and Escape gated to the grid the pane's own
  Close button was the only way out. Two rules for the pane's entry: it is
  a **one-way hide** (`if (!isGrid) hideDetailPane()`; the toolbar's
  `hidden = !isGrid` idiom would force the pane open, possibly on no row,
  every time the grid comes back — closed-and-forgotten is what an analyst
  expects, and `d` or a double-click reopens it), and hiding **never clears
  `#noteInput.dataset.rid/sourceId`**: `saveNote` is a 500 ms debounce that
  reads them when it fires, so a note typed just before a page or table
  switch still posts against the row it was typed for. A case open is the
  one exception, and it is home.js's to make, not the hide's: `/api/note`
  writes into whichever store is current, so a save that fired after the
  `/api/case/open` swap would attach the previous case's `{source_id, rid}`
  to an unrelated row of the new one — `openCase` blanks the rid before
  the POST (and puts it back if the open fails). `openSource` hides the
  pane only when it is actually **leaving** a table (`leaving = S.sourceId
  !== id`, decided before `S.sourceId` is overwritten): every refresh idiom
  — `loadSources()` with no select after a column add, a sidebar folder
  op or closing some *other* tab, `openSource(S.sourceId)` from the
  derived-column modal, the plugin API's `refreshSources()` — re-enters
  `openSource` for the table already open, and an unconditional hide there
  took the pane away under the analyst on all of them. `loadSources`'
  empty-state branch (last tab closed, the table on screen removed) and
  the case-open reset in home.js hide it too. Nothing reopens it in the
  background: `ensurePage` and `maybeShowDetail` are gated on the pane
  already being visible.
- **The SQL pane has named sub-tabs** (`sql_tabs`, a per-case sidecar
  table; `list/create/update/delete/reorder_sql_tabs`, `/api/sql_tabs`,
  `renderSqlTabs` and friends in `static/js/sql.js`). Stored in the **case file**, not
  `localStorage` like `winnow.sidebar` and not `workspace/` like a saved
  filter: a worked-out query is analysis *about this evidence* ("the join
  that pulls 4624s against the RDP source"), so it should travel with the
  case when it's handed to another analyst — and it's still only SELECTs
  the analyst typed, so invariant #1 holds (no source table is touched).
  The editor holds one tab's text at a time; every action that changes
  *which* tab that is `await`s `flushSqlTabSave()` first, so the debounced
  autosave can't lose an edit because you clicked away inside its window
  (and it captures the tab id it read the text for, so a late PUT can't
  land on the wrong tab). `savedSql` mirrors what the server holds so that
  flush is a no-op when nothing changed — it fires on every tab switch,
  not just after an edit. Result sets live in `S.sqlResults` keyed by tab
  id, **in memory only**: they're re-derivable by pressing Run, can be
  large, and are a snapshot of the data rather than the analysis.
  `runSql` captures `S.sqlTabId` up front and only paints if that tab is
  still showing, since you can switch tabs while a query is in flight.
  `wireDragReorder` grew optional `currentIds`/`onReorder` callbacks
  (defaulting to the source-tab behaviour) so the sub-tab strip reuses the
  one DnD implementation; its `drop` handler now *returns* when the
  dragged id isn't in the target surface's own id list, which is what
  stops a SQL sub-tab dropped on the source strip from being spliced into
  `S.tabOrder`.
- **The SQL pane opens in the analyst's vocabulary, not the schema's.**
  Three things in `static/js/sql.js` exist only for that, and all three
  are easy to undo by accident:
  - `starterSql()` puts `-- Security.csv (src_1)` on the first line. The
    seeded query used to be a bare `SELECT * FROM src_1 LIMIT 50;`, and
    `src_1` is a name that appears nowhere else in the product — the tab
    strip, the sidebar, the Tables manager and the dashboards all say
    `Security.csv`. A leading comment is the one place to say which file
    that is that survives the query being edited into something real;
    `run_sql` and `sql_to_table` both tolerate it (the latter wraps the
    query in `SELECT COUNT(*) FROM (...)`, which is why the comment ends
    in a newline rather than being appended). **Everything in
    `sqlassist.js` that reads a query with a regex reads
    `sqlStructural()` first** — comments and string literals blanked,
    lengths preserved, double-quoted names left alone because `FROM
    "src_2"` is a real reference. The comment says `src_1`, so a query
    edited to read another table with that line kept — the flow the
    comment exists for — scanned as two tables, and `sqlRowRef` answered
    nothing: the result silently lost its live Tags column, row
    selection, the tag hotkeys, Ctrl+C on a selection and
    double-click-into-the-table, while the autocomplete offered both
    tables' columns. It is the same pass `run_sql` makes server-side
    (`_strip_sql_comments` + `_blank_string_literals`) before its own
    scan.
  - `sqlStarters()` builds two or three clickable queries **from this
    case's own sources**, never from a canned example — one that names a
    table the analyst doesn't have errors on the first click. Each is
    offered only when it would return rows (`tagged_row_count`, a second
    real table), and each is shaped for a measured reason: the tagged-rows
    query drives off `row_tags` by `rid` rather than `WHERE Tags IS NOT
    NULL` (which can use no index — 32ms scanning 200k rows to find five,
    against 0.4ms for the subquery form), the per-table counts read the
    `src_N` views rather than `main.src_N` because SQLite flattens the
    view and never evaluates the Tags/Note correlated subselects for a
    `COUNT`, and a merge gets `(source_id, rid) IN (…)` because its rids
    are only unique per member (invariant #9). Table labels go in as
    string literals through `sqlLiteral`, so `O'Brien.csv` is not a syntax
    error.
  - `openStarter()` fills the current tab only when its text is blank or
    is itself a starter, and opens a **named sub-tab** otherwise. The
    editor autosaves into the case file, so clobbering a half-written
    query is a real deletion — but always opening a tab would leave an
    abandoned "Query 1" behind in every case.
  The header above the editor is a sentence in mixed case with the
  identifiers in `<code>`; `.sql-head` overrides the `text-transform:
  uppercase` it shares with `.detail-head`, which is a label ("ROW
  DETAIL") and stays shouted. Blueprint re-uppercases its headers and
  paints them on a solid accent bar, so it needs its own override *and* a
  `code` colour — the global `code` rule is accent-on-panel, which on that
  bar is the bar's own colour. `tests/ui/test_sql_pane_welcome.py` pins
  all of it.
- **The 2026-08 navigation batch** — five smaller features, and the traps
  each one carries:
  - **"Open filter in SQL pane"** (`Store.spec_sql`, `POST /api/view/sql`,
    keybind `Q`) renders the live spec through the *same*
    `_compile_where`/`_compile_order` the view build uses — never a
    parallel SQL generator that could drift — then inlines bound params as
    literals via `_inline_sql_params`, which walks string literals with
    SQLite's own ''-doubling rule so a `?` *inside* an analyst's raw
    filter fragment is not mistaken for a placeholder. `run_sql`'s
    connection now registers TS_NORMALIZE/DAY_BUCKET alongside REGEXP —
    a compiled timeframe filter contains them, and the pane erroring on
    its own generated SQL was the bug that surfaced this.
  - **Grouping travels with saved filters**: `currentFilterPayload()` adds
    `group_by`/`group_sort`/`group_sort_dir` *only when a grouping is
    active*, so filters saved without one keep byte-identical payloads —
    that's what keeps `activeSavedFilterRecord()`'s JSON-stringify
    matching honest, and it gives apply-time the same leniency `sort:
    p.sort || S.sort` has (a payload without the key leaves the current
    grouping alone; setGrouping() replaces wholesale, restoring
    `S.preGroupOrder` first so a formerly-grouped column doesn't leak out
    of the visible layout). `clearAllFilters()` now drops grouping too —
    stashed in `S.lastGroupBy` first, which is also what the `X`
    toggleGrouping keybind restores (the deliberate contrast with lowercase
    `x` dropGrouping, which just drops).
  - **Jump to timestamp** (`Store.find_nearest_timestamp`,
    `POST /api/view/find_ts`, keybinds `J`/`.`) measures closeness by
    `ABS(julianday(TS_NORMALIZE(col)) - julianday(target))` — string order
    can rank timestamps but can't measure *between* them — which makes it
    a scan of the view; that's the same cost shape as group_summary's
    aggregate and it runs on a pooled reader. Returns a pos each view kind
    computes its own way (root_virtual: rid-1; materialized: vv.pos-1;
    group_virtual: COUNT of group rows with a smaller rid, matching that
    path's rid-order paging). `S.jumpTs` deliberately survives
    `openSource()` — the workflow is "show me 13:22:01 in *each* table".
  - **Timeframe-from-tags** (`Store.tag_time_bounds`,
    `POST /api/tag_time_bounds`, the timeframe modal's "Fill range")
    returns TS_NORMALIZE'd bounds — the exact shape the timeframe filter
    compares through — over any-tag or a tag subset, honoring the modal's
    column choice with the same all-datetime-columns fallback the filter
    itself has, so the filled range always covers the rows it came from.
  - **Saved-filter reordering** predates this batch (▲/▼ +
    `SavedFilters.reorder`); the addition is drag-to-reorder on the modal
    rows via the one shared `wireDragReorder`, scoped by
    `currentIds: sameGroupFilterIds(...)` so a drag across header sets is
    a structural no-op rather than a rule someone has to remember.
- **Session comparison** (session.js) is counts in the panel and rows in
  the grid, never rows in the panel. The first cut listed the differing
  rows inside the modal, three columns of tag names per row: it read as
  cramped at twenty rows and useless at two thousand, and the rows were
  a copy of what the grid already shows. So `renderDiff` draws one line
  per table with a count per kind (only-left, only-right, tagged
  differently, notes) from the server's uncapped per-table tally
  (`sources`), and each count calls `pivotDiff`, which sets
  `S.diffMarks` (`{sourceId, left, right, rows: {rid: {tags, note}},
  what, n, prevTree}`), opens the table if it isn't the one on screen,
  and lands on a `rid IN` **cond** node through `replaceFilters` — nothing
  else ANDed under it, so the grid shows the N rows the count promised.
  `rid` is a column to both the raw validator and the condition compiler
  via `Store.PHYSICAL_COLUMNS`, not a keyword. grid.js paints a
  `.diff-mark` pill in the gutter for any row in `S.diffMarks` — A for
  the left session, B for the right, A→B for both (`diffMarkNode` is the
  one builder the legend and banner use too) — with both sides' tags and
  notes in its title. The `#diffBanner` track above the grid is drawn
  from `S.diffMarks` alone (`syncDiffBanner`, called on
  `winnow:viewchange` — which the cached re-open path also dispatches —
  and on tab switches), and Done lands on `prevTree`. The marks are
  cleared by Done, by `clearAllFilters`, by opening another case and by
  removing the table; put the rows back in the panel, or leave any of
  those paths out, and the grid wears comparison chrome over a view that
  isn't the comparison.
- **The right-click surfaces** (row menu, column-header menu, table menu,
  header value picker) all hang off one floating-menu implementation in `static/js/ui.js` —
  `showFloating`/`placeFloating` plus the single `openMenuEl`/`openMenuAnchor`
  pair, with `dropdownMenu` (anchored under a button), `contextMenu`
  (positioned at the pointer) and `anchoredPanel` (a card with real
  controls in it) as the three entry points. That's what makes "only one
  of these is open at a time, and Escape closes it" true across all of
  them rather than four near-copies of the same two listeners. An item
  may carry `submenu` (an array, or a function for one that repaints —
  the tag list's ✓) and opens a `.menu-sub` flyout beside itself on
  hover or click; one per level, closed with the root or when a plain
  sibling is hovered; a click on the parent opens (never toggles shut)
  and the arrow keys walk it (Right opens, Left closes and refocuses the
  parent); from outside the menu only Down/Up step in, so a caret in a
  text field keeps its arrows. A flyout's identity is its parent item's
  `key` (else its label) at its depth — never its position, which a
  repaint shifts when Undo appears. A menu opened with
  `{ pins: '<key>' }` lets submenu items that declare a stable `pinId`
  be dragged or ☆-starred onto a **Pinned** section at its top
  (`menuPins`, localStorage `winnow.menupins`). The root's context
  (pin store + repaint) and each submenu button's item live in WeakMaps
  (`MENU`, `BTN`) rather than on the nodes, and one `repaintAll` rebuilds
  the root, re-binds every open flyout to its parent's new button,
  refills and re-places it — after a keepOpen click, a pin, or a tag
  hotkey pressed with the menu up (`repaintOpenMenus`) — so a pinned
  tag's ✓ and its twin inside the flyout always agree, and a pinned
  plugin action simply isn't shown while the plugin is off (the plugins
  panel refreshes `S.pluginRowActions` on toggle for that). Tag pins key
  on the tag's *name* (ids are per case file). The row menu is the one
  using it: filters for the clicked column stay broken out, Tag / Add to
  dashboard / Copy / Plugins fold into submenus, Undo sits at the top
  level beside Tag, and rules fall where a fold meets something broken
  out (no section is named in the loop). The
  column-header menu is the one that *replaced* a visible control rather
  than adding a surface: its `▾` (`.hcell-fmt`) cost a slot of every
  header's width, on every table, forever, to be opened rarely — the same
  trade the tab strip's `▦` lost. Both handed their discovery burden to a
  title attribute. Two
  details are load-bearing: `onMenuKeydown` now `stopPropagation()`s its
  Escape (the document-level handler underneath clears the row selection,
  and dismissing a menu shouldn't throw away what was selected under it),
  and a menu's `items` may be a *function* — which is what `keepOpen`
  items re-run to repaint themselves, so toggling three tags from the row
  menu is three clicks instead of three right-clicks. `placeFloating`
  flips above the anchor rect when there's no room below; right-clicking
  a row near the bottom of the grid is the common case, not the edge one.
- **The row context menu is a section registry** (`ROW_MENU_SECTIONS`,
  `rowMenuItems`), not one function that spells the list out, because it's
  now the place per-row features are expected to land — a new action
  should be an entry, never surgery on a growing if-chain. Sections get
  `{pos, colName, colIndex, value}` and return items; an empty return is
  skipped. Sections are not separator-delimited any more: the only rules
  are before and after the broken-out filter block (`cell`), and every
  other section contributes one folded `{label, submenu}` entry. The row is re-resolved (`rowAt(ctx.pos)`) on
  every repaint rather than captured, because a keepOpen tag item
  re-renders after tagging and the bulk tag path clears the row caches
  (`clearRowCaches`, both the flat and the grouped one) underneath it. Scope follows the selection: right-clicking *inside* one
  acts on the whole selection (tagging 200 checked rows shouldn't collapse
  to the row under the pointer), right-clicking outside it moves the
  cursor there first. Works in grouped mode too now (see "Grouped mode's
  rows are ordinary rows" below); a right-click on a *group header* opens a
  different menu instead — `groupMenuItems`. A tag's ✓
  reads the clicked row even when the target is a whole selection, which
  is deliberately the same sample-one-row rule `resolveTagDirection`
  already uses for the number hotkeys, so the menu can't promise a
  different outcome than pressing `2` would.
- **The clicked cell's filter block names the value once, in its heading**
  (`rowMenuCellItems`). It used to spell it into all three verbs —
  "Filter to <value>", "Filter to <value> only", "Exclude <value>" — so a
  40-character provider name appeared three times and the whole difference
  between the first two was the word "only", sitting at the end of the
  longer label where a long value had already been ellipsized away. The
  heading is `Column = value`, `literal: true` so it is mono and *not*
  uppercased (half of it is a value, and an uppercased hash or path is a
  different string to the eye), with the value budgeted against the column
  name's length to keep it on one line and the untruncated text on the
  heading's `title`. The three verbs — Narrow / Reset / Exclude — each
  carry a `desc`, the dim second line `menuItemNode` renders under a
  label, saying what that verb does to the filters already on. That field
  is for exactly this shape of problem: sibling verbs whose consequence,
  not whose name, is what an analyst is choosing between. "Filter by
  values…" below them deliberately stays one line — it is not a verb on
  this value.
- **The header value picker** (`openValuePicker`, the `▾` in each filter
  cell) is an *author* for the filter the header box already understands —
  it writes `=v` or `a|b|c` into `S.filters` and nothing downstream knows
  it exists. Four things about it are decisions, not accidents:
  - **Which values it lists.** Unfiltered column: the current view, via
    `group_summary` — so the list reflects every *other* filter in play,
    which is Excel's behaviour and the one that answers "which processes
    survive this timeframe". Already filtered on this column: the whole
    table, via `column_values` — a view narrowed to three values can only
    offer those three back, and widening is the main reason to reopen the
    dropdown. Both are swappable from the panel, because a guess about
    scope that isn't visible is a lie. Building a *second* view with just
    this column's filter removed would be the truly Excel-exact answer and
    is not available: `Store._views` evicts any other view for the same
    source (backlog item 2).
  - **`bucket_datetime=False`.** `group_summary` day-buckets datetime
    columns, and a `2024-01-05` bucket matches no stored value, so an
    `=`/`in` filter built from one selects nothing. The flag turns the
    bucketing off for this one caller. It relaxes nothing about grouping's
    contract — the picker returns values, never groups anything gets
    expanded against, so there's no `_eq_condition` on the other side to
    keep in step — and raw values make the column index worth building
    again, hence the `whole_source and not is_datetime` gate admitting them.
  - **The size gate.** Distinct-values-with-counts is an aggregate pass
    with no index to lean on until the lazy per-column one exists, so the
    button is only rendered under `VALUE_FILTER_AUTO_MAX` (250k) rows by
    default. Overrides are three-layer, most specific first: the column's
    own pin (in the layout, so it travels with a saved default layout for
    the header set) → the table's `value_filters` mode (per-source, in the
    layout payload — it's a judgement about *this table's* size, which a
    header-set-keyed cross-case layout has no business carrying) → the row
    count. The row menu's "Filter by values…" opens it regardless: an
    explicit click is consent to pay for the scan in a way an
    always-present button isn't.
  - **What the filter box can't spell.** `=v` round-trips any value
    including one containing `|` (parseFilter matches the `=` prefix
    first), but the box trims, `a|b|c` is its only multi-value spelling,
    and `IN ()` drops empty strings server-side. So a selection with edge
    whitespace, a `|` in a multi-selection, or `(empty)` mixed with real
    values goes into the guided filter tree instead (`setPickerTreeNode`),
    with a toast saying so. That node is recognised *structurally* on the
    way back (an in/equals/empty cond on the column, or an OR of exactly
    those) rather than by a marker field, because `openFilterBuilder`
    round-trips the tree through SQL text and would drop any marker we
    invented.
- **Settings' sections are collapsed on open, every time**
  (`settingsSection`, one wrapper per `h4` the modal used to append
  straight into its body). Seven sections had grown to ~900px of scroll,
  so the setting you came for was rarely the one on screen. Two
  deliberate non-features: state isn't remembered between opens ("open
  where I left it" and "collapsed by default" are different promises, and
  the second is the one that was asked for), and opening one doesn't
  close the others. A section's own code is unchanged apart from what it
  appends into — which is also what keeps a section that fills itself
  later (`buildPluginsPanel`'s async listing) landing inside its own
  section rather than at the end of the modal.
- **`S.keymap` must hold its own key arrays, not `DEFAULT_KEYMAP`'s.**
  The settings UI's "+ key"/"✕" handlers splice and push those arrays in
  place, so the old shallow `{...DEFAULT_KEYMAP}` handed them the
  defaults' own arrays: on a profile with nothing stored yet, adding a
  binding edited `DEFAULT_KEYMAP` itself, and "Reset to defaults" then
  copied the polluted defaults back and looked like it did nothing.
  `defaultKeymap()` (a per-action `[...keys]`) is what `loadKeymap` and
  the reset button both go through now.
- **The filter bar replaced the always-on filter row, and the row is a
  setting rather than a casualty.** Measured on a seven-table review case:
  **27 filter boxes on screen, 0 in use** — the emptiest strip in the
  viewport was also the second heaviest thing in it after the data. The
  default now is `#filterBar` (`renderFilterBar` in columns.js): one line
  naming only the filters that exist, as chips carrying column + value and
  a ✕, plus "+ filter a column…". A column's box appears under its header
  when the header's `⌕` (`.hcell-filter`), a chip, or that picker asks for
  it, and folds away again on Enter or Escape. `S.appearance.filterUi`
  picks the surface and `FILTER_UI_DEFAULT` (settings.js) is the single
  value that flips which one a fresh install gets; Settings → Appearance's
  "Always-on filter row" is the analyst's own switch, because typing
  straight into a column box without looking is the Timeline Explorer
  reflex and the analysts who have it are not wrong. Six things are
  decisions:
  - **The bar lives OUTSIDE `#gridHead`, above `.grid-body`.** Everything
    inside the head is in the grid's horizontal scroller and sized
    `width: max-content`, so a strip in there either scrolls its chips out
    of reach with the columns or needs a width equal to the scrollport —
    which no CSS length can express from inside a `max-content` box. Out
    here it is simply as wide as the grid and wraps. The price is that its
    `hidden` has to be driven: `renderFilterBar` owns all three reasons
    (classic row on, no table open, a page tab up) and `syncTabChrome`
    calls it for the same reason it hides the toolbar.
  - **The row survives under the bar, mostly as empty cells.** A revealed
    box still has to sit under its own header, so every column keeps an
    `.fcell` at its own `flex-basis` and only the open ones get an input;
    the whole row is `hidden` while none is open. Rendering just the one
    open cell would put it under the gutter.
  - **The `⌕` is in flow, not on `:hover`.** Hiding it until hover would
    reflow the header row as the pointer crossed it — the mistake
    `.fcell-pick` refuses to make. It costs ~13px of every header, which is
    the charge that got the column-options `▾` removed (see the menus entry
    above); the trade is different here, since that was a rarely-opened
    menu and this replaces a whole row of boxes with a row of nothing. A
    modified click falls through to the header, so Alt-click still pins and
    Shift-click still adds a sort when the glyph is what got hit.
  - **An open box is not also a chip.** The box IS that filter while it is
    being edited; two copies of one filter a keystroke apart is the
    confusion the bar exists to remove. Which is also why Enter closes the
    box under the bar and does not under the classic row.
  - **`setColumnFilter` repaints the bar and nothing else.** It
    deliberately avoids `renderHead` (that would drop the cell selection
    its callers — the row menu, `f`, the picker's single-value case — are
    still acting on), and under the bar the chip is the only place those
    writes show at all. `S.filterOpen` is transient per table: pruned to
    the visible columns on every `renderHead`, emptied by `openSource`,
    `clearViewNarrowing` and `landOnFilters`. `columnFilterChips` covers
    both spellings a column can be filtered by — the header box's text and
    the value picker's node in the guided tree — because a bar that showed
    only one would leave the other invisible.
  - **Revealing or folding a box changes the head's HEIGHT, so it has to
    repaint the rows as well: `renderHeadResized()`, not `renderHead()`.**
    `#rows` is positioned at `headH()` and that top is written in one place
    only, `syncRowsTop()` inside a paint, so a head that grew by the filter
    row while the rows stood still draws the first row of data underneath
    the sticky header, leaves a blank strip at the bottom, and hands the
    gutter drag and the autoscroll (`rowAtClientY`, which subtracts
    `headH()`) a row that isn't the one under the pointer. Nothing repairs
    it by itself — a column already in view scrolls nowhere, so no scroll
    event fires, and folding a box away rebuilds nothing at all. Three
    paths resize the head this way: the `⌕`/chip/picker reveal, the fold,
    and the Appearance switch between the two surfaces.
- **`.fcell` needs its `min-width: 0`, and it's not tidying.** Giving the
  filter cell `display: flex` (to seat the value picker's ▾ next to the
  input) also made its own automatic minimum size content-based — and a
  text input's intrinsic width is ~177px, so every filter cell silently
  floored at 177px while its header stayed at the column's real
  flex-basis. Measured: a 90px column had a 179px filter cell under it,
  and the two rows stopped lining up from the first narrow column
  onward. `.hcell` has never had the problem because the `overflow:
  hidden` it already carries suppresses the same automatic minimum.
  Anything else in this file that becomes a flex container while sitting
  in the `.head-row`/`.filter-row` flex line needs one or the other.
- **Autofit measures the header, it doesn't estimate it.** `widthForLen`
  used `max(dataChars, name.length) * 7 + 24`, which ignored everything
  the header cell carries besides its text — the sort arrow, the ▾
  options button, the derived `ƒ` mark, 8px of padding either side — and
  the header font is uppercase and letter-spaced, so it was never 7px per
  character either. Result: a fit-to-content pass could leave `EVEN…▾`
  sitting over a column of `1`s. `headerWidthFor` now reads the live DOM
  instead: the label's `scrollWidth` (its full text, even while clipped)
  plus `hcell.clientWidth - label.clientWidth` (padding, gaps and every
  non-label child; the grip is absolutely positioned, so it isn't in
  that difference). It's idempotent by construction — once the label
  isn't clipped, both terms stop changing — and returns 0 for a column
  with no header on screen, where callers fall back to the old estimate.
- **The autofit cap is a user setting** (`S.appearance.autofitMax`,
  Settings → Appearance, default `AUTOFIT_MAX_W_DEFAULT` = 900px, `0`
  meaning uncapped), not the old hardcoded 480. A cap still exists by
  default because the rows are `width: max-content`: one column of
  base64 command lines fits to ~3,600px uncapped (measured) and every
  horizontal scroll of every other column then goes through it. Two
  rules inside `widthForLen`: the header may exceed the cap (a column
  whose *name* is cut off can't be identified, while a truncated value
  can still be read in the detail pane) but only to 2x, so one absurd
  header can't defeat the cap either. Stored with the other per-browser
  look-and-feel prefs rather than in the layout — it's a statement about
  this screen, not about this table's columns.
- **The table menu replaced the tab strip's `▦` column-chooser button**
  (`TABLE_MENU_SECTIONS`/`openTableMenu`, right-click a tab or a sidebar
  row, or press `C`). Same registry reasoning as the row menu: it's where
  per-table features land, and the tab strip can't grow an icon per
  feature. `openTableMenu(sourceId)` opens that source first when it isn't
  the one on screen — not a convenience, a precondition, since every panel
  reads the live `S.layout`/`S.order`/`S.columns` rather than the record it
  was handed.
- **"Hide empty rows" is a view predicate, not a layout edit.** Unlike
  its neighbour "Hide empty columns" (a one-shot client-side sample that
  writes `S.layout`), the row toggle is `S.hideEmptyRows` →
  `currentSpec().hide_empty_rows` → one clause in `Store._compile_where`,
  so it reaches both branches of `build_view` (merge parity) and
  `spec_sql`, and the toolbar's "N of M rows" follows it. Remembered per
  table in the layout payload (`hide_empty_rows`, beside
  `value_filters`) and in the per-tab stash. "Reset view"/`clearAllFilters`
  leave it alone, like the value-filter default.
- **`clearAllFilters(seed)`** — Shift+F ("filter to this value and drop the
  rest") is that reset plus one filter, so it goes through the same
  function rather than a second implementation that would forget the
  carve-outs: the timeframe filter survives, grouping is stashed into
  `S.lastGroupBy` rather than lost. Note `$('btnReset').onclick` is now a
  wrapper — passing `clearAllFilters` directly would hand it the MouseEvent
  as `seed`.
- **A filter change lands on the same row, and that lives in `rebuildView`
  (`keepRow`, on by default), not in the callers.** The tag chips, the
  timeframe toggle and its Clear, the value picker, search Escape and a
  saved filter all rebuild with `keepScroll: false`, and each used to land
  at row 0 with `S.cursor` left as the number it had been — a number that,
  once the view widened and renumbered, named an unrelated row far below
  the fold (and, with the detail pane open, put that stranger in the pane
  as its page landed). Only `clearAllFilters` found the row again, by
  bracketing its rebuild with `selectedRowAnchor` → `/api/row_position` →
  `recenterOnRow`. That bracket is inside the rebuild now: `cursorRowAnchor`
  (cursor first — the highlighted row is the place; picks and the cell
  range only stand in for a missing cursor) is captured before the build,
  its position in the new view is asked for **before the seed fetch** so
  the pages seeded are the ones the grid will show (recentring after a
  page-0 seed painted the target rows as placeholders for a round trip),
  and after the seq/source guards the cursor is re-pointed by identity.
  Three rules. With `keepScroll: true` (header-box typing, a sort click)
  the viewport never moves, so the one thing that would notice a
  re-pointed cursor is an open detail pane (grid.js re-points it at
  `rowAt(S.cursor)` as pages land — a number left behind puts a stranger
  in it): the lookup is issued only while the pane is open, alongside the
  seed and awaited before the paint, and skipped otherwise, leaving the
  cursor the number it was — the header box's old behaviour, the highlight
  keeping its screen spot. The skip is not a nicety: on a materialised
  view `find_position` is a scan of the whole view table (`pos` is its
  only key), and every debounced keystroke would pay it before the grid
  could repaint. A row the narrower view no longer has (`pos: null`)
  clears the cursor and hides the detail pane rather than leaving a stale
  number; a lookup that failed (a 409 from a view a newer rebuild already
  evicted, a dropped request — `rowPositionIn` returns `undefined`) is
  not that answer and leaves the cursor alone for the next rebuild to
  resolve. A cursor whose page has left the cache can't be captured at all
  (`cursorRowAnchor` is null): a `keepScroll: true` rebuild then touches
  nothing, and a landing at the top clears it, since the number would
  name a stranger there. `keepRow: false` is for a navigation
  that means "the top of a fresh table" (a dashboard drill, `openSource`'s
  first build). The `/api/row_position` GET has its own catch: the chip
  handlers don't await the rebuild, so a 409 from a view a newer rebuild
  already evicted would be an unhandled rejection, which the UI fixture
  fails the test on. Grouped views are untouched (grouped positions are
  another address space and `regroupAll` resets the cursor), which is why
  `landOnFilters` keeps its own bracket for exactly the grouped case — it
  drops the grouping before the build, and the cursor with it. Pinned by
  `tests/ui/test_tag_filter_keeps_row.py`; the materialised branch of
  `find_position` (every sort, filter and merge) by
  `tests/test_row_position_merge.py`.
- **A running build says so, and a superseded one says nothing.** While
  `rebuildView` has a build in flight, `#viewStats` reads
  `Searching "term"… 3.2 s` (the spec has a search — the box's text, or
  the advanced terms) or `Filtering… 3.2 s`, on a 250ms timer
  (`BUILD_TICK_MS`; a build that lands inside the first tick never shows
  it, the same rule the cancel chip's 1.2s follows), and `#search` carries
  `aria-busy="true"` — a token-only border pulse — for a build with a
  search in it, not for a header-box filter. The count from before the
  build comes back when it is cancelled or fails (the old rows are still
  the rows on screen); a build that lands writes its own. The indicator
  belongs to the newest rebuild: a burst of keystrokes hands the text
  from before the *first* of them along, so a cancel never restores
  `Filtering… 0.3 s`, and a table switch under it stops it without
  restoring anything (the stats are the other table's now). Two guards
  keep that true. A rebuild superseded while it awaited `/api/view/keys`
  leaves before it starts any chrome of its own (`seq !== rebuildSeq`
  right after the lookup) — otherwise its indicator replaced the newer
  build's and its `finally` then took both down. Its one exception is
  the cancel chip, which it claims at the supersede rather than here
  (below): a chip claimed that way is handed straight to the rebuild
  that supersedes this one in turn, so the disarm on the way out finds
  it owned by another token and leaves it standing. And a landed build
  writes its count *before* the selection remap's `await
  /api/view/positions`: the indicator stops in the `finally` with its
  last `Searching… 3.2 s` frozen in `#viewStats`, and a rebuild starting
  inside that await read the frozen label as the text to come back to.
  The chip survives a supersede too: `cancelInflight` reports whether
  the superseded build's chip was up, and the new build claims its own
  at that moment rather than 1.2s later — at the supersede itself, not
  after the keys lookup, since the build being displaced disarms as soon
  as its aborted fetch rejects and the lookup is a whole round trip
  wide. `armOpCancel(token, 0)` claims it synchronously for the same
  reason: a zero-delay timer still loses to a disarm that runs on a
  rejection. The 2px bar and the chip were the only running state
  before; an analyst watching a full-table scan saw the old count and
  nothing moving. `updateSearchHint` adds `index building` while the open
  table's trigram index is still being built (`fts_building && !has_fts`);
  regex is always `full scan`. **The client has to ask about that build.**
  The first search on an unindexed table is what starts it — server-side,
  from inside `build_view` (`_ensure_fts_building`, contains and advanced
  only; regex never indexes) — and the view's payload says nothing about
  it; the jobs poll does not run on an idle case and only refetches the
  sources while `ftsWatch` already has something in it. So `rebuildView`,
  when a build with a search in it lands on a table whose record says
  `!has_fts`, calls `followFtsBuild` (jobs.js): `refreshSourcesQuietly`,
  recompute the hint, `startJobsPoll` — the poll then sees
  `fts_building`, watches the source (the indexing row in the panel,
  the hint while it runs) and toasts `Search index ready` when it lands.
  A table that answers "no index, no build" (a SQLite older than the
  trigram pushdown never builds one) is not asked again that case. A
  rebuild that supersedes one in flight cancels it — server-side token
  and client-side fetch both, see [store.md](store.md)'s cancellable-ops
  entry — and the superseded build's 499/abort is silent: no toast, no
  repaint. Only the chip's cancel toasts. `tests/ui/test_search_supersede.py`
  — the index-build test plays the server for `/api/sources` (route +
  reload) rather than writing `fts_building` into client state, so it
  proves the refresh is issued, not just that the hint renders.
- **A search-box build goes to the background after `SEARCH_DETACH_MS`
  (5 s) and its result waits for Apply.** The box's debounce, Enter,
  Escape, the mode switch and the advanced chips pass `detachAfterMs` to
  `rebuildView`; the build then runs as a job (`POST /api/view/start`,
  polled 150→400 ms) with the same busy bar, chip and "Searching… N s"
  as a blocking build, and a fast one adopts its view and lands exactly
  as before. Past the deadline the chrome comes down, the old rows stay
  (a held build evicted nothing — [store.md](store.md)), the build is
  stashed in `S.pendingViews` (keyed by source: one per table, which is
  what "one pending per source" means server-side too), a jobs-panel
  notice with Cancel stands for it, `#viewStats` reads "Searching in
  background…" and `inFlightWork` lists it for the shutdown guard. When
  it lands the notice offers Apply and Discard and a `toastAction`
  offers Apply — **a finished search never installs itself**; the
  analyst may be three tables away. `applyPendingView` opens the table
  first if it isn't the open one (`openSource(id, {skipBuild: true})`
  restores that table's stash into S, so the job's state has to go in
  AFTER it), puts back the box/filters/sort/tags/timeframe the search
  was run with (a snapshot of S, not the compiled spec — `S.filters` is
  raw header-box text), repaints the chrome from them, and adopts the
  view through the same landing every rebuild takes, so the cursor row,
  the picks and the seeded pages resolve against the adopted view. A
  409 "expired" on the adopt (a build landed in between and evicted the
  held view) toasts and runs the same spec again inside the chrome that
  is already up. Only search-box rebuilds detach: filter, sort, tag-chip
  and timeframe rebuilds are awaited by code that acts on the NEW view
  afterwards (a restored scroll offset, `recenterOnRow`, a dashboard
  drill), and a rebuild that resolved with the old view still installed
  would run that against the wrong rows. Which is why `setSearchMode`
  takes `{ detach: false }`: it ends in a rebuild, and it is not always
  the box calling it — `landOnFilters` (Reset, Shift+F, the row menu's
  "…only", a session comparison's "open these differing rows")
  normalises the mode back to contains on its way and then runs
  `syncSearchExpansion` and `recenterOnRow` against the view that build
  lands. The detail pane's `searchForText` is the box in another place
  and keeps the detaching form. `installView` is the tail of
  every rebuild and the only place `winnow:viewchange` fires from, so a
  search still pending has not "changed the view" until it is applied
  (docs/writing-plugins.md says so). **The table's own record is
  optional there.** Remove takes it out of `S.sources` while a build for
  it can still be in flight, so both readers treat a missing record as
  an answer rather than an error: the stats line denominates with the
  view's own count (the rows it holds are the rows it found), and the
  trigram-index question a search build ends in is simply not asked. A
  throw instead left the view half installed — `S.view` swapped, nothing
  painted, no `winnow:viewchange` — which is what
  `tests/ui/test_rebuild_handoffs.py` pins. A new search-box rebuild for
  the table cancels its pending search first — restoring the stats text
  before its own indicator reads it as the "before" — so Escape in the
  box is also how a pending search is called off. `setSearchDetachMs(0)`
  is the test hook; `tests/ui/test_search_background.py` masks the real
  job as still running via `page.route` rather than sleeping, and the
  Apply it clicks does the real adopt.
- **Any other rebuild of the table calls its pending search off first;
  coming back to the table does not.** `runBuild` cancels the table's
  pending search for every build that is not that record's own adopt.
  A search-box rebuild is the newer search (its notice replaces the old
  one); any other — a header filter, a sort, a tag chip, the timeframe,
  a return to the table with a different spec — lands as a normal build,
  which evicts the held view server-side (newer intent wins), so a
  notice left standing would offer an Apply that could only 409 into a
  blocking re-run of the search. Cancelling first also frees the writer
  lock a running search holds: told it was in the background, the
  analyst would otherwise find a header-box keystroke queued for the
  rest of the scan and then the same scan run again. A finished search
  waiting for Apply goes the same way (its row closes) — the analyst's
  newer action is the newer intent. Coming BACK to the table is the
  exception: the stash puts the search in the box again, that spec is
  the pending record's (`rec.cacheKey`), and `openSource` takes the
  cached path — the old live view, still exactly what the server pages
  — and writes the stats from the record (`pendingViewStatsText`)
  instead of posting the same search as a second, blocking build queued
  behind the first. Smaller rules that follow from the same shape: the
  row's ✕ is Cancel while it runs and Discard once it has landed
  (`createNotice`'s app-only third argument, `onDismiss` — plugins pass
  `opts` only and cannot reach it), never a plain dismiss that would
  leave the search polling with nothing to apply or drop it from; a
  search that ends cancelled or superseded server-side finishes its row
  (lingers, closes) and only a build error waits in red; `restoreStats`
  puts the pre-search count back only over the view it described
  (`rec.viewId`) and recomputes from the live view otherwise; the cancel
  chip is not armed for an adopt — it cancels a build's statement, and
  an adopt's wait is the writer lock's — and comes up only if the adopt
  409s into a rebuild; removing a table cancels its pending search, and
  Apply for a table since removed discards the result with a toast.
- **Stored keymaps are migrated on load, not merged blindly.**
  `loadKeymap` used to be `{...DEFAULT_KEYMAP, ...stored}`, which means a
  returning analyst's localStorage outranks every later change to the
  defaults — including a *rename*, where the stored entry keeps swallowing
  its key while pointing at an action that no longer has a handler (
  `matchAction` scans the stored map, so the key resolves and nothing
  happens). So there's a `KEYMAP_MIGRATIONS` list with a version counter in
  `winnow.keymap.v`, and unknown actions are dropped on the way through.
  The v1 migration carries `openColumns` → `openTableMenu` and moves the
  `f`/`Shift+F` pair (focus-first-filter → filter-by-this-value, plus the
  new drop-the-others variant) *only* for analysts still on the old
  defaults — a binding someone chose themselves is never touched. v5 adds
  `Ctrl+f` beside `/` on `focusSearch` the same way, with a second guard
  the earlier ones did not need: it stands down when any other action
  already holds one of the four chord spellings, because Ctrl+F matched
  nothing before that release, Settings therefore accepted it for
  anything, and the pre-gate below would shadow such a binding.
  **A change to `DEFAULT_KEYMAP` with no migration entry reaches nobody
  who has run Winnow before**: `loadKeymap` persists the whole expanded
  default map on a profile's very first load, so by the second run the
  stored map — which outranks the defaults — already has the old value
  written down.
- **Table nicknames** (`sources.nickname`, `Store.set_source_nickname`,
  `POST /api/source/{id}/nickname`) are display-only: `name` is never
  rewritten — it's the file's identity (session hash warnings, the record
  of what was imported), and everything that matches or fingerprints keeps
  using it. On a merge (negative id) the same call renames `merges.name`
  instead, since a merge's name already is analyst-chosen; clearing a
  merge's name is refused. Old case files get the column via an
  ALTER-if-missing in `Store.__init__` (CREATE TABLE IF NOT EXISTS can't
  add a column). Frontend renders every user-facing source name through
  `sourceLabel(s)` (nickname || name) with `sourceTitle(s)` keeping the
  real file name in the hover title — new UI that prints a source name
  should go through those, not `s.name`.
- **Keybindings can be combinations** (`keySpecFromEvent`): a binding is
  stored as `e.key` optionally prefixed `Ctrl+`/`Alt+`/`Meta+`/`Shift+` in
  that fixed order. Two deliberate asymmetries: Shift never appears for a
  printable key (e.key already arrives shifted — `'G'` *is* the
  capital-letter binding), and for non-printable keys an unprefixed
  binding still matches the shifted press (matchAction's fallback) — this
  is what keeps Shift+ArrowDown reaching moveDown, whose handler reads
  e.shiftKey to extend the selection. The same fallback is why
  **Ctrl+Shift+Arrow needs no binding of its own**: it is spelled
  `Ctrl+Shift+ArrowDown`, matches nothing, gets Shift stripped, and lands
  on `jumpEdgeDown`'s `Ctrl+ArrowDown` — whose handler reads e.shiftKey
  and extends to the edge instead of jumping to it. Note the asymmetry
  that makes this work at all: Shift falls back, every other modifier
  does not, so `Ctrl+ArrowDown` itself has to be a binding spelled out in
  full or it reaches no handler and the browser scrolls the page.
- **Excel's arrow keys are four NEW action names, not new chords on the
  old ones.** `moveLeft`/`moveRight`/`jumpEdge{Up,Down,Left,Right}` were
  added rather than extending `moveDown`/`moveUp`'s arrays, because
  `loadKeymap` lets a stored array replace a default one wholesale: adding
  a chord to an action an analyst has already used reaches nobody without
  a migration, while an action name their stored keymap has never seen
  takes its default for free. Same reasoning the v2 entry records for
  `openFilterBuilder`/`openValuePicker`. `moveDown`/`moveUp` did change
  *behaviour* — they move the cell cursor now — but their bindings are
  untouched, so no migration was needed for that either. The settings capture handler ignores
  modifier-only keydowns and keeps listening (it used to commit on the
  first keydown, so pressing Ctrl for Ctrl+K bound "Control" and combos
  were impossible). findKeyConflict also refuses the hardcoded
  modifier shortcuts (Ctrl/Meta+C copy, Ctrl/Meta+z undo, Alt+digit tab
  switching, and Ctrl/⌘+F while search still holds it) since those are
  handled before matchAction and would shadow a binding silently. Side
  effect worth knowing: a bare-key binding no longer fires when
  Ctrl/Alt/Meta is held (matchAction used to look at e.key alone, so
  Ctrl+T opened the Tables manager).
- **Shortcuts are gated off the home screen**: the document keydown
  listener returns early when `$('app').hidden` — every keymap action, tag
  hotkey, Alt+digit and the copy/undo combos act on case UI that isn't on
  screen there (`t` opened the previous case's Tables manager from home).
  Escape stays above the gate: home has modals of its own to close.

- **Ctrl/⌘+F opens the search box, and the browser's find bar is refused.**
  Find-in-page reads the DOM and invariant #6 keeps only the visible window
  of rows in it, so Chromium answers "not found" for a value that is in the
  table — a wrong answer, not a missing one, which is the failure class
  worth spending a reserved chord on. The binding lives on `focusSearch`
  (`'/'` and `'Ctrl+f'`, KEYMAP_MIGRATIONS v5) but the *dispatch* is
  hardcoded in `wireKeymap`, above the `typing` guard like Alt+digit,
  because the box is exactly what you want from a filter cell or the SQL
  editor and `matchAction` never looks there. Four things that gate is
  carrying, each deliberate:
  - It matches `(e.ctrlKey || e.metaKey)` and `'f'` or `'F'` — the copy
    handler's shape, giving ⌘ for macOS (the keymap has no platform branch)
    and the capital for Caps Lock and Shift — plus a term neither the copy
    nor the undo handler has: `!e.altKey`, because Ctrl+Alt is AltGr on a
    European layout and AltGr+F there is a character being typed. The
    keymap stores the one spelling `'Ctrl+f'`; `findKeyConflict` refuses
    all four to another action, since the gate would shadow them silently.
  - It is conditional on `focusSearch` still holding the chord
    (`searchChordBound`), so unbinding it in Settings really does hand
    Ctrl+F back to the browser rather than leaving a dead chip.
  - It sits *below* the `$('app').hidden` gate (the home screen has no
    search box and is entirely in the DOM, where find-in-page tells the
    truth) and returns without `preventDefault` while `#modal` or a
    `.confirm-overlay` is up — a dialog owns the keyboard, Ctrl+C already
    falls through to the native copy there, and a dialog's text really is
    all in the DOM. A dropdown menu is neither of those, so it is closed
    (`closeMenu`) and the chord taken; otherwise the bar would open behind
    a menu still floating over it.
  - Off the grid it calls `showGridTab()` first. `syncTabChrome` hides the
    whole toolbar on a page tab, so focusing `#search` there would put the
    caret in a `display:none` input and read as a keystroke that did
    nothing. `expandSearch` then focuses the box; `collapseSearchIfEmpty`
    leaves it alone because focus landed inside `.search-wrap`.

  The two strings that name the key — the magnifier's tooltip and the box's
  placeholder — are built from the binding by `syncSearchKeyCopy`
  (filters.js), not written into `static/index.html`, for the same reason
  the gate is conditional: unbind the chord and copy naming it would be
  advertising a key the browser has taken back. `updateSearchHint` calls it
  after setting the padding, `keymapChanged` after every rebinding, and
  main.js once at startup. Keys print exactly as the Settings chips spell
  them (`Ctrl+f`, not `Ctrl+F`), so the two agree character for character.
  The placeholder is measured against what is left of the box's 320px after
  the mode chip's padding, and drops to the bare `"All columns"` when the
  keys will not fit — in regex and advanced mode they do not, and a hint
  clipped mid-chord is worse than a terse one. The tooltip has no width
  limit and always names them.

- **The 2026-08 left-hand keybind pass** is additive on purpose: q/w beside
  [/] for saved-filter cycling (the highest-traffic key in a triage pass,
  moved under the resting left hand), a/A beside T/R for the timeframe, and
  two previously mouse-only surfaces gained keys (e — Filter builder, v —
  value picker for the selected cell's column, falling back to the first
  visible column so it always lands somewhere). KEYMAP_MIGRATIONS v2
  appends the aliases only where the stored binding is exactly the old
  default — a deliberate rebinding is never touched — and the two new
  actions need no migration at all, since loadKeymap merges stored keys
  over the defaults and a stored map has no entry for an action that
  didn't exist. When retiring an alias later, remember both halves: the
  DEFAULT_KEYMAP entry and a migration for maps that carry it.

- **Stack view** (stack.js) — the column-header menu's "Stack values (rarest first)…" opens a modal of the current view's distinct values by count (via group_summary, order=count direction=asc), drawn with charts.js. Click a bar to filter the grid to that value. Least-frequency-of-occurrence triage. **The box is sized to its bars** — `max-height: min(60vh,520px)` over a canvas whose height is `rows × 22`, not a fixed height: a column that stacks to nine distinct values used to open a screen-high modal with nine bars in the top corner and four fifths of nothing under them. See docs/design/analysis-suite.md.

- **Case notes tab** (notes.js) — a free-form Markdown scratchpad for the investigation narrative, stored in the case file (Store.case_notes) so it travels with the .db — distinct from the per-row notes, which the page now LISTS below the split (see below). The editor and a live preview sit side by side (`#notesSplit`, a flex row inside `.page-main`) split by a draggable divider; Edit / Split / Preview collapse one pane or the other through `data-mode` on the row, and the stylesheet does the hiding. Debounced autosave, a tiny dependency-free Markdown renderer (airgap). Page tabs now route visibility through sql.js's showMainView(id)/MAIN_VIEWS registry so adding a tab is a one-place edit. See docs/design/analysis-suite.md. Things that bite here:
  - **The mode never persists, the layout does** (`winnow.notes = { split, rows }` — the divider ratio as a ratio of the row, and whether the row-note strip is open; per browser, like `winnow.sidebar`, and written only when the analyst drags or toggles, never on a visit that touched nothing): Notes always opens in Split, and `showNotesTab` re-applies both *synchronously before* `await ensureNotesLoaded()` — the page is already showing by then, so applying them after the fetch paints the markup's 50/50 default and then jumps. A remembered preview-only mode would also hide the editor from Playwright's `fill()` in tests/ui/test_case_notes.py after its reload.
  - **The page lists the OTHER notes too** — `#notesRows`, a collapsible strip below the split, one entry per row of `row_notes` in the case ("Security.csv · Line 412" plus the note on one line), each opening its row through `jumpToTimelineRow`, the same open-source-then-recenter the watchlist hits use. Two different things were called notes and only the narrative was on the page called Notes, so an analyst writing up findings had to remember which rows they had annotated. `GET /api/row_notes` → `Store.list_row_notes` answers `{total, notes}` joined to `sources` for the label (a note whose table is gone is omitted rather than listed unnameably) and capped at 500, with `total` so the strip can say which part of them it is showing. It is **re-fetched on every visit, not cached**: row notes are written on the grid between visits here. **`resetNotes` empties the strip on a case switch, and a failed fetch writes a line saying so rather than leaving the last list up** — the entries are `(source_id, rid)` pairs and both ids restart at 1 in a new case, so a leftover button does not error, it opens a real row of a real table and presents it as one the analyst annotated (`showNotesTab` reveals the page before its unawaited `loadRowNotes()` answers, and if that answer never comes there is no second fetch to correct it). Same hazard, same fix as the detail pane's note binding, which `openCase` blanks before the swap. Merge parity needs nothing — a note taken on a merged table's row is stored against the member, like a tag, so it is listed under that member. The open/closed state joins the divider ratio in `winnow.notes` (`{ split, rows }`).
  - **The placeholder is prose, and quiet.** It used to be a worked sample note — a heading, bullets, timestamped lines — which on an empty case read as a narrative somebody had written beside a preview pane that had failed to render it. Dimming alone does not fix that; the SHAPE is what reads as content, so it is two sentences now, `.notes-editor::placeholder` is `var(--text-faint)` italic, and `renderPreview` puts `.notes-preview-empty` ("Nothing written yet…") in the pane instead of leaving it blank. That empty line is a DOM node rather than markdown through the renderer, so nothing an analyst types reproduces it — and `resetNotes` still blanks the pane outright (tests/ui/test_notes_split.py pins that a case switch takes the preview with the editor).
  - **The divider is `#notesDivider`, not a `.page-panel-resize`.** `syncPluginPanels` shows and hides `#notesPanelResize` (the plugin column's handle) by id, and the plugin column stays the outermost sibling of `.page-main`, so a notes page panel and the split coexist untouched.
  - **One `input` listener drives autosave and the live render.** Every write path — typing, Link ▾, a plugin's `notesPage.setText`/`insert` — dispatches `input`; `ensureNotesLoaded`'s seed assigns `.value` directly (an input event there would autosave the body straight back) and calls `renderPreview()` by hand. The render is skipped and marked stale in editor-only mode or while the page is hidden; `setNotesMode`/`showNotesTab` catch up. It swaps `innerHTML` wholesale (why the link clicks are delegated) and puts the pane's own `scrollTop` back afterwards so editing the bottom of a long note doesn't jump the preview to its top.
  - The clamp is 0.2–0.8 of the row *and* a 220px floor per pane (`clampNotesSplit`; a row too narrow for two floors splits evenly): with a plugin column open at 70% of the section the ratio alone could leave the editor a few characters wide. **It is re-applied by a `ResizeObserver` on `#notesSplit`, not only when the ratio is written** — the row narrows without the page doing anything (the plugin column toggled or dragged wider, the window resized), and the observer re-applies the *stored* ratio against the new width, so a pane squeezed to the floor gets its share back when the room returns. `.notes-editor` is flex-basis-driven (`--notes-split`), not `width: 100%`, and the 82ch measure cap sits on the inner `.notes-preview-body` so the pane itself fills its half rather than leaving dead space against the divider.
  - **The divider's hit target overlaps the preview only** (`margin: 0 -8px 0 0`; the 1px line sits at the editor's edge). The editor's right edge is where the textarea's vertical scrollbar sits on classic-scrollbar platforms, and the symmetric `.tab-split` overlap turned a grab of the scrollbar thumb into a split drag. Headless Chromium's overlay scrollbars can't show it, so tests/ui/test_notes_split.py pins it with `elementFromPoint` and a drag started inside the editor's edge. `.notes-preview` paints `var(--panel)` itself — `#app`'s skin backdrop (the blueprint graph paper) otherwise showed through one half of what reads as a single surface.

- **The search dialog carries a scope** (search.js, `openSearchAllModal`) — titled "Search tables" rather than "Search all tables", because a scope row above the term builder now decides how much of the case it covers: **Every table / This table / Choose…** (the value picker's `.vp-seg` idiom, `aria-pressed`), with the case's real tables as chips under it, open ones first and the tail folded behind "+ N more". The default is *This table* when one is open and *Every table* otherwise. Clicking a chip from either fixed scope means "start from what is in scope and take this one out", so it seeds a Choose… scope and toggles that chip; a pick the analyst empties by hand stays empty (Search refuses it with a toast) rather than quietly widening back to the whole case. A pick whose tables have *all* since left the case is a different thing and does widen back to every table — with the scope row saying so, because a three-table check silently becoming a whole-case sweep is minutes nobody asked for. **A pick is keyed by id *and* name** (`searchAllLivePickIds`): SQLite reuses a source id once the table holding it is dropped, so an id whose live table is now a different file is not one of the tables that was ticked — the rule `subsetParentLabel` follows, for the same reason. `S.searchAll.scope` is the pending choice, `S.searchAll.ranScope` is the scope **the results on screen came from** — the same rule `terms` follows, so re-scoping without re-running cannot re-label numbers nobody re-ran, and the badge and the finished toast read from it too. On the wire it is `source_ids` (null = every table); a merged table is sent AS the merge and the server expands it to its members (invariant #9), which the results pane reports. Three things that bite: the poller used to decide "is this pane on screen" by comparing `$('modalTitle').textContent` to the literal 'Search all tables', which a title that varies with scope silently breaks — it asks `currentModalAction() === 'openSearchAll'` now; one search job runs per case, so a scoped start stops a sweep that may be four minutes in, which is why Search is no longer disabled while one runs and `startSearchAll` asks first instead — and why an empty box while a sweep runs is refused with a toast rather than taking the "nothing to search for" path, which would drop the job id and the partial hits while the server kept scanning, Stop button and all (a poll that 404s because the start it raced superseded its job is ignored for the same reason: only the job still being followed ends the run); and the 1,000-row count cap is unchanged at every scope — scoping does not make a table smaller — so the pane names the cap and points at "Open ↦", where the grid's own count is exact.

- **IOC watchlist tab** (watchlist.js) — case-level indicators (Store.watchlist / watchlist_hits, in the .db) scanned across every table; matches are counted, listed, and optionally auto-tagged through the normal tag path. **The scan is a background job** (`runScan`, the one helper behind Add, Scan all, the file import, From a case, the jobs.js source-done hook and Search-all's "Add to watchlist"): `POST /api/watchlist/scan/start`, a jobs-panel row with the tables-scanned progress (its ✕ and its Cancel both stop the scan), a 400 ms poll with the search-all rule that a **404 means stop** (superseded from another window, or the case closed) and a bound on any other failure (three in a row — the server exited or crashed — settle the row and the markers rather than polling forever), `#wlStatus` mirroring "Scanning 3/12 tables…" while the tab shows. Starts from one window go out one at a time, and **a newer scan folds the running one**: the server widens the new job to the old one's remaining scope, so the row being followed settles as "folded into the newer scan" (never orphaned in the running state with a Cancel that reaches nothing) and its "…" markers stay until the job that covers them lands — `finishScan` clears only the markers in the finished job's `watchlist_ids` (all of them when it was unscoped). **A new entry is in the list the moment the server has it** — the add/import responses already carry the indicator(s), so the row renders from them with "…" for a count (the in-flight ids live in a module-level Set, not on the row object: any `load()` mid-scan replaces `indicators` wholesale and would drop a flag carried there) and the scan that follows is scoped to the new ids. When the job lands: `load()` if the tab is showing, else the badge refresh and — when it found something — the row turns into the sticky "Watchlist: N hits · Open watchlist" alert; and when an auto-tag landed on the OPEN table — or on a member of the open merge, whose rows are the member's (invariant #9) — both row caches are cleared and the ribbon counts refreshed, or the tags would not paint until something else refreshed the grid. A grouping BY TAG is regrouped with it (`regroupIfGroupedByTag`, the same trio every other tag path ends in), or the auto-tagged rows would sit in "(untagged)" with the pre-tag counts until the analyst regrouped by hand. The repaint itself waits for the grid to be showing: `render()` against a grid a page tab hides measures a zero-height viewport and paints the first rows at the top, and the return to the tab restores the real scroll position over an empty viewport — so `finishScan` sets `S.gridRepaintPending` and `showGridTab` (the Alt+1, tab-history and mouse-thumb return paths) pays it on the way back, rail and regroup included. "Showing" is `gridIsShowing()` (sql.js), not `S.activeTab === 'grid'`: the home screen hides `#app` wholesale with the grid still the active tab. `showGridTab` consumes the flag whether or not it repaints — `openSource` and `openCase` pass `repaint: false` precisely because S.view/S.sourceId are still the OLD table's there, and they satisfy the owed repaint themselves once they have swapped (`installView`, or the cached-view path's own render/rail/regroup). A duplicate value is refused by the server (400 → toast). **The hits pane groups by table**: `/api/watchlist/hits` answers `{sources, hits}` with an exact per-table count and up to 200 rows per table in rid order; one sticky `.wl-hit-group-head` per table (label via `sourceLabel`, arrow and count styled like `.group-header-row`/`.group-header-count`), folding is client state, and a table past the cap ends in "…and N more — open the table" (jumpToTimelineRow). **The tab opens on the latest hits across every indicator**, not on "Select an indicator to see its hits": `renderLatestHits` paints `/api/watchlist/latest` — one line per flagged ROW naming the indicators that matched it, the table, the row and when, newest first — because a case with thousands of findings spent half the page on an instruction, and "which of these fired most recently" could only be answered by clicking every entry in turn. Clicking an indicator's name in a hit line, or a row on the left, narrows to the grouped-by-table pane; clicking the selected row again, or `← all indicators`, goes back. Every `load()` repaints the pane now, not only the one where the selection died under it — a scan that just landed changed what "latest" means. **The count cell is not always a count**: the same `.wl-count` cell takes a `.wl-state` variant that says `clean` (every table read, nothing matched — the one an analyst can quote), `not scanned` or `N/M tables`, off `scanned_sources`/`scan_targets` from `/api/watchlist/overview`. The slot class stays on whatever the answer is — stylesheet and tests find the cell the one way — and the variant only drops the pill geometry a number wanted. **Indicators covering the same rows carry a `.wl-dup` marker** and the summary leads with distinct rows flagged, naming the hit total for what it is ("counted across indicators") — `mimikatz` and `mimikatz.exe` both reported 2,323 hits and the summary added them. The marker opens a dialog that merges the pair through `POST /api/watchlist/merge`, and only ever offers the direction that loses no rows. See docs/design/analysis-suite.md and the scan entry in store.md.

- **Entity pivot** — a plugin, not a module: `examples/plugins/pivot` (there is no `entity.js`). The store's `entity_pivot` reuses the blob search + TS_NORMALIZE.

- **Case dashboards** (dashboard.js, dashwidgets.js) — named boards of widgets, each a data source (sql via read-only run_sql, watchlist, tags) plus a render kind (stat/kv/chips/list/bar/histogram), plus one card kind that is a grid of several: `render: signals` / `source: cells`, where the widget carries `cells: [{label, source, query, drill, tone?, chip?}]` and **every cell keeps its own drill**. That is what let the KAPE board fold eleven single-number cards into one without folding eleven drill-throughs into one OR-of-everything — `Store._cells_preview` runs the cells and answers with label/value rows plus a parallel `cell_errors`, so one card is one request and a cell whose table is missing from the case reports in its own place instead of taking the card's other numbers with it. `paintSignals` looks a cell's metadata up by LABEL rather than by position, so a payload cached before the card was edited still lines its drills up with its numbers; the label is in the store's fingerprint for the same reason (unlike a card title, it is IN the answer). The editor shows a signals card's cells read-only — it writes one question per widget — and carries them through Save. Widgets are built from RECIPES (dashwidgets.js `WIDGET_TEMPLATES` + `widgetFrom`): a template, a table and the column/value it needs produce the SQL, the render, a `build` (the recipe, so the editor reopens guided) and a `drill` — `{table, where:[{column,op,value}] | tree: <filter-tree node>, column?, bucket?}` or `{table, spec}` for a count-of-this-view widget — which `drillInto` turns into the grid opened on those rows: `openSource(id, { skipBuild: true })`, every stashed filter/search/tag/timeframe reset, then one view build (placeholder tables resolve through `POST /api/dashboard/resolve`, which lists every source a `{{all:…}}` spans so the analyst picks one; a widget with SQL but no drill opens as a query in the SQL pane; a bucket the timeframe can't express is refused, and a bucket on a column not typed datetime filters by the label's prefix instead). The shipped KAPE drills are checked against their SQL on a fixture in tests/test_dashboard_drill.py: a stat's drill opens exactly the rows it counted. Hand-editing a recipe's SQL drops `build` and `drill` rather than leaving them describing a query they no longer match. Entry points that skip the editor: the column header menu (top values / distinct / over time), the row menu (count of this value) and the Filters menu (count of this view), all through `quickAddWidget`, which asks which board only when there are several. `createDashboard` offers a starting point — blank, a starter built from the open table (`buildStarter`: count, activity window, over time, top values of 2–12-distinct columns), a shipped board, or a library board. Layout lives in the case .db; 'Save as profile' extends a plugin bundle with the board. The shipped KAPE triage board carries hand-written drills (checked against the header sets in tests/test_dashboard_drill.py). It is **ten cards**, consolidated from 26: five registry kv cards into one **Host** card that omits a fact the batch did not carry instead of rendering four cards reading "(not in this RECmd output)", two chips cards plus the two tampering counts into **Logging posture** (one card, two tables — the cells are asked separately, so a case with no RECmd batch still gets the tampering numbers), coverage plus the activity window into **Coverage** (whose rows name their own population — `Security: oldest` beside `All channels: window` — because a kv card's label is the only place a row can say what it counted, and four Security-only numbers under unscoped labels next to an all-channel one cannot be reconciled from the card), and ten single-number stats into **Triage signals** (ten cells) and **Findings** (the watchlist and tag counts, the one card on the board marked `live`). Spans are 2,1,1 / 3,1 / 2,2 / 2,2 / 2 — every intended row sums to four, because `grid-auto-flow` is left at its default and a card that does not fit drops to the next row and leaves a hole. Existing cases are NOT migrated: `upsert_dashboard_by_name` only runs on apply, so a case that has the 26-card board keeps it until the profile is applied again. See docs/design/analysis-suite.md.

- **A card that resolved to nothing is a line, not a panel** (dashboard.js `empties`/`renderEmpties`, `.dash-empties`). Walked against a real KAPE collection, the first screen of the shipped triage board was two full-width cards reading "(no host facts in this RECmd output)" and "(no Defender alert events in the logs)". Nothing was wrong — neither artefact was in that collection — but the board spent its best space saying so and never said what would change it. Empty cards now fold into one dashed strip at the top of the grid, a line each, and the card itself is hidden rather than removed (`.dash-card.is-empty`) because `paintAges`, `refreshBoard` and the editor's Run now all reach a card by its INDEX among the grid's cards. Five things to know before touching it. **The two kinds of empty are not the same answer and the strip has to say which**: a `{{registry}}` placeholder that binds to nothing is a gap in the collection an import would fill, and a query that ran and matched nothing is a finding ("no Defender alerts in these logs") with nothing to import — an analyst about to write "not present" in a report needs to know whether anything looked. The distinction comes off the wire, not off the message text: `Store.MissingTable` (a ValueError subclass, so every `except ValueError` on the widget paths is unchanged) carries the header-set name, and `/api/dashboard/widget/preview` answers `400 {"message", "missing_table"}` — core.js already unwraps a structured `detail.message`, so nothing else had to change. Sniffing for "table in this case yet" would have worked until somebody reworded it. **A `stat` is never folded**: 0 is an answer, and folding "0 failed logons" away hides the reassuring half of a triage board. Nor is a `signals` card that answered any cell — a cell that could not answer already reports in its own place. **The shipped queries no longer UNION a sentinel row in.** The Host and Defender cards each appended a literal "(no …)" row so the card would not render as a blank box, and that one line is what held the full-width card; they answer with NO ROWS now and the board says why (tests/test_kape_host_widgets.py, tests/test_kape_host_overview.py pin it). A shipped or profile widget that adds a sentinel row back becomes un-foldable, by construction. **Emptiness is decided per answer, not per render**: `noteEmpty` is called from the cache paint, from every run that lands and from the editor's Run now, keyed by the card ELEMENT (a widget has no id until the store mints one, and the answers arrive out of order), and the strip repaint is rAF-coalesced for the same reason `scheduleBar` is. **The fold is lifted BEFORE the paint, not a frame after it.** `noteEmpty` is asked first and returns the empty state, so a caller paints only what it says has something to paint, and a card leaving the strip has `is-empty` taken off it synchronously there rather than on `renderEmpties`'s rAF. A folded card is `display:none`, and `bar`/`histogram` measure their canvas inside a `requestAnimationFrame` of their own — unfold a frame late and a card that finally got its rows (the RECmd batch imported, then ↻ Refresh all, which runs quiet and never calls `render()`) comes back with a chart drawn at `fit()`'s 300×150 fallback and stretched across a span-2 card, whose `drawBars` boxes `pickBar` then tests against real click offsets: clicks past ~300px drill nothing at all, and the ones that land are a row out, because the row height is the canvas height over the row count. Nothing repaints a card on resize, so it lasts until the next full `render()`. tests/ui/test_dashboard_empty_cards.py.

- **A board paints from its last results, it does not re-run itself** (dashboard.js `cache`, Store `dashboard_widget_cache`) — the shipped KAPE board was 26 widgets, and opening it used to be 26 `POST /api/dashboard/widget/preview` calls, every open, every card drag and every edit of any other card (measured: 26 previews per reopen; the consolidated 10-card board is 1, the live Findings card). `GET /api/dashboards/{id}` returns the widget definitions AND the cached payloads, `render()` paints them at once and runs only the widgets carrying `"live": true`, and `↻ Refresh all` (or Run now in one widget's editor) is what re-runs the rest. Traps, in the order they bite: **widgets had no ids** — position in a JSON list was the only identity and a drag rewrites it, so `Store._mint_widget_ids` assigns one on write and `get_dashboard` back-fills one DETERMINISTICALLY on read (a random back-fill would hand a board that nobody has edited a new key on every open, and it would never see a hit). **The cache cannot live in the widget dict**: `saveToLibrary`, `saveAsProfile` and `upsert_dashboard_by_name` copy widget dicts verbatim into workspace JSON and into other cases, and `set_dashboard_widgets` clears `origin` on every write — so a payload in there would carry case A's numbers into case B and silently un-stamp a plugin-offered board. **The editor's Preview must not read the cache** (it runs an unsaved draft, and a Preview that answered from the cache is not previewing), which is why caching keys off `dashboard_id`+`widget_id` in the request rather than off `runWidget`. **A cached number with no date on it reads as a live one** — hence the bar's "As of 10:42 · 3 minutes ago", the per-card age in `.dash-mark`, and `Store.data_generation` (source count + row total + max id, read off `sources`, plus `STATE_GENERATION_KEY`, a counter the conclusion writes bump because `row_tags` and `watchlist_hits` are both WITHOUT ROWID and too big to count on every board open), which marks a result stale rather than hiding it. The counter is the half that can be FORGOTTEN, so every writer of those tables bumps it inside its own transaction — the tag delta path and its undo, but also the three writes that replace tag state wholesale and bypass that path (`delete_tag`, `import_session`, `start_new_session`), `upsert_tag` (a tags widget lists one row per definition), and the watchlist's add/delete/scan (a watchlist widget's number is the sum of the hit counts). What it does NOT cover is stated in its docstring rather than left to be discovered. **Staleness is only computed when a board loads**, so a board left open through a twenty-minute import would go on claiming to be current: `markDashboardStale()` is called from the jobs poll and the watchlist scan, and it ASKS the server rather than assuming, so a re-scan that changed nothing does not redden a board that is fine. **A board switch mid-flight** would file the old board's answers under this one's widget ids, so `boardGen` guards the write the way `S.pageGen` guards the page cache. **A failed ↻ Refresh keeps the numbers**: blanking every card because the server blinked throws away the only thing the cache was for, so a quiet run that fails leaves the card alone and marks it `!3m`. **A card that answered in part is not filed at all**: a widget that fails is reported and not cached so the next open retries it, and `Store._answered_in_full` applies that same rule to a `cells` payload carrying any `cell_errors` entry — cached, one cell that blinked would paint its em dash out of the case file until somebody pressed ↻. Both writers check it (`cache_widget_result`, `refresh_dashboard`) and both say "not filed" the same way, by answering without a `ran_at` — which is already the flag `runWidget` and the editor's Run now key their own cache write off. The cost is a card that can never answer in full (the five registry cells of Logging posture on a case with no RECmd batch) running again on every open, which is what a whole widget in that state already costs.

- **Profiles: a two-pane manager, and a sheet in front of Apply** (bundles.js; `WS.plugin_bundles`, `/api/plugin_bundles/*`). A profile is a kind of case — plugins, boards, a starter watchlist, variable definitions — and the list this replaced was one ellipsised row per profile, with the shipped description pushing Apply and Copy off the end of it. The manager (M) is `.pm` : profiles left with a one-line count summary, the whole selected profile right — every plugin, every board with EVERY widget's render kind and whether it is `live`, the watchlist, the variables. Sections carry `data-sec` (plugins/board/watchlist/variables) because a board section is headed with the board's NAME and "Watchlist" is also the start of a widget title on the shipped board. **The sheet is the point.** Apply sets an explicit on/off override for every installed plugin (so everything not in the profile is turned OFF), upserts boards BY NAME, seeds indicators and starts a scan, and said none of it first. `GET /api/plugin_bundles/{id}/plan` counts each of those against the OPEN CASE — which plugins go on and off and how many stay off, what each board replaces and by how many widgets, which indicators are new and how many tables seeding them scans (`Store.watchlist_scan_sources`, the scan's own predicate, so the number cannot disagree with the scan), which variables are required and unset (a required one with a declared DEFAULT is not among them: the apply creates the row carrying the default, so nothing prompts for it) — and `POST .../apply` takes `parts` from `APPLY_PARTS`, so a board can be taken without the watchlist. The plugins part stays ticked even when the case's set already matches the profile, because applying writes an explicit override per installed plugin and that write is what pins the case against a later machine-wide toggle. **No body still means all four parts**: the new-case dialog's Profile select and every script predate the sheet. **"You have edited this board" is answered by `origin`**, which the profile apply now stamps `profile:<name>` — any hand edit clears it (`set_dashboard_widgets`), a board stamped by this profile is its own untouched copy and worth no warning, and a plugin offering a board of the same name still asks, because a profile stamp is not a plugin stamp. A board applied before the stamp existed reads as edited, which errs toward warning. **Which profiles this case has had applied** is a `profiles_applied` case setting (a name → timestamp map) — the badge cannot be inferred from a board of the same name, which the analyst may have built by hand. **One profile exports as one file** (`winnow-profile/1`, `{format, profile}`) the way saved filters already do, minus the id and the `shipped` flag; a SHIPPED profile exports as a copy that records its ancestry. Import REFUSES a key it does not understand rather than dropping it, and refuses a KNOWN key holding the wrong kind of value for the same reason (`PluginBundles.IMPORT_SHAPE`; `save` is permissive by design, so `"plugins": "lateral_movement"` would store one plugin per letter and a string `dashboard` would reach the manager as a board whose widgets cannot be iterated). It never upserts by name either — a file from outside must not overwrite an afternoon's work, so a taken name is suffixed `(imported)`, from a base trimmed to leave room for the suffix, since a 100-character name plus a suffix truncates back to itself and the search for a free name never ends. **Lineage**: a copy records `from_profile`/`from_version` (written by "Copy to edit" and carried through every later edit, since a content comparison would lose the thread on the first edit), `profiles.json` carries a per-profile `version`, `list()` answers `update_available` when the shipped one is newer, and the manager shows one line offering the diff. Taking it is `POST .../take_update`: always a button, never a merge — an analyst's copy and a revised shipped profile can disagree about any part of a board, and a three-way merge of SQL widgets produces a board neither of them wrote. tests/test_profile_export.py and tests/ui/test_profile_manager.py.

- **Save a view, or a selection, as a table** (subset.js, `POST
  /api/view/save_as_table`). Two entry points, one helper: the row menu's
  **Save as table ▸** fold ("Save N selected rows as new table…" for the
  picks / cell range / clicked row, plus "Save this whole view as a
  table…"), and Filters ▾ → "Save this view as a table…". Folded rather
  than broken out so the row menu's top level keeps its shape (the two
  rules sit around the filter block only — pinned by
  tests/ui/test_row_menu_submenus.py); both items pin. Two shapes go to
  the server, and each item says which it is. The **whole view** —
  `exclude: []`, every row the filters, search and timeframe show, a
  select-all's unchecked rows included — is what "Save this whole view as
  a table…" and Filters ▾ send (`saveCurrentViewAsTable`; both titles
  read "unchecked rows included"). The **selection** is the scope-worded
  item only: under a select-all that is the view minus its unchecked
  rows, sent as `view_id` + `exclude` pairs (`saveSelectAllAsTable`,
  `applyTag`'s rule — never through `positions()`, which under select-all
  is `selPositions()` walking every position of a 2M-row view into an
  array before anything else happens), and its title says "minus the
  ones you unchecked". Explicit picks go through `loadRowsForPositions` +
  `rowAt` (so grouped mode works — a tree position resolves to its row,
  and the keys are resolved server-side against the ROOT view, which
  holds every group's rows), a hole is refused rather than papered over,
  and there is a **20,000-pick cap** (`SUBSET_PICK_CAP`, the server's
  selection-remap ceiling) with a toast pointing at "filter the view
  down, then save the view" — the view route has no cap beyond the 500k
  soft confirm. The name prompt defaults to `<parent label> — subset`.
  The POST runs under `setBusy` and a module-level in-flight guard held
  from the name prompt through the response (`tagWholeViewSelection`'s
  shape plus the re-entry guard the prompt needs): a long copy shows the
  busy bar, and a second click while one is saving gets a toast, not a
  second prompt and a second identical table. The new table starts
  **untagged** — the UI never sends `copy_tags` (store.md has the
  Timeline/export double-count reason) — and the success toast says so:
  `Created "<name>" · N rows · tags and notes stay on <parent>`. Then it
  is opened (`loadSources(); openSource(id)` — an explicit save may
  navigate; only background refreshes may not). The badge: `tab-subset`
  + a ⊂ glyph from `sourceGlyph(s)` in the tab strip, sidebar and Tables
  manager (⛓ for merges, same function), and `sourceTitle` adds
  `subsetDescription(s)` — "Subset of <parent> (N of M rows) · tags and
  notes on it are its own — none write back" from `origin_meta`, which
  keeps the parent's name and size from creation so a deleted parent
  still reads right (ids are reused, so the live table under
  `parent_source_id` only counts while its name still matches). The
  Tables manager row uses `subsetParentLabel(s)` — the same line without
  the "(N of M rows)", since the row count follows on the same line. The
  its-own-tags line is deliberate: an analyst may expect the parent's
  tags to have come along, or the subset's to appear on the parent, and
  the tooltip is where both expectations get corrected.

- **The detail pane's field list is a two-column read, and reads like
  one.** The names are painted as the column writes them; the uppercasing
  they used to get from CSS cost a beat per field in a list of forty (the
  word shapes go with it) and hid that `TimeCreated` and `CommandLine` are
  the same strings the header row shows. What separates the name from its
  value now is the typeface — the UI face against the value's `--mono` —
  which is the difference the eye was using anyway. Each `<dd>` also
  carries its own copy button (`copyFieldButton`, revealed on `dd:hover`,
  `float`ed so the value wraps around it and reserved in the layout at all
  times so the hover costs no reflow); "Copy row" was the only copy the
  pane offered, so lifting one SID out of forty fields meant selecting it
  by hand across a wrapped pretty-printed block. It copies the **raw**
  cell, like "Copy row" and unlike the pane's rendering of it, and finds
  that cell through `#detailFields`'s `dataset.pos` rather than a value
  stashed on the button — a multi-KB payload duplicated into an attribute
  would put the row in the DOM twice. The button is not a tab stop: forty
  of them between the pane's own buttons and the note box would bury the
  keyboard path that exists.
