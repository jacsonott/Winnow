# UI surfaces: menus, filters, settings, tabs, keybindings

Everything in `static/js/` and `static/style.css` that isn't the grid
itself: the right-click menus, the filter row and its value picker, saved
filters, the timeframe filter, tab strips and the sidebar, Settings, and
the keymap.

Part of the working notes split out of [CLAUDE.md](../../CLAUDE.md) —
see [docs/notes/README.md](README.md) for the whole set.

---

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
  and undoing — calls `regroupIfGroupedByTag()`: the tag just changed which
  group those rows belong to, and the expanded sub-views are server-side
  with nothing here to patch them with.
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
  re-renders after tagging and the bulk tag path clears the page cache
  underneath it. Scope follows the selection: right-clicking *inside* one
  acts on the whole selection (tagging 200 checked rows shouldn't collapse
  to the row under the pointer), right-clicking outside it moves the
  cursor there first. Works in grouped mode too now (see "Grouped mode's
  rows are ordinary rows" below); a right-click on a *group header* opens a
  different menu instead — `groupMenuItems`. A tag's ✓
  reads the clicked row even when the target is a whole selection, which
  is deliberately the same sample-one-row rule `resolveTagDirection`
  already uses for the number hotkeys, so the menu can't promise a
  different outcome than pressing `2` would.
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
  leaves before it starts any chrome (`seq !== rebuildSeq` right after
  the lookup) — otherwise its indicator replaced the newer build's and
  its `finally` then took both down. And a landed build writes its count
  *before* the selection remap's `await /api/view/positions`: the
  indicator stops in the `finally` with its last `Searching… 3.2 s`
  frozen in `#viewStats`, and a rebuild starting inside that await read
  the frozen label as the text to come back to. The chip survives a
  supersede too: `cancelInflight` reports whether the superseded build's
  chip was up, and the new build then arms its own with no delay rather
  than 1.2s later. The 2px bar and the chip were the only running state
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
  would run that against the wrong rows. `installView` is the tail of
  every rebuild and the only place `winnow:viewchange` fires from, so a
  search still pending has not "changed the view" until it is applied
  (docs/writing-plugins.md says so). A new search-box rebuild for the
  table cancels its pending search first — restoring the stats text
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
  defaults — a binding someone chose themselves is never touched.
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
  e.shiftKey to extend the selection. The settings capture handler ignores
  modifier-only keydowns and keeps listening (it used to commit on the
  first keydown, so pressing Ctrl for Ctrl+K bound "Control" and combos
  were impossible). findKeyConflict also refuses the hardcoded
  modifier shortcuts (Ctrl/Meta+C copy, Ctrl/Meta+z undo, Alt+digit tab
  switching) since those are handled before matchAction and would shadow
  a binding silently. Side effect worth knowing: a bare-key binding no
  longer fires when Ctrl/Alt/Meta is held (matchAction used to look at
  e.key alone, so Ctrl+T opened the Tables manager).
- **Shortcuts are gated off the home screen**: the document keydown
  listener returns early when `$('app').hidden` — every keymap action, tag
  hotkey, Alt+digit and the copy/undo combos act on case UI that isn't on
  screen there (`t` opened the previous case's Tables manager from home).
  Escape stays above the gate: home has modals of its own to close.

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

- **Stack view** (stack.js) — the column-header menu's "Stack values (rarest first)…" opens a modal of the current view's distinct values by count (via group_summary, order=count direction=asc), drawn with charts.js. Click a bar to filter the grid to that value. Least-frequency-of-occurrence triage. See docs/design/analysis-suite.md.

- **Case notes tab** (notes.js) — a free-form Markdown scratchpad for the investigation narrative, stored in the case file (Store.case_notes) so it travels with the .db, distinct from per-row notes. Edit/preview toggle, debounced autosave, a tiny dependency-free Markdown renderer (airgap). Page tabs now route visibility through sql.js's showMainView(id)/MAIN_VIEWS registry so adding a tab is a one-place edit. See docs/design/analysis-suite.md.

- **IOC watchlist tab** (watchlist.js) — case-level indicators (Store.watchlist / watchlist_hits, in the .db) scanned across every table via the blob substring search-all uses; matches are counted, listed, and optionally auto-tagged through the normal tag path. Auto-scans new imports (jobs.js source-done hook). Import a list / paste / scan-all. See docs/design/analysis-suite.md.

- **Entity pivot tab** (entity.js) — pick any value and see everywhere it appears across every table: per-source counts, which columns it landed in, a merged time histogram (charts.js) and a chronological evidence stream. Reachable from any cell's right-click ('Pivot on X'), the watchlist, or the tab's search box. Backend entity_pivot reuses the blob search + TS_NORMALIZE (shared with a future super-timeline). See docs/design/analysis-suite.md.

- **Case dashboards** (dashboard.js, dashwidgets.js) — named boards of widgets, each a data source (sql via read-only run_sql, watchlist, tags) plus a render kind (stat/kv/chips/list/bar/histogram). Widgets are built from RECIPES (dashwidgets.js `WIDGET_TEMPLATES` + `widgetFrom`): a template, a table and the column/value it needs produce the SQL, the render, a `build` (the recipe, so the editor reopens guided) and a `drill` — `{table, where:[{column,op,value}] | tree: <filter-tree node>, column?, bucket?}` or `{table, spec}` for a count-of-this-view widget — which `drillInto` turns into the grid opened on those rows: `openSource(id, { skipBuild: true })`, every stashed filter/search/tag/timeframe reset, then one view build (placeholder tables resolve through `POST /api/dashboard/resolve`, which lists every source a `{{all:…}}` spans so the analyst picks one; a widget with SQL but no drill opens as a query in the SQL pane; a bucket the timeframe can't express is refused, and a bucket on a column not typed datetime filters by the label's prefix instead). The shipped KAPE drills are checked against their SQL on a fixture in tests/test_dashboard_drill.py: a stat's drill opens exactly the rows it counted. Hand-editing a recipe's SQL drops `build` and `drill` rather than leaving them describing a query they no longer match. Entry points that skip the editor: the column header menu (top values / distinct / over time), the row menu (count of this value) and the Filters menu (count of this view), all through `quickAddWidget`, which asks which board only when there are several. `createDashboard` offers a starting point — blank, a starter built from the open table (`buildStarter`: count, activity window, over time, top values of 2–12-distinct columns), a shipped board, or a library board. Layout lives in the case .db; 'Save as profile' extends a plugin bundle with the board. The shipped KAPE triage board carries hand-written drills (checked against the header sets in tests/test_dashboard_drill.py). See docs/design/analysis-suite.md.
