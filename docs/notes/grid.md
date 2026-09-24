# The grid: rendering, paging, selection, geometry

the virtualized grid (`static/js/grid.js`) — what's in the DOM, what's in the page
cache, how a scroll position becomes a row, and how a selection is
represented. Invariant #6 (only the visible window is ever in the DOM) is
the rule; these are the things that bit us enforcing it.

Part of the working notes split out of [CLAUDE.md](../../CLAUDE.md) —
see [docs/notes/README.md](README.md) for the whole set.

---

---

- **The "no rows match" overlay is derived, never assigned.** `#noRows`
  used to be written in one place (`installView`) and cleared in another
  (`openSource`), which meant it outlived the view it was an answer
  about: it sat on screen for the whole of the next build, telling the
  analyst there was nothing to find while the search that would find it
  was still running. Search-all's "Open ↦" hit it every time, because
  that path built twice. `view.syncNoRows()` is the one writer now — no
  overlay while this table has a build in flight or a search running in
  the background (the stats line carries that state, in words), otherwise
  whatever the live view's count makes true. Call it rather than touching
  `hidden`, and call it after the state it reads has settled.

- **Pinned columns are `position: sticky`, not a second pane.** A column
  the analyst pinned (Alt-click its header, or the columns panel) keeps its
  place in the flex row and gets `left: <gutter + widths of the pinned
  columns before it>`; `columns.pinnedOffsets()` computes that once per
  render and the three sites that draw cells — header, filter row, body —
  all place them through `applyPin`. Columns are deliberately NOT reordered
  to pin them: sticky keeps an element in flow until it would scroll past
  its offset, so an unpinned column between two pinned ones simply slides
  underneath, and reordering would also have silently changed export column
  order, which follows the arrangement.
- **A one-column table is its column.** `colWidth` sizes an unset text
  column from its header (`name.length * 9 + 30`, 90–360px), which made a
  log imported one line per row — nothing but `Message` — a 93px column
  with every line cut to `2026-03-1…` and the rest of the grid empty. When
  the table has exactly one base column (`columns.fillsGrid`) that column
  takes the grid body's width minus the gutter instead, and the resize
  handler repaints the header as well as the rows so the two stay the
  same width. A dragged width (`S.layout[name].w`) still wins, and
  columns derived from it keep the ordinary defaults — it is the base
  column count that decides, not the visible one, so extracting a field
  from the log doesn't snap Message back to 93px.
- **A pinned column is not immovable.** It scrolls with everything else
  until it reaches its offset, then holds — so a test asserting "it did not
  move" is only right once it has parked. Assert across two scrolls, with
  the first derived from the column's own `offsetLeft` rather than a fixed
  number: where a column sits depends on the order, which the layout
  remembers.
- **Opaque backgrounds on pinned cells are load-bearing.** A transparent
  one shows the columns sliding underneath straight through it, and each
  row state (even, hover, selected, cell-selected) needs its own opaque
  colour for the same reason.
- **Row selection (`static/js/state.js`) is a flag plus a Set, never a list of every
  selected position** — `S.selectAll` off means `S.selection` holds the
  selected view *positions*; on means it holds the *exclusions* (everything
  else in the view is selected). Nothing outside the `sel*` helper block at
  the `sel*` block in state.js touches either field directly. Positions, not rids — but a rebuild no longer simply drops them:
  `rebuildView` asks `/api/view/keys` for the picked rows' ids while the
  old view is still there, builds, then `/api/view/positions` maps them
  into the new one and counts the ones it no longer shows
  (`S.selHidden`, the toolbar's "N filtered out"). Explicit picks only —
  a select-all is a statement about *this* view — and capped at 20,000. The inversion isn't just to avoid allocating a 1.2M-entry Set
  for "everything": it's what lets `applyTag` *recognise* a whole-view
  selection and hand it to `/api/row_tags/view` as one server-side set
  operation. That matters because the page cache only ever holds the pages
  you've scrolled through — the old `positions.map(rowAt).filter(Boolean)`
  silently dropped every selected row that wasn't cached, so "select all
  1.2M rows, press a tag hotkey" tagged a few hundred of them and reported
  that smaller number in a toast. **A partial tag must never be silent** —
  the analyst's record of what they've triaged is the thing being
  corrupted. So there are exactly two paths: whole-view (bulk endpoint,
  with any unchecked rows passed as `exclude` — server-side so an excluded
  row that legitimately already had the tag keeps it), or an explicit
  subset (every page it spans is fetched *first*, and a failure is a toast,
  not a gap).
- **Grouped mode's rows are ordinary rows.** They paint through the same
  `buildDataRow` the flat grid uses and address themselves with the same
  `pos`, so selection, the cursor, the cell range, the row menu, copy,
  tagging and the detail pane all work in both modes off one
  implementation. `rowAt(pos)` is the pivot: a `S.rowsByPos` lookup when
  flat, `groupDataRowAt(pos)` when grouped. Five things follow from sharing
  the address space:
  - A grouped `pos` indexes the *flattened tree* (`S.groups` +
    `S.groupPrefix`), which interleaves group headers with data rows.
    `groupCoordAt()` answers "is there a row here" from the tree alone — no
    page fetch — which is what lets `selSetRange`, the click handlers and
    the context menu skip headers synchronously. `gridRowCount()` is the
    bound for that space; nothing should reach for `S.view.row_count`
    directly once a grouping can be on.
  - **Expanding or collapsing renumbers everything below the toggled
    header**, so `shiftGroupPositions` moves selection/cursor/anchor by the
    exact delta (positions inside a collapsed span are *dropped*, since
    those rows are no longer on screen). Re-resolving through row identity
    would be the obvious alternative and needs a lookup the tree has no
    index for. `selRemap` exists so this stays inside the `sel*` block.
    Capture the (headerPos, oldTotal) pair via `groupShiftAnchor` at the
    moment of the mutation, not at the top of `toggleGroup` — the expand
    paths await a fetch first.
  - The select-all *checkbox* stays disabled under a grouping: it means
    "every row in the view", and the flattened tree is a mix of data rows
    and headers whose collapsed groups aren't even loaded. `S.selectAll` is
    therefore never set in grouped mode, which is what keeps `selCount()`/
    `selHas()` honest there. Whole-view tagging still works — `Shift`+a tag
    hotkey, and the group menu's tag-this-group, are both server-side.
  - `loadRowsForPositions`/`positionsNeedLoading` are the mode-aware front
    ends to `waitForPages`; grouped pages are per-group-sub-view and can't
    share the root view's page index space. `waitForGroupPages` throws
    rather than returning short for the same reason `waitForPages` does.
  - **Two row caches, one view id, and a write invalidates the one it did
    not patch.** Grouping leaves the flat `S.pages`/`S.rowsByPos` alive
    underneath `S.groupPages` (`regroupAll` clears only the group cache;
    `S.view.view_id` doesn't change), so while grouped the flat rows sit
    off-screen with the `tags` arrays they were fetched with, and
    `dropGrouping` paints them straight back — `ensurePage` short-circuits
    on `S.pages.has(idx)`, so nothing refetches. A tag applied while
    grouped therefore showed on the rail and in the ribbon (both
    server-read) and not on the rows after Ungroup, until something else
    rebuilt the view. The rule now: a write the server did on this
    client's behalf (whole view, whole group, undo, the SQL pane's tag
    hotkey, the tag editor's Delete) calls `tags.clearRowCaches()`, which
    drops *both*; a write that patched row objects in place
    (`tagRowsAtPositions`, `saveNote`) keeps the patch — that is the
    instant feedback — and drops the flat cache when grouped, since the
    objects it patched were group-page rows (`saveNote` also drops it when
    the row under the cursor is no longer the input's row: it is debounced
    500 ms, and an Ungroup or a cursor move inside that window means it
    patched nothing). Clearing the flat cache from grouped mode is safe because no
    flat fetch can be in flight there (`schedulePrefetch` and
    `loadRowsForPositions` both route to group pages, and select-all is
    refused under a grouping), so the `S.pageGen` bump strands nothing.
    The same one-sidedness ran the other way — Shift+hotkey and Ctrl+Z
    cleared only the flat cache, so under a grouping by an ordinary column
    the rows on screen kept their old stripes — and the helper closes both.
    The visible cost is one round trip of `pending` placeholders on the
    next paint, the same thing a bulk tag already costs in flat mode.
- `waitForPages` has **no deadline and bounded concurrency**
  (`PAGE_FETCH_CONCURRENCY`), and throws rather than returning short. It
  used to fire one `ensurePage` per missing page at once — ~2,400
  simultaneous requests at a single-connection SQLite backend for a
  select-all copy — and give up after 8s, after which its callers emitted
  `''` for every row still missing. A clipboard full of correct-looking
  rows with quiet blanks in it is worse than a copy that fails. Both copy
  paths now cap at 20,000 rows and refuse a hole rather than papering over
  one.
- The page cache is capped at `MAX_CACHED_PAGES` (~100 pages / 50k rows),
  evicted furthest-from-viewport first by `trimPageCache`. Deep-scrolling a
  1.2M-row × 27-column view used to accumulate the whole table in the JS
  heap — the DOM has always held only the visible window (invariant #6),
  and memory now follows the same rule. Two sets are never evicted: the
  pages currently being painted (render() would refetch them on the next
  frame, and ensurePage calls render() on arrival — that pair loops
  forever), and any `keep` set an in-flight bulk copy/tag still needs. Both
  can exceed the cap, in which case nothing is evicted; it's a cap on idle
  scrollback, not a hard limit that could break an operation mid-flight.
- **Neighbouring pages are prefetched at idle** (`schedulePrefetch`,
  `PREFETCH_RADIUS`). A page is 5,000 rows, so crossing a boundary is rare
  — and when it happens the grid paints `pending` placeholders for a whole
  5,000-row round trip, which is the entirety of "scrolling feels
  sluggish". Three details are deliberate: it runs on
  `requestIdleCallback` (a prefetch competing with the page the viewport is
  actually waiting on would make the visible case slower to fix the
  invisible one); only one pass is ever pending and it reads the viewport at
  *fire* time, so nothing needs cancelling on a view rebuild; and a
  prefetched page that lands outside the visible range skips the `render()`
  every other arrival triggers — but checks rather than assumes, since the
  analyst may have scrolled onto it mid-flight. Grouped mode has its own
  pass (`prefetchGroupPages`) because it has two kinds of boundary: the
  next page inside a big expanded group, and the first page of the next
  expanded group.
- **The spacer that gives the grid its scroll height is capped**
  (`MAX_SPACER_PX`, 16M px) and above that cap `scrollTop` is no longer a
  row offset. A DOM element can't be arbitrarily tall — Blink clamps at
  33,554,365px (measured; 2^25 LayoutUnits), Gecko at ~17.9M — so
  `row_count * ROW_H` stops growing somewhere around 1.4M rows at 24px
  while the row count keeps going, and the tail of the view becomes
  unreachable: a 2,459,653-row `$J` table scrolled only to row ~1,398,090,
  hiding 43% of the evidence with nothing on screen to say so. So every
  conversion between `scrollTop` and a row goes through `vScroll()` (real →
  virtual) / `rScroll()` (virtual → real), and the rows block is positioned
  by `rowsPaintY()`, never by `first * ROW_H`. **Below the cap all three are
  exactly the arithmetic they replaced**, which is what makes them safe to
  apply everywhere — `render`, `renderGrouped`, `renderTimelineRows`,
  `visiblePageRange`, `scrollIntoView`, `recenterOnRow`, `applyDensity`, and
  `rebuildView` (whose kept scroll position is captured in *virtual* pixels,
  since the outgoing and incoming views can have different row counts and so
  different spacer scales). The non-obvious part is `rowsPaintY` subtracting
  the fractional part of the virtual offset: drop it and the top row snaps
  to the viewport edge, so the grid moves in whole-`ROW_H` steps instead of
  scrolling smoothly. Above the cap the cost is granularity, not reach —
  2.46M rows get ~6.5px of spacer per row instead of 24, so a wheel notch
  travels ~3.7x further; every row stays addressable (that needs 1px/row,
  which the cap doesn't reach until ~16M rows) and keyboard nav moves by row.
  Related, and the reason this was found at all: `#app`'s four grid children
  each pin their own `grid-row`. `#presetBanner` is `hidden` by default and a
  `display:none` item isn't placed in the grid at all, so under
  auto-placement `.main-area` slid into the 3rd (`auto`) track — harmless
  until the spacer passed the browser's ceiling, at which point that track's
  intrinsic size resolved to 0 and collapsed `.main-area`/`#grid`/`#body` to
  zero height. Correct row count, sticky header painted, not one data row.
- **The tag rail has its own gutter; it overlays nothing** (`.rail` and
  `.grid-body`'s `margin-right: var(--rail-w)` in style.css, `drawRail`
  in grouping.js). It is absolutely positioned against `.grid`, and for
  as long as it spanned the whole of it at `right: 0` it had to declare
  `pointer-events: none` — 14px of overlay across the vertical scrollbar
  swallows every grab of the thumb (the trap `.notes-divider` documents
  from the other side). The cost of that was silence: an element the
  pointer never reaches cannot show a `title` either, so the rail was
  the one surface in the app where a tag appeared as a colour with no
  name anywhere near it. **Giving up the overlay, not moving it, is what
  bought the tooltip back.** The scroller now ends a rail-width short of
  the grid and the strip stands in what it gave up, so it covers neither
  the thumb nor the right-hand column of cells, and a hover can name the
  tag a mark belongs to and the rows it stands for.

  Measuring instead was tried and is worth not repeating: setting
  `right` per draw from `#body`'s own box (`offsetWidth - clientWidth`)
  moves the strip a scrollbar's width *inward*, onto `.rows` — which is
  `min-width: 100%` of the padding box and therefore reaches
  `clientWidth` — so the rightmost 14px of every row went opaque and
  inert, no `.cell` under a mousedown, the browser's own menu on a
  right-click, and a cell-range drag stalling the moment the pointer
  crossed the strip (the drag listener is on `#body`, and the rail is a
  sibling of it). It also went stale: the scrollbar comes and goes on
  paths that repaint without redrawing the rail — `toggleGroup` calls
  `render()` and nothing else — so the first group expand put it back
  over the thumb. A reserved gutter needs no measurement and has no
  such path.

  Three consequences to keep: the marks are remembered keyed by the
  canvas row they landed on (`railMarks`), because hit-testing a 2px
  dash needs the arithmetic the paint used and a readout on every
  mousemove must not walk a list that is as long as the tagged rows are
  numerous; the hover's y is scaled by `cv.height / rect.height` before
  that lookup, since dragging the detail pane's divider resizes the
  strip without redrawing it and the bitmap stretches to fit; and the
  wheel is forwarded to `#body` by hand, since the rail is a sibling of
  the scroller rather than a child of it and a wheel over it would
  otherwise scroll nothing at all.
- **The row gutter and its header share one three-slot CSS grid**
  (`.gutter` / `.gutter-head` / `.gutter-filter`: checkbox | tag stripes +
  note mark | row number). The gutter used to be `justify-content:
  flex-end` over a variable child list, so the checkbox's x-position
  shifted from row to row depending on whether that row happened to be
  tagged or annotated, and the header's checkbox — left-aligned in a
  plain flex `.hcell` — sat above none of them. Only the middle slot
  flexes (`minmax(0, 1fr)`, so a long stripe run clips rather than
  pushing the number out of alignment); both edge slots are content-sized
  and therefore fixed down the column. **The three selectors must keep
  the same `grid-template-columns`, `gap` and horizontal padding** — that's
  the whole contract. `.rid` sets `grid-column: 3` explicitly rather than
  relying on sibling order, because a row still paging in has an empty
  middle slot and a rid placed by flow would land in column 1. `.gutter-head` also opts out of
  `.hcell`'s `cursor: pointer` and hover tint — it's the one header cell
  that doesn't sort. The select-all box's indeterminate state was already
  handled by `syncSelectAllCheckbox`.
- **The whole gutter is the row handle** (grid.js's `.gutter` mousedown).
  The 12px box and the digits were the only targets, with a dead strip
  between them that *cleared* the selection when hit. A mousedown
  anywhere in the gutter toggles that row; Shift+click adds the run from
  the anchor (Ctrl+Shift+click removes it) without dropping earlier picks;
  a drag selects the span from the press to the pointer — computed from
  the pointer's y (`rowAtClientY`), never from the element under it, so a
  fast drag can't skip rows and dragging back shrinks the span — with
  autoscroll past either edge. Picking is never clearing: `moveCursor`'s
  plain move keeps the picks (arrow keys are looking, not choosing), a
  cell click keeps them, a right-click outside them keeps them. They go
  with Escape, the toolbar's Clear, or a new pick; every gesture snapshots
  first so the toolbar's Undo can take it back. Shift+click on a *cell*
  is the cell rectangle and nothing else — it used to also pick rows,
  which is why Ctrl+C and a tag key disagreed about "the selection";
  `Shift+Space` turns a range into picks, `Space` toggles the cursor row,
  and with nothing picked a multi-row cell range is the scope of a tag
  key and the row menu alike (`rowMenuTargets`), and the toolbar says so.
  One anchor (`S.anchor`) serves the gutter, the cells and the keyboard.
- **The cell cursor is three fields, and they go together.** `S.cellAnchor`
  is the corner a Shift run extends FROM, `S.cellFocus` is the active cell
  (the moving corner, the one wearing the `.cell-active` ring), and
  `S.cellRange` is the normalised rectangle between them. Eleven places
  drop a cell selection — a rebuild, a table switch, Escape, Ctrl+A, the
  gutter, the select-all box, a group toggle, `renderHead` — so they all
  go through `clearCellSelection()` rather than nulling fields by hand;
  before that they were eleven chances to clear two of the three and leave
  a rectangle with no corners.
- **`S.cellFocus` carries its column's NAME as well as its index**, and
  that is the whole reason an active cell survives a repaint. `col` is an
  index into `visibleCols()`, a list that hiding, reordering or pinning a
  column rewrites — so `renderHead` drops the rectangle (its far corner
  may be in a column that just left) and puts the active cell back by
  name, or not at all. Restoring by index instead moved the ring onto
  whichever column slid into that slot, which is worse than losing it.
  This covers repaints only — a resize, a pin, a reorder, a hide,
  revealing a filter box. A filter edit or a sort REBUILDS, and
  `rebuildView` drops the cell selection outright: after a re-sort "row 4"
  is a different row, so a cursor left on it would point at evidence the
  analyst never chose. Row picks survive a rebuild because `selRemap`
  re-finds them by rid; a cell cursor has no such identity.
- **`S.cellRangeExplicit` is the word "explicit" from the copy rule,
  written down.** `handleCopyShortcut` always preferred *an explicit
  rectangle* to picked rows, and before the arrow keys moved the cell
  cursor `if (S.cellRange)` said exactly that — only a mouse gesture could
  make a range, so its existence WAS the intent. A plain arrow now leaves
  a one-cell range wherever the cursor stops, so the flag distinguishes
  asking from standing: a click, a drag, a right-click and a Shift+Arrow
  set it; a plain arrow and a jump through `moveCursor` clear it. Without
  it, an analyst who picked forty rows in the gutter and pressed Down to
  read the next one found Ctrl+C had become "copy one cell". Inferring it
  from SIZE instead (a one-cell range is never explicit) was the first
  attempt and it was wrong in the other direction: clicking a single cell
  to copy it is a real gesture, and it stopped working whenever rows
  happened to be picked.
- **Arrow keys step over group headings.** A heading owns a position but
  has no cells (one spanning element), so an active cell on one is a ring
  on nothing, and `cellRangeRows` skips headings anyway — a range that
  started on one would report rows it did not cover. `nextCellRow` and
  `edgeCellRow` walk past them, which also means Ctrl+Down in grouped mode
  lands on the last ROW rather than the last heading.
- **Horizontal scroll-into-view is arithmetic, not `Element.scrollIntoView`**
  (`scrollColIntoView`). The gutter and every pinned column are
  `position: sticky` over the left edge of the scroller, so a cell scrolled
  flush to `scrollLeft` parks *underneath* them and the ring vanishes. The
  real left edge is `GUTTER_W` plus the widths of the pinned columns —
  the same sum `pinnedOffsets()` already computes for the paint.
- **`cellRangeRowCount()` exists because the toolbar asks every paint.**
  Ctrl+Shift+Down makes the answer "the whole view", and materialising
  200,000 positions per scroll frame just to read `.length` off them is
  the difference between a smooth grid and a janky one. Ungrouped it is
  arithmetic; grouped it still has to walk, since headings do not count.
- **A cell says what it says only when it was cut** (`titleClippedCells`,
  the last thing every paint pass does). A `LEVEL` column rendering
  `Informati…` carried an empty `title`, so the value was reachable by
  widening the column or opening the row and nowhere else — while tabs,
  sidebar rows and column headers have always carried one. The check is
  `scrollWidth > clientWidth + 1` per painted cell, which forces one
  layout: it therefore runs *after* `replaceChildren` (a node still in a
  DocumentFragment has no geometry — scrollWidth is 0) and *last* in the
  pass, because the loop only reads geometry and writes `title`, which
  cannot dirty layout, so the frame pays for the reflow it was going to do
  anyway. The 1px of slack is what keeps an autofit column — sized to
  exactly its content, then rounded — from titling every one of its cells
  with the text already on screen. Deliberately not set unconditionally:
  the grid's rule is that only the visible window is in the DOM
  (invariant #6), and a title on every cell of every row is a lot of
  string for the majority that fit.
- **The row says how it opens, in the gutter's middle slot.** A single
  click selects a *cell*; the detail pane opens on double-click or the
  detail hotkey, and that was written down in one source comment and
  nowhere else. `buildDataRow` paints a `.row-open` on every landed row
  (not on a `pending` one — there is nothing to open yet), CSS keeps it
  `visibility: hidden` until `.row:hover`, and the gutter's mousedown
  takes it *before* the pick, then `moveCursor` + `showDetail` — the
  cursor has to follow, since the pane, its note box and "Copy row" all
  read the cursor row. It shares the middle slot with the tag stripes
  rather than adding a fourth track, because the three-slot template is
  what keeps the checkbox and the row number aligned down the page; it
  carries the gutter's own `background: inherit` so a long stripe run
  passes behind it. Like every other child of the gutter it names its row
  as well as its column (`grid-area: 1 / 2`) — grid auto-placement is
  sparse, so a child whose definite column sits left of the cursor opens a
  new implicit row instead of backing up, and naming column 2 alone made
  the gutter two rows tall: the checkbox and the digits off centre on
  every landed row, and the glyph itself overhanging the row below, where
  the click selected that row rather than opening this one. Remote session mode hides it: appearing on hover is a
  repaint to encode on every mousemove, which is the whole point of that
  mode (double-click, the hotkey and the hint still work). The hint —
  one line beside the row count — is `detail.js`'s
  `syncRowOpenHint`/`noteRowOpened`, keyed on `winnow.rowOpenSeen` in
  localStorage and marked from `showDetail`, so it answers "has a row ever
  been opened", not "was this affordance clicked".
- **The stats line describes the rows, not the machinery.** `statsLine`
  has one writer, `writeStatsLine`, and `openSource`'s cached path goes
  through it too. That path used to end the sentence with `· cached`,
  which put "the materialised view was still alive server-side" into the
  one sentence that says what is on screen — and only on the reopen path,
  so the same 6,000 rows described themselves two ways depending on
  history the analyst had no reason to track. The fact is kept in the
  line's `title`, and the build time it shows there is the time that build
  really took.
