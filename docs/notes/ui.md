# UI surfaces: menus, filters, settings, tabs, keybindings

Everything in `static/js/` and `static/style.css` that isn't the grid
itself: the right-click menus, the filter row and its value picker, saved
filters, the timeframe filter, tab strips and the sidebar, Settings, and
the keymap.

Part of the working notes split out of [CLAUDE.md](../../CLAUDE.md) —
see [docs/notes/README.md](README.md) for the whole set.

---

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
  `rowMenuTagItems` does. The throwaway view is safe to drop immediately
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
  is a **folder tree** now, not the old Open/Closed split: every table sits
  at the root or inside a folder (`source_folders`/`source_folder_map` in
  the case file — see store.py), open state reduced to a per-row treatment
  (the active highlight and the ✕ close). Folders are created/renamed/
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
- **The right-click surfaces** (row menu, column-header menu, table menu,
  header value picker) all hang off one floating-menu implementation in `static/js/ui.js` —
  `showFloating`/`placeFloating` plus the single `openMenuEl`/`openMenuAnchor`
  pair, with `dropdownMenu` (anchored under a button), `contextMenu`
  (positioned at the pointer) and `anchoredPanel` (a card with real
  controls in it) as the three entry points. That's what makes "only one
  of these is open at a time, and Escape closes it" true across all of
  them rather than four near-copies of the same two listeners. The
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
  skipped, separator and all. The row is re-resolved (`rowAt(ctx.pos)`) on
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
- **`clearAllFilters(seed)`** — Shift+F ("filter to this value and drop the
  rest") is that reset plus one filter, so it goes through the same
  function rather than a second implementation that would forget the
  carve-outs: the timeframe filter survives, grouping is stashed into
  `S.lastGroupBy` rather than lost. Note `$('btnReset').onclick` is now a
  wrapper — passing `clearAllFilters` directly would hand it the MouseEvent
  as `seed`.
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
