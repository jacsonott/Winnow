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

- **The tag editors save as they change, and never send an empty name.**
  Edit tags and Settings → Default tags for new cases are the same row
  (`tagEditRow` in tags.js): the colour and the key save at once, the name
  after `TAG_NAME_SAVE_MS` of quiet or on leaving the box, and the row's
  own slot says "Saving…"/"Saved" (the row note's idiom) — a refusal is a
  toast with the server's words and "Not saved" left on the row. Three
  things are load-bearing. An empty name is held, not sent: the template's
  server side (`TagTemplate.save`) silently drops nameless entries, so a
  pause while retyping a name used to be one debounce away from deleting
  the row being edited; leaving the box empty puts the last saved name
  back. The template is one file written as a whole list, so its saves
  are chained one at a time and `tags` holds only what each row has
  *committed* — another row's save must not send a name still being typed.
  And anything that redraws a list (Add tag, Delete, Apply default
  template, the template's + tag and ✕) awaits every row's `flush()` first,
  or the redraw paints the old name and the next edit to that row builds
  on it. The pending timer outlives the dialog on purpose: a name typed
  and closed straight away still lands. The editor never checked for two
  tags on one digit and still doesn't — the wiki documents that the
  higher one in the list wins (`tests/ui/test_tag_editors_save_on_change.py`).

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
  in tables.js exists for the same reason on the polling path. The plugin
  context's `refreshSources()` is one: it was a plain `loadSources()`,
  harmless while First/Last's Create table always called `openSource` on
  the new table right after, and wrong once a create could land while the
  analyst was on the grid with a filter set — it reset the filter, and
  pulled anyone still on the First/Last tab to the grid.

- **Row striping is a preference, saved per skin** (`STYLES[].stripes` is
  the default, `S.appearance.stripes` holds only the skins the analyst has
  overridden; `stripesOn`/`setStripes`/`paintStripes` in settings.js). It
  is per skin because the skins disagree about it by construction — Jenna's
  theme reconstructs a grid with no zebra, the rest are drawn around having
  one — so one global switch would be wrong for half the list the moment it
  was set. Off is a single token: everything that paints the zebra reads
  `--row-even` (the grid's rows, the pinned cells tracking them, the
  Timeline's rows), so `html[data-stripes="off"]` collapses it onto `--ink`,
  the base both surfaces paint. That rule carries three attributes
  deliberately — the skin blocks set `--row-even` at
  `[data-style][data-theme]`, and a bare `html[data-stripes="off"]` is the
  weaker selector and loses to every one of them, invisibly, because a
  custom property that loses a cascade looks exactly like one never set.
  Pinned by `tests/ui/test_row_striping_setting.py`, which reads painted
  backgrounds rather than the token for that reason.

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
  that shrinks (`flex: 0 1 auto`, 60px floor). A pressed segment in a
  dialog or panel (`.vp-seg`, `.segmented`, `.pl-scope`, the search
  dialog's Paste/Advanced) is the builders' tint — 16% accent over a
  transparent unpressed row, `--text` on it — and not accent text, which
  is what failed: accent on the dim accent fill measured 1.4–2.7:1 across
  the skins, and accent on the panel fails in Phosphor light. It was the
  filled accent for a while, which read but outshouted each dialog's one
  `.btn.primary`. The unpressed buttons have to be transparent for the
  tint to show: on `--panel-2` it sat 3.7 ΔE from them in Harvest light.
  Toolbar toggles (`.btn[aria-pressed]`) keep the accent-dim fill.
  `tests/ui/test_visual_pass.py` measures text contrast and the
  pressed/unpressed gap in every skin and theme.

- The **timeframe filter** (`S.timeRange`, `static/js/timeframe.js`, `time_range` on
  `ViewSpec`, compiled in `_compile_where` via the registered SQL function
  `TS_NORMALIZE`) is deliberately a separate piece of state from every
  other filter mechanism, and every place that resets "the filters" —
  `clearAllFilters()`, `applyPreset()`, opening a different source (unless
  the analyst turned that off; see the next entry) — is written to skip it
  on purpose (see the comments at each site). It's
  meant to stay pinned while everything else changes underneath it, and
  toggles on/off via its own keybind (`toggleTimeRange`) rather than
  needing its config modal reopened. **Apply always turns it on**; the
  dialog has no Enabled box. It had one, and a range typed in with the box
  unticked was saved but not applied, so Apply looked broken. Off is
  Clear (range gone) or the toggle key (range kept); the dialog says so
  when it is opened on a range that is off. `column: null` means "every datetime
  column on whichever table is open, OR'd together" — the MFT case this
  exists for: a timestomped Created date shouldn't hide a row whose
  Modified date is genuinely in range. `TS_NORMALIZE(x)` (a zero-padded
  `"YYYY-MM-DD HH:MM:SS"`, same ISO/US shapes as `DAY_BUCKET`/
  `parseTimestamp`) is what both the column values and the start/end
  bounds get compared through — a bare text/numeric comparison on the raw
  stored value sorts the US `M/D/YYYY` shape wrong.
- **Following the analyst between tables is a preference**
  (`timeframeAcrossTables` in timeframe.js, `S.appearance.timeframeAcrossTables`,
  default on; the switch is in the Timeframe dialog and in Settings → Filter
  & search syntax, both writing the one key). On is the behaviour above.
  Off, `openSource` treats the range like the other per-table state: the
  per-tab stash always records `timeRange`, and on a real switch
  (`leaving`, never a refresh of the open table — every refresh idiom
  re-enters `openSource`, and must not drop a range just set) it puts back
  the arriving table's, or none. The callers that clear the timeframe for
  a count-faithful landing (dashboard drill, search-all "Open ↦", the
  session-diff pivot) still work in either mode because each clears it
  AFTER `openSource(…, { skipBuild: true })` has restored the stash, not
  before. It is a machine preference like `openNewTables`, so it is in
  `workspace.AppSettings.APPEARANCE_KEYS` too — leave it out of that set
  and the server drops `false` on the floor, and a new origin boots back
  into "on". `updateTimeRangeButton` reads it during boot, before
  `initAppearance` has set `S.appearance`; the helper guards for that, and
  a version that didn't left the page blank.
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

- **And it says both that it is working and that it stopped.** The
  re-asking above fixed the staleness; what stayed broken was that none
  of it was visible. `draw()` mentioned loading only in its
  nothing-to-draw branch, so the first chart said "Loading…" and every
  one after it repainted the previous answer byte-for-byte while a new
  request was out — and a chart one filter behind looks exactly like a
  chart that has stopped, which is how one gets reported as the other.
  `aria-busy` on the panel drives a breathe on the count line while a
  fetch is out; deliberately on the CHROME, because a canvas does not
  inherit CSS and anything drawn into it would need a redraw on a timer
  to animate. The kill switches at the end of style.css (reduced motion,
  remote session) already cover it. When the bounded re-asks run out the
  strip keeps the old chart — an empty strip mid-filter is worse than a
  slightly old one — and appends "showing the previous filter" to the
  count line, because a chart that is quietly wrong is worse than one
  that is obviously absent.

- **…and an old chart is faded from the moment the view starts to
  change, not from when the new answer is asked for.** The breathe above
  was the only cue, and it started late: the strip asked only on
  `winnow:viewchange` (after the build, the seed fetch, the remap, the
  regroup and the paint) plus `REFRESH_MS`, and the answer took seconds
  on a big table, so for all of that the previous view's chart sat at
  full strength under the new view's rows — scrolling moved nothing on
  it — and then "suddenly updated". Now `fresh()` decides whether the
  chart is the answer for the grid as it is now; when it is not, the
  canvas is faded and grey (`.is-outdated`, after a 120 ms beat so a
  small table never blinks), the position marks and the tag legend are
  dropped, the drag and the hover are off, and the count line says
  "Updating…". Not fresh means a rebuild in flight (`rebuildInFlight()`,
  so view.js's own notion, not a second flag), a landed view whose answer
  is not back, or a column or split changed and not yet answered. view.js
  says when: `histogramFollowBuild()` as a build starts and whenever it
  ends without landing (cancelled, superseded, refused, detached — the old
  view and its chart are current again), and `histogramViewLanded()` from
  `installView` the moment `S.view` is swapped, which is the ask. Not
  earlier: asked as `POST /api/view` returned, the aggregate (a Python
  function per row, so it holds the GIL) took the grid's seed page from
  ~2 ms to 40–80 ms on a 500k-row table. The viewchange listener skips
  its ask when the view was asked about as it landed (`askedFor`); a 409
  clears `askedFor` so the landing asks again. The strip is still not
  emptied: one that collapsed to a line and back would move the grid
  under the pointer on every filter change.
  tests/ui/test_histogram_fades_while_updating.py.

- **A case switch resets the histogram strip; it does not wait for a
  view change.** The strip only ever redrew on `winnow:viewchange`, so
  after opening another case it went on showing the previous case's
  chart — its bars and its row count — until the new case's first view
  landed. A case that opens on no table (a new one, or every tab closed)
  never builds a view, so that was indefinitely; a large one, for as long
  as its first build took. `openCaseNow` calls `resetHistogram()` with
  the other per-case resets: it drops the chart, bumps `seq` so an answer
  still out about the old case can't land, and sets `opening`, under
  which the strip says "Loading…" and does not ask — `S.view` and
  `S.sourceId` still name the previous case's table then (loadSources
  keeps `S.sourceId` on purpose). The next view change clears it; so does
  `refreshHistogram()`, which loadSources' no-table branch calls because
  no view change is coming from there — the same branch the last tab
  closing within a case goes through, which had the same stale chart.
  tests/ui/test_case_switch.py and test_histogram_empty_state.py.

- **A case switch must not stash what is on screen.** `openCaseNow`
  clears the per-tab stash (`clearViewStateStash`), then `loadSources`
  opens the new case's first table, and `openSource` stashes the table it
  is leaving before it reads the arriving one's. `S.sourceId` is kept
  across the switch on purpose, so that stash wrote the previous case's
  filters and search under the old id; ids restart at 1 in every case, so
  the arriving table almost always had that id and opened with them,
  usually showing 0 rows. `clearViewStateStash` now makes the next
  `stashViewState` a no-op (`stashOnScreen`). Nulling `S.view` or
  `S.sourceId` in `openCaseNow` instead would also stop it, but both are
  read by the rest of the switch: loadSources picks its tab from
  `S.sourceId`. tests/ui/test_case_switch.py.

- **Coming back to a table must call off any build already running for
  it.** `openSource` clears the filters, search and sort, awaits the
  table's layout (and its derived columns and tags), and only then puts
  the stash back. A rebuild that starts in that gap builds the whole
  table. When the return then finds its view cached, nothing used to
  supersede that build, and installView's checks (the newest seq, the
  same table) let it land afterwards: the grid showed every row under a
  filter box that still said `=4624`, the scroll went back to the top,
  and the server evicted the restored view. The cached path now calls
  `supersedeBuild()` (view.js) first, which bumps `rebuildSeq`, cancels
  the build in flight and drops its indicator, as a new build would. The
  rebuild path needed nothing, since its own build supersedes. What
  started the stray build in CI was the header filter box: typing arms
  `rebuildSoon` (220 ms) and Enter or Escape builds at once, but neither
  called the debounce off, so every filter committed with Enter was
  built twice, and the second build could fire mid-switch. Both keys
  now cancel it, as the search box's Enter always did with `searchSoon`.
  This was the intermittent `scrollTop === 240` timeout in
  tests/ui/test_view_state.py, which also pins each half on its own.

- **Opens run one at a time, and the case clicked last wins.**
  `openCase` (home.js) used to hand ANY call made during an open the
  in-flight promise — right for a double-click, but a click on a
  different case after going Home was dropped, and the case that ended
  up open was the one clicked first. Now a call for the same case gets
  the promise (and marks it `reasked`), a call for another case waits
  in `caseNext` (newest replaces older, with the busy bar up), and the
  open in flight stops short at its next `superseded()` check — after
  the per-case resets, which must always run, and before each load the
  next open would only reset again — resolving `SUPERSEDED` so the loop
  runs the queued one. Each open still runs the whole `openCaseNow`;
  never start a second one beside it.

- **An open does not take the analyst back off the home screen.**
  `showApp()` at the end of an open the analyst had left for Home (the
  `homeVisits` counter, bumped by every `showHome`) put the app up over
  the case list mid-pick, and the click meant for a case row landed on
  the app. The case still loads out of sight and shows when it is asked
  for again: by a click on it while the open is still running
  (`reasked`, revealed after `loadSources`), or by a fresh `openCase`,
  which the server short-circuits as already open.

- **The Timeline pane's note is saved before the switch, then
  forgotten.** It is the detail pane's `#noteInput` hazard in another
  box: a note inside its 500 ms debounce posted after the swap, under the
  NEW case's token, onto whichever row of it shared the `{source_id, rid}`. Blanking
  would lose it, so `openCaseNow` awaits `flushTimelineNote()` before the
  `/api/case/open` POST — the server still has the case it was typed in
  — and `resetTimelinePane()` drops the shown row, the pending note and
  any row still on its way afterwards. tests/ui/test_case_switch.py.

- **The histogram strip** (`static/js/histogram.js`, `GET /api/histogram`
  over `Store.time_histogram`, toggled by `#btnHistogram` or `h`) was the
  `table_histogram` example plugin until 2026-09 and is built in now;
  three things from its life as a plugin are worth knowing before touching
  it. **A 409 from the route means mid-rebuild, not an error**: the strip
  fetches 150 ms after `winnow:viewchange`, and a second rebuild in that
  window evicts the view it asked about — so on a 409 it keeps what is
  drawn rather than blanking the chart, then compares the view the answer
  was about with the view the grid has and asks again when they differ —
  the evicting rebuild fired its own view change before the 409 came back,
  so nothing further is coming (the entry above has that forensics). A view
  that is still the grid's and still answering 409 is re-asked `RETRY_MAX`
  (3) times, after which the strip marks itself stale and appends "showing
  the previous filter" to the count line. Only an 'expired' KeyError is a
  409; an unknown column (a derived column just removed) or a
  non-datetime one is a 400 the strip shows as text in
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
  and every case switch). Prefs are `winnow.histogram` `{open, column,
  view, groupsTop, valueFollow}` per browser (`stack`, the Tags toggle's
  flag from before there were other views, is still written beside
  `view` and read when `view` is missing); the plugin's
  `winnow.panels['table-histogram.histogram']` is migrated to `open`
  once and deleted.
- **The strip's color-by views — Tags, Value, Groups — are one pref,
  one pressed at a time** (histogram.js `histogramView`; the store side
  is in store.md). Five things to know before touching them. **Value
  keeps the value it was shown** (`showValueInHistogram`, from "Show in
  histogram" on the row menu's value block), **unless it follows the
  selection** (`valueFollow`, a right-click on the Value button). It was
  follow-only at first, and a chart that changed under every click was
  the wrong default for reading the rows around a finding. Following is
  **by identity, not position**: every move assigns `S.cellFocus` a new
  object, and `followSplit` (run from `histogramFollowGrid`, so on every
  grid render) takes the focused cell's value only when that object
  changes. Keyed on position, a view rebuild that left the focus at the
  same position — now naming another row — re-charted a value nobody
  picked. A focus on a group heading, or none, keeps the last value.
  **Only a followed cell waits before asking** (`SPLIT_MS`, so holding
  an arrow key asks once where it stops); a button, First N, the menu
  item and new groups ask at once — the debounce on everything was 200 ms
  of the Groups view's "slow". **Groups' colors follow the value, not
  its rank** (`groupSlots`): a filter that reorders the first groups
  must not repaint the survivors, or the color an analyst learned for
  one group means another after it. A value keeps its slot while it
  stays in the first N; one that joins takes the lowest free slot; the
  map resets with the column or N. While a regroup has emptied
  `S.groups`, `lastGroups` stands in, so the chart does not drop to
  plain and back. **The values ride in the GET's query string**, so
  `SPLIT_JSON_MAX` caps their JSON: a value holding a whole document is
  not charted (the legend says so), and a group past the cap falls into
  "Other" — without it the request line outgrew what uvicorn accepts.
  **The group headings ask the strip for their color**
  (`histogramGroupColor`, from `renderGroupHeaderRow`), and only while
  the strip is on screen, so anything that changes the answer repaints
  the grid: the view buttons, First N, and the strip's show/hide edge.
  The palette is `--series-1…8` in style.css, per theme rather than per
  skin, because its order is what keeps neighboring segments apart for
  color-blind readers and was checked as a whole. The row menu is nine
  items at most since Show in histogram joined the value's verbs
  (tests/ui/test_row_menu_submenus.py). tests/ui/test_histogram_value_views.py.
- **The strip marks where the grid is** (histogram.js `gridMarks`,
  grid.js `onScreenRows`). The rows on screen — no overscan, none under
  the sticky header — are a band when their times run one way (a veil
  behind the bars and a thumb in the 6px lane above the plot), and a
  tick each when they do not; the row cursor is a 1px `--text` line on a
  `--panel` halo through the plot. Five things to know before touching
  it. **It reads the times out of rows the grid already holds**
  (`parseTimestamp` to epoch, the client twin of TS_NORMALIZE), so a
  scroll never asks the server: `render()` and the grouped path call
  `histogramFollowGrid`, which looks once per frame and repaints only
  when a mark would land on another pixel — `paintChart` is split out of
  `draw` so that repaint leaves the legend and the count line alone.
  **Band or ticks is decided from the rows, not from `S.sort`**: tool
  output usually arrives in time order, and sorted by Host the ties fall
  back to rid order, so a screenful of one host IS one stretch of time —
  keyed on the sort, both would have got ticks. **The veil goes behind
  the bars and the cursor line in front.** Over them the veil tinted every
  bar it crossed, and bar colour is what the tag split is read by (and
  what test_histogram_panel samples); behind them a 1px line vanished
  into the busiest bucket, and without the halo it still did, between two
  tall bars. **The cursor has no mark in the lane**: it is usually one of
  the rows on screen, and a notch there sat on the band's thumb and hid
  it. **Times outside the chart's span are dropped, not clamped**: between
  a rebuild and the strip's next answer the rows are the new view's and
  the axis is the old one's. tests/ui/test_histogram_position.py.
- There's no separate "preset" concept anymore — a preset is just a saved
  filter (`workspace.SavedFilters`, cross-case) whose `col_names` happens to
  match (exactly, or "similar" per the same Jaccard/subset heuristic the old
  case-scoped `filter_presets` table used) the table just opened. The match
  (`checkPresets`/`matchingSavedFilters` in `static/js/savedfilters.js`) is
  computed entirely client-side against the already-loaded `S.savedFilters` —
  no request — and it surfaces as a state of the Filters button rather than a
  row of chrome of its own: an accent ring on the button
  (`updateFiltersButton` in `static/js/timeframe.js`), and the matches
  themselves listed in its dropdown under a "For this table" header, below a
  separator and the fixed entries. A case file saved before this change may
  still have rows in the old `filter_presets` SQLite table;
  `Store.pop_legacy_presets()` reads and clears it once on open, and server.py
  folds whatever it finds into `WS.filters`. Nothing writes to
  `filter_presets` anymore — it stays in the
  schema purely as a one-way migration source for old case files.
- A header-set **nickname** (`workspace.HeaderNicknames`, `header_nicknames.json`)
  is a separate tiny store from `SavedFilters`, not a field on it — several
  saved filters commonly share one header set (e.g. five different EVTX
  filters), and should all pick up the same nickname rather than needing it
  set per-filter. Keyed the same way as `ColumnLayouts` (sorted, lowercased
  column names) — saving again for the same set overwrites in place.
- **An excluded tag is a `'!<id>'` entry in `S.tagFilter`, not a field of
  its own** (`tagExcludeKey` in tags.js; the chip's right-click menu and a
  tag group header's Exclude write it). Ten-odd places copy, stash, reset
  or replay `S.tagFilter` — the per-table stash (`stashViewState`, put back
  by `openSource`), the background search's `viewStateSnapshot`,
  `clearViewNarrowing`, `landOnFilters`, a dashboard's count-of-this-view
  drill and its reset — and a separate `S.tagExclude` would have had to be
  added to every one, with the one that got missed showing a table
  "excluding" rows it no longer was, or the reverse, with nothing on screen
  to say so. In the same list it travels for free. `_compile_where` ANDs the exclusions where it
  ORs everything else: "Suspicious, but not Benign" must drop a row carrying
  both, and an OR'd "not Benign" would bring back every untagged row. Any
  code that reads `S.tagFilter` as a list of tag ids has to skip
  `isTagExclusion` entries; anything that replaces it with one tag (the tag
  group's Filter to) keeps `tagFilterExclusions()`, since the rows it was
  asked to narrow to were counted without the excluded ones.
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
  `grid-column: 1; grid-row: 3 / -1` (it slides in UNDER the tab bar, so
  toggling it never reflows the top chrome). The chrome it tucks beneath —
  `.temp-banner` and `.bar` — spans `grid-column: 1 / -1` for that reason,
  while everything the sidebar sits beside (`.toolbar`, `.plugin-panels`,
  `.diff-banner`, `.main-area`) is `grid-column: 2` — so hiding it
  (`[hidden]`) collapses that column to zero width for free, nothing else
  occupies it. `renderSidebar` is called from inside `renderTabs()` itself
  (both of `renderTabs`'s callers — `loadSources` and the tab strip's own
  drag-drop handler — mean `S.sources`/`S.tabOrder` just changed), not from a
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
  state is per-browser `localStorage` (`FOLDERS_KEY`), filed under the
  case's token (core.js `pageCaseToken`): folder ids are numbered per case,
  and every case opened on one port shares that storage, so a single list
  collapsed folder 3 of whatever case came next. The folders themselves
  are case data that travels with the `.db`. Page tabs still get
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
  self-contained dialog: "Edit…" in a Saved filters row's ⋯ applies that
  filter (`applyPreset`) and *then* opens `openFilterBuilder(f)` with the
  record, so the row count behind the modal is live feedback on the change
  being made. The only thing the `editing` argument adds is an `Update
  "<name>"` button; everything else, including "Save as new…", is the
  normal builder. That button deliberately sends **only `payload`** —
  never `col_names`. A filter's header set is its identity for `[` / `]` cycle
  order and the suggested-filter state of the Filters button (see the
  saved-filters entries above), so re-binding it to whatever table happened to
  be open during an edit would silently move it out of the group it was saved
  for; "Save as new…" is the rebind path. `workspace.SavedFilters.update`
  replaced the old name-only `rename` with the same
  None-means-leave-alone partial-update convention `CaseRegistry.update`
  already used, so one method serves both a rename and a conditions
  re-save. Edit needs a table open (it applies the filter to preview it),
  hence the disabled entry, with "Open a table first" under it, when none is.

- **A list's per-row actions are a ⋯, built with `dropdownMenu`** — Saved
  filters and the Tables manager (`.btn.row-more`, borderless until
  pointed at). Five buttons a row was the complaint: the actions an
  analyst reaches for once in a while went into the menu, and only the
  everyday one stayed visible (the name itself in Saved filters, which
  applies the filter; the open tick in the Tables manager, whose ⋯ is
  the table's right-click menu, `tableMenuItems`). Pass the items as
  a *function* so a menu reads the row's state when it opens, not when the
  list was drawn — Move up/Move down are disabled at the ends of their
  header set. Those two exist so the order can still be changed without a
  mouse, which only works because a move puts the focus back on the same
  row's ⋯ after the redraw (closing a menu restores no focus by itself):
  Enter, arrows, Enter again. A list that **redraws on a timer** must not
  redraw under its own open menu — the Tables manager's 1.5 s poll
  replaced every row, which left the menu anchored to a detached button
  (and dropped a keyboard user's focus each tick). It now skips a tick
  while `openMenuAnchor` is inside the panes, and any tick whose sources,
  folders and indexes match what was last painted; a tick that does
  repaint puts the focus back on the same control in the same row
  (`keepingFocus` in tables.js, by `data-id` and `data-role`)
  (`tests/ui/test_tables_manager_row_menu.py`,
  `test_tables_manager_panes.py`).
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
- **A page tab can be popped out into a window of its own**
  (static/js/popout.js; asked for so the Pivot could sit beside the table
  it counts). The pop-out is the whole app booted at
  `/?popout=<page key>&owner=<main window id>`, with the main window's
  chrome hidden by `html[data-popout]` — deliberately not the main
  window's DOM rendered into a child window, which would share `S` but
  open every menu, dialog and toast (and run every document-level
  listener a plugin adds) in the wrong window. The server needed nothing:
  several windows on one case were already safe (per-window presence, the
  case token on every request — docs/notes/server.md). What it costs is
  four rules in the browser, each a trap for whoever adds the next page or
  route:
  - **One live copy of a page.** Every page's show function starts with
    `if (pageElsewhere(key)) return;` — in the main window that shows
    `#popoutPlaceholder` for a page that is out, in a pop-out it sends any
    page but its own to the main window. **A new page's show function
    needs that line.** Two Pivots would both save the same
    `winnow.tabState` row through a 500ms debounce, last write wins,
    silently. For the same reason the saves a page has pending are
    flushed before it moves either way (`flushPageSaves`: plugin tab
    state, the SQL tab's text, the Notes narrative), and on a pop-out's
    pagehide with `keepalive` — the window the page lands in reads them
    back as it mounts. The main window also waits `GONE_SETTLE_MS` after a
    closed pop-out's "gone" before it remounts, so that keepalive post has
    landed.
  - **Navigation goes to the main window, whole.** A pop-out has no grid,
    so `openSource`, `openFilteredRows`, `jumpToTimelineRow`, dashboard
    `drillInto`, `setCaseTimeRange` and showing another page each check
    `inPopout()` first and `sendToMain(name, …args)` to the `ACTIONS`
    table. Whole: opening the table there and then filtering here would
    filter nothing anyone can see. A new navigation needs an action.
  - **Writes are announced, from an allowlist.** Each window holds its
    own copy of the case (`S.tags`, `S.sources`, the row caches), so
    core.js's `api()` tells popout.js about every non-GET that succeeded
    (`onMutation` — a registry, because core.js imports nothing), and
    `MUTATION_KINDS` names the ones that changed case data. The rest stay
    quiet on purpose: most POSTs are reads with a body (view builds,
    previews, SQL runs) or this window's own bookkeeping (layouts, tab
    state), and a refresh in every other window per keystroke of those
    would be the bill. **A new route that changes tags, tables or notes
    needs a line there**, or the other windows stay stale until they next
    load. Changes no route of this window makes — a job finishing (jobs.js),
    a watchlist scan (finishScan) — call `announceChange` themselves. The
    listener reloads what it holds and repaints what is showing the way a
    visit to it would (`runRefresh`); the Timeline keeps its scroll
    (`buildTimeline({keepScroll})`), where a visit goes to the top.
  - **A pop-out belongs to the main window that opened it.** Messages on
    the `winnow.windows` channel carry that window's id (`family`), so a
    second main window on the same server neither drives nor hears
    another's pop-outs. The id is in sessionStorage so a reloaded main
    window keeps it — and **a pop-out must never ask for its own**:
    `window.open` copies the opener's sessionStorage into the new window,
    so it would find its owner's id and answer to itself. A main window
    says "leaving" on pagehide and "hello" when it boots; a pop-out waits
    `OWNER_GRACE_MS` for the hello (a reload) and closes otherwise, and
    also watches `window.opener.closed`, for a main window killed without
    a pagehide.
  - The window is opened **before anything is awaited**:
    `window.open('', name, features)` inside the click, then the saves are
    flushed, then `win.location.replace(url)`. A browser allows a window
    only while the click that asked is fresh; awaiting the flush first got
    it refused as a pop-up.
  - A pop-out boots with `S.activeTab` set to its page *before*
    `loadSources({navigate: false})`, which is what keeps that from
    opening a table (it opens one when nothing is on screen) and flipping
    the window to a grid it hides.
  - A case switch in the main window is **followed, not refused**: the
    pop-out's `caseGone` reloads it (`followCaseSwitch`) onto the case now
    open instead of covering it with "This window is out of date", and
    says nothing on the way out, so the main window doesn't remount the
    page for the moment until it says "here" again. A page the new case
    doesn't have (a board, a plugin switched off) gets a card saying so.
  - The look follows through the `storage` event on `winnow.appearance`
    (settings.js), and **Shut down**'s "other windows" count leaves out
    this window's own pop-outs (`otherWindows`). The keys that switch tabs
    or open Settings/Tables/Search all do nothing in a pop-out.
  - tests/ui/test_popout.py, including that a pop-out survives its main
    window's reload and follows it to another case.
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
  the POST (and puts it back if the open fails). The tag list is the same
  kind of per-case state: `openCase` (and `boot`) call `loadCaseTags`
  before `loadSources`, because `loadTags` only ran by way of opening a
  table — an empty case never did, and kept the previous case's
  definitions in `S.tags`, which the Watchlist's auto-tag picker and the
  Timeline's tag chips read (tests/ui/test_case_switch.py). `openSource` hides the
  pane only when it is actually **leaving** a table (`leaving = S.sourceId
  !== id`, decided before `S.sourceId` is overwritten): every refresh idiom
  — `loadSources()` with no select after a column add, a sidebar folder
  op or closing some *other* tab, `openSource(S.sourceId)` from the
  derived-column modal — re-enters
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
  **The tab id is not enough to say a result still belongs there**, so
  each run also holds its claim in `S.sqlRuns` (its op token, per tab) and
  checks it after every await: running again in the same tab, closing the tab
  (`closeSqlTab`) and opening another case (`openCase`, whose tabs are
  numbered from 1 again) all end the claim through `abandonSqlRuns`,
  which also aborts the request. Without it a slow run painted over the
  quicker re-run after it (and Copy/CSV/tagging took its rows), and a run
  started in case A landed under case B's "Query 1", where tagging one of
  its rows wrote a tag into case B.
  `wireDragReorder` grew optional `currentIds`/`onReorder` callbacks
  (defaulting to the source-tab behaviour) so the sub-tab strip reuses the
  one DnD implementation; its `drop` handler now *returns* when the
  dragged id isn't in the target surface's own id list, which is what
  stops a SQL sub-tab dropped on the source strip from being spliced into
  `S.tabOrder`.
- **The SQL result table builds only what the pane shows**
  (`sqlResultNodes` in plugins.js) — invariant #6 for the SQL pane, both
  axes. It used to build the whole result as one `<table>` and rebuild all
  of it on every row click: `SELECT *` on a 150-column table is 5,000 x 150
  = 750,000 cells, measured at 45 s to appear and 42 s per click (20
  columns: 3.5 s and 8 s), with the page frozen throughout. Now a paint is
  the rows in view plus `OVERSCAN` each way (the next entry) and the
  columns in view plus `SQL_OVERSCAN_PX` each way; the result's own
  scroller and a ResizeObserver on it re-paint that window, and a click
  only moves `.sql-row-sel` on the rows already built. Things that bite:
  - **The header always holds every column**; only body rows are
    windowed. A row too wide for the pane starts with one `td.sql-gap`
    spanning the columns left of the window, so every built cell keeps
    its column's x, and simply ends after the window on the right.
  - **The columns don't jump because their widths come from the server**
    (every row's longest value, measured as the run lands, set on the
    header cell) rather than from whichever cells happen to be built. A
    windowed `<table>` sized from its own contents re-sized every column
    on every scroll.
  - **The first paint runs before the table is in the document**, inside
    `sqlResultNodes`, because every caller does
    `out.replaceChildren(...sqlResultNodes(r))`. It estimates the column
    edges from the widths in characters at `SQL_EST_CH_PX` — a lower bound,
    so it builds more columns than the pane shows, never fewer — and the
    ResizeObserver's first call re-windows from the header cells' real
    rects.
  - Copy, CSV…, Excel…, Save as table… and the tag hotkeys read the rows
    from the server and the selection from `r.tags.sel`, never the DOM, so
    a Shift range or a copy reaches rows and columns that were never
    built. Keep it that way.
  `tests/ui/test_wide_tables_ui.py` covers it: a bounded cell count, the row
  under the middle of the pane holding the rid its position means at the
  top, middle and bottom, column widths unchanged by scrolling, and a
  click that leaves every built row in place.
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
- **The SQL result is uncapped, server-held and virtualized, with its
  actions pinned** (`sqlResultNodes` in plugins.js; the server side is in
  server.md). `/api/sql` used to stop at 5,000 rows, and since Copy / CSV
  / Excel exported the rows the pane HELD, every export of a bigger answer
  was cut at 5,000 too. Lifting the cap by shipping every row to the
  browser had no memory bound anywhere, so the server keeps the result
  (`POST /api/sql/run`) and the pane holds a handle (`r.id`), the first
  page, `r.total` and `r.widths`. The table is built like the grid: only
  the window in view plus `OVERSCAN` is in the DOM, between a top and a
  bottom spacer row, through the grid's own `spacerPx`/`vScroll`/
  `rowsPaintY` so the tail stays reachable past `MAX_SPACER_PX` (without
  that a 2M-row result stops at row ~1.68M). Rows not fetched yet paint
  as empty rows of the same height and come in `SQL_PAGE` at a time into
  a cache on the result, one request in flight, keyed by the sort; a
  click-sort is the server's and starts a new cache. Copy, CSV…, Excel…
  and `selectedRows` all ask the server, in the displayed sort, and a
  shift-range that runs through rows nobody scrolled past gets its keys
  from `/keys`. Things that bite: **rows must be one height**, and
  `.sql-result tbody tr` sets it rather than leaving it to the cells'
  line-height: a cell with nothing in it has no line box, so a row of
  NULLs or empty strings was 5px, and on a mostly-blank result the last
  rows could not be scrolled to at all (`test_sql_result_blank_rows.py`);
  **a zero-height spacer is left out**, so a short result is the plain
  table the older UI tests address by `tr` index; **column widths come
  from the server's measure of every row** (set as the header cell's
  `width`, which auto layout treats as a minimum), or columns size to
  whichever rows are painted and jump as you scroll; **a row click marks
  the selection in place** rather than repainting, because a
  double-click's second click would otherwise replace the row under the
  pointer and hand the `dblclick` to a detached node that never bubbles
  to `tbody`'s handler. Sort, scroll position and the row cache live on
  the result (`r.pane`), since every return to the page rebuilds the
  nodes (`showSqlTab` → `applySqlTabToEditor`); a page that lands calls
  `pane.repaint`, which the latest build owns. The result bar sits
  OUTSIDE `.sql-result-scroll`, which is the whole of "pin the export
  options": it used to scroll away with the rows.
  `tests/ui/test_sql_result_uncapped.py`.
- **A SQL run paints only while its token is still the one its tab is
  waiting on** (`S.sqlRuns`, `runSql`). The run is awaited, and while it
  is out the analyst can re-run (a slow first run landing after a fast
  second one overwrote it, and the exports then took the wrong rows),
  close the tab (when `sql_tabs` ids were reused, the result came back in
  the next new tab; a closed tab's query has to stop either way), or open
  another case — the result painted under the new
  case's "Query 1", and tagging rows in it wrote tags into the wrong case.
  A re-run, a tab close and a case switch (`resetSqlPane`) all end the
  run they replace through `abandonSqlRuns`, which aborts its request —
  the server stops a query whose client hangs up — and cancels its token
  too; whatever lands late is released, never painted, and `runSql`
  returns a "Superseded" error for it. The same token is what **Stop**
  and the busy chip cancel (`/api/cancel_op`): stopped after rows have
  landed, the run keeps them and says "stopped" in its status; stopped
  before the first one, it is "Query cancelled". "Running… N rows so
  far" polls `/api/sql/run/progress` with it. A tab whose run is out shows
  that, not the result it is replacing.
  `tests/ui/test_sql_run_tokens.py`.
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
    grouping alone; setGrouping() replaces wholesale, and a
    formerly-grouped column is back in the grid by itself because grouping
    never takes it out of `S.order` — see grid.md). `clearAllFilters()`
    now drops grouping too — stashed in `S.lastGroupBy` first, which is also what the `X`
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
  a copy of what the grid already shows. So `diffRows` draws one line
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
  isn't the comparison. The two dropdowns start on the newest saved
  session (From) against Current work (to) — both used to start on
  Current work, the case compared with itself — unless a comparison is
  pivoted in (they show its sides) or the analyst has picked one
  (`dataset.touched`); `fill()` re-runs on every list refresh, so those
  two rules are what keep a choice from being reset under the analyst.
  The note over a comparison of sessions that cover different tables
  says those tables ARE counted (diff_sessions keys every tagged row and
  note of both sides); it said the counts covered "what they share".
- **The right-click surfaces** (row menu, column-header menu, a table's
  tab/sidebar menu, header value picker) all hang off one floating-menu implementation in `static/js/ui.js` —
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
  cursor there first. Works in grouped mode too now (grid.md, "Grouped
  mode's rows are ordinary rows"); a right-click on a *group header* opens a
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
    source (backlog item 2). Either list is capped at `VALUE_PICKER_LIMIT`
    (1,000) most-common values, and the panel's Find filters only those
    client-side (`pickerRows`) — neither route takes a search term — so
    the capped-list warning sends the analyst to a text filter on the
    column instead of promising Find reaches the rest.
  - **"Everything ticked" is "no filter" only on a list this column's
    filter didn't narrow, and Apply with nothing changed is a no-op.**
    The picker opens on This view for any column whose filter isn't a
    value selection — a contains, regex, `>`, `!` — and that list holds
    only what the filter left, all ticked. `apply()` read it as "every
    value ticked" and cleared the box: Apply with nothing touched widened
    `p1` from 8 rows back to 40, and the wiki's own advice ("narrow the
    column first … and reopen the picker") walked straight into it. Apply
    now compares the ticks with the ones the list arrived with
    (`loadedChecked`) and does nothing when they match; and a changed
    selection that ends up all ticked writes the values out, not a clear,
    when the list is This view and the column has its own filter (box text
    or the picker's tree node — the two things a clear removes).
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
  - **The box spells every selection but one.** It used to spell only
    the plain ones: a selection with edge whitespace, a `|` in a
    multi-selection, or `(empty)` beside real values went into the guided
    filter tree (`setPickerTreeNode`) and left the box empty — and since
    "(empty)" is ticked by default, unticking one value on any column
    with blanks did it, which reads as the picker filling the box only
    some of the time. Worse, a list whose first value started with an
    operator character (`<unknown>|mid`) or that started and ended with
    `/` (Unix paths) did land in the box, and the box read it as a
    comparison or a regex. Now:
    - **`""` in a list is the empty cell**, and the server agrees: an `in`
      whose value is a LIST holding `''` compiles to `IN (…) OR IS NULL
      OR = ''` (`Store._compile_condition`, and `serializeCond` says the
      same in the builder's SQL). A newline-separated STRING `in` still
      drops blank lines — there a blank is a stray newline, not a pick.
    - **Quotes, read only when needed** (`unquoteValue`): a double-quoted
      value in a list or after `=`/`!=` is the text inside it only if
      that text could not be written bare — empty, edge whitespace, a
      `|`, or a leading quote, operator or `/`. Any other quoted text is
      literal, quotes and all, because `="C:\x.exe"` is what `f` writes
      for an everyday quoted service path and must keep matching the
      quotes. The leading-quote rule is what makes a value that itself
      looks quoted (`" x "`, quotes included) round-trip: it is written
      as `"""" x """"` and read back.
    - **The writers check by parsing back** (`quickFilterTextForValues`,
      `valueFilterText`, `valueExcludeText`): a spelling that doesn't
      reparse to exactly the picked values is refused rather than
      written, so a gap in the rules lands in the tree instead of
      filtering on something nobody picked. The only refusal left is a
      line break, which an `<input>` strips — that still goes to the
      tree, with a toast saying why.
    - **The filter builder absorbs `a|""` as the picker's node** (an OR
      of `in`/`equals` and `empty`), not as an `in` holding `''`: its
      one-per-line box drops a blank line on the first edit.
    The tree node is still recognised everywhere, for the line-break case
    and for sessions and saved state written before this. It is
    recognised *structurally* (an in/equals/empty cond on the column, or
    an OR of exactly those) rather than by a marker field, because
    `openFilterBuilder` round-trips the tree through SQL text and would
    drop any marker we invented.
- **Text pasted into a filter box is syntax, so every writer parses it
  back first.** The detail pane's "Filter <col> to <selection>", "… only",
  "Exclude" and a document node's "Filter <col> to this value"
  (`filterByContains`, detail.js) used to write the text in bare, and
  evidence is full of the box's operators: `whoami | findstr` became an
  any-of on two exact values (the rows holding the selection vanished),
  `=cmd` an equals, `^HKLM` starts-with, `/usr/bin/` a regex, `*` and `""`
  not-empty and empty. The box has quoting for exact values only, none for
  a contains, so text that doesn't parse back as exactly that contains —
  or that the box would trim or strip (edge whitespace, a line break) —
  goes into the filter tree as a `contains`/`not_contains` cond instead,
  the picker's line-break route, with a toast saying so. The server
  escapes `%`/`_` for LIKE in a cond itself, so nothing is escaped here.
  Like the picker's node, that cond is the column's one slot: the next
  gesture on the column replaces it whichever route it takes
  (`setContainsTreeCond`, found structurally — a top-level contains on the
  column whose text the box couldn't hold). Any new "filter to this text"
  writer should take the same check; the `=`-writers already do
  (`valueFilterText`).
- **Settings is two panes, one section at a time, and it opens where it
  was left** (`settingsPanes` in settings.js; the plugins and profiles
  managers' shape). It was a column of collapsible sections
  (`settingsSection`, which survives only for the profile builder):
  thirteen of them, only one readable at once anyway, the last few a long
  scroll down. Things that are decisions, not accidents:
  - **Fixed size.** `.settings-panes` has a fixed height and the card is
    `.tall` (top-anchored), so Shut down's one button and the keymap's
    forty rows are the same box; each pane scrolls on its own and
    `#modalBody` never does. The column of sections had settled on
    "sized to content, grows downward" because a fixed 82vh left an
    all-collapsed Settings mostly empty panel — in two panes a short
    section leaves room under it in the right pane instead, which is the
    managers' trade too, and the one asked for.
  - **Every section is built on open; switching flips `hidden`.** A
    section's code is unchanged apart from what it appends into, which is
    what keeps a section that fills itself later (the plugins count, the
    version, the associations list) landing in its own pane — and since
    nothing rebuilds, a pane keeps its scroll and anything half-typed. A
    lazily built pane would also defer those fetches, which is the one
    thing to weigh before changing it.
  - **Remembered, per browser** (`winnow.settingsSection`, try/catch'd
    like every per-viewer pref). The collapsible version deliberately did
    NOT remember; this one was asked to. An unknown stored id (a section
    renamed or removed by a later build) opens the first section.
  - **`openSettings(id)` is the deep link, and a hand-off back names its
    section** — `returnTo: () => openSettings('plugins')`, not
    `returnTo: openSettings`. The remembered section is shared by every
    window on the origin, so a second Winnow window that showed Keyboard
    shortcuts in the meantime would otherwise decide where the plugins
    manager's × lands. `openSettings` is also a bare click handler, so it
    reads its argument only when it is a string — a MouseEvent is not a
    section.
  - **The list is a vertical tablist**: one tab stop (the chosen
    section, which takes the focus on open), ↑/↓ wrap and Home/End jump,
    each showing what it lands on; `aria-selected` marks the chosen one.
    The keymap already ignores everything but Escape and the opening key
    while a dialog is up, so the arrows never reach the grid behind it.
  `tests/ui/test_settings_two_pane.py`.
- **A side-list dialog is one frame, shared** (`sideRail` in ui.js, the
  `.side-*` rules in style.css): what to choose down the left under a
  search box, pinned favourites on top, everything about the choice on
  the right, the buttons under both. Add derived column, Import a folder
  and Import are the three. Decisions held in common:
  - **Fixed height, top-anchored.** `.side-panes` is one height and the
    card is `.tall`; on the right, exactly one box takes what is left and
    scrolls (the derive settings, the folder import's file list) and
    everything else is a fixed size. Both dialogs were centred columns
    sized to their content, so every choice slid the header; each has a
    test that walks its choices and asserts nothing moved
    (test_derived_op_groups.py, test_import_side_list.py). A control
    added to either goes inside its scrolling box, or the jump is back.
  - **The rail repaints; the choice syncs.** `paint()` rebuilds the rows
    (search, pin, a list that changed) and puts the focus back where it
    was; a pick only calls `sync()`, so the row that was clicked or
    arrowed to keeps the focus. The caller owns the choice
    (`selected()`/`onPick`), so the rail holds no state of its own.
  - **A search hit can name what it matched** (`match` returns
    `{hint}`): a timestamp format, a profile's pattern. An entry found by
    its own name shows no hint.
  - **Pins are `menuPins(key)`** ('derive', 'import'), per browser like
    the row menu's. A list with nothing to pin passes `action` instead
    (Import's queue: ✕ to take a file out), and its button sits where ☆
    would. A pin whose entry is gone shows nothing; the folder
    import also drops a deleted profile's pin when it deletes one,
    because profile ids are max+1 (`workspace._next_id`) — delete the
    newest profile and the next one saved gets its id, and would arrive
    already pinned. A profile deleted from another browser can still
    leave that pin behind; it is one row, and unpinning fixes it.
  - **The preview keeps its last answer, dimmed** (`.side-preview.stale`,
    `aria-busy`), with the verdict in a fixed-height footer — so a test
    waits for `aria-busy="false"`, not for rows.
  - **No `labeledRow` around buttons.** It is a `<label>`, which passes a
    click anywhere on it to its first control: Browse… or the `.csv` chip
    in the folder import. Those rows are a `div.derived-row`.
- **Appearance is the look; Grid & layout and Remote session took the
  rest.** The always-on filter row, Pages as a dropdown and the autofit
  limit change what is on screen and where, not how it looks, so they are
  Grid & layout; Remote session mode is about the display Winnow is drawn
  on (wheel quantization, animations off for a remote encoder), so it is a
  small section of its own — and the first-run offer points there. Row
  density, striped rows, the launch animation and the Windows hidden-start
  box stayed in Appearance. In Grid & layout the checkboxes come before
  the autofit block, because a box after a `.settings-sub-label` reads as
  belonging to that heading — which is how Remote session mode's heading
  used to sit over the launch animation and the filter row.
- **On Windows, `prefers-reduced-motion` is not a preference.** Chromium
  derives it from the Animation effects switch, which VM images, RDP
  profiles and "best performance" leave off, so a launch animation that
  honoured it by default played for nobody on Windows except the analysts
  who had unticked and re-ticked its box (that writes `'always'`). The
  default now ignores the hint there and honours it everywhere else
  (`splash.osSkipsSplash`), and the Settings box shows whether the
  animation will actually play rather than whether it is switched on: a
  ticked box over a skipped animation is what sent people toggling it.
  The CSS kill switch at the end of style.css still reads the same hint,
  so on those machines the splash's own fade is instant.
  `tests/ui/test_splash.py` spoofs Edge on Windows for it.
- **Remote session mode arrives with the page, not with boot().** The
  setting is machine-side (`/api/settings/app`), and boot() only fetches
  it after main.js has already decided whether to run the launch
  animation — so the one full-screen animation in the app played on the
  remote displays the mode exists to spare, and choppily: an RDP encoder
  re-sends the whole canvas every frame however fast the VM draws it.
  `server.py` `index()` now stamps `class="remote"` on `<html>` when the
  mode is on, `initAppearance` seeds `S.appearance.remoteSession` from it
  (its `paintRemote()` would otherwise strip the class again), and
  `splash.splashEnabled` skips the default there; `'always'` still
  plays. The stamp also means the CSS kill switch for animations is on
  from the first paint. If "/" ever goes back to serving the file as is,
  `tests/test_api_routes.py` fails before the splash quietly regresses.
- **Settings → Updates has a develop sync nobody is told about.**
  Alt+right-click on Check for updates (or Install update, when it is
  showing) is `python update.py --dev` without a terminal:
  `/api/updates/check` and `/api/updates/apply` with `dev: true`, the
  develop tip named in a confirm, then the same backup, protected paths
  and `develop@<sha>` manifest record as the CLI. It is unadvertised on
  purpose — no `title`, no hint in the panel, nothing in docs/wiki/ — so
  that an analyst mid-case doesn't wander onto unreleased code; beta
  testers are told. Don't give it a tooltip or a menu entry "for
  discoverability". A plain right-click is left to the browser and a left
  click is still the release path; `tests/ui/test_updates_panel.py`
  answers every `/api/updates/*` call in the browser, because the real
  dev apply would overwrite the checkout the suite runs from.
- **`S.keymap` must hold its own key arrays, not `DEFAULT_KEYMAP`'s.**
  The settings UI's "+ key"/"✕" handlers splice and push those arrays in
  place, so the old shallow `{...DEFAULT_KEYMAP}` handed them the
  defaults' own arrays: on a profile with nothing stored yet, adding a
  binding edited `DEFAULT_KEYMAP` itself, and "Reset to defaults" then
  copied the polluted defaults back and looked like it did nothing.
  `defaultKeymap()` (a per-action `[...keys]`) is what `loadKeymap` and
  the reset button both go through now.
- **A map keyed by column name is a `nameMap()`, never a `{}`** (core.js).
  Column names are user data in the frontend too (invariant #5 is the
  backend half), and a CSV can carry a `toString`, `constructor` or
  `__proto__` column. A plain object answers those with Object.prototype's
  members: under the filter bar the grid never rendered and said nothing
  (`parseFilter` called `.trim()` on the inherited function), under the
  row the `toString` box opened prefilled with
  `function toString() { [native code] }` and its value picker threw, an
  unpinned `toString` column drew sticky (`pinnedOffsets`), and
  `S.layout['__proto__'] = …` replaced the map's PROTOTYPE — the
  `__proto__` column could not be filtered, and hiding it hid every column
  without a layout entry of its own. `S.filters`, `S.layout`, the pinned
  offsets, the autofit lengths and the profile `appliedAt` map are
  null-prototype now. The trap is the copy: `{ ...map }` is a PLAIN object
  again, so every assignment back onto `S` goes through `nameMap(…)`
  (snapshots made with a spread are fine as long as they are restored that
  way). JSON round-trips are unaffected. The `Object.fromEntries` maps in
  `rowPaintContext` are safe as they are — built from every column, so
  each lookup hits an own key. tests/ui/test_prototype_column_names.py.
- **There are two filter surfaces, and the always-on row is the one that
  ships.** The filter bar was built because the row was measured on a
  seven-table review case: **27 filter boxes on screen, 0 in use** — the
  emptiest strip in the viewport was also the second heaviest thing in it
  after the data. `#filterBar` (`renderFilterBar` in columns.js) is one
  line naming only the filters that exist, as chips carrying column +
  value and a ✕, plus "+ filter a column…"; there a column's box appears
  under its header when the header's `⌕` (`.hcell-filter`), a chip, or
  that picker asks for it, and folds away again on Enter or Escape. It is
  the opt-in rather than the default because typing straight into a column
  box without looking is the Timeline Explorer reflex and the analysts who
  have it are not wrong — so a fresh install gets the row and Settings →
  Grid & layout's "Always-on filter row" is the switch between them.
  `S.appearance.filterUi` picks the surface at runtime and
  `FILTER_UI_DEFAULT` (settings.js) is the single value deciding which one
  a fresh install gets; nothing else compares against a side, so a future
  flip is that constant plus the sentences that name a side out loud (this
  one, the header in columns.js, the block above the checkbox in
  settings.js, and the `.filter-bar` comment in style.css). Six things are
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
    and the Grid & layout switch between the two surfaces.
- **The filter builder's SQL box maps a LIKE only to the condition that
  means exactly that LIKE** (`likePatternCond`, filterbuilder.js). It used
  to map every LIKE without looking at `%` and `_`: `"Proc" LIKE '%.exe'`
  became equals `%.exe` (0 rows where SQLite matches 41), `LIKE 'p_.exe'`
  an equals, `LIKE 'P1.EXE'` a case-SENSITIVE equals (LIKE folds A–Z),
  `LIKE '%p%exe%'` a contains of the literal `p%exe`. Only two shapes have
  a guided twin, because they are what the server compiles contains and
  starts-with to (`Store._compile_condition`: LIKE over the escaped text,
  `ESCAPE '\'`, same ASCII-only case folding): `'%text%'` and `'text%'`
  with no wildcard left inside the text. Everything else stays raw SQL,
  which runs as typed. The other direction has the same trap: a guided
  contains on `a%b` was shown as `LIKE '%a%b%'`, so `likeSql` escapes
  `%`/`_`/`\` with an `ESCAPE '\'` clause when the text holds a wildcard,
  and the parser reads that clause back. An empty contains/starts is shown
  as nothing, which is what the server compiles it to; `LIKE '%%'` drops
  NULLs and is raw SQL when typed.
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
  non-label child). It's idempotent by construction — once the label
  isn't clipped, both terms stop changing — and returns 0 for a column
  with no header on screen, where callers fall back to the old estimate.
- **A column divider is found by distance, not by an element under the
  pointer** (columns.js `dividerAt`, wired once on `#headRow` by
  `wireColumnDividers`). The grab used to be a 7px `.grip` inside each
  header at `right: -3px`, meant to straddle the 1px line — but
  `.hcell`'s `overflow: hidden` (the `.fcell` entry above says why it
  stays) clipped it to the 4px left of the line. The line itself and
  everything right of it were headers, so the double-click analysts aimed
  at the line sorted that column twice instead of fitting the one they
  meant, which analysts reported as extremely easy to do. The zone
  is now `DIVIDER_REACH` (5px) either side of the line, 11px in all,
  which stays inside both cells' 8px padding and so never covers a name,
  a sort arrow or a button. Two things make it work, and a "just give
  each header a wider grip again" would lose both. **Half the zone
  lies over the next column's cell**, which nothing inside this one can
  reach through the clip — hence one set of listeners on the row, in the
  capture phase, so a press that grabs a line never reaches the header
  under it (no sort, no reorder drag; a right-click is left alone and is
  still that header's column menu). **Which line is under the pointer is
  read off what is painted** (`elementFromPoint`), not off the cells'
  order or boxes: a pinned header is drawn over whatever scrolls under it
  and two pins park side by side whatever lies between them in the order,
  so the line beside a pin is the pin's, and a scrolled-under column's
  line never pokes through a pinned header. A per-header grip with a
  z-index gets one of those wrong whichever z-index it picks — the next
  pin covers the outer half of a pin's grip, or a scrolled-under grip
  covers a pin. The cursor (`.head-row.at-divider`) is the zone's only
  visible sign, and its tooltip is borrowed: the header the pointer is
  over wears "Drag to resize, double-click to autofit this column" while
  the pointer is on a line. tests/ui/test_column_divider.py presses on
  the line and either side of it, between two parked pins, and on the
  names beside it.
- **The Group by strip's drop slot is found by position too, and shown
  before the release** (grouping.js, "the strip as a drop zone"). Each
  pill used to be its own drop target for a reorder and the strip's
  background the target for a header, so a drop counted only on the exact
  element that listened for it: released just under a pill or on a ›, a
  reorder did nothing, and a header released ON a pill was swallowed by
  the pill's `stopPropagation` while the strip had lit up to say it would
  take it. Analysts called it working half the time. Now one zone, the
  strip plus `DROP_SLACK` px round it clamped to the toolbar, takes both
  kinds of drag; the slot is read from the pointer against the pills'
  boxes (nearest wrapped line, then midpoints), and a dashed ghost pill
  stands in that slot while the pointer is over the zone. Three things
  to keep. **The listeners are on the document**: the margin is outside
  the strip's box, and dragover only reaches an element the pointer is
  over. **The preview moves things with flex `order`, never by moving
  nodes**: one of the pills is the drag source, and a source taken out of
  the document mid-drag gets a dragend that never bubbles (or none at all
  in Firefox); the lifted pill is `display: none` from the first
  dragover, never in dragstart, which would cancel the drag. **The slot
  is measured with the ghost in place**, which is what keeps a pointer
  resting on a midpoint from flicking the ghost between two slots: the
  ghost going in pushes the neighbouring pill away from the pointer.
  tests/ui/test_group_strip_drop.py drops on a pill, under one, outside
  the border and on a ›, and reads the strip mid-drag. Its `_rest`
  exists because Playwright sends dragover only when the mouse moves,
  where Chromium sends one every 50ms; without it a drop is judged on
  whatever the pointer passed over last.
- **The autofit cap is a user setting** (`S.appearance.autofitMax`,
  Settings → Grid & layout, default `AUTOFIT_MAX_W_DEFAULT` = 900px, `0`
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
- **Autofit measures only the columns it fits.** `fetchColumnMaxLens(names)`
  sends them as repeated `columns=` to `/api/column_maxlen`, and
  `column_max_lengths` reads `max(length(...))` for those alone, caching
  each column's answer per source (the cache key still carries the
  column list, so an add or remove starts it again). Double-clicking one
  divider used to measure every column of the table, 4.4 to 4.7 s on a
  2.1M-row EvtxECmd table before the one width changed; it is 0.1 to
  1.4 s now, and nothing on a second fit. Fit-all asks for the visible
  columns only, so hidden ones cost nothing. No `columns` at all still
  measures every column.
- **The table menu replaced the tab strip's `▦` column-chooser button**
  (`TABLE_MENU_SECTIONS`/`openTableMenu`: press `C`, or Columns… on a
  table's right-click menu or its ⋯ in the Tables manager). Same registry
  reasoning as the row menu: it's where per-table features land, and the
  tab strip can't grow an icon per feature. `openTableMenu(sourceId,
  section)` opens that source first when it isn't the one on screen — not
  a convenience, a precondition, since every pane reads the live
  `S.layout`/`S.order`/`S.columns` rather than the record it was handed.
- **The table menu is two panes, the shape of Settings and the side-list
  dialogs** (`.side-panes.table-menu`). It used to be one long scroll with
  a list scroller inside the dialog's scroller. Each registry entry is
  `{id, group, title, hint | asyncHint, build}`: the side list shows the
  groups and, under each title, the hint (what is set there now), and the
  chosen section is built fresh into `.table-menu-pane`, because another
  dialog opened from it (the merge builder) can change what it reads. A
  pane dispatches `tablemenu:changed` when it changes something, and the
  hints repaint. The frame is a fixed height, so the column list is the
  one box that scrolls and moving to a short pane moves nothing. **Focus
  starts on the side list, not the filter box**: Up/Down/Home/End walk it
  from the first keystroke, and `C` still closes the menu it opened (a
  caret in a box would take the C as typing, test_modal_toggle.py). The
  everyday actions (Tables manager, Reset view, Close this tab) are the
  `.table-menu-foot` under every section. Under 640px the side list
  becomes a wrapping row of tabs without groups or hints. The panes after
  Columns borrow the merge builder's vocabulary (`.mb-stats`, `.mb-seg`,
  `.mb-notes`, `.mb-field`) rather than adding their own.
  tests/ui/test_table_menu_sections.py, test_table_menu_strip.py.
- **The Tables manager is the same two panes, sharing the frame**
  (`sectionPanes` in ui.js, which `openTableMenu` builds on too;
  `TABLES_MANAGER_SECTIONS` in tables.js). Its sections are case-wide
  rather than one table's (a table's own settings are the table menu, a
  click away on its ⋯), so it is not a master/detail list: **Every
  table**, **Merges & saved tables** and **Indexes**. A section's
  `build(pane, refresh)` may return `{update}`, which the poll calls to
  repaint in place; without one the pane is rebuilt. Every table was one
  list with Open/Close, a search-index chip and a ⋯ on every row and the
  filter indexes between rows: the open state is now a tick (opening with
  `navigate: false`, so the analyst stays in the manager), the counts are
  columns with a dim dash for none so the tables with tagged rows or notes
  stand out, and both kinds of index moved to their own pane, where
  "index ready" is said once as a count rather than on every row.
  **Kinds** groups by `columnGroupKey`, named by `nicknameFor` or the
  first columns, which is how a KAPE import is read (every host's
  EvtxECmd output together), with Merge… on the group preselecting its
  imported tables — not the saved ones, whose rows are already in the
  table they came from. The row's ⋯ is `tableMenuItems`, so it can't drift
  from the tab's right-click menu; errored rows get Remove… alone, as
  before. Build (`POST /api/source/{id}/search_index`,
  `Store.start_search_index`) starts the index `_ensure_fts_building`
  would start on the first slow search; `GET /api/search_index` says
  whether this SQLite can have one at all (3.45+), so the pane says so
  rather than offering Build and showing "not indexed" forever. Compact
  case file stays in the Case menu (case upkeep, not table upkeep).
  `tablesView` keeps the filter, grouping and sort for the page, so they
  survive a trip into the table menu and back.
  tests/ui/test_tables_manager_panes.py, test_tables_density.py.
- **The Import dialog is the side-list frame too** (`openImportModal`
  in importer.js, `.import-*` in style.css). The queue is the rail: a
  queued file by name, with how it will be read under it (`queueHint`),
  and a SQLite file or workbook with nothing ticked says *pick tables* in
  the accent colour (`entry.warn`). The pane is the chosen file: File,
  Read as (`readAsOptions`/`switchKind`; the kind a file was queued as is
  kept in `queuedAs`, so the way back from One line per row restores a
  plugin's format and options), the kind's settings, and the preview box
  that takes the rest — a SQLite file's or workbook's tables to tick
  stand in that box. It used to be a column of rows with a button on
  each (Preview & configure, Pick tables…, Options, As text) that swapped
  the whole dialog for a one-file dialog (`openImportPreview`,
  `openJsonImportPreview`, `openUnitPicker`, `openPluginOptionsForm`, all
  gone), whose settings only stuck on Use these settings. Decisions:
  - **A setting is the queue item's as it changes.** There is nothing to
    confirm, so choosing another file can't lose one. The chosen file is
    a `qid` (`importSel`), kept across the dialog's reopening, which is
    how the server-disk picker comes back to it.
  - **The preview loads when a file is chosen**, so choosing one must be
    cheap: an upload's delimited, text or IIS preview sends its first
    `PREVIEW_HEAD_BYTES`, and a whole-file one (JSON, SQLite, Excel) past
    `AUTO_PREVIEW_UPLOAD_BYTES` waits for Load preview. `previewRunner`
    keeps the last answer dimmed behind a sequence number, like the
    folder import's preview.
  - **A SQLite file's tables and a workbook's sheets are read once per
    queued file** (`unitListing`, the request's promise kept as
    `item.listing`). The pane is rebuilt every time the file is chosen,
    and each rebuild used to ask again: a large upload showed Load
    preview again, went up whole again, the answer still in flight was
    dropped, and the server read the workbook once more for each — the
    sheets never showed and the reads starved every other import of the
    GIL. A failed read isn't kept, so choosing the file again retries.
  - **Import goes to the file that still needs a choice** instead of only
    saying so: it selects that file and toasts what to pick.
  - **Not `sectionPanes`.** The left side is a list of like things to
    filter, as in Import a folder, not a fixed set of sections.
  - **No intro line.** The empty queue's pane says what to drop and what
    imports; a full one has no line to spare.
  tests/ui/test_import_queue_ui.py, test_iis_queue.py,
  test_xlsx_picker_ui.py, test_text_import_ui.py.
- **The home screen's case list is the Tables manager's frame too**
  (`renderHome`, `homeSections`, `buildCasesPane` in home.js; `.home-*`
  in style.css). Sections down the side: Recent (opened in the last 30
  days, or never), All cases (grouped under each group's heading), one
  per group by its newest open, and No group. It used to be one column
  of cards, each with its full path, the counts and the opened time on
  one line and Open/Edit/Remove/Delete on every row; the path took the
  line and an ellipsis took what came after it. Decisions:
  - **The row is the Tables manager's row.** Tables, Rows, Tagged with a
    dim dash for none and the tagged count in the accent, then Opened as
    people say it (`openedAgo`: counted in calendar days, so last night is
    yesterday). The path is in the name's tooltip and Copy case file path.
    Tagged is `tagged_row_count` from `/api/cases`: distinct rows in
    `row_tags`, in the same read-only peek as the other counts.
  - **One ⋯ per row** (`caseMenuItems`, also the row's right-click), and
    the whole row still opens the case, behind `openCase`'s queue (a
    double-click opens it once). A missing file's Open and Delete are disabled, not hidden.
  - **The filter looks inside its section**, and says when a match is in
    another one ("1 more match in All cases"): the old "Show N cases not
    opened in over 30 days…" fold hid older cases from the search box too.
  - **`S.homeSection`, `S.homeSearch` and `S.homeSort` outlive a repaint**,
    since `refreshCases` rebuilds the whole screen after Remove from list,
    Edit, or coming back from a case.
  - **Quick look at files…** (`startQuickLook`: `POST
    /api/case/quicklook/new`, open it, then the Import dialog) is the drop
    gesture as a button; nothing on screen said the drop existed.
  - **The CSS is scoped under `.home-box`/`.home-list`**: the home rules
    sit before `.side-pane > *` and the `.tables-head` grid in the file,
    and at equal specificity those won (the box collapsed to its content
    and the heads took the Tables manager's columns).
  - Under 640px the side list goes across the top as in the other
    two-pane dialogs, and Tables, Tagged, notes and the group chip drop
    out of the row.
  tests/ui/test_home_screen.py, test_home_fixes.py.
- **The rest of the older dialogs are the frame too**: Sessions
  (session.js), Saved filters (timeframe.js `openSavedFiltersModal`), the
  folder and file picker (home.js `openFolderBrowser`), the quick look's
  Add these tables to a case (home.js `tempCopyModal`), Case settings and
  Export (settings.js, sources.js, on `sectionPanes`), the filter builder
  (filterbuilder.js, the merge builder's `.mb-*` panes), lateral_movement's
  Movement events, and Stack (below). Each was paragraphs over rows of
  buttons. Decisions:
  - **`.list-panes`/`.list-pane` are the Tables manager's pane rules,
    shared**: the frame's sizes, the box that scrolls, the 640px narrow
    layout. The rules that draw the manager's own columns stay scoped to
    `.tables-manager`, since the other dialogs' `.tables-row` lists have
    columns of their own.
  - **A row's actions are in its ⋯ and its right-click**, and a click on
    its name is the one gesture that changes nothing, or the one it is
    opened for: compare a session with the current work, apply a saved
    filter, go into a folder, pick a case to copy into. Load, Delete and
    the like are a menu away, behind their own confirm as before.
  - **One main button on the foot, naming what it does** ("Copy 3 tables
    into ACME", "Export 1,204 rows (.csv)", "Add 2 files"). The copy is
    guarded by `oneAtATime`, as the old per-row button was
    (test_double_click_submits_once.py).
  - **Comparing runs as the sides change**; `runCompare` clears the last
    answer before asking, so a pane never shows one comparison's counts
    under the other's names. The heads are A/B, as the gutter marks are,
    and the figures above them spell the names out.
  - **No intro paragraphs.** What each takes is said in its panes' notes
    and feet (test_dialog_intros.py keeps the Timeframe filter's one line).
  - **`sectionPanes` is in the plugin API** (14; tab context `apiVersion`
    8) so a plugin's dialog can be the same frame; lateral_movement's
    Events… is the example.
  tests/ui/test_sessions_panel.py, test_saved_filters_rows.py,
  test_case_variables_ui.py, test_import_ux.py, test_export_all_xlsx_ui.py,
  test_quicklook_copy_targets.py, test_stack.py.
- **A header set's default layout seeds a table once, so it needs its
  own way onto the rest** (the table menu's Layout section with the
  default chosen; columns.js `applyDefaultLayout` is the same as a
  keymap action, unbound by default). `openSource` reads the default only
  when the table has no layout of its own, and almost every table has one
  by the second visit: a resize, a sort, a hidden column and Reset view
  all call `saveLayout`. Reset view keeps the layout on purpose (it is the
  clear-everything button, used many times an hour), so a default saved
  after a table was first opened had no way to reach it, which is how it
  was reported. Applying takes the default's order and per-column
  settings for the imported columns, keeps each derived column's own
  entry and re-places it beside its input with `placeUnorderedColumns`
  (the same helper `openSource` uses), and offers Undo, since nothing
  else brings back the layout it replaced. `defaultLayoutResult` is that
  computation, shared by applying and the Layout section's preview. A
  grouped table needs nothing extra: grouped columns stay in `S.order`
  and `visibleCols()` leaves them out (the preview leaves them out too).
  tests/ui/test_apply_default_layout.py.
- **Another table's layout is the one-off, for tables whose columns
  differ** (`buildLayoutPanel`/`layoutSources` in timeframe.js, columns.js
  `copiedOrder`, `copiedColumns`). The header-set default only matches
  identical column sets, and saving one changes what every later import
  opens to, which is wrong for "make this table look like that one". The
  Layout section's "Take the layout from" lists the default, then tables
  sharing at least one column, the same header set first, then by columns
  shared. Shared columns take the other table's order and per-column
  entry outright (no entry there clears this table's, or a width it never
  had would survive the copy); a column only this table has keeps its
  entry and goes right after the column it follows now, so a derived
  column stays beside its input and an extra column beside its
  neighbour. The other table's order is its saved layout, or its columns
  in file order when it has none. It was its own sideRail dialog (Copy
  layout from another table…) until the table menu became two panes;
  putting the default and the tables in one list gave the default the
  preview copying always had. **The preview marks only what would
  change**: shown, hidden, pinned, unpinned, width (`w`), and "moved" for
  the columns outside the longest run still in their old relative order
  (`movedColumns`), since comparing positions marks every column after
  one dragged to the top as moved too. Apply layout is greyed when
  nothing would change. Applying goes through `replaceLayout`, which
  offers Undo. tests/ui/test_copy_layout_from_table.py.
- **Names in Name & merge save as they are typed**, the way the tag
  editor saves: Enter or leaving the box saves, Escape takes back the
  edit (and stops there, so the dialog stays open). The table's nickname
  posts `/api/source/{id}/nickname`; the header set's name goes through
  `saveHeaderNickname` (savedfilters.js), which `setNicknameFor`'s prompt
  also uses. A merge refuses a blank name. The merge row lists the tables
  sharing columns with this one, best fit first, which is what the merge
  builder will offer.
- **Right-clicking a table is a short menu, not the table menu**
  (`tableMenuItems`/`openTableContextMenu` in sources.js, one handler for
  the tab and both kinds of sidebar row). The right-click used to open the
  full-height dialog above, for what was most often a close or a rename;
  now it is a `contextMenu` at the pointer holding Close this tab (open
  tables only), the nickname, Columns… (the dialog, unchanged), the merge
  builder and Remove…. Each entry calls what the Table dialog or the
  Tables manager already calls, under their label, so there is still one
  implementation of each. Three things to keep: **items are built from
  `S.sources` on every paint**, by id — a background `loadSources()` can
  land while the menu is up and the record the row was drawn from is
  stale by then. **A keyboard opening anchors under the node**: the
  ContextMenu key and `Shift+F10` fire the same `contextmenu` event, which
  Chromium places at the focused node's centre with `button === -1` (other
  engines have used 0,0), so taking its coordinates dropped the menu over
  the tab it was about; `button === -1` or a point off the node means
  "place it like a dropdown". And **Remove…'s refusal is one predicate**
  (`mergesUsing`/`removeRefusal` in tables.js): the manager's button
  refuses with an alert, the menu greys its entry out and names the merge
  in the `desc` line, and both read the same rule — the menu can say it
  before the click, which a button in a list of buttons can't. The
  Tables manager's ⋯ is this menu too now. Errored rows (a broken merge)
  still get no right-click menu; the Tables manager's ⋯ offers them
  Remove… alone, which is where those are removed. tests/ui/test_table_context_menu.py.
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
  the rows seeded are the ones the grid will show (recentring after a
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
- **Enter in the search box doesn't supersede the build it would start.**
  The box's debounce fires 220 ms after the last key, so by the time
  most analysts press Enter the same search is already running, and
  Enter's own build cancelled it and started the scan over: on the
  693k-row EvtxECmd table, "Windows" + Enter 400 ms later painted at
  2.1 s instead of 1.4 s. Enter now cancels a debounce still waiting
  (`.cancel()` on core.js's `debounce` — left waiting, it superseded
  Enter's build instead), and lets a build in flight run when
  `sameSearchInFlight` says it is Enter's: a search-box build of the same
  spec for the same table, started at the top of the grid, with no
  cursor, pick or cell range for Enter to bring back. Otherwise Enter
  supersedes it as before, since it would land somewhere else.
- **The jobs panel has a Clear all, and what it will not clear is the
  point of it.** A folder import queues one job per file and every one of
  them leaves a finished row behind. The server keeps the last
  `INGEST_JOB_KEEP` (20) finished jobs, so the pile tops out at twenty
  rather than one per file — but twenty ✕ clicks per folder is still
  twenty, and the failures are what stay: a done row auto-dismisses after
  8 s and an errored one never has. The header button
  (`clearFinishedNotices`, jobs.js) dismisses every finished import —
  landed, failed or cancelled — and every finished plugin notice, and
  leaves two kinds standing. **Anything running or queued**, because
  clearing notifications is not cancelling work: those rows wear a ✕ that
  means Cancel (`jobPanelRow` gives `onCancel` and `onDismiss` the same
  glyph), and a Clear all that quietly killed a half-finished folder
  import would be a worse bug than the one it fixes. **Any finished row
  that declares `holdsResult`** — the third argument to `createNotice`,
  beside `onDismiss`, so it is the app's own and not part of the plugin
  contract. A background search that has landed holds a built view
  server-side until Apply or Discard, which is also why that row's own ✕
  is a Discard and not a plain dismiss. A row kept for that reason is
  announced ("Kept 1 notification that is waiting for an answer"), since
  a survivor of a button labelled Clear all needs explaining and a row
  that vanished does not.

  **`holdsResult` is declared, not inferred, and the obvious inference is
  wrong.** "Finished and still carrying buttons" was the first rule and
  it kept exactly the wrong rows: `announceHits` (watchlist.js) settles
  its scan row as `done` + `sticky` + an "Open watchlist" button, and the
  bundled claude_assistant ends the same way — both shortcuts to a tab,
  neither holding anything. `settle` skips the linger timer for
  sticky-with-actions, so nothing else took those rows away either. And
  a folder import is what *produces* the watchlist alert (every landed
  import runs `scanWatchlistForSources`), so the one row the feature
  exists to sweep up was the one row it refused, while telling the
  analyst it was waiting for an answer.

  **Clearing does not run `onDismiss`.** That handler means "the ✕ must
  act on this rather than merely hide it", which is only true of a row
  standing for something live — and those are the rows Clear all keeps.
  What it does clear stands for nothing, so there is nothing to act on,
  and firing the handler would aim *past* the row: the detached search's
  calls off that table's background search, and a receipt for a search
  that failed ten minutes ago is no instruction about the one running now.
  Clearing a stale receipt is not consent to kill live work.

  **That reach is now closed at the source, because the row's own ✕ had
  it too — and that ✕ is the one an analyst actually clicks.** An error
  row waits on screen indefinitely, so tidying one away after starting a
  fresh search on the same table cancelled the fresh search.
  `cancelPendingView(sourceId, only)` takes the record as well and does
  nothing unless it is still the table's pending one, which is the guard
  `cancelScan` has always had (`scanJob !== rec`) — made once in the
  function rather than in each handler, so no closure written later can
  reach past the search it was created for. The table-only form stays
  right where the caller means "whatever this table has pending, call it
  off": `runBuild` before a rebuild, and Remove in the tables manager.
  `settlePending` clears the row's actions on both settle paths for a
  separate reason — `applyNoticeOpts` leaves what it is not given, so a
  failed search would keep the Cancel it was created with, and a receipt
  offering a button about work that has already ended misleads even when
  the button is inert. The header is
  `position: sticky` inside the panel's scroller: twenty rows overflow its
  50vh `max-height`, and a button that scrolled away with them would be
  exactly as much work as the ✕s it replaces. It carries the **clearable**
  count ("8 finished"), not the row count — the running import and the
  queued summary above it are not what the button acts on, and a header
  reading "11 finished" over a panel that loses eight rows explains itself
  wrongly. The button wears `.job-action`'s treatment rather than a ghost
  one: beside ten rows of dim text a dim button disappears into them, and
  a control nobody finds does not answer a complaint about tedium. The
  count changes as jobs land inside `#jobsPanel`'s `aria-live` region,
  which is not a new cost — `renderJobsPanel` `replaceChildren()`s the
  whole panel on every 900 ms poll while a batch runs, so that region
  already re-announces everything it holds; the fix, if one is wanted, is
  at the panel level.
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
  the picks and the seeded rows resolve against the adopted view. A
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

  v6 moves the profiles manager `M` → `p`, and is the first **replacement**
  rather than an addition — which needs one more thing than v5's guard.
  Declining to move is not enough on its own: a migration edits the
  *stored* map, `loadKeymap` then merges it over the defaults, so an
  action the stored map has no entry for takes the new default anyway —
  `p` would arrive beside the `p` the analyst had bound to something
  else. When the key is taken the migration writes `M` back explicitly, so
  the refusal survives the merge. (A stored map carries every action after
  one real run, so this only reaches a hand-edited or very old one — which
  is exactly the shape that would otherwise collide.)

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
  e.shiftKey to extend the selection. The settings capture handler ignores
  modifier-only keydowns and keeps listening (it used to commit on the
  first keydown, so pressing Ctrl for Ctrl+K bound "Control" and combos
  were impossible). findKeyConflict also refuses the hardcoded
  modifier shortcuts (Ctrl/Meta+C copy, Ctrl/Meta+z undo, Alt+digit tab
  switching, and Ctrl/⌘+F while search still holds it) since those are
  handled before matchAction and would shadow a binding silently. Side
  effect worth knowing: a bare-key binding no longer fires when
  Ctrl/Alt/Meta is held (matchAction used to look at e.key alone, so
  Ctrl+T opened the Tables manager).
  The same rule means a Ctrl+Shift+letter chord is STORED as
  `Ctrl+<capital>` (copyUniqueValues' default is `'Ctrl+U'`), which next
  to a modifier reads as plain Ctrl+U — a different chord, and the one
  Chromium opens view-source on. `keyLabel` is the display side: Settings'
  chips, the conflict toast and the row menu's hint spell the Shift back
  out (`Ctrl+Shift+U`). Store the spec, show the label; a stored
  `'Ctrl+Shift+U'` would never match anything.
- **Copy unique values reads one of two places, by scope**
  (`copyUniqueValues`). Rows — a multi-row cell range or the picks, by
  Ctrl+C's precedence — go through the page cache under copy's 20,000-row
  ceiling and keep first-appearance order. Anything wider — one clicked
  cell, a select-all with nothing unchecked, the header menu — asks
  `/api/distinct_values` for the whole view: group_summary with
  `bucket_datetime=False` (a day bucket is not a value to paste) and
  `order=value`, capped at `DISTINCT_VALUES_MAX` and refused past it
  rather than copied in part. "One row" is deliberately NOT a scope here,
  unlike every other row-menu action: one row has one value, so the
  menu's "this row" means the whole view, and its second line says so.
  It is on Ctrl+Shift+U because Ctrl+Shift+C was asked for and is taken:
  the copy handler reads Shift there as "with headers".
- **Ctrl+Shift+Arrow needs no binding of its own**, which is the same
  Shift fallback at work: it is spelled
  `Ctrl+Shift+ArrowDown`, matches nothing, gets Shift stripped, and lands
  on `jumpEdgeDown`'s `Ctrl+ArrowDown` — whose handler reads e.shiftKey
  and extends to the edge instead of jumping to it. Note the asymmetry
  that makes this work at all: Shift falls back, every other modifier
  does not, so `Ctrl+ArrowDown` itself has to be a binding spelled out in
  full or it reaches no handler and the browser scrolls the page.
- **Excel's arrow keys are six NEW action names, not new chords on the
  old ones.** `moveLeft`/`moveRight`/`jumpEdge{Up,Down,Left,Right}` were
  added rather than extending `moveDown`/`moveUp`'s arrays, because
  `loadKeymap` lets a stored array replace a default one wholesale: adding
  a chord to an action an analyst has already used reaches nobody without
  a migration, while an action name their stored keymap has never seen
  takes its default for free. Same reasoning the v2 entry records for
  `openFilterBuilder`/`openValuePicker`. `moveDown`/`moveUp` did change
  *behaviour* — they move the cell cursor now — but their bindings are
  untouched, so no migration was needed for that either.
- **A key handled by hand is listed by hand** (`FIXED_KEYS`, keymap.js).
  Settings → Keyboard shortcuts draws the rebindable rows from S.keymap,
  so those can't go missing — but every key dispatched outside the map
  (wireKeymap's pre-gates, the search and filter boxes, the SQL editor's
  Ctrl+Enter and Ctrl+Space, menu arrows, Settings' own section list, the
  modified clicks in grid.js/columns.js) reaches Settings only through
  that list, and a dozen of them were never in it. Add the line in the
  same change as the handler, and mirror it in the wiki's "Fixed keys"
  tables (docs/wiki/Settings-and-shortcuts.md), which use the same groups.
  The entries are display strings (`Ctrl/⌘ + C`), not keymap specs.
  tests/ui/test_shortcuts_listed.py holds the pane to both lists and pins
  the keys that were missing by name.
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

- **`z` undoes filter changes, and it is recorded in the rebuild, not at
  the call sites** (filterundo.js). Forty-odd places change a filter and
  every one of them ends in `runBuild`, which calls `recordFilterState`
  with the table it is about to build; the history is a baseline per
  table (what it was last built with) plus a stack of what changed. A new
  way to change a filter needs nothing from this — as long as it goes
  through a rebuild. Five things it relies on:
  - **Compared by what the view is built from, restored from the raw
    text.** Each part (column filters, search, tree, tag filter,
    timeframe) has a key in the spec's terms — parsed filters, `null` for
    an empty tree, `''` for a disabled timeframe — so a stray space, a box
    emptied to `''` rather than deleted, or a mode toggle with nothing
    typed is not a step to undo through. An entry holds only the parts
    that changed and undo puts back only those, which is why sort,
    grouping and Hide empty rows are never touched.
  - **Typing coalesces.** A header box rebuilds on every pause past the
    220ms debounce, so builds that only change one box's text (or the
    search text) within `FILTER_UNDO_COALESCE_MS` of the last one fold
    into it; typed back to where it started, the entry drops. Only while
    that box has the focus: `c` pressed straight after typing changes the
    same one column, and folded into the word it dropped the entry, so
    `z` found nothing to bring back.
  - **A derived-column rename moves the history too.**
    `renameColumnInStash` runs `renameColumnIn` over
    `filterHistoryStates` as well as the stash, or undo past the rename
    put a filter back on a column that no longer exists by that name —
    and `put` drops those, silently.
  - **`openSource` re-baselines** (`syncFilterBaseline`) before it builds.
    Opening a table is not a change to it — and without that, a case-wide
    timeframe set on one table (`timeframeAcrossTables`) turned up as an
    undo step on the next table opened.
  - **It dies with the stash** — `clearViewStateStash` and
    `dropViewStateFor` drop it too, which is why `closeTab` and
    `closeAllTabs` call those rather than touching the Map. Source ids
    restart in a new case and SQLite reuses a removed table's id; an
    entry kept past either would put one table's filters on another.
  Baselines and entries are deep copies: `setPickerTreeNode` edits
  `S.filterTree.children` in place, and a baseline holding the live tree
  would change along with it and see no difference at the next build.
  `tests/ui/test_undo_filter_change.py`.

- **Stack view** (stack.js) — the column-header menu's "Stack values (rarest first)…" opens a chart of the current view's distinct values by count (via group_summary, order=count, up to 1,000 values), one bar per value, click-to-filter. Least-frequency-of-occurrence triage. **The chart is the dialog**: the table's columns down a side list (`sideRail`, pinnable) and the bars taking the rest of an `xwide`, tall modal. It used to be a canvas drawn with charts.js in a box sized to its bars, which read as too small. Decisions: **the bars are DOM elements, not canvas bars**, so each is focusable (ArrowUp/Down walk them, Enter filters), carries its full value in its title, takes the row menu (Filter to / Exclude / Copy value) and scrolls; **one scale for the column**, ending at the largest count with gridlines on the nearest 1-2-5 step (`scaleStep`, so 412 gets 0…400 by 100 and the longest bar spans the track — rounding the axis up to 600 wasted a third of it), kept when Filter values… narrows the bars; **the row height shares the chart's height out** (`size`: between 26 and 44px), so five values are five broad bars and a long tail scrolls; the value gutter is measured in the chart's face, at most two fifths of it. The foot says when the count or the drawing (500 bars) was cut short. tests/ui/test_stack.py. See docs/design/analysis-suite.md.

- **Case notes tab** (notes.js) — a free-form Markdown scratchpad for the investigation narrative, stored in the case file (Store.case_notes) so it travels with the .db — distinct from the per-row notes, which the page now LISTS below the split (see below). The editor and a live preview sit side by side (`#notesSplit`, a flex row inside `.page-main`) split by a draggable divider; Edit / Split / Preview collapse one pane or the other through `data-mode` on the row, and the stylesheet does the hiding. Debounced autosave, a tiny dependency-free Markdown renderer (airgap). Page tabs now route visibility through sql.js's showMainView(id)/MAIN_VIEWS registry so adding a tab is a one-place edit. See docs/design/analysis-suite.md. Things that bite here:
  - **The mode never persists, the layout does** (`winnow.notes = { split, rows }` — the divider ratio as a ratio of the row, and whether the row-note strip is open; per browser, like `winnow.sidebar`, and written only when the analyst drags or toggles, never on a visit that touched nothing): Notes always opens in Split, and `showNotesTab` re-applies both *synchronously before* `await ensureNotesLoaded()` — the page is already showing by then, so applying them after the fetch paints the markup's 50/50 default and then jumps. A remembered preview-only mode would also hide the editor from Playwright's `fill()` in tests/ui/test_case_notes.py after its reload.
  - **The renderer's output goes through `innerHTML`, so every character the analyst typed is escaped — quotes included — and nothing it generates is read again.** Notes travel with the case file to whoever reviews it, and pasting a URL out of evidence as a link is routine, so this is the one place in the frontend where analyst text becomes markup. The escaper used to stop at `& < >` while a link's URL went straight into `href="…"`: `[x](https://host"onmouseover="…)` closed the attribute and ran on hover. `renderMarkdown` now cuts each line into pieces BEFORE escaping — links first (`winnow:` kind/digits or an `http(s):` URL, anything else stays text), then code spans in the rest and in each label — and escapes each piece once on its own, `"` and `'` too. Don't go back to chaining `.replace()` rules over the HTML earlier rules produced: that is how a URL ended up holding the `<em>` made from its own asterisks and a code span had its contents re-read as emphasis (code spans are literal now). tests/ui/test_notes_markdown_xss.py throws a payload at every inline and block context, hovers every link, and checks the ordinary forms (a query string's `&`, quotes in prose, an in-app link) still render as themselves.
  - **The page lists the OTHER notes too** — `#notesRows`, a collapsible strip below the split, one entry per row of `row_notes` in the case ("Security.csv · Line 412" plus the note on one line), each opening its row through `jumpToTimelineRow`, the same open-source-then-recenter the watchlist hits use. Two different things were called notes and only the narrative was on the page called Notes, so an analyst writing up findings had to remember which rows they had annotated. `GET /api/row_notes` → `Store.list_row_notes` answers `{total, notes}` joined to `sources` for the label (a note whose table is gone is omitted rather than listed unnameably) and capped at 500, with `total` so the strip can say which part of them it is showing. It is **re-fetched on every visit, not cached**: row notes are written on the grid between visits here. **`resetNotes` empties the strip on a case switch, and a failed fetch writes a line saying so rather than leaving the last list up** — the entries are `(source_id, rid)` pairs and both ids restart at 1 in a new case, so a leftover button does not error, it opens a real row of a real table and presents it as one the analyst annotated (`showNotesTab` reveals the page before its unawaited `loadRowNotes()` answers, and if that answer never comes there is no second fetch to correct it). Same hazard, same fix as the detail pane's note binding, which `openCase` blanks before the swap. Merge parity needs nothing — a note taken on a merged table's row is stored against the member, like a tag, so it is listed under that member. The open/closed state joins the divider ratio in `winnow.notes` (`{ split, rows }`).
  - **The placeholder is prose, and quiet.** It used to be a worked sample note — a heading, bullets, timestamped lines — which on an empty case read as a narrative somebody had written beside a preview pane that had failed to render it. Dimming alone does not fix that; the SHAPE is what reads as content, so it is two sentences now, `.notes-editor::placeholder` is `var(--text-faint)` italic, and `renderPreview` puts `.notes-preview-empty` ("Nothing written yet…") in the pane instead of leaving it blank. That empty line is a DOM node rather than markdown through the renderer, so nothing an analyst types reproduces it — and `resetNotes` still blanks the pane outright (tests/ui/test_notes_split.py pins that a case switch takes the preview with the editor).
  - **The divider is `#notesDivider`, not a `.page-panel-resize`.** `syncPluginPanels` shows and hides `#notesPanelResize` (the plugin column's handle) by id, and the plugin column stays the outermost sibling of `.page-main`, so a notes page panel and the split coexist untouched.
  - **One `input` listener drives autosave and the live render.** Every write path — typing, Link ▾, a plugin's `notesPage.setText`/`insert` — dispatches `input`; `ensureNotesLoaded`'s seed assigns `.value` directly (an input event there would autosave the body straight back) and calls `renderPreview()` by hand. The render is skipped and marked stale in editor-only mode or while the page is hidden; `setNotesMode`/`showNotesTab` catch up. It swaps `innerHTML` wholesale (why the link clicks are delegated) and puts the pane's own `scrollTop` back afterwards so editing the bottom of a long note doesn't jump the preview to its top.
  - The clamp is 0.2–0.8 of the row *and* a 220px floor per pane (`clampNotesSplit`; a row too narrow for two floors splits evenly): with a plugin column open at 70% of the section the ratio alone could leave the editor a few characters wide. **It is re-applied by a `ResizeObserver` on `#notesSplit`, not only when the ratio is written** — the row narrows without the page doing anything (the plugin column toggled or dragged wider, the window resized), and the observer re-applies the *stored* ratio against the new width, so a pane squeezed to the floor gets its share back when the room returns. `.notes-editor` is flex-basis-driven (`--notes-split`), not `width: 100%`, and the 82ch measure cap sits on the inner `.notes-preview-body` so the pane itself fills its half rather than leaving dead space against the divider.
  - **The divider's hit target overlaps the preview only** (`margin: 0 -8px 0 0`; the 1px line sits at the editor's edge). The editor's right edge is where the textarea's vertical scrollbar sits on classic-scrollbar platforms, and the symmetric `.tab-split` overlap turned a grab of the scrollbar thumb into a split drag. Headless Chromium's overlay scrollbars can't show it, so tests/ui/test_notes_split.py pins it with `elementFromPoint` and a drag started inside the editor's edge. `.notes-preview` paints `var(--panel)` itself — `#app`'s skin backdrop (the blueprint graph paper) otherwise showed through one half of what reads as a single surface.

- **The search dialog carries a scope** (search.js, `openSearchAllModal`) — titled "Search tables" rather than "Search all tables", because a scope row above the term builder now decides how much of the case it covers: **Every table / This table / Choose…** (the value picker's `.vp-seg` idiom, `aria-pressed`), with the case's real tables as chips under it, open ones first and the tail folded behind "+ N more". The default is *This table* when one is open and *Every table* otherwise. Clicking a chip from either fixed scope means "start from what is in scope and take this one out", so it seeds a Choose… scope and toggles that chip; a pick the analyst empties by hand stays empty (Search refuses it with a toast) rather than quietly widening back to the whole case. A pick whose tables have *all* since left the case is a different thing and does widen back to every table — with the scope row saying so, because a three-table check silently becoming a whole-case sweep is minutes nobody asked for. **A pick is keyed by id *and* name** (`searchAllLivePickIds`): SQLite reuses a source id once the table holding it is dropped, so an id whose live table is now a different file is not one of the tables that was ticked — the rule `subsetParentLabel` follows, for the same reason. `S.searchAll.scope` is the pending choice, `S.searchAll.ranScope` is the scope **the results on screen came from** — the same rule `terms` follows, so re-scoping without re-running cannot re-label numbers nobody re-ran, and the badge and the finished toast read from it too. On the wire it is `source_ids` (null = every table); a merged table is sent AS the merge and the server expands it to its members (invariant #9), which the results pane reports. Three things that bite: the poller used to decide "is this pane on screen" by comparing `$('modalTitle').textContent` to the literal 'Search all tables', which a title that varies with scope silently breaks — it asks `currentModalAction() === 'openSearchAll'` now; one search job runs per case, so a scoped start stops a sweep that may be four minutes in, which is why Search is no longer disabled while one runs and `startSearchAll` asks first instead — and why an empty box while a sweep runs is refused with a toast rather than taking the "nothing to search for" path, which would drop the job id and the partial hits while the server kept scanning, Stop button and all (a poll that 404s because the start it raced superseded its job is ignored for the same reason: only the job still being followed ends the run); and the 1,000-row count cap is unchanged at every scope — scoping does not make a table smaller — so the pane names the cap and points at "Open ↦", where the grid's own count is exact.

- **`jumpToTimelineRow` marks the table open, it does not just switch to
  it.** The one navigation behind a Timeline row, a watchlist hit in
  either pane, "…and N more — open the table", and a row-note entry on
  the Notes page. `openSource()` switches the grid to a table without
  touching `sources.is_open`, which is server-side state — so a jump into
  a CLOSED table landed the analyst in a grid with no tab for it, no row
  in the sidebar's Open section, and the table still listed under the
  closed ones: nothing on screen to come back to, and nothing to close.
  It now takes the two steps search-all's "Open ↦" takes — `POST
  /api/source/<id>/open` then `loadSources(id)`, which opens the table
  itself once it is in the open tabs, so this is not a second switch —
  and keeps the plain `openSource` fast path for a table already open.
  A failed POST still jumps: the grid can show the row, which is what was
  asked for. `tests/ui/test_watchlist_opens_the_table.py`.

- **A Timeline line is picked by a click, not opened by it**
  (timeline.js, `selectTimelineLine`). A click used to call
  `jumpToTimelineRow`, so reading the Timeline meant not touching it.
  Now: click picks (`S.timeline.sel`), double-click, Enter or the
  gutter's ⤢ open. Things worth knowing before touching it:
  - **The keys are the keymap's**, not a second set: keymap.js hands
    `moveDown`/`moveUp`/`pageDown`/`pageUp`/`jumpFirst`/`jumpLast` (and
    the edge jumps) to `timelineKey` while the Timeline is the page
    showing, plus Enter from the list itself, and Escape to
    `clearTimelineSelection`. A rebinding in Settings moves this list
    too. The digits tag the selected line (`tagTimelineLine`, see the
    details pane entry below); the header's tag chips still carry no
    hotkey, since they filter rather than tag.
  - **Clicks are delegated on `#timelineRows`** (invariant #6), and a
    click that picks a line on screen moves the `.selected` class rather
    than repainting: a repaint replaces the node under the pointer, and a
    double-click whose second click landed on a node the first one threw
    away never reached the listener (`tests/ui/test_timeline_lines.py`
    has the double-click).
  - **The place survives the rebuild every visit is**
    (`keepTimelinePlace`): `showTimelineTab` rebuilds, so opening a line
    and coming back used to land at the top. The selection is found
    again by `(source_id, rid)` through `GET /api/timeline_position`
    and scrolled a third of the way down; a line that left the Timeline
    puts the list where it was in time, with nothing selected. Scrolling
    goes through `rScroll`/`vScroll`, since a long Timeline's spacer is
    capped like the grid's (MAX_SPACER_PX).
  - **The Type column is the kind of table plus the host**
    (`timelineKinds`): the configured Type, else the header set's
    nickname less the tool in brackets, else the table's name; tables of
    one kind are told apart by their names less the common prefix and
    suffix. One `--series-N` colour per table, tables with tagged rows
    first. The pill keeps `.type-badge` and the file name as its title,
    and its text is the kind alone (the host is a sibling `.tl-host`),
    which `tests/ui/test_timeline_config_builder.py` reads. The memo is
    keyed on `S.sources`, `S.timelineTemplates` and `S.headerNicknames`
    by identity — each is replaced, never mutated, when it changes.
  - **The gutter is the grid's**: a stripe per tag, ✎ for a note (the
    page now carries `note`), ⤢ to open. The tag dots it replaces sat in
    the Type column. The raw toggle shows on the line under the mouse or
    the one picked (opacity, so it keeps its place and Playwright can
    still click it), and stays shown on a line showing its raw row.
  `tests/ui/test_timeline_lines.py`.

- **The Timeline's overview strip** (timelineoverview.js) is the
  histogram strip's look over the Timeline's own view, with three things
  worth knowing before touching it:
  - **Its classes are its own** (`.tl-ov-*`, `.tl-overview`), not the
    histogram's `.th-*`. The histogram's tests find that strip's parts by
    bare class (`.th-info`, `.th-legend-item`, `canvas.th-canvas`), and a
    second strip under the same names put them in strict-mode violations
    the moment the Timeline had been opened. The rules in style.css copy
    the histogram's rather than sharing them.
  - **What it narrows is kept as what is OFF or what was dragged**:
    `S.timeline.sourcesOff` (the tag chips' rule, so a table tagged later
    is on) and the Timeline's timeframe (next entry): a drag over
    `[t0, t1)` writes `t0 → t1 - 1s`, inclusive, so the chip and the
    dialog read the same stretch the drag picked. The
    drag is snapped with the histogram's rule (a unit picked from the
    drag's width, never coarser than a bar), not to the bars: snapping to
    bars made a stretch inside one bar impossible to pick. Both reset on
    a case switch (home.js), as `tagsOff` does.

- **The Timeline's timeframe filter** (`S.timeline.timeRange`,
  `{enabled, start, end}`; the "timeframe" section of timeline.js) is the
  table pages' ⏱ Timeframe over the Timeline, and copies its rules
  rather than its state:
  - **It is not `S.timeRange`.** The tables' range is set to read one
    table (and can follow the analyst between them); applied to the
    Timeline it would take findings off it unasked. The dialog shows
    the tables' range under "From the tables' timeframe" with Fill range
    to copy it in, as the tables' dialog offers "From tagged rows".
  - **Same keys, same dialog shape.** `timelineKey` handles
    `openTimeRange`/`toggleTimeRange` while the Timeline is the page
    showing, before its `!total` return — a timeframe that matches
    nothing leaves an empty list, and the key that turns it off has to
    work there. `markModalAction('openTimeRange')` lets R close the
    dialog it opened. Apply always turns it on; `r` keeps the range;
    Clear and the chip's ✕ forget it; the dialog says "Off right now"
    when it opens on a range that is off — all as the tables' does.
  - **Stored as ISO, validated before the build.** The boxes are read
    with `parseTimestamp` (tsformat.js, the client twin of
    `_ts_normalize`), so "7/9/2026 9:30 AM" is kept, shown in the chip
    and reopened as `2026-07-09 09:30:00`, and text it cannot read is
    refused with a toast in the dialog instead of a 400 after it closed.
  - **The chip is the header's** (`#timelineTimeRange`), not the
    overview strip's. It was the strip's while only the drag set a
    range; with the strip hidden the Timeline was narrowed and nothing
    said so. `renderTimelineTimeRange` paints it and the button, and is
    called from every place that changes the range, from
    `buildTimeline`, at boot, on a case switch and on a keymap change
    (the button's title names the keys).
  - **The header wraps.** With both chips on, a 1280px window had no
    room for the switches: they ran off the right edge and the tag
    chips were squeezed into a column. `.timeline-head` is
    `flex-wrap: wrap`, and the switches are one group
    (`.timeline-head-acts`, `margin-left: auto`) so they wrap together.
  `tests/ui/test_timeline_timeframe.py`.
  - **The bars come from the server, the marks from the client.**
    `GET /api/timeline_overview` after each build (and on Tags/Tables);
    the band, the thumb and the selected line's mark are worked out from
    lines the list already holds, on `renderTimelineRows`' and
    `selectTimelineLine`'s call, coalesced to a frame and repainted only
    when they would land on other pixels. A chart whose `view_id` is not
    the current view's is drawn faded and takes no clicks (`fresh`).
  Bars are square-root scaled (single findings beside a burst of
  hundreds were a pixel tall on a linear scale); the hover gives each
  bar's real count. `timelineOverviewX` is what the strip paints with and
  what `tests/ui/test_timeline_overview_strip.py` clicks by.

- **The Timeline's details pane** (timelinepane.js) shows the selected
  line in full and is where "What else happened then" lives:
  - **It does not open on a click.** It is always there (until Details
    or its ✕ puts it away, remembered in `winnow.timelinePane`), empty
    with nothing selected. A first version opened on the first click and
    closed on Esc; opening narrowed the list under the pointer, so the
    second click of a double-click landed on whatever had moved there
    and the row never opened. `tests/ui/test_timeline_pane.py` asserts
    `#timelineBody`'s box is the same before and after a pick, and
    test_timeline_lines.py's double-click is the one that caught it.
  - **It follows `S.timeline.sel`, cheaply.** `syncTimelinePane` runs on
    every `renderTimelineRows` (so every scroll frame) and only redraws
    when the selected row changes or its page object does; the row is
    asked for (`GET /api/timeline_row`) debounced, so holding j does not
    fetch every line it passes. After a rebuild `refreshTimelinePane`
    asks again but keeps the old row on screen until the answer lands,
    so a tag key does not blink the pane to "Loading…".
  - **A redraw keeps the note being typed**: the textarea is rebuilt
    with the pending text and the caret put back. A pending note is
    saved before the pane takes another row (`flushTimelineNote`), so
    moving on with j never drops one.
  - **Tagging is one pair through `/api/row_tags`**, which journals it
    (invariant #7), then the usual clearRowCaches / regroup / undo-state
    refresh and a Timeline rebuild. The digits route here only on the
    Timeline tab with a line selected; `undoLastTagChange` rebuilds the
    Timeline when it is the tab showing.
  - **Values are shown as stored.** The grid's detail pane formats a
    datetime column with the open table's display format (`displayCell`
    reads `S.columns`); this pane's row is from any table, usually not
    the open one, so it does not.
  - **The rows around a finding** are `S.timeline.context` (`{minutes,
    around, start, end}`, reset on a case switch), posted as
    `build_timeline`'s `context`. They come back with `ctx` set and get
    `.ctx` (hatched, faded, ○ in the gutter). The header's count is the
    findings (`row_count - context_rows`); the chip carries the rest.

- **The Timeline reads as a story** (`timelineRowNode`'s `story`, which
  only `renderTimelineRows` passes; Configure tables…' preview keeps the
  plain timestamp, since its lines are samples rather than neighbours):
  - **The Timestamp cell holds the whole ISO timestamp**, on every
    line, and so do the pane's large "when" and both chips. The story
    redesign had it as the time of day with the day said once at the
    first line of each ("Fri 13 Mar") and in the column heading;
    ISO was asked for back — a line read, copied or compared on its
    own has to carry its date, and every other place in Winnow writes
    times as `YYYY-MM-DD HH:MM:SS`. The day is still marked by the
    `day-start` rule. **A line is found by its `data-ts`**, not by the
    cell's text; the UI tests' `_line` helpers match
    `.timeline-row[data-ts^='…']`. `timelineDayLabel` survives for the
    overview's axis, which has room for a day only at its first tick.
  - **The rules are borders inside the row** (`day-start`, `gap`), and
    rows are border-box, so a rule costs no height: the virtual list
    places every row at `pos * ROW_H`, and a row one pixel taller would
    drift every line under it. `tests/ui/test_timeline_story_lines.py`
    holds every row to one height.
  - **`gap` and `new_day` come from the server** (store.md), since the
    line above the first line on a page is on another page.
  - **The lead does not shrink** (`flex: 0 0 auto`, up to 55% of the
    cell); the note and then the details give way. With the details
    pane open the Body column is narrow, and a lead that shrank with the
    rest came out as "PowerShell script…".
  - **Fold repeats is off by default** (`winnow.timelineFold`): a line on
    the Timeline is a finding, and a fold nobody asked for hides
    findings. `S.timeline.unfold` holds the runs opened, by first row,
    reset on a case switch and on the toggle. A tag on a folded line
    goes on its first row only, which the pane says. A selected row that
    folds away (`timeline_position`'s `folded`) leaves its run's line
    selected, as `{pos, rid: null}`, which the line's row fills in when
    its page paints, the way a line picked by key before its page
    arrived does.
  - **The header's switches are tinted when on, not filled.**
    `.btn[aria-pressed]` fills with `--accent-dim`; Overview's and
    Details' rule set the text to `--accent` and kept that fill, which on
    the light themes was gold on gold.

- **The Timeline's tag filter is the grid's tag chips** (`.tag-chip`,
  renderTimelineTagFilter). It asks what the ribbon asks, from the same
  strip across the top, and was the one tag control in the app answering
  with a form: a checkbox plus a bare swatch, with none of the chip's
  pressed state, hover or per-skin treatment. Pressed means "on the
  timeline" and every tag starts pressed, since a timeline is every tagged
  row until it is narrowed — **including a tag created after the case
  opened**. What is kept is the set switched OFF (`S.timeline.tagsOff`;
  `timelineTagIds()` is S.tags minus it), not a list of the ones on: that
  list was snapshotted when the case opened, so every later tag started
  off and its rows were missing until its chip was clicked or the case
  reopened. A tag the analyst switched off stays off through any number
  of new ones. The chip's state is an attribute the function writes, so
  clicking one repaints the strip before it rebuilds (a checkbox kept its
  own state, this does not). Two parts of the ribbon's
  chip are deliberately absent: the COUNT, because the ribbon's is scoped
  to the open table's view while this list is case-wide, and the open
  table's number under a case-wide chip is a wrong answer rather than a
  missing one; and the HOTKEY, because the digits tag the grid's selection
  and there is nothing to tag here, so the key would be a promise the tab
  cannot keep. The empty line now says **which** of three reasons applies
  (`emptyReason`) — every tag off, some on with no rows carrying them, or
  nothing tagged anywhere — because switching a tag off is one click now,
  and "tag some rows in any table, then come back" over a case full of
  findings sends the analyst looking for work they have already done.
  `tests/ui/test_timeline_tag_chips.py`.

- **Configure tables… is per HEADER SET, so it lists header sets, not
  tables** (`openTimelineSourceConfig`, `timelineHeaderSets`). A timeline
  template is keyed by a table's own columns (derived ones left out —
  the key `_resolve_timeline_configs` looks up), so one setting serves
  every table of that shape. The dialog it replaced drew one block per
  TABLE, each with a Save: in a case with two EvtxECmd tables, saving one
  rewrote the other's Timeline lines, and nothing on screen said so. Now
  each header set is one entry naming every table it covers, and the
  right side says "Applies to A and B — and to any table with these N
  columns". Things that are easy to undo by accident:
  - **The saved/default mark mirrors the server's lookup**
    (`headerSetTemplate`): the template under the set's own columns,
    else one an older release saved under a table's every column. A
    client that only checked the first would call a set "default" while
    the Timeline still applied the old template.
  - **A blank Type saves as blank**, not as the first table's file name
    (what the per-table dialog sent). The template is shared, so that
    name went on every other table's badges; blank lets each table read
    as its kind (the nickname-derived pill, `timelineKinds`).
  - **Edits are kept per header set until saved** (`state.drafts`):
    switching sets keeps them, the list marks each `unsaved`, and the
    footer names the others, because Save writes only the set shown.
    Closing the dialog still drops them, as the old dialog did.
  - **Save is disabled until something changed, and its state is put
    back AFTER `oneAtATime` settles** (`renderFoot` in the click handler,
    around the guarded call). `oneAtATime` restores the `disabled` it
    found when the click landed — "enabled" — so setting it from inside
    the handler is undone on the way out, leaving a just-saved set's Save
    clickable.
  - **The preview is the server's, not a re-implementation**:
    `POST /api/timeline/preview` runs `_timeline_parts` (the code
    `fetch_timeline_rows` uses) over a few rows under the UNSAVED settings, and
    the rows are painted by `timelineRowNode`/`paintTimelineBody`, the
    Timeline's own row code, minus the raw/summary toggle (that state is
    the Timeline's, `S.timeline.raw`). Debounced, with a sequence number
    so a slow answer for the previous set can't paint over the current
    one.
  - **Saving rebuilds the Timeline when it is the page showing**
    (`S.activeTab === 'timeline'`), so the analyst closes the dialog onto
    what they saved. It used to apply only on the next build.
  `tests/ui/test_timeline_config_builder.py`, and
  `tests/test_timeline_preview.py` for the preview matching the Timeline.

- **IOC watchlist tab** (watchlist.js) — case-level indicators (Store.watchlist / watchlist_hits, in the .db) scanned across every table; matches are counted, listed, and optionally auto-tagged through the normal tag path. **The scan is a background job** (`runScan`, the one helper behind Add, Scan all, the file import, From a case, the jobs.js source-done hook and Search-all's "Add to watchlist"): `POST /api/watchlist/scan/start`, a jobs-panel row with the tables-scanned progress (its ✕ and its Cancel both stop the scan), a 400 ms poll with the search-all rule that a **404 means stop** (superseded from another window, or the case closed) and a bound on any other failure (three in a row — the server exited or crashed — settle the row and the markers rather than polling forever), `#wlStatus` mirroring "Scanning 3/12 tables…" while the tab shows. Starts from one window go out one at a time, and **a newer scan folds the running one**: the server widens the new job to the old one's remaining scope, so the row being followed settles as "folded into the newer scan" (never orphaned in the running state with a Cancel that reaches nothing) and its "…" markers stay until the job that covers them lands — `finishScan` clears only the markers in the finished job's `watchlist_ids` (all of them when it was unscoped). **A new entry is in the list the moment the server has it** — the add/import responses already carry the indicator(s), so the row renders from them with "…" for a count (the in-flight ids live in a module-level Set, not on the row object: any `load()` mid-scan replaces `indicators` wholesale and would drop a flag carried there) and the scan that follows is scoped to the new ids. When the job lands: `load()` if the tab is showing, else the badge refresh and — when it found something — the row turns into the sticky "Watchlist: N hits · Open watchlist" alert; and when an auto-tag landed on the OPEN table — or on a member of the open merge, whose rows are the member's (invariant #9) — both row caches are cleared and the ribbon counts refreshed, or the tags would not paint until something else refreshed the grid. A grouping BY TAG is regrouped with it (`regroupIfGroupedByTag`, the same trio every other tag path ends in), or the auto-tagged rows would sit in "(untagged)" with the pre-tag counts until the analyst regrouped by hand. The repaint itself waits for the grid to be showing: `render()` against a grid a page tab hides measures a zero-height viewport and paints the first rows at the top, and the return to the tab restores the real scroll position over an empty viewport — so `finishScan` sets `S.gridRepaintPending` and `showGridTab` (the Alt+1, tab-history and mouse-thumb return paths) pays it on the way back, rail and regroup included. "Showing" is `gridIsShowing()` (sql.js), not `S.activeTab === 'grid'`: the home screen hides `#app` wholesale with the grid still the active tab. `showGridTab` consumes the flag whether or not it repaints — `openSource` and `openCase` pass `repaint: false` precisely because S.view/S.sourceId are still the OLD table's there, and they satisfy the owed repaint themselves once they have swapped (`installView`, or the cached-view path's own render/rail/regroup). A duplicate value is refused by the server (400 → toast). **The hits pane groups by table**: `/api/watchlist/hits` answers `{sources, hits}` with an exact per-table count and up to 200 rows per table in rid order; one sticky `.wl-hit-group-head` per table (label via `sourceLabel`, arrow and count styled like `.group-header-row`/`.group-header-count`), folding is client state, and a table past the cap ends in "…and N more — open the table" (jumpToTimelineRow). **The tab opens on the latest hits across every indicator**, not on "Select an indicator to see its hits": `renderLatestHits` paints `/api/watchlist/latest` — one line per flagged ROW naming the indicators that matched it, the table, the row and when, newest first — because a case with thousands of findings spent half the page on an instruction, and "which of these fired most recently" could only be answered by clicking every entry in turn. Clicking an indicator's name in a hit line, or a row on the left, narrows to the grouped-by-table pane; clicking the selected row again, or `← all indicators`, goes back. Every `load()` repaints the pane now, not only the one where the selection died under it — a scan that just landed changed what "latest" means. **The count cell is not always a count**: the same `.wl-count` cell takes a `.wl-state` variant that says `clean` (every table read, nothing matched — the one an analyst can quote), `not scanned` or `N/M tables`, off `scanned_sources`/`scan_targets` from `/api/watchlist/overview`. The slot class stays on whatever the answer is — stylesheet and tests find the cell the one way — and the variant only drops the pill geometry a number wanted. **Indicators covering the same rows carry a `.wl-dup` marker** and the summary leads with distinct rows flagged, naming the hit total for what it is ("counted across indicators") — `mimikatz` and `mimikatz.exe` both reported 2,323 hits and the summary added them. The marker opens a dialog that merges the pair through `POST /api/watchlist/merge`, and only ever offers the direction that loses no rows. See docs/design/analysis-suite.md and the scan entry in store.md.
- **Importing a list of indicators asks about the list**
  (`openWatchlistImport`). "Import…" read the ADD row's two selects — at
  the other end of the page, set for whatever single indicator was typed
  last — so a file of two hundred hashes went in as whatever kind that
  happened to be, with an auto-tag nobody had chosen for it, and nothing
  on screen said so before or after. The file is read first and the
  dialog reports what it found (`N indicators from <file>`, the blank and
  #-comment lines it ignored, the first eight values) and then offers the
  Type and Auto-tag for the batch. The count is there to be checked: a
  list pasted into the wrong file, or another tool's CSV that still has
  its header row, is visible here and invisible from a file picker. The type
  select is CLONED from `#wlKind` rather than re-listed, so index.html
  stays the one place the kinds are written down, and the auto-tag
  select goes through the same `fillAutoTagSelect` the Add row's does. The
  line rules stay the server's (`/api/watchlist/import`,
  `Store.parse_indicator_list`: blanks and #-comments dropped, a per-line
  `value,kind` overriding the default) and are mirrored here only to count
  and preview — the import posts the text as read, never a client-parsed
  list. One count that is NOT the server's rule: a file's final newline is
  not a blank line anybody wrote, so trailing empties come off before
  anything is counted. `tests/ui/test_watchlist_import_options.py`.
- **The Watchlist's own Export… goes back in through Import…** Export
  writes a CSV under a header row (`EXPORT_COLUMNS`: value, kind, note,
  hits), and the import read it as a plain list: the header became an
  indicator called `value`, each kind the rest of its line
  (`filename,,3`), and a value or note holding a comma was cut at it. A
  first non-comment line made of export column names — `value` first, at
  least two, none twice — is now that header, and every row after it is
  read as CSV (quoted commas and line breaks included), keeping its kind
  (an empty one takes the dialog's Type) and note; `hits` is left to the
  scan. Recognised on IMPORT rather than changing the export, so files
  written before the fix import cleanly too. Only that header switches:
  a plain list whose first indicator is the word `value` is one cell, not
  a header. The dialog mirrors the rule (`exportHeader`, `csvRows`) to
  count rows rather than lines and to say the types come from the file.
  `tests/test_watchlist_export_import.py`,
  `tests/ui/test_watchlist_export_roundtrip.py`.


- **Entity pivot** — a plugin, not a module: `examples/plugins/pivot` (there is no `entity.js`). The store's `entity_pivot` reuses the blob search + TS_NORMALIZE.

- **Case dashboards** (dashboard.js, dashwidgets.js) — named boards of widgets, each a data source (sql via read-only run_sql, watchlist, tags) plus a render kind (stat/kv/chips/list/bar/histogram), plus one card kind that is a grid of several: `render: signals` / `source: cells`, where the widget carries `cells: [{label, source, query, drill, tone?, chip?}]` and **every cell keeps its own drill**. That is what let the KAPE board fold eleven single-number cards into one without folding eleven drill-throughs into one OR-of-everything — `Store._cells_preview` runs the cells and answers with label/value rows plus a parallel `cell_errors`, so one card is one request and a cell whose table is missing from the case reports in its own place instead of taking the card's other numbers with it. `paintSignals` looks a cell's metadata up by LABEL rather than by position, so a payload cached before the card was edited still lines its drills up with its numbers; the label is in the store's fingerprint for the same reason (unlike a card title, it is IN the answer). The editor shows a signals card's cells read-only — it writes one question per widget — and carries them through Save. Widgets are built from RECIPES (dashwidgets.js `WIDGET_TEMPLATES` + `widgetFrom`): a template, a table and the column/value it needs produce the SQL, the render, a `build` (the recipe, so the editor reopens guided) and a `drill` — `{table, where:[{column,op,value}] | tree: <filter-tree node>, column?, bucket?}` or `{table, spec}` for a count-of-this-view widget — which `drillInto` turns into the grid opened on those rows: `openSource(id, { skipBuild: true })`, every stashed filter/search/tag/timeframe reset, then one view build (placeholder tables resolve through `POST /api/dashboard/resolve`, which lists every source a `{{all:…}}` spans so the analyst picks one; a widget with SQL but no drill opens as a query in the SQL pane; a bucket the timeframe can't express is refused, and a bucket on a column not typed datetime filters by the label's prefix instead). The shipped KAPE drills are checked against their SQL on a fixture in tests/test_dashboard_drill.py: a stat's drill opens exactly the rows it counted. Hand-editing a recipe's SQL drops `build` and `drill` rather than leaving them describing a query they no longer match. Entry points that skip the editor: the column header menu (top values / distinct / over time), the row menu (count of this value) and the Filters menu (count of this view), all through `quickAddWidget`, which asks which board only when there are several. `createDashboard` offers a starting point — blank, a starter built from the open table (`buildStarter`: count, activity window, over time, top values of 2–12-distinct columns), a shipped board, or a library board. Layout lives in the case .db; 'Save as profile' extends a plugin bundle with the board. The shipped KAPE triage board carries hand-written drills (checked against the header sets in tests/test_dashboard_drill.py). It is **ten cards**, consolidated from 26: five registry kv cards into one **Host** card that omits a fact the batch did not carry instead of rendering four cards reading "(not in this RECmd output)", two chips cards plus the two tampering counts into **Logging posture** (one card, two tables — the cells are asked separately, so a case with no RECmd batch still gets the tampering numbers), coverage plus the activity window into **Coverage** (whose rows name their own population — `Security: oldest` beside `All channels: window` — because a kv card's label is the only place a row can say what it counted, and four Security-only numbers under unscoped labels next to an all-channel one cannot be reconciled from the card), and ten single-number stats into **Triage signals** (ten cells) and **Findings** (the watchlist and tag counts, the one card on the board marked `live`). Spans are 2,1,1 / 3,1 / 2,2 / 2,2 / 2 — every intended row sums to four, because `grid-auto-flow` is left at its default and a card that does not fit drops to the next row and leaves a hole. Existing cases are NOT migrated: `upsert_dashboard_by_name` only runs on apply, so a case that has the 26-card board keeps it until the profile is applied again. See docs/design/analysis-suite.md.

- **A card that resolved to nothing is a line, not a panel** (dashboard.js `empties`/`renderEmpties`, `.dash-empties`). Walked against a real KAPE collection, the first screen of the shipped triage board was two full-width cards reading "(no host facts in this RECmd output)" and "(no Defender alert events in the logs)". Nothing was wrong — neither artefact was in that collection — but the board spent its best space saying so and never said what would change it. Empty cards now fold into one dashed strip at the top of the grid, a line each, and the card itself is hidden rather than removed (`.dash-card.is-empty`) because `paintAges`, `refreshBoard` and the editor's Run now all reach a card by its INDEX among the grid's cards. Five things to know before touching it. **The two kinds of empty are not the same answer and the strip has to say which**: a `{{registry}}` placeholder that binds to nothing is a gap in the collection an import would fill, and a query that ran and matched nothing is a finding ("no Defender alerts in these logs") with nothing to import — an analyst about to write "not present" in a report needs to know whether anything looked. The distinction comes off the wire, not off the message text: `Store.MissingTable` (a ValueError subclass, so every `except ValueError` on the widget paths is unchanged) carries the header-set name, and `/api/dashboard/widget/preview` answers `400 {"message", "missing_table"}` — core.js already unwraps a structured `detail.message`, so nothing else had to change. Sniffing for "table in this case yet" would have worked until somebody reworded it. **A `stat` is never folded**: 0 is an answer, and folding "0 failed logons" away hides the reassuring half of a triage board. Nor is a `signals` card that answered any cell — a cell that could not answer already reports in its own place. **The shipped queries no longer UNION a sentinel row in.** The Host and Defender cards each appended a literal "(no …)" row so the card would not render as a blank box, and that one line is what held the full-width card; they answer with NO ROWS now and the board says why (tests/test_kape_host_widgets.py, tests/test_kape_host_overview.py pin it). A shipped or profile widget that adds a sentinel row back becomes un-foldable, by construction. **Emptiness is decided per answer, not per render**: `noteEmpty` is called from the cache paint, from every run that lands and from the editor's Run now, keyed by the card ELEMENT (a widget has no id until the store mints one, and the answers arrive out of order), and the strip repaint is rAF-coalesced for the same reason `scheduleBar` is. **The fold is lifted BEFORE the paint, not a frame after it.** `noteEmpty` is asked first and returns the empty state, so a caller paints only what it says has something to paint, and a card leaving the strip has `is-empty` taken off it synchronously there rather than on `renderEmpties`'s rAF. A folded card is `display:none`, and `bar`/`histogram` measure their canvas inside a `requestAnimationFrame` of their own — unfold a frame late and a card that finally got its rows (the RECmd batch imported, then ↻ Refresh all, which runs quiet and never calls `render()`) comes back with a chart drawn at `fit()`'s 300×150 fallback and stretched across a span-2 card, whose `drawBars` boxes `pickBar` then tests against real click offsets: clicks past ~300px drill nothing at all, and the ones that land are a row out, because the row height is the canvas height over the row count. Nothing repaints a card on resize, so it lasts until the next full `render()`. tests/ui/test_dashboard_empty_cards.py.

- **A board paints from its last results, it does not re-run itself** (dashboard.js `cache`, Store `dashboard_widget_cache`) — the shipped KAPE board was 26 widgets, and opening it used to be 26 `POST /api/dashboard/widget/preview` calls, every open, every card drag and every edit of any other card (measured: 26 previews per reopen; the consolidated 10-card board is 1, the live Findings card). `GET /api/dashboards/{id}` returns the widget definitions AND the cached payloads, `render()` paints them at once and runs only the widgets carrying `"live": true`, and `↻ Refresh all` (or Run now in one widget's editor) is what re-runs the rest. Traps, in the order they bite: **widgets had no ids** — position in a JSON list was the only identity and a drag rewrites it, so `Store._mint_widget_ids` assigns one on write and `get_dashboard` back-fills one DETERMINISTICALLY on read (a random back-fill would hand a board that nobody has edited a new key on every open, and it would never see a hit). **The cache cannot live in the widget dict**: `saveToLibrary`, `saveAsProfile` and `upsert_dashboard_by_name` copy widget dicts verbatim into workspace JSON and into other cases, and `set_dashboard_widgets` clears `origin` on every write — so a payload in there would carry case A's numbers into case B and silently un-stamp a plugin-offered board. **The editor's preview must not read the cache** (it runs an unsaved draft, and a preview that answered from the cache is not previewing), which is why caching keys off `dashboard_id`+`widget_id` in the request rather than off `runWidget` — and why the live preview sends `widgetRequest(w)` bare (see the widget-editor entry below). **A cached number with no date on it reads as a live one** — hence the bar's "As of 10:42 · 3 minutes ago", the per-card age in `.dash-mark`, and `Store.data_generation` (source count + row total + max id, read off `sources`, plus `STATE_GENERATION_KEY`, a counter the conclusion writes bump because `row_tags` and `watchlist_hits` are both WITHOUT ROWID and too big to count on every board open), which marks a result stale rather than hiding it. The counter is the half that can be FORGOTTEN, so every writer of those tables bumps it inside its own transaction — the tag delta path and its undo, but also the three writes that replace tag state wholesale and bypass that path (`delete_tag`, `import_session`, `start_new_session`), `upsert_tag` (a tags widget lists one row per definition), and the watchlist's add/delete/scan (a watchlist widget's number is the sum of the hit counts). What it does NOT cover is stated in its docstring rather than left to be discovered. **Staleness is only computed when a board loads**, so a board left open through a twenty-minute import would go on claiming to be current: `markDashboardStale()` is called from the jobs poll and the watchlist scan, and it ASKS the server rather than assuming, so a re-scan that changed nothing does not redden a board that is fine. **A board switch mid-flight** would file the old board's answers under this one's widget ids, so `boardGen` guards the write the way `S.pageGen` guards the page cache — and it guards the board's own load too: `loadWidgets` compares it after its await and tells `showDashboard` not to paint, because for months it only bumped it, and board A answering after board B had been clicked put A's cards under B's name. **What is written back goes to the board it was read from** (`widgetsBoard`), never to `S.dashboardId`: that moves at the click, while the cards on screen — the ones a drag or an edit acts on — are still the previous board's until the new widgets land, so `persist()` saving to `S.dashboardId` wrote board A's cards over board B's own with one drag (`tests/ui/test_dashboard_edit_mode.py`). **A failed ↻ Refresh keeps the numbers**: blanking every card because the server blinked throws away the only thing the cache was for, so a quiet run that fails leaves the card alone and marks it `!3m`. **A card that answered in part is not filed at all**: a widget that fails is reported and not cached so the next open retries it, and `Store._answered_in_full` applies that same rule to a `cells` payload carrying any `cell_errors` entry — cached, one cell that blinked would paint its em dash out of the case file until somebody pressed ↻. Both writers check it (`cache_widget_result`, `refresh_dashboard`) and both say "not filed" the same way, by answering without a `ran_at` — which is already the flag `runWidget` and the editor's Run now key their own cache write off. The cost is a card that can never answer in full (the five registry cells of Logging posture on a case with no RECmd batch) running again on every open, which is what a whole widget in that state already costs.

- **The widget editor's preview is live, and an answer belongs to a QUESTION, not to a request** (dashboard.js `openWidgetEditor`: `runPreview`, `changed`, `shown`/`asking`). The editor is in the builders' shape now — the question on the left (Data from, What to show, Table, the recipe's picks, Advanced — the SQL, Title), how it looks on the right (Render as chips, Sub-label, a Width segmented control, Run every time and the cost line) above a `.dash-card.dash-preview` that runs the same `draft()` Save stores, `PREVIEW_DELAY_MS` (500) after the last edit, with no Preview button. Things that bite. **Stale answers are dropped by comparing the answer's `questionOf` with the CURRENT draft's**, not by a sequence number bumped per edit: an edit that only changes the look (render, title, sub-label, width) does not ask again — `changed()` repaints the answer already on the card at once — so a counter bumped by that edit would discard the in-flight answer that is still right, and nothing would ever ask for it again. `asking` dedupes the one question already out (edit away and back inside the pause and the answer still paints when it lands). **There is no cancel**: `/api/dashboard/widget/preview` is a sync route with no `op_token`, so a half-typed runaway (a cross join before its WHERE) runs to the end on a server thread; the client only refuses to paint it. Making it stoppable is the SQL pane's recipe — an async route through `_run_pane_query` and an `AbortController` on the fetch — and has not been done. **The preview names no board and no widget**, so the server files nothing and an unsaved draft never replaces the number the board's card is showing (tests/ui/test_widget_editor_preview.py reads the case file's cache back after an Escape). **"Is this dialog still open?" is `b.isConnected && !#modal.hidden`**: Escape hides the modal and leaves its body in the document until the next `modal()` replaces it, so `isConnected` alone says yes to a timer that fires after the dialog closed. The first run is a `setTimeout(…, 0)` for the same reason in reverse — `modal()` unhides only after the builder returns. **A draft that cannot run says why and asks nothing** (`whyNot`: no query, a template whose column the table cannot offer, header sets still loading, a signals card with no cells); an empty answer says what the board will do with it (fold into the empty-cards line, via `emptyStateOf`/`emptyReason`) rather than drawing a box the board never shows. **Run now runs the SAVED widget**, so it repaints the preview only when the draft still asks the saved question. **Names**: `WIDGET_SOURCES` / `WIDGET_RENDERS` (dashwidgets.js) map the stored values to what the editor shows ("A query", "Signals card"; "Number", "Key / value", "Yes / no chips"…); the stored values never change, a source this version has no name for is kept as an extra option rather than becoming "A query" on Save, and an unknown render stays the widget's render with no chip pressed. **Title moved to the bottom of the left pane**, so `#modal .confirm-input` first is the recipe's Value box now — tests select the title by `.dash-title-input`, Render as by `.dash-kinds [data-render]` and Width by `.dash-width [data-span]`. Save widget is `oneAtATime` (it awaits `persist()` before closing, and a double-click pushed the widget twice). tests/ui/test_widget_editor_preview.py.

- **Profiles: a two-pane manager, and a sheet in front of Apply** (bundles.js; `WS.plugin_bundles`, `/api/plugin_bundles/*`). A profile is a kind of case — plugins, boards, a starter watchlist, variable definitions — and the list this replaced was one ellipsised row per profile, with the shipped description pushing Apply and Copy off the end of it. The manager (M) is `.pm` : profiles left with a one-line count summary, the whole selected profile right — every plugin, every board with EVERY widget's render kind and whether it is `live`, the watchlist, the variables. Sections carry `data-sec` (plugins/board/watchlist/variables) because a board section is headed with the board's NAME and "Watchlist" is also the start of a widget title on the shipped board. **The sheet is the point.** Apply sets an explicit on/off override for every installed plugin (so everything not in the profile is turned OFF), upserts boards BY NAME, seeds indicators and starts a scan, and said none of it first. `GET /api/plugin_bundles/{id}/plan` counts each of those against the OPEN CASE — which plugins go on and off and how many stay off, what each board replaces and by how many widgets, which indicators are new and how many tables seeding them scans (`Store.watchlist_scan_sources`, the scan's own predicate, so the number cannot disagree with the scan), which variables are required and unset (a required one with a declared DEFAULT is not among them: the apply creates the row carrying the default, so nothing prompts for it) — and `POST .../apply` takes `parts` from `APPLY_PARTS`, so a board can be taken without the watchlist. The plugins part stays ticked even when the case's set already matches the profile, because applying writes an explicit override per installed plugin and that write is what pins the case against a later machine-wide toggle. **No body still means all four parts**: the new-case dialog's Profile select and every script predate the sheet. **"You have edited this board" is answered by `origin`**, which the profile apply now stamps `profile:<name>` — any hand edit clears it (`set_dashboard_widgets`), a board stamped by this profile is its own untouched copy and worth no warning, and a plugin offering a board of the same name still asks, because a profile stamp is not a plugin stamp. A board applied before the stamp existed reads as edited, which errs toward warning. **Which profiles this case has had applied** is a `profiles_applied` case setting (a name → timestamp map) — the badge cannot be inferred from a board of the same name, which the analyst may have built by hand. **One profile exports as one file** (`winnow-profile/1`, `{format, profile}`) the way saved filters already do, minus the id and the `shipped` flag; a SHIPPED profile exports as a copy that records its ancestry. Import REFUSES a key it does not understand rather than dropping it, and refuses a KNOWN key holding the wrong kind of value for the same reason (`PluginBundles.IMPORT_SHAPE`; `save` is permissive by design, so `"plugins": "lateral_movement"` would store one plugin per letter and a string `dashboard` would reach the manager as a board whose widgets cannot be iterated). It never upserts by name either — a file from outside must not overwrite an afternoon's work, so a taken name is suffixed `(imported)`, from a base trimmed to leave room for the suffix, since a 100-character name plus a suffix truncates back to itself and the search for a free name never ends. **Lineage**: a copy records `from_profile`/`from_version` (written by "Copy to edit" and carried through every later edit, since a content comparison would lose the thread on the first edit), `profiles.json` carries a per-profile `version`, `list()` answers `update_available` when the shipped one is newer, and the manager shows one line offering the diff. Taking it is `POST .../take_update`: always a button, never a merge — an analyst's copy and a revised shipped profile can disagree about any part of a board, and a three-way merge of SQL widgets produces a board neither of them wrote. tests/test_profile_export.py and tests/ui/test_profile_manager.py.

- **The profile builder is the builders' two panes, and its Save waits
  for its lists** (profilebuilder.js, `.pb-*`). It was one column of three
  collapsible Settings sections, each variable a row of five boxes whose
  only captions were placeholders — gone the moment anything was typed.
  Left is what the profile turns on (name, Plugins, Dashboards), right is
  what a case gets: one sentence saying what Apply will do, the variables
  as captioned cards, and the values prompt. Four things to keep.
  **The frame is drawn before `/api/plugins` and the board lists answer**
  (so `modal()`'s `focus` finds the name box), which means Save exists
  before there is anything to save: it stays disabled, and its handler
  unwired, until the lists land — live in that gap, a click would save a
  profile with no plugins and no boards. It used to be safe by accident,
  since the button was only drawn after the awaits. **The preview IS the
  prompt**: `variablePromptFields` (settings.js) builds the fields for both
  `promptForVariables` and the builder, which renders them under `inert` —
  a look-alike drawn here would drift the first time the prompt changed,
  and an inert copy takes no focus and no typing. **The summary counts what Apply does, not what
  is ticked**: a ticked plugin this machine lacks is not "turned on" (it
  is a note instead — apply skips it), and no plugins ticked reads "Turns
  every plugin off", because that is what the plugins part does. **The
  name hint is about upsert-by-name**: `PluginBundles.save` matches on
  name, so a taken name replaces that profile and renaming one while
  editing saves a second beside it; both are said under the name box
  before Save rather than discovered in the manager afterwards.
  tests/ui/test_profile_builder_ui.py.

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
  Tables manager row says "from <parent>" (`subsetParentLabel(s)`) —
  without the "(N of M rows)", since the Rows column is beside it; its
  Merges & saved tables pane has the N of M. The
  its-own-tags line is deliberate: an analyst may expect the parent's
  tags to have come along, or the subset's to appear on the parent, and
  the tooltip is where both expectations get corrected.

- **The note box is ahead of the server while a save is pending, and
  `showDetail` must not undo that** (detail.js `keepsUnsavedNote`,
  `flushNoteSave`, `writeNote`). The box saves 500 ms after the last
  keystroke, so every row object a fetch brings back in that window (and
  while the POST is out) has the old note. `showDetail` is not only a
  cursor move: `ensurePage` re-shows the cursor row whenever a visible
  page lands, and anything that clears the row caches makes one land — a
  derived column finishing, an import, a tag written elsewhere — so
  setting the box to `r.note` there emptied it mid-sentence and the rest
  of the sentence was then saved alone. The box keeps its text when it is
  already bound to that row (`dataset.rid`/`sourceId`) and `noteUnsaved`
  is set — on a keystroke, cleared only when a save of exactly what the
  box holds, for the row it is bound to, has come back. Not on focus:
  a box with focus and nothing typed should still show a note a session
  load or another window wrote. Any other row takes the box over, and
  the save still pending for the row being left is **flushed before the
  rebind**: it reads its row when it fires, so a cursor move inside the
  500 ms used to post the new row's own note back to it and drop what
  was typed on the old one. "Saved" only appears when the box still holds
  what that save carried, and **only a new row or a new note clears it**:
  a landing that re-shows the same row with the same text leaves it. It
  used to be blanked on every `showDetail`, and a save on a row whose page
  isn't cached asks for that page itself (`refetchPageOf`, after a
  scrollbar jump), so its own repaint took "Saved" away a few ms after it
  came — test_scrollbar_drag_lands.py only passed when a frame happened
  to fall in between. tests/ui/test_detail_note_autosave.py.

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
  by hand across a wrapped pretty-printed block. It copies the
  **displayed** cell (`displayCell`: a datetime column's chosen format and
  a duration's units, as the grid's copies do; "Copy row" copies the
  stored text) but not the pane's pretty-printing of it, and finds
  that cell through `#detailFields`'s `dataset.pos` rather than a value
  stashed on the button — a multi-KB payload duplicated into an attribute
  would put the row in the DOM twice. The button is not a tab stop: forty
  of them between the pane's own buttons and the note box would bury the
  keyboard path that exists.

- **The detail pane lays out documents JSON.parse refuses, and only a
  whole-value one is addressable** (detail.js `findLooseDocuments`,
  `appendLooseNodes`, `looseDecode`). It only ever tried a strict
  whole-value parse, so three shapes analysts actually have came out as
  one unbroken line: Python's repr of a list or dict (`[{'Name':
  'UserAgent', 'Value': ...}]`, what `str()` of parsed JSON writes and
  what a pipeline through pandas leaves in a CSV), JSON a lax exporter
  wrote (a raw newline or tab in a string, an unescaped `C:\Windows`, NaN,
  a trailing comma), and a document inside other text (`[INFO] {...}`,
  `[ctx {...}]`). The strict path still runs first. The loose one is a
  structure-only parser: brackets, strings of either quote (prefixes like
  `b'...'` included), separators and a short list of bare words. Five
  things it does on purpose. **Tokens are shown as the cell writes them**,
  never decoded and re-encoded: a selection lifted out of the layout has
  to match what the contains-filter searches, and `'Name'` re-quoted as
  `"Name"` would match nothing; it is also why a FILETIME past 2^53 keeps
  its digits here (the strict path still rounds them through JSON.parse).
  **A path only when the document is the whole value**, give or take
  surrounding spaces and line breaks, and is at most `LOOSE_MAX_CHARS`
  long: structparse.load_json falls back to
  `load_loose`, the same grammar in Python, and reads a cell only when all
  of it is one document, so a path on a document inside other text would
  fill the column with nothing on every row. The two parsers are twins
  and have to accept exactly the same values, and `looseDecode` has to
  decode strings, keys and numbers exactly as `load_loose` does (the path
  holds `UserAgent` where the cell says `'UserAgent'`);
  tests/ui/test_detail_name_value_paths_ui.py renders values in the
  browser and extracts every offered path in Python to hold them to it.
  **Only `[` and `{` start a candidate, and only a dict with an entry or a
  list holding a non-empty container counts**: JSON.parse takes the
  `[2099123]` of `hostd[2099123]:` and the `[0]` of `items[0]`, and prose
  is full of parentheses, so a tuple only reads as one inside a list or
  dict. A failed candidate is retried one character later, which is how
  the document inside a `[ctx ...]` wrapper is found. **One budget bounds
  the whole scan** (`LOOSE_SCAN_BUDGET`, every character and token across
  all candidates, plus `LOOSE_MAX_DEPTH`): retrying per character is
  quadratic on a run of `[`, and showDetail runs on every cursor move.
  Past it the rest of the value is text. **The depth limit is shared**:
  `LOOSE_MAX_DEPTH` is 256 on both sides, so a document the pane gives up
  on is one the backend refuses too. tests/ui/test_detail_loose_documents_ui.py.

- **A document is measured before it is laid out, and the note box is
  bound before any field is** (detail.js `structTooBig`, `shownAsText`,
  `appendDetailField`). `appendJsonNodes` and `appendXmlNodes` recurse
  once per level, so ~2,100 nested arrays (a 4 KB cell) or ~3,000 nested
  elements threw "Maximum call stack size exceeded" out of `showDetail`,
  and a 200,000-element array cost ~2 s per cursor move. Evidence decides
  what a cell holds, so this is reachable on purpose. `structTooBig` walks
  the parsed document with its own stack (recursing to measure would
  overflow on exactly the documents it is for) and stops at the first
  node past `STRUCT_MAX_DEPTH` (64) levels or `STRUCT_MAX_NODES` (5,000)
  items — JSON values, XML elements plus their attributes; an XML
  fragment is measured from its own top elements, not the pane's wrapper.
  Past either, the field is the text it holds under a `.dfield-hint` line
  saying which, in a `.dfield-text` that breaks anywhere: `break-all`
  never breaks after an opening bracket, so the attack value as one 4 KB
  line widened the field list until the note box left the pane. The limits are far outside real data (an EVTX record is
  4-6 levels and ~100 items; the busiest JSON exports a dozen-odd levels
  and low thousands), and 5,000 items lay out in ~45 ms. The loose layout
  is measured too, though its parser already stops at `LOOSE_MAX_DEPTH`:
  otherwise single quotes would be the way round the item limit. That
  makes 64 a layout limit there, not a parse one — a loose document 65 to
  256 levels deep is still read by `load_loose`, and the pane shows it as
  text rather than offering paths on it. **The order in `showDetail`
  matters as much as the limits**: the throw came after the title said
  "Line 2" and before the note box was rebound, so a note typed under it
  was saved on line 1. The note box (like the title and `#detailFields`'
  `dataset.pos`) is now bound before the fields are built, and each field
  is built in a try/catch (`appendDetailField`) that shows the stored
  text under "Could not be laid out" — nothing known throws past the
  limits, so that is a backstop, not the fix.
  tests/ui/test_detail_deep_documents_ui.py.

- **A Name/Value list is addressed by name, and the Name stands for its
  Value** (detail.js `jsonKvShape`, `kvNameTarget`; structparse
  `_kv_shape`). `[{"Name": "UserAgent", "Value": ...}, ...]` puts its
  entries in the writer's order, so each entry's path is
  `[Name='UserAgent']`, not `[3]`, in both appendJsonNodes and the loose
  layout. The rules for what counts are in `_kv_shape`'s comment and the
  pane's copy has to agree with it; a disagreement doesn't break
  extraction (the predicate walk doesn't care how a path was found) but
  does make the pane name a column Flatten wouldn't offer. The Name node
  restates the predicate, so it isn't a field of its own: it carries the
  path of the one other key in its entry, which is what makes
  right-click `'UserAgent'` → **Add as a column** produce the UserAgent
  column. Beside two values (OldValue/NewValue) it would be a guess, so it
  carries nothing. Its `data-value` is therefore the Value's text, not the
  name's, and **Filter to this value** on it filters to the Value.

- **A plugin describes itself without being imported, and the plugins
  manager is what that buys.** The Settings → Plugins panel used to show a
  switched-off plugin as its folder name, an "example" badge and the word
  "off" — seven rows reading `mft_usn`, `first_last`, `top_values`… with a
  four-option `<select>` as the widest thing on each, the state printed
  twice (the select said "Off — …" and a right-hand column said "off"),
  and the same badge repeated seven times. All seven bundled examples
  ship a name, a version and a sentence of description; none of it was
  reachable, because a disabled plugin is never imported (that is the
  entire value of an off switch on something that runs with the app's
  privileges) and the record's metadata only existed *after* an import.
  The only way to learn what a plugin did was to run it, which is
  backwards for the decision about whether to run it.

  `plugin_api.static_meta` reads `PLUGIN`, `WINNOW_API_VERSION` and the
  `register_*` calls out of the **source**: `ast.parse` plus
  `ast.literal_eval` on the module-level assignment, so not one line of
  the file executes. A `literal_eval` over a hand-written dict is the
  same trust boundary as reading JSON — it is not `eval`, and it cannot
  call anything. Every failure mode (unparseable source, `PLUGIN` bound
  to something that isn't a dict, a value that isn't a literal) answers
  empty rather than raising: a plugin whose source won't parse is one
  that wouldn't import either, and the listing's job is to describe, not
  to diagnose. A plugin that *fails to load* still reports its error the
  usual way — and now keeps its name while doing it, which is the moment
  it is most wanted.

  Two things the pane is careful to keep apart. For a **running** plugin,
  "What it adds" is the registry's own answer, attributed by `plugin_fs`
  (two plugins may call themselves the same thing; only the folder name
  is unique). For one that is **not running** it is `declares` — kinds,
  never counts, and labelled "from its source". The count is the part
  that cannot be trusted: `examples/plugins/mft_usn` contains three
  `register_ingest_format` calls across its folder and registers two,
  because the third is in a helper module the entry file never reaches.
  A kind survives that; a number doesn't.

- **Plugin scope is two controls, not one dropdown.** The model is a
  machine-wide default plus an optional per-case override living in the
  case file. One `<select>` mixing them could spell every state except
  the useful one — *drop my override and follow the machine again* —
  because the only route back was re-picking an "everywhere" scope, which
  clears the override as a side effect. That is fine when the machine
  setting you want is the one already set, and a silent rewrite of every
  other case's behaviour when it isn't. Hence the `follow_case` scope and
  the Follow button, and a foot line that states the state in a sentence
  rather than leaving two controls to be read together. The per-case row
  is still gated on a case being **on screen**, not on the server holding
  a Store: `#home` and `#app` are siblings and `showHome()` only hides
  `#app`, so `case_open` stays true after it (`tests/ui/
  test_plugin_scope_needs_a_case.py`).

- **A dialog Settings opens in its own modal owes the analyst a way
  back.** `#modalBody` is a singleton, so Settings → Plugins → "Manage
  plugins…" *replaces* Settings rather than stacking on it, and the ✕ hid
  the whole thing — one level up, not where the analyst came from, for a
  dialog reached through two clicks of another one. Three dialogs now take
  the same `returnTo` hand-off (plugins manager, Profiles, Saved filters),
  each naming the Settings section it came from (see the two-pane entry):
  a one-shot `winnow:modalclose` listener armed per opening, guarded by
  BOTH the title and a `pmReturn` flag on the body, plus a `‹ Settings`
  button in the foot. Both guards are load-bearing. The title, because the
  dialogs these open in turn (the installer, the import queue, a profile
  applying) replace the title or hide the modal without the event, and
  none of them should bounce to Settings when IT is closed; the flag,
  because a listener left armed by one of those closes must not fire for a
  later opening that came from somewhere else. The way back is handed
  **in**, never imported: Settings owns its own opener, which is why
  `buildPluginsPanel(b, { returnTo })` takes it as an argument rather than
  importing `openSettings` back across the boundary it is called from.
  `tests/ui/test_plugins_from_settings.py`,
  `tests/ui/test_profiles_from_settings.py`.

- **A click handler reports its own refusal.** `api()` throws on any
  non-2xx with the server's `detail` as the message, and nothing above a
  click handler catches it — so `try { await post(...) } finally {
  setBusy(false) }`, or a bare `await api(...)`, turns a 409 with a
  perfectly good reason into an unhandled rejection: no toast, the busy
  bar gone, and a button that looks as if it did nothing. Session Load,
  Start a fresh pass, the Tables manager's Close/Remove (its open tick
  and ⋯ now), a tab's ✕ and
  five others shipped like that. Catch, toast `e.message`, and return
  before the success path (closing the dialog, reloading, a "Saved"
  toast) — a dialog the action failed in stays open so the analyst can
  try again. `tests/ui/test_refusals_are_reported.py` fails each request
  with `page.route` and looks for the server's words in the toast; the
  page fixture's uncaught-error check catches the rejection itself.

- **html/body are `overflow: clip`, not just `hidden`.** Nothing at the
  page level is meant to scroll — every scroller is a panel of its own —
  but a `hidden` box is still a scroll container, and the browser will
  scroll it programmatically to bring a focused element into view. Once
  the window is narrower than the toolbar's min-content (~717px with a
  few tables open), opening a dialog did exactly that: <body> slid
  sideways and stayed there, widening included, until a reload. `clip`
  can't be scrolled at all; `hidden` stays in the rule first as the
  fallback. `tests/ui/test_narrow_window_dialog.py` finds the width at
  which the suite's case starts to overflow rather than hard-coding one.

- **One dialog at a time, but each gets a body of its own.** `modal()`
  replaces `#modalBody` with a fresh node (same id, same class) on every
  call instead of emptying it. Builders are often async — draw a frame,
  await a fetch, fill it in — and every one writes into the `b` it was
  handed; with the one shared node, a builder whose answer arrived after
  the analyst had moved on wrote into whatever was open by then (a
  Profile Apply sheet's checkboxes and working Apply button turned up
  inside Settings). A stale builder now writes into a detached node and
  nothing sees it, which is the only version of the guard that does not
  depend on each of a dozen builders remembering a generation check. Two
  consequences: never hold on to the `#modalBody` element across a
  `modal()` call (look it up, or use the `b` you were given), and a flag
  set on `b.dataset` lasts exactly as long as its dialog — the old node
  carried the Tables manager's `kind='tables'` into the next dialog, so
  that poll's "is Tables still open?" check never said no.
  `tests/ui/test_modal_body_per_dialog.py`.

- **A button whose handler awaits a write before its dialog closes takes
  `oneAtATime`** (ui.js): disabled from the first click until the handler
  settles, and a click that still gets through is dropped. Without it the
  second click of a double-click ran the whole handler again — every file
  of a folder import queued twice ("Import checked (N)"), two cases
  created on one path (new-case Create), two merges, and a second "Copy
  into this case" that found the target locked by the first copy and
  called it "open in another Winnow". Handlers that hide the dialog
  *before* their first await (the import previews) were never exposed,
  since the second click lands on whatever is underneath; anything new
  that awaits first should take the wrapper.
  `tests/ui/test_double_click_submits_once.py` holds each POST in flight
  until the double-click is over and counts them.

- **Older dialogs borrow the builders' look through two shared classes,
  not inline styles.** The merge and lateral-movement builders set the
  look (dim uppercase section heads, labels above fields, one accent-filled
  main button); the older dialogs drifted from it one inline `cssText` at
  a time — Timeframe's Start and End boxes started at different x because
  each label sat beside its box, and every dialog's Apply looked like its
  Cancel. `.btn.primary` is a dialog's main action (one per dialog — the
  prompt/confirm OK, Apply, Create, Import…); `.dlg-field` is a label
  above a full-width control, `.dlg-pair` two of them side by side,
  `.dlg-input` a box plus its 📅. `.modal-body h4` is dim like the
  builders' heads; the collapsible section heads (`settingsSection`, the
  profile builder's) stay accent because they are buttons. A new dialog uses these rather than restyling a select or an
  input inline.
