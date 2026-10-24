/* The home screen: the case registry, opening/creating cases, and boot().

   Split out of the former single static/app.js — see CLAUDE.md. */
import { applyBundle } from './bundles.js';
import { PAGE_ID, closePresence } from './connection.js';
import { $, api, dragHas, el, post, setBusy, setCaseToken, switchCase, toast } from './core.js';
import { pad2 } from './tsformat.js';
import { loadPlugins, openImportModal, queueFiles } from './importer.js';
import { inFlightWork, startJobsPoll } from './jobs.js';
import { dropPendingTabState, resetPluginTabMounts } from './plugins.js';
import { resetDerivedSuggestions } from './derived.js';
import { hideDetailPane } from './detail.js';
import { resetHistogram } from './histogram.js';
import { resetJobState } from './jobs.js';
import { resetNotes } from './notes.js';
import { resetWatchlist } from './watchlist.js';
import { resetDashboard } from './dashboard.js';
import { loadAppSettings, loadCaseSettings, loadHeaderNicknames, loadSavedFilters } from './savedfilters.js';
import { updateSearchAllButton } from './search.js';
import { clearViewStateStash, applyPageTabsSize, loadCollapsedFolders, loadSources } from './sources.js';
import { clearTabHistory } from './tabhistory.js';
import { resetSqlPane, showGridTab } from './sql.js';
import { openSettings, syncAppearanceFromServer } from './settings.js';
import { drawWordmark } from './splash.js';
import { S } from './state.js';
import { fmtBytes } from './tables.js';
import { loadCaseTags } from './tags.js';
import { updateTimeRangeButton } from './timeframe.js';
import { loadTimelineTemplates } from './timeline.js';
import { confirmDialog, contextMenu, dropdownMenu, modal, oneAtATime, promptDialog, sectionPanes } from './ui.js';
import { writeClipboardText } from './grouping.js';

/* -------------------------------------------------------------- home screen */

// Winnow's own case-file extension — mirrors store.CASE_SUFFIX. Only the
// default filename a new case is proposed under; an analyst who types a
// path keeps whatever they typed, and existing ".db" cases open unchanged.
const CASE_EXT = '.db-winnow';

/* Home manages "Cases" (one case.db each — recent, grouped, renamed,
   annotated). Distinct from the existing Session feature (the Session
   button above), which snapshots tags/notes/layout *within* one already-open
   case — that feature is untouched. Opening a Case from here is what
   actually swaps the server's STORE; navigating back to Home via #btnHome
   just changes what the client is looking at. */

export function showApp() {
  $('home').hidden = true;
  $('app').hidden = false;
  applyPageTabsSize(); // first moment the bar has a real width to clamp against
}

/* The off switch — the server otherwise only ever stops when someone finds
   the terminal it was started in. Confirmed because it's outward-facing in
   the one way this app has (every tab pointed at this server dies), then a
   static farewell replaces the page: there is deliberately no "restart"
   affordance, because there's no server left to serve one.

   The server stops at once, open windows or not (server.py
   _end_presence_streams), so the other windows on it are named here,
   where the analyst can still say Keep running — and each of them is told
   it was shut down when it happens (connection.js). */
export async function shutdownWinnow() {
  // Warn if work is still running — committed data is safe, but an
  // in-flight import/sweep/build/query would be interrupted.
  const busy = inFlightWork();
  let warn = busy.length
    ? `\n\n\u26a0 Still running: ${busy.join(', ')}. Shutting down now interrupts ${busy.length === 1 ? 'it' : 'them'} — `
      + 'anything already committed is saved, but unfinished work is not.'
    : '';
  const others = await otherWindows();
  if (others > 0) {
    warn += `\n\n\u26a0 ${others} other Winnow window${others === 1 ? ' is' : 's are'} open on this server `
      + 'and will stop working too.';
  }
  const thisPage = others === null
    ? 'This page (and any other tab using this server) will stop working'
    : 'This page will stop working';
  const go = await confirmDialog(
    'Shut down the Winnow server?' + warn
    + '\n\nEverything already committed is saved in the case file on disk — tags, notes and finished '
    + `imports are never lost. ${thisPage} until you start Winnow again.`,
    { okLabel: busy.length ? 'Shut down anyway' : 'Shut down', cancelLabel: 'Keep running', danger: true });
  if (!go) return;
  try { await post('/api/shutdown', {}); } catch { /* the server may drop before the response lands */ }
  closePresence();
  const note = el('div', 'shutdown-note');
  note.append(
    el('div', 'shutdown-note-title', 'Winnow is off'),
    el('div', null, 'The server has shut down. You can close this tab — start Winnow again with "python server.py".'),
  );
  document.body.replaceChildren(note);
}

/* Windows other than this one with a presence stream open, or null when
   the server can't say — the confirm then keeps its general wording. */
export async function otherWindows() {
  try {
    return (await api(`/api/presence/others?page=${PAGE_ID}`)).others;
  } catch { return null; }
}

export function showHome() { $('app').hidden = true; $('home').hidden = false; setBrandLabel(null); }

// The brand button doubles as "which case is this" once one's open — falls
// back to the app name on the home screen / before any case has loaded.
export function setBrandLabel(name) {
  $('brandLabel').textContent = name || 'Winnow';
}

/* Text for the "already open elsewhere" prompt. Deliberately says what the
   consequence is rather than just "in use" — the analyst clicking this is
   deciding whether their colleague's afternoon survives, and "case is
   locked" doesn't give them anything to decide with. */
export function describeCaseHolder(holder) {
  const who = holder.user || 'an unknown user';
  const where = holder.host || 'an unknown host';
  let when = '';
  if (holder.started_at) when = `, open since ${holder.started_at.replace('T', ' ')}`;
  if (holder.evidence === 'unreadable') {
    return 'A lock file sits next to this case but can\u2019t be read, so Winnow can\u2019t tell '
      + 'whether another server has it open.';
  }
  const age = holder.heartbeat_age_sec;
  const seen = (age === null || age === undefined) ? '' : ` (last seen ${Math.round(age)}s ago)`;
  return `This case is already open in another Winnow \u2014 ${who} on ${where}${when}${seen}.\n\n`
    + 'Opening it here too means neither server sees the other\u2019s tags, notes or imports until '
    + 'it reloads, and a long write in one (an import, or Compact case) will start failing the '
    + 'other. If the case file is on a network share, SQLite\u2019s locking does not work there '
    + 'at all and the file can be corrupted.';
}

/* One case switch at a time from this page. A double-click on a case card
   is two clicks, and each used to start its own open: two POSTs racing on
   the server (which serialises them now — see _CASE_SWITCH in server.py),
   then two state resets and two loadSources interleaving here. A second
   call while one is in flight gets the first one's promise instead —
   whichever case was asked for first is the one that opens. */
let caseOpening = null;

export function openCase(path, opts = {}) {
  if (caseOpening) return caseOpening;
  caseOpening = openCaseNow(path, opts).finally(() => { caseOpening = null; });
  return caseOpening;
}

export async function openCaseNow(path, opts = {}) {
  // A note save still inside its 500 ms debounce fires after the server
  // has swapped stores, and /api/note writes into whichever store is
  // current — the previous case's {source_id, rid} landing on an unrelated
  // row of this one. saveNote drops a write with no rid, so the binding
  // is blanked before the POST rather than after it. Only here: a page or
  // table switch within a case leaves the binding alone, since the row it
  // names is still in the case (see hideDetailPane in detail.js).
  const note = $('noteInput');
  const boundRid = note.dataset.rid;
  note.dataset.rid = '';
  // A plugin tab's debounced state save is the same hazard with the same
  // fix: whatever it was about to write describes THIS case, and the POST
  // below hands the server another one. Dropped before the swap, not after.
  dropPendingTabState();
  let res;
  try {
    // switchCase: from here, a request this page made about the case it is
    // leaving is refused by the server and dropped here without a word.
    res = await switchCase(() => post('/api/case/open', { path, force: !!opts.force }));
  } catch (e) {
    note.dataset.rid = boundRid;   // nothing was opened: same case, same row
    if (e.status === 409 && e.detail && e.detail.error === 'case_in_use') {
      const go = await confirmDialog(describeCaseHolder(e.detail.holder), {
        okLabel: 'Open anyway', cancelLabel: 'Don\u2019t open', danger: true,
      });
      if (!go) return;
      // Straight to the worker: this IS the in-flight open, still holding
      // the guard above, so going through openCase would get itself back.
      return openCaseNow(path, { force: true });
    }
    toast('Could not open case: ' + e.message, 6000);
    return;
  }
  // Source ids are small and sequential per-case, so this case's "source 1"
  // may well collide with the id of a cached view_id from whatever case was
  // open before — that view_id belongs to a Store instance the server just
  // closed and can't possibly still exist. Drop the cache rather than let
  // openSource() try it, get a 409, and rebuild anyway.
  S.viewCache.clear();
  clearViewStateStash(); // per-tab filters describe the previous case's tables
  // Job ids and source ids restart at 1 in a new case, so anything keyed by
  // one of them would attach to the wrong thing here.
  resetJobState();
  resetDerivedSuggestions();
  S.diffMarks = null;    // a comparison's rids belong to the previous case's tables too
  // A repaint a watchlist scan left owed was owed to the PREVIOUS case's
  // grid — its rows are gone with the Store that held them, and this
  // case's table paints itself on the way in (loadSources below). Carried
  // over, the first return to the grid here would pay a debt nobody owes:
  // a second pass over the new rows, rail and regroup included. The
  // showGridTab call further down consumes it, but only from a page tab;
  // a case opened with the grid already showing never goes through it.
  S.gridRepaintPending = false;
  S.tabOrder = [];
  // Another case's tab history points at ids that mean nothing here.
  clearTabHistory();
  // Folder ids too: this case's collapsed folders are its own.
  S.collapsedFolders = loadCollapsedFolders();
  // Unlike a tab switch within the *same* case (where the timeframe filter
  // deliberately survives — see clearAllFilters()/applyPreset()), a
  // different case is a different investigation; a timeframe pinned in
  // the last one has no reason to silently carry over into this one.
  S.timeRange = { enabled: false, column: null, start: '', end: '' };
  updateTimeRangeButton();
  // Same reasoning for the timeline: its view_id belongs to the Store
  // instance that just closed, and its switched-off tag ids belong to the
  // previous case's tag_defs — neither means anything here.
  S.timeline = { view: null, pages: new Map(), pending: new Set(), reqId: S.timeline.reqId + 1,
                 tagsOff: new Set(), raw: new Set(), sel: null };
  // sql_tabs is a per-case table, so the previous case's tabs (and the
  // results and runs keyed by their ids) don't describe this one.
  resetSqlPane();
  // A Search-all job belongs to the Store instance the server just closed;
  // its results reference source ids from the previous case.
  S.searchAll = null;
  updateSearchAllButton();
  // A mounted plugin tab's UI was built from the previous case's data —
  // tear the mounts down so the next activation rebuilds against this one.
  resetPluginTabMounts();
  resetNotes();   // another case's narrative isn't this one's
  resetWatchlist();
  resetDashboard();
  // The histogram strip's chart is the previous case's rows; it waits for
  // this case's first view (or says there is no table) instead.
  resetHistogram();
  // The effective plugin set is per-case (case_settings overrides beat the
  // machine default), and the server reloaded its registry when this case
  // opened — refetch so tabs/formats/panel reflect THIS case's plugins.
  await loadPlugins();
  // The pane shows a row of the previous case's table; the grid it
  // belongs to is about to be rebuilt against this one. Before showApp,
  // so it is never on screen for a frame beside the new case.
  hideDetailPane();
  // repaint:false — S.view/S.sourceId are still the previous case's here
  // (loadSources says why S.sourceId survives a switch); loadSources below
  // opens and paints this case's table. A render() against the old view id
  // would hit the new Store, 409 as expired, and rebuild the old filters
  // against whichever of this case's sources shares the number.
  if (S.activeTab !== 'grid') showGridTab({ repaint: false });
  setBrandLabel(res.name);
  showApp();
  // A quick-look (temp) case — e.g. one made by dropping files on the home
  // screen — surfaces its banner so it can be saved or discarded, the same
  // as one opened at boot (only boot painted this before).
  paintTempBanner(!!res.temp);
  // ts_format lives in the case file, so it's per-case state like sql_tabs
  // — reload it rather than carrying the last case's setting over.
  await loadCaseSettings();
  // Tag definitions are per case too (see loadCaseTags), and an empty case
  // never opens a table to reload them on the way in.
  await loadCaseTags();
  await loadSources();
}

export function slugify(name) {
  return (name || 'case').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '') || 'case';
}

export function fieldInput(value) {
  const inp = el('input');
  inp.value = value || '';
  inp.style.cssText = 'flex:1;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:5px 8px;font:inherit';
  return inp;
}

export function groupOptionsDatalist(id) {
  const dl = el('datalist');
  dl.id = id;
  for (const g of new Set(S.cases.map((c) => c.group).filter(Boolean))) {
    const opt = document.createElement('option');
    opt.value = g;
    dl.append(opt);
  }
  return dl;
}

/* Browses the server machine's filesystem, one directory level at a time
   — the only way to hand back a real absolute path, since a browser's own
   folder picker (webkitdirectory / showDirectoryPicker) deliberately never
   exposes one. Assumes server and browser are the same machine, true for
   how this tool is actually run (see CLAUDE.md). onSelect gets the chosen
   directory's absolute path; nothing here touches file contents. */
/* One browser, two modes. mode 'folder' (default): pick a directory —
   the new-case Browse… and directory import. mode 'files': pick one or
   more files; onSelect gets {dir, files: [{path, name, size}]} — the
   import modal's "Add from this machine…", i.e. the no-upload transport.
   Both modes take a typed path too (the box at the top): analysts who
   know where the evidence sits shouldn't have to click down to it. */
export function openFolderBrowser(startPath, onSelect, onCancel, { mode = 'folder' } = {}) {
  const filesMode = mode === 'files';
  let listing = null;
  const picked = new Map(); // path -> {path, name, size}, insertion-ordered
  modal(filesMode ? 'Add files from this machine' : 'Choose a folder', (b) => {
    const pathInput = fieldInput('');
    pathInput.style.cssText += ';width:100%;font-family:var(--mono);font-size:12px;margin-bottom:8px';
    pathInput.placeholder = filesMode ? 'Type a folder or file path, or browse below' : 'Type a folder path, or browse below';
    pathInput.onkeydown = (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const typed = pathInput.value.trim();
      if (typed) load(typed);
    };
    b.append(pathInput);
    // Files mode gets a filter box and a sort: a tool's output folder is
    // hundreds of files, and the ones wanted are "the big ones" or "the
    // ones from this run" as often as they are a name. Folders always
    // list first; the sort is remembered across folders and sessions.
    const SORT_KEY = 'winnow.browse.sort';
    let sort = { by: 'name', dir: 'asc' };
    try { sort = { ...sort, ...JSON.parse(localStorage.getItem(SORT_KEY) || '{}') }; } catch { /* default */ }
    let query = '';
    const filterBox = el('input', 'panel-search browse-filter');
    const sortSel = el('select', 'browse-sort');
    const sortDir = el('button', 'btn ghost browse-sort-dir');
    if (filesMode) {
      const tools = el('div', 'browse-tools');
      filterBox.type = 'search';
      filterBox.placeholder = 'Filter this folder…';
      filterBox.autocomplete = 'off';
      filterBox.oninput = () => { query = filterBox.value.trim().toLowerCase(); if (listing) paint(); };
      for (const [v, l] of [['name', 'Name'], ['size', 'Size'], ['mtime', 'Modified']]) sortSel.append(new Option(l, v));
      sortSel.value = sort.by;
      const paintDir = () => {
        sortDir.textContent = sort.dir === 'asc' ? '↑' : '↓';
        sortDir.title = sort.dir === 'asc' ? 'Ascending — click for descending' : 'Descending — click for ascending';
      };
      const remember = () => { try { localStorage.setItem(SORT_KEY, JSON.stringify(sort)); } catch { /* private mode */ } };
      // A sort change re-asks the server: the listing is capped at 2000
      // entries, and the cap has to be taken in the order being shown.
      const resort = () => { remember(); if (listing) load(listing.path); };
      sortSel.onchange = () => { sort.by = sortSel.value; resort(); };
      sortDir.onclick = () => { sort.dir = sort.dir === 'asc' ? 'desc' : 'asc'; paintDir(); resort(); };
      paintDir();
      tools.append(filterBox, el('span', 'fb-help', 'Sort'), sortSel, sortDir);
      b.append(tools);
    }
    const list = el('div', 'session-list');
    list.style.maxHeight = '42vh';
    list.style.overflow = 'auto';
    b.append(list);
    const status = el('div', 'note-status', '');
    b.append(status);

    const actions = el('div', 'row-actions');
    const useBtn = el('button', 'btn primary', filesMode ? 'Add selected' : 'Use this folder');
    const paintUse = () => {
      if (!filesMode) return;
      useBtn.disabled = !picked.size;
      useBtn.textContent = picked.size ? `Add ${picked.size} file${picked.size === 1 ? '' : 's'}` : 'Add selected';
    };
    useBtn.onclick = () => {
      if (filesMode) { onSelect({ dir: listing.path, files: [...picked.values()] }); return; }
      // The path box is editable: typed-but-not-Entered text is the
      // analyst's most recent statement of intent, so it wins over the
      // last listing rather than being silently discarded. The caller
      // validates the path the moment it uses it.
      const typed = pathInput.value.trim();
      onSelect(typed && listing && typed !== listing.path ? typed : (listing ? listing.path : typed));
    };
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { if (onCancel) onCancel(); else $('modal').hidden = true; };
    actions.append(useBtn);
    if (!filesMode) {
      // Filing a case somewhere that doesn't exist yet shouldn't mean
      // leaving Winnow to make the folder and coming back.
      const mk = el('button', 'btn ghost', 'New folder…');
      mk.onclick = async () => {
        const parent = (listing && listing.path) || pathInput.value.trim();
        if (!parent) { toast('Browse to where the folder should go first'); return; }
        const name = await promptDialog(`New folder inside:\n${parent}`, '');
        if (name === null || !name.trim()) return;
        let res;
        try {
          res = await post('/api/browse_dir/new', { parent, name: name.trim() });
        } catch (e) {
          toast(e.message, 5000);
          return;
        }
        toast(`Created ${res.name}`);
        load(res.path);   // step into it — it's where they're going
      };
      actions.append(mk);
    }
    actions.append(cancel);
    b.append(actions);

    /* Loads are async and can overtake each other — the walk-up on open
       fires a chain of them, and an analyst typing a path while that is
       still resolving would have their navigation overwritten by an older
       answer arriving late. Same stale-response guard as the derived
       preview: only the newest load may paint. */
    let loadSeq = 0;

    async function load(path, { fallback = false } = {}) {
      const seq = ++loadSeq;
      let res;
      try {
        res = await api(`/api/browse_dir?path=${encodeURIComponent(path || '')}`
          + (filesMode ? `&files=true&sort=${sort.by}&dir=${sort.dir}` : ''));
      } catch (e) {
        // The configured cases folder often doesn't exist yet — which is
        // exactly when someone reaches for "New folder". Opening onto an
        // error with nothing listed leaves them nowhere to create it FROM,
        // so the first load walks up to the nearest folder that does
        // exist. Only on open: a typed path that's wrong should say so
        // rather than silently landing somewhere else.
        if (seq !== loadSeq) return;   // superseded while we waited
        if (fallback) {
          // Walk up to the nearest folder that does exist. A path with no
          // separator is the RELATIVE default ('cases', resolved against
          // the server's cwd) — stripping a segment leaves it unchanged, so
          // that case falls back to the server's own default listing
          // instead of looping.
          const up = /[\\/]/.test(path || '') ? path.replace(/[\\/][^\\/]*$/, '') : '';
          if (up !== path) { load(up, { fallback: up !== '' }); return; }
        }
        toast(filesMode ? 'No folder or file at that path: ' + e.message
          : 'Could not list that folder: ' + e.message, 4000);
        return;
      }
      // A typed path that names a FILE comes back as {picked} — the server
      // resolved it with os.path, which is what makes a pasted Windows path
      // or a file past the listing cap work. It's a complete answer.
      if (seq !== loadSeq) return;   // a newer navigation won
      if (res.picked) {
        onSelect({ dir: res.path, files: [{ path: res.path + '/' + res.picked.name, name: res.picked.name, size: res.picked.size }] });
        return;
      }
      listing = res;
      pathInput.value = listing.path;
      // A filter is about the folder it was typed over.
      if (filesMode) { query = ''; filterBox.value = ''; }
      paint();
    }

    /* The listing arrives name-sorted from the server; the picker's own
       sort and filter are applied here, over what it already has, so a
       change is a repaint and not a round trip. */
    /* The server already returns the files in the picker's order (see
       the sort= param) — only the filter is applied here, so a keystroke
       is a repaint and not a round trip. */
    function sortedFiles(files) {
      return query ? files.filter((f) => f.name.toLowerCase().includes(query)) : files.slice();
    }
    const fmtDay = (epoch) => {
      const d = new Date(epoch * 1000);
      return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
    };

    function paint() {
      if (!listing) return;   // a control poked before the first listing landed
      // Built off-DOM: up to 2×BROWSE_LIST_CAP rows appended one at a time
      // into a live scroll container re-layouts on every append.
      const frag = document.createDocumentFragment();
      if (listing.parent) {
        const up = el('button', 'btn ghost browse-row', '.. (up a level)');
        up.style.cssText = 'justify-content:flex-start;text-align:left';
        up.onclick = () => load(listing.parent);
        frag.append(up);
      }
      for (const d of listing.dirs) {
        const row = el('button', 'btn ghost browse-row', '📁 ' + d);
        row.style.cssText = 'justify-content:flex-start;text-align:left;width:100%';
        row.onclick = () => load(listing.path + '/' + d);
        frag.append(row);
      }
      if (filesMode) {
        const files = sortedFiles(listing.files || []);
        for (const f of files) {
          const path = listing.path + '/' + f.name;
          const row = el('label', 'session-row browse-row');
          row.style.cssText = 'cursor:pointer';
          const cb = el('input');
          cb.type = 'checkbox';
          cb.checked = picked.has(path);
          cb.onchange = () => {
            if (cb.checked) picked.set(path, { path, name: f.name, size: f.size });
            else picked.delete(path);
            paintStatus();
          };
          row.append(cb, el('span', 'session-name', f.name),
            el('span', 'browse-mtime', f.mtime ? fmtDay(f.mtime) : ''),
            el('span', 'count', fmtBytes(f.size)));
          frag.append(row);
        }
        if (!files.length && (listing.files || []).length) {
          frag.append(el('div', 'note-status', 'No files here match that filter.'));
        } else if (!(listing.files || []).length && !listing.dirs.length) {
          frag.append(el('div', 'note-status', 'Nothing here.'));
        }
      } else if (!listing.dirs.length) {
        frag.append(el('div', 'note-status', 'No subfolders here.'));
      }
      list.replaceChildren(frag);
      paintStatus();
    }

    function paintStatus() {
      // The number comes from the response, so the message can't lie when
      // the server's cap changes.
      status.textContent = filesMode && listing && listing.truncated
        ? `Showing the first ${listing.limit.toLocaleString()} entries — type a full path to reach anything past them.` : '';
      paintUse();
    }

    load(startPath, { fallback: true });
    paintUse();
  }, { wide: true });
}

/* `state` carries everything back across a "Browse..." round trip —
   opening the folder browser swaps this modal's content out entirely
   (modal() replaces #modal's content in place rather than stacking), so
   there's no surviving DOM to read values back out of afterward; the only
   way to preserve what the analyst already typed is to snapshot it into
   plain values and re-invoke this function with them as the new initial
   state, the pattern Import a folder's Browse… uses too.

   `state.saveQuickLook` makes this the quick look's "Save as a case…"
   (tempSaveAs): the same fields, but the open quick look's file moves to
   the path rather than a new case being made there. */
export function openNewCaseModal(state = {}) {
  const saving = state.saveQuickLook || null;
  modal(saving ? 'Save as a case' : 'New case', (b) => {
    if (saving) {
      b.append(el('p', null, 'Its tables, tags and notes move into the case file below, '
        + 'and the case is listed on the home screen.'));
    }
    const nameInput = fieldInput(state.name || '');
    nameInput.placeholder = 'Case name';
    const nameRow = el('div', 'row-actions');
    nameRow.append(nameInput);
    b.append(el('label', 'field-label', 'Name'), nameRow);

    const groupInput = fieldInput(state.group || '');
    groupInput.placeholder = 'Group (optional) — e.g. an IR engagement name';
    groupInput.setAttribute('list', 'home-new-case-groups');
    const groupRow = el('div', 'row-actions');
    groupRow.append(groupInput, groupOptionsDatalist('home-new-case-groups'));
    b.append(el('label', 'field-label', 'Group'), groupRow);

    let chosenDir = state.chosenDir || S.casesDir || S.defaultCasesDir || 'cases';
    const pathInput = fieldInput(state.path || joinPath(chosenDir, slugify(state.name || '') + CASE_EXT));
    pathInput.style.fontFamily = 'var(--mono)';
    let pathTouched = state.pathTouched || false;
    pathInput.oninput = () => { pathTouched = true; };
    nameInput.oninput = () => { if (!pathTouched) pathInput.value = joinPath(chosenDir, slugify(nameInput.value) + CASE_EXT); };
    const browseBtn = el('button', 'btn ghost', 'Browse…');
    browseBtn.onclick = () => {
      const snapshot = {
        name: nameInput.value, group: groupInput.value, path: pathInput.value,
        chosenDir, pathTouched, saveQuickLook: state.saveQuickLook,
        caseType: typeSel.value,
        vars: Object.fromEntries([...varInputs].map(([k, inp]) => [k, inp.value])),
      };
      openFolderBrowser(
        chosenDir,
        (dir) => openNewCaseModal({
          ...snapshot, chosenDir: dir, pathTouched: false, path: joinPath(dir, slugify(snapshot.name) + CASE_EXT),
        }),
        () => openNewCaseModal(snapshot),
      );
    };
    const pathRow = el('div', 'row-actions');
    pathRow.append(pathInput, browseBtn);
    b.append(el('label', 'field-label', 'Case file path'), pathRow);

    // The profile, applied right after the case opens, so a Triage case
    // starts with the triage plugins on — no settings trip. (The record is
    // a plugin_bundles row; only the word the analyst reads is "profile".)
    const typeSel = el('select');
    typeSel.style.cssText = 'flex:1;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:6px 8px;font:inherit';
    const noneOpt = el('option', null, 'None — machine defaults');
    noneOpt.value = '';
    typeSel.append(noneOpt);
    let bundlesById = new Map();
    const varsBox = el('div', 'case-vars new-case-vars');
    varsBox.hidden = true;
    const varInputs = new Map();     // variable name -> input, for the chosen profile
    // A profile can declare variables the case should carry (the
    // engagement name, an API base URL…); required ones are asked for
    // here, before the case exists, rather than as a surprise afterwards.
    const renderProfileVars = () => {
      varsBox.replaceChildren();
      varInputs.clear();
      const bd = bundlesById.get(Number(typeSel.value));
      const defs = (bd && bd.variables) || [];
      varsBox.hidden = !defs.length;
      if (!defs.length) return;
      varsBox.append(el('div', 'fb-help', `“${bd.name}” asks for:`));
      for (const d of defs) {
        const row = el('div', 'case-var-row');
        const name = el('span', 'case-var-name', (d.label || d.name) + (d.required ? ' *' : ''));
        name.title = d.name;
        const inp = el('input', 'case-var-value');
        inp.dataset.var = d.name;
        inp.value = state.vars && state.vars[d.name] != null ? state.vars[d.name] : (d.default || '');
        inp.placeholder = d.required ? 'required' : 'optional';
        row.append(name, inp, el('span', 'case-var-desc', d.description || ''), el('span'));
        varInputs.set(d.name, inp);
        varsBox.append(row);
      }
    };
    api('/api/plugin_bundles').then((bundles) => {
      bundlesById = new Map(bundles.map((bd) => [bd.id, bd]));
      for (const bd of bundles) {
        const o = el('option', null, `${bd.name} (${bd.plugins.length} plugin${bd.plugins.length === 1 ? '' : 's'})`);
        o.value = String(bd.id);
        typeSel.append(o);
      }
      if (state.caseType) typeSel.value = state.caseType;
      renderProfileVars();
    }).catch(() => {});
    typeSel.onchange = renderProfileVars;
    const typeRow = el('div', 'row-actions');
    typeRow.append(typeSel);
    b.append(el('label', 'field-label', 'Profile'), typeRow, varsBox);


    const actions = el('div', 'row-actions');
    const create = el('button', 'btn primary', saving ? 'Save case' : 'Create case');
    create.onclick = oneAtATime(create, async () => {
      const name = nameInput.value.trim();
      if (!name) { toast('Name the case first'); return; }
      const path = pathInput.value.trim();
      if (!path) { toast('Give the case file a path'); return; }
      const bd = bundlesById.get(Number(typeSel.value));
      const missing = ((bd && bd.variables) || []).filter((d) => d.required && !(varInputs.get(d.name)?.value || '').trim());
      if (missing.length) {
        toast(`“${bd.name}” needs: ${missing.map((d) => d.label || d.name).join(', ')}`, 5000);
        varInputs.get(missing[0].name).focus();
        return;
      }
      if (saving) {
        let res;
        try {
          // The file moves, so the case's token does too (switchCase).
          res = await switchCase(() => post('/api/case/save_as', { path, name, group: groupInput.value.trim() }));
        } catch (e) {
          toast('Could not save the case: ' + e.message, 6000);
          return;
        }
        $('modal').hidden = true;
        paintTempBanner(false);
        setBrandLabel(res.name);
        toast(`Saved — "${res.name}" is on the home screen now`, 6000);
      } else {
        try {
          await post('/api/cases', { path, name, group: groupInput.value.trim(), notes: '' });
        } catch (e) {
          toast('Could not create case: ' + e.message, 6000);
          return;
        }
        $('modal').hidden = true;
        await openCase(path); // shared with the home screen's "open" flow — same brand-label/view-cache handling
      }
      if (typeSel.value) {
        try {
          // Values typed above land first, so applying the profile (which
          // seeds definitions without overwriting values) finds them filled.
          for (const [vname, inp] of varInputs) {
            if (inp.value.trim()) await post('/api/case/variables', { name: vname, value: inp.value.trim() });
          }
          await applyBundle({ id: Number(typeSel.value) });
        } catch (e) {
          toast(`Case ${saving ? 'saved' : 'created'}, but the profile failed to apply: ` + e.message, 6000);
        }
      }
      if (saving && saving.onSaved) await saving.onSaved();
    });
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => {
      $('modal').hidden = true;
      if (saving && saving.onCancel) saving.onCancel();
    };
    actions.append(create, cancel);
    b.append(actions);
  });
}

/* First run of this Winnow instance (no cases_dir configured, no cases
   yet): ask where case files should live instead of silently defaulting
   to cases/ beside server.py. Asked once — either answer configures the
   instance. */
export async function maybeOfferStorageDir() {
  let prefs;
  try {
    prefs = await api('/api/prefs');
  } catch { return; }
  S.casesDir = prefs.cases_dir || null;
  // The server's own answer, absolute: cases/ beside server.py. What the
  // dialog names is exactly what "Use the default" stores, and what a new
  // case is filed under while nothing has been chosen.
  S.defaultCasesDir = prefs.default_cases_dir || null;
  if (!prefs.first_run) return;
  const pick = await confirmDialog(
    'First run — where should Winnow keep its case files? '
    + `The default is a "cases" folder next to the server: ${prefs.default_cases_dir}. `
    + 'You can pick any folder on this machine instead.',
    { okLabel: 'Choose a folder…', cancelLabel: 'Use the default' });
  if (pick) {
    openFolderBrowser(undefined, async (dir) => {
      try {
        const res = await post('/api/prefs', { cases_dir: dir });
        S.casesDir = res.cases_dir;
        toast(`Case files will be created in ${res.cases_dir}`, 6000);
      } catch (e) {
        toast('Could not set the folder: ' + e.message, 6000);
      }
      $('modal').hidden = true;
    }, () => { $('modal').hidden = true; });
  } else {
    // "Use the default" is still an answer — record it so we never nag.
    try {
      const res = await post('/api/prefs', { cases_dir: prefs.default_cases_dir });
      S.casesDir = res.cases_dir;
    } catch { /* next launch asks again — fine */ }
  }
}

/* Join a directory and a file name with the separator the directory
   itself uses. Paths here come from the server (the folder browser, the
   cases-dir preference) in the OS's own shape, so on Windows the directory
   carries backslashes — and a path shown as C:\\Cases/acme.db-winnow reads
   as broken to the analyst even though the OS would accept it. */
export function joinPath(dir, name) {
  const d = String(dir || '').replace(/[\\/]+$/, '');
  const sep = d.includes('\\') && !d.includes('/') ? '\\' : '/';
  return d ? `${d}${sep}${name}` : name;
}

export function openExistingCasePrompt(state = {}) {
  modal('Open existing case file', (b) => {
    const pathInput = fieldInput(state.path || '');
    pathInput.placeholder = 'Path to a .db-winnow case file';
    pathInput.style.fontFamily = 'var(--mono)';
    // Browse… is the same server-disk picker the new-case dialog and
    // "Add from this machine…" use, in file mode — typing a full path is
    // fine when you know it, but a case file on an evidence share usually
    // gets found, not remembered.
    const browseBtn = el('button', 'btn ghost', 'Browse…');
    browseBtn.onclick = () => {
      const snapshot = { path: pathInput.value };
      openFolderBrowser(S.lastBrowsePath || S.casesDir || undefined, (sel) => {
        const f = (sel.files || [])[0];
        openExistingCasePrompt({ path: f ? f.path : snapshot.path });
      }, () => openExistingCasePrompt(snapshot), { mode: 'files' });
    };
    const row = el('div', 'row-actions');
    row.append(pathInput, browseBtn);
    b.append(el('label', 'field-label', 'Case file'), row);
    b.append(el('p', 'fb-help', 'Registers the case on this machine\u2019s list and opens it.'));

    const actions = el('div', 'row-actions');
    const openBtn = el('button', 'btn primary', 'Open');
    openBtn.onclick = oneAtATime(openBtn, async () => {
      const trimmed = pathInput.value.trim();
      if (!trimmed) { toast('Enter or browse to a case file'); return; }
      const name = trimmed.split(/[\\/]/).pop().replace(/\.db-winnow$|\.db$/i, '');
      try {
        // must_exist: a path with nothing at it is refused, not created —
        // creating a case is + New case's job.
        await post('/api/cases', { path: trimmed, name, group: '', notes: '', must_exist: true });
      } catch (e) {
        toast('Could not open case: ' + e.message, 6000);
        return;
      }
      $('modal').hidden = true;
      await openCase(trimmed);
    });
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; };
    actions.append(openBtn, cancel);
    b.append(actions);
    pathInput.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); openBtn.click(); } };
    setTimeout(() => pathInput.focus(), 0);
  });
}

export function openEditCaseModal(c) {
  modal('Edit case', (b) => {
    const nameInput = fieldInput(c.name);
    const nameRow = el('div', 'row-actions');
    nameRow.append(nameInput);
    b.append(el('label', 'field-label', 'Name'), nameRow);

    const groupInput = fieldInput(c.group || '');
    groupInput.setAttribute('list', 'home-edit-case-groups');
    const groupRow = el('div', 'row-actions');
    groupRow.append(groupInput, groupOptionsDatalist('home-edit-case-groups'));
    b.append(el('label', 'field-label', 'Group'), groupRow);

    const notesArea = el('textarea');
    notesArea.rows = 4;
    notesArea.value = c.notes || '';
    notesArea.placeholder = 'Notes about this case…';
    notesArea.style.width = '100%';
    b.append(el('label', 'field-label', 'Notes'), notesArea);

    b.append(el('div', 'note-status', c.path));

    const actions = el('div', 'row-actions');
    const save = el('button', 'btn primary', 'Save');
    save.onclick = async () => {
      try {
        await api(`/api/cases/${c.id}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: nameInput.value.trim() || c.name, group: groupInput.value.trim(), notes: notesArea.value }),
        });
      } catch (e) {
        toast('Could not save: ' + e.message, 6000);
        return;
      }
      $('modal').hidden = true;
      refreshCases();
    };
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; };
    actions.append(save, cancel);
    b.append(actions);
  });
}

export async function removeCaseFromList(c) {
  if (!(await confirmDialog(`Remove "${c.name}" from this list? The case file itself is left untouched on disk.`, { danger: true, okLabel: 'Remove' }))) return;
  // `path` says which case this list meant — see deleteCaseFile.
  try {
    await api(`/api/cases/${c.id}?path=${encodeURIComponent(c.path)}`, { method: 'DELETE' });
  } catch (e) {
    toast('Could not remove: ' + e.message, 6000);
  }
  refreshCases();
}

export async function deleteCaseFile(c) {
  // Coming back to this list leaves the case open on the server, so the
  // case just left is usually the one being deleted here, and the server
  // closes it first. Said, along with the other windows that would lose
  // it — the way Shut down names them.
  let closing = '';
  if (c.open) {
    closing = '\n\nWinnow still has this case open (the case list doesn\u2019t close it), so it is closed first.';
    const others = await otherWindows();
    if (others > 0) {
      closing += `\n\n\u26a0 ${others} other Winnow window${others === 1 ? ' is' : 's are'} open on this server. `
        + 'Any showing this case will stop working.';
    }
  }
  if (!(await confirmDialog(
    `Permanently delete the case file for "${c.name}"?\n\n${c.path}\n\nThis cannot be undone — all tags, notes and imported data in it will be lost.${closing}`,
    { danger: true, okLabel: 'Delete permanently' },
  ))) return;
  // The path as well as the id: this list can be older than the registry
  // (another window forgot this case and registered a new one), and the
  // server refuses rather than delete whichever file the id names now.
  // switchCase: deleting the open case closes it, and the answer's
  // case_token (none) is what this page carries from here on.
  try {
    await switchCase(() => api(`/api/cases/${c.id}?delete_file=true&path=${encodeURIComponent(c.path)}`,
                                { method: 'DELETE' }));
  } catch (e) {
    toast('Could not delete: ' + e.message, 6000);
    // Not always a no-op: a path that is not a case file is still taken
    // off the list (the message says so), so the list is re-read either way.
    refreshCases();
    return;
  }
  refreshCases();
}

/* Home-screen drag-and-drop import. Dropping files on a case row imports
   into THAT case; dropping on the home screen but not on any case makes a
   fresh quick-look (temp) case. Both open the target and hand the files to
   the normal import modal — so the analyst still configures/reviews before
   ingest, exactly like an in-app drop. */
const isFileDrag = (e) => dragHas(e, 'Files');

async function importDroppedFiles(files, casePath) {
  try {
    const path = casePath || (await post('/api/case/quicklook/new', {})).path;
    await openCase(path);        // switches to the app and opens the case
    queueFiles(files);
    openImportModal();
  } catch (e) { toast('Could not start the import: ' + e.message, 6000); }
}

/* Quick look at files…: the drop-on-the-home-screen gesture as a button,
   since nothing on screen said that gesture existed. A quick look with an
   empty queue, so the import dialog's own Add buttons pick the files. */
export async function startQuickLook() {
  let path;
  try { path = (await post('/api/case/quicklook/new', {})).path; }
  catch (e) { toast('Could not start a quick look: ' + e.message, 6000); return; }
  await openCase(path);
  openImportModal();
}

export const HOME_STALE_MS = 30 * 24 * 60 * 60 * 1000;   // 30 days

export function isStaleCase(c) {
  // Never-opened cases (just created, or from before last_opened existed)
  // aren't "stale" — they need attention, not hiding.
  if (!c.last_opened) return false;
  const t = Date.parse(c.last_opened);
  return !Number.isNaN(t) && (Date.now() - t) > HOME_STALE_MS;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const dayStart = (t) => { const d = new Date(t); return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime(); };

/* When a case was last opened, the way an analyst says it: "37 min ago",
   "yesterday", "3 days ago", then the date. The registry keeps local
   wall-clock time without a zone (workspace._now), which Date.parse reads
   as local too, so the two agree. Days are counted between calendar days,
   not in 24-hour steps: a case opened at 23:50 was opened "yesterday" at
   00:10. `now` is a parameter for the tests. */
export function openedAgo(iso, now = Date.now()) {
  if (!iso) return 'never';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const ms = Math.max(0, now - t);
  if (ms < 60e3) return 'just now';
  const days = Math.round((dayStart(now) - dayStart(t)) / 86400e3);
  if (days === 0) return ms < 3600e3 ? `${Math.floor(ms / 60e3)} min ago` : `${Math.floor(ms / 3600e3)} h ago`;
  if (days === 1) return 'yesterday';
  if (days < 7) return `${days} days ago`;
  if (days < 28) { const w = Math.round(days / 7); return `${w} week${w === 1 ? '' : 's'} ago`; }
  const d = new Date(t);
  if (d.getFullYear() === new Date(now).getFullYear()) return `${MONTHS[d.getMonth()]} ${d.getDate()}`;
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

const plural = (n, one, many = `${one}s`) => `${(n || 0).toLocaleString()} ${n === 1 ? one : many}`;
const latestOpened = (cases) => cases.map((c) => c.last_opened).filter(Boolean).sort().pop() || null;

/* A case's ⋯ menu, and its right-click menu: the four buttons every row
   used to carry, plus the path the row no longer shows. Open and Delete
   are greyed out for a case whose file is gone; Edit and Remove from list
   are how that one gets fixed or forgotten. */
export function caseMenuItems(c) {
  const gone = c.exists === false;
  return [
    { label: 'Open', onclick: () => openCase(c.path), disabled: gone },
    { label: 'Edit name, group and notes…', onclick: () => openEditCaseModal(c) },
    { label: 'Copy case file path', title: c.path,
      onclick: () => writeClipboardText(Promise.resolve(c.path), 'Copied the case file’s path') },
    '-',
    { label: 'Remove from list…', title: 'Forget this case on this machine — the file is left on disk',
      onclick: () => removeCaseFromList(c) },
    { label: 'Delete case file…', title: gone ? 'The file is already gone' : 'Permanently delete the case file from disk',
      onclick: () => deleteCaseFile(c), disabled: gone },
  ];
}

/* One case in the list: its name with its group and notes beside it, the
   Tables manager's number columns (a dim dash for none, tagged rows in the
   accent), when it was last opened, and the ⋯. The whole row opens the
   case, as the old card did; the name is a button so the keyboard can
   reach it, and its click is the row's. The path is in the name's
   tooltip and the ⋯ menu: shown in the row it was most of the line, and
   pushed the counts and the "opened" time out of sight. */
export function renderCaseRow(c, { showGroup = true } = {}) {
  const gone = c.exists === false;
  const r = el('div', 'home-row' + (gone ? ' missing' : ''));
  r.dataset.id = String(c.id);
  const cell = el('span', 'home-name-cell');
  const name = el('button', 'home-name', (gone ? '⚠ ' : '') + c.name);
  name.type = 'button';
  name.title = gone ? `${c.path}\nThe case file isn’t at this path any more` : `${c.path}\nClick to open`;
  name.disabled = gone;
  cell.append(name);
  if (showGroup && c.group) cell.append(el('span', 'home-group-chip', c.group));
  if (gone) cell.append(el('span', 'home-sub warn', 'file not found'));
  else if (c.error) cell.append(el('span', 'home-sub warn', c.error));
  else if (c.notes) {
    const notes = el('span', 'home-sub', c.notes);
    notes.title = c.notes;
    cell.append(notes);
  }
  r.append(cell);
  const num = (n, cls) => el('span', 'tables-num ' + cls + (n ? ' some' : ''), gone || c.error ? '' : (n ? n.toLocaleString() : '—'));
  r.append(num(c.source_count, 'home-tables'), num(c.row_count, 'tables-rows'), num(c.tagged_row_count, 'tables-tagged'));
  const opened = el('span', 'home-opened' + (c.last_opened ? '' : ' never'), openedAgo(c.last_opened));
  if (c.last_opened) opened.title = c.last_opened.replace('T', ' ');
  r.append(opened);
  const more = el('button', 'btn ghost row-more tables-more', '⋯');
  more.type = 'button';
  more.title = 'Open, edit, copy the path, remove';
  more.setAttribute('aria-label', `Actions for ${c.name}`);
  more.setAttribute('aria-haspopup', 'menu');
  more.onclick = () => dropdownMenu(more, caseMenuItems(c));
  r.append(more);
  if (!gone) {
    r.onclick = (e) => { if (!more.contains(e.target)) openCase(c.path); };
    r.addEventListener('dragover', (e) => {
      if (!isFileDrag(e)) return;
      e.preventDefault(); e.stopPropagation();
      r.classList.add('home-case-drop');
    });
    r.addEventListener('dragleave', () => r.classList.remove('home-case-drop'));
    r.addEventListener('drop', (e) => {
      if (!isFileDrag(e)) return;
      e.preventDefault(); e.stopPropagation();   // don't let the home-area handler also fire
      r.classList.remove('home-case-drop');
      const files = [...e.dataTransfer.files];
      if (files.length) importDroppedFiles(files, c.path);
    });
  }
  r.addEventListener('contextmenu', (e) => { e.preventDefault(); contextMenu(e, caseMenuItems(c)); });
  return r;
}

/* The side list: Recent and All cases, then one entry per group, newest
   work first, and the cases in none. Rebuilt on every renderHome, so a
   group that was renamed away is simply not there next time. */
export function homeSections(cases) {
  const recent = cases.filter((c) => !isStaleCase(c));
  const missing = cases.filter((c) => c.exists === false).length;
  const groups = new Map();
  for (const c of cases) if (c.group) (groups.get(c.group) || groups.set(c.group, []).get(c.group)).push(c);
  const loose = cases.filter((c) => !c.group);
  const byLatest = [...groups].sort((a, b) => (latestOpened(b[1]) || '').localeCompare(latestOpened(a[1]) || '')
    || a[0].localeCompare(b[0]));
  return [
    { id: 'recent', group: 'Cases', title: 'Recent', cases: recent,
      hint: () => (recent.length ? `${recent.length.toLocaleString()} opened in the last 30 days` : 'None in the last 30 days') },
    { id: 'all', group: 'Cases', title: 'All cases', cases, byGroup: true,
      hint: () => cases.length.toLocaleString() + (missing ? ` · ${plural(missing, 'file')} missing` : '') },
    ...byLatest.map(([g, cs]) => ({
      id: 'group:' + g, group: 'Groups', title: g, cases: cs, labelCls: 'mono',
      hint: () => plural(cs.length, 'case') + (latestOpened(cs) ? ` · ${openedAgo(latestOpened(cs))}` : ''),
    })),
    ...(loose.length && groups.size ? [{ id: 'nogroup', group: 'Groups', title: 'No group', cases: loose, hint: () => plural(loose.length, 'case') }] : []),
  ];
}

export function sortCases(list, by) {
  if (by === 'name') return list.slice().sort((a, b) => a.name.localeCompare(b.name));
  // Most recently opened first; a case that's never been opened (no
  // last_opened) sorts after every case that has been.
  if (by === 'opened') return list.slice().sort((a, b) => (b.last_opened || '').localeCompare(a.last_opened || '') || a.name.localeCompare(b.name));
  return list.slice().sort((a, b) => ((b[by] || 0) - (a[by] || 0)) || a.name.localeCompare(b.name));
}

const caseMatches = (q) => (c) => !q || `${c.name}\n${c.group || ''}\n${c.notes || ''}`.toLowerCase().includes(q);

function caseGroupHead(label, cases, { code }) {
  const h = el('div', 'tables-group home-group' + (code ? ' code' : ''));
  h.append(el('span', 'tables-group-name', label));
  const rows = cases.reduce((n, c) => n + (c.row_count || 0), 0);
  const latest = latestOpened(cases);
  h.append(el('span', 'tables-group-meta',
    `${plural(cases.length, 'case')} · ${rows.toLocaleString()} rows` + (latest ? ` · opened ${openedAgo(latest)}` : '')));
  return h;
}

/* A section's pane: the Tables manager's Every table, for cases. A filter
   over names, groups and notes on top; then the box that takes the room
   that is left and is the one thing that scrolls, with sortable column
   heads; then a line on what a row does. All cases lists each group under
   its own heading; the others are one list, where a row carries its group
   as a chip (a group's own section leaves it off: every row would say the
   same thing). */
function buildCasesPane(section, sections, frame) {
  return (pane) => {
    const top = el('div', 'tm-cols-top');
    const search = el('input', 'side-search home-filter');
    search.type = 'search';
    search.placeholder = 'Filter cases, groups and notes…';
    search.setAttribute('aria-label', 'Filter cases');
    search.value = S.homeSearch;
    const count = el('span', 'tm-cols-count');
    top.append(search, count);

    const box = el('div', 'side-preview tm-cols-box tables-box home-box');
    const head = el('div', 'tables-head home-list-head');
    const sortBtn = (key, label, cls, title) => {
      const b = el('button', 'tables-sort ' + cls, label);
      b.type = 'button';
      b.dataset.sort = key;
      b.title = title;
      b.onclick = () => { S.homeSort = key; paint(); };
      return b;
    };
    head.append(sortBtn('name', 'Case', 'tables-name-h', 'Sort by name'),
      sortBtn('source_count', 'Tables', 'tables-num', 'Most tables first'),
      sortBtn('row_count', 'Rows', 'tables-num', 'Most rows first'),
      sortBtn('tagged_row_count', 'Tagged', 'tables-num', 'Most tagged rows first'),
      sortBtn('opened', 'Opened', 'home-opened-h', 'Most recently opened first'), el('span'));
    const scroller = el('div', 'side-preview-rows');
    const list = el('div', 'tables-list home-list');
    scroller.append(list);
    const foot = el('div', 'side-preview-foot');
    foot.append(el('p', 'fb-help', 'Click a case to open it. Drop files on a case to import them into it, or anywhere else for a quick look.'));
    box.append(head, scroller, foot);
    pane.append(top, box);

    function paint() {
      const q = S.homeSearch.trim().toLowerCase();
      const shown = section.cases.filter(caseMatches(q));
      count.textContent = q ? `${shown.length} of ${plural(section.cases.length, 'case')}` : plural(section.cases.length, 'case');
      for (const b of head.querySelectorAll('.tables-sort')) b.setAttribute('aria-pressed', String(b.dataset.sort === S.homeSort));
      const nodes = [];
      if (section.byGroup) {
        for (const s of sections) {
          if (!s.id.startsWith('group:') && s.id !== 'nogroup') continue;
          const mine = sortCases(s.cases.filter(caseMatches(q)), S.homeSort);
          if (!mine.length) continue;
          nodes.push(caseGroupHead(s.title, mine, { code: s.id !== 'nogroup' }));
          nodes.push(...mine.map((c) => renderCaseRow(c, { showGroup: false })));
        }
        // No groups at all: one plain list, no heading over it.
        if (!nodes.length) nodes.push(...sortCases(shown, S.homeSort).map((c) => renderCaseRow(c)));
      } else {
        nodes.push(...sortCases(shown, S.homeSort).map((c) => renderCaseRow(c, { showGroup: !section.id.startsWith('group:') })));
      }
      // A filter only looks inside the section it was typed in; when the
      // case it was typed for is in another one (an older case, from
      // Recent), say where, rather than "no match" over a case that exists.
      const elsewhere = q && section.id !== 'all' ? S.cases.filter(caseMatches(q)).length - shown.length : 0;
      if (!shown.length) {
        nodes.push(el('p', 'fb-help tables-empty', q ? `No case here matches “${S.homeSearch.trim()}”.`
          : section.id === 'recent' ? 'No case has been opened in the last 30 days — All cases has the rest.' : 'No cases here.'));
      }
      if (elsewhere > 0) {
        const more = el('button', 'btn ghost mb-small home-elsewhere', `${plural(elsewhere, 'more match', 'more matches')} in All cases`);
        more.type = 'button';
        more.onclick = () => { S.homeSection = 'all'; frame.show('all', { focus: false }); };
        nodes.push(more);
      }
      list.replaceChildren(...nodes);
    }
    search.oninput = () => { S.homeSearch = search.value; paint(); };
    search.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && search.value) { e.preventDefault(); e.stopPropagation(); search.value = ''; S.homeSearch = ''; paint(); }
      else if (e.key === 'ArrowDown') { const first = list.querySelector('.home-name:not(:disabled)'); if (first) { e.preventDefault(); first.focus(); } }
    });
    // Up/Down walk the case names, and Up from the first goes back to the
    // filter — the keyboard's way down a list it can't otherwise tab
    // through quickly. Enter on a name is its click, which opens the case.
    list.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
      const names = [...list.querySelectorAll('.home-name:not(:disabled)')];
      const at = names.indexOf(document.activeElement);
      if (at < 0) return;
      e.preventDefault();
      if (e.key === 'ArrowDown') { if (at < names.length - 1) names[at + 1].focus(); }
      else if (at > 0) names[at - 1].focus();
      else search.focus();
    });
    paint();
    return { update: paint };
  };
}

/* The home screen: the wordmark, then one card in the Tables manager's
   frame — the case list's sections down a side list, the chosen section
   on the right, and along the foot the ways a case starts, with + New
   case as the main action. Settings and Shut down sit by the wordmark:
   they are about this Winnow, not about any case. The card takes the
   window's height and the list inside it scrolls, so the page itself
   never does (until the window is too short for the card's floor). */
export function renderHome() {
  const home = $('home');
  home.replaceChildren();
  const inner = el('div', 'home-inner');

  const head = el('div', 'home-head');
  // The wordmark, not the word: the same dot field the launch animation
  // settles into, in the accent colour. Redrawn on open rather than cached
  // because the accent follows the skin, and a stale canvas would be the
  // one element that ignored a theme change.
  const brand = el('div', 'home-brand');
  const mark = el('canvas', 'home-brand-mark');
  brand.append(mark);
  head.append(brand);
  drawWordmark(mark, {
    color: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || '#cfa057',
  });
  head.append(el('div', 'home-head-spacer'));
  // Settings is reachable from here, not only from inside a case. Theme,
  // keybindings, default tags for new cases and the update check are all
  // things you might want to change BEFORE opening anything — and the gear
  // that used to be the only way in lives on the app bar, which the home
  // screen doesn't have.
  const setBtn = el('button', 'btn ghost', '⚙ Settings');
  setBtn.title = 'Appearance, keyboard shortcuts, default tags, plugins, updates';
  setBtn.onclick = openSettings;
  const offBtn = el('button', 'btn ghost', '⏻ Shut down');
  offBtn.title = 'Stop the Winnow server — cases stay saved on disk';
  offBtn.onclick = shutdownWinnow;
  head.append(setBtn, offBtn);
  inner.append(head);

  const card = el('div', 'home-card');
  inner.append(card);
  home.append(inner);

  if (!S.cases.length) {
    const empty = el('div', 'home-empty');
    empty.append(el('div', 'home-empty-title', 'No cases yet'),
      el('p', 'fb-help', '+ New case starts one. Open existing case file… adds one someone gave you. '
        + 'Files dropped here open in a quick look, which is kept only if you save it.'));
    card.append(empty);
  } else {
    const sections = homeSections(S.cases);
    const frame = sectionPanes({ sections, label: 'Case list sections', cls: 'home-panes', paneCls: 'home-pane' });
    for (const s of sections) s.build = buildCasesPane(s, sections, frame);
    card.append(frame.panes);
    // The section an analyst was on comes back with the list (a refresh
    // after Remove from list, or coming back from a case). Recent with
    // nothing in it opens on All cases instead of on an empty pane.
    let start = sections.find((s) => s.id === S.homeSection) || sections[0];
    if (start.id === 'recent' && !start.cases.length) start = sections[1];
    frame.show(start.id);
    frame.panes.querySelector('.side-rail-list').addEventListener('click', () => { S.homeSection = frame.current(); });
    frame.panes.querySelector('.side-rail-list').addEventListener('keydown', () => { S.homeSection = frame.current(); });
  }

  const foot = el('div', 'home-foot');
  const openBtn = el('button', 'btn ghost', 'Open existing case file…');
  openBtn.onclick = () => openExistingCasePrompt();
  const quick = el('button', 'btn ghost', 'Quick look at files…');
  quick.title = 'A scratch case that isn’t listed or kept until you save it — import, look, then keep it or discard it';
  quick.onclick = startQuickLook;
  const newBtn = el('button', 'btn primary', '+ New case');
  newBtn.onclick = () => openNewCaseModal();
  foot.append(openBtn, quick, el('span', 'home-foot-spacer'), newBtn);
  card.append(foot);
}

export async function refreshCases() {
  try { S.cases = await api('/api/cases'); } catch { S.cases = []; }
  renderHome();
}

/* The quick-look banner and its three exits. Save promotes the temp case
   to a real one (the file moves out of quicklook/ and lands on the home
   screen); Add copies every table into a case picked from the registry
   via copy_sources_to; Discard deletes the temp case outright. */
export function paintTempBanner(on) {
  S.tempCase = !!on;   // the one flag the home-navigation guard reads
  $('tempBanner').hidden = !on;
}

/* The three ways out of a quick look, shared by the banner's buttons and
   the leave-guard dialog (openQuickLookExitDialog). Discard returns
   whether it actually happened, so the guard knows if navigation may
   proceed; Save and Add open dialogs of their own and say so through
   their callbacks instead. */

/* Save is + New case's own dialog (openNewCaseModal), so a quick look is
   filed with the same name, group, path and profile as any other case. It
   used to ask for a name only and always land in the cases folder. The
   folder offered is the one the quick look already sits in (the parent of
   quicklook/), so saving there is a rename; variables the quick look
   already holds fill in the profile's fields. `onSaved` runs once the
   case is saved (and any profile applied); `onCancel` on Cancel. */
export async function tempSaveAs({ onSaved, onCancel } = {}) {
  const [cur, vars] = await Promise.all([
    api('/api/case/current').catch(() => null),
    api('/api/case/variables').catch(() => []),
  ]);
  const parentOf = (p) => (p || '').replace(/[\\/][^\\/]*$/, '');
  openNewCaseModal({
    chosenDir: parentOf(parentOf(cur && cur.path)) || undefined,
    vars: Object.fromEntries(vars.filter((v) => v.value).map((v) => [v.name, v.value])),
    saveQuickLook: { onSaved, onCancel },
  });
}

export function tempCopyModal({ thenDiscard = false } = {}) {
  (async () => {
    let cases;
    // The registry marks a case whose file is gone `exists: false` — there
    // is no `missing` key, so filtering on one offered every case, and
    // picking a vanished one only failed after the copy was asked for.
    try { cases = (await api('/api/cases')).filter((c) => c.exists !== false); }
    catch (e) { toast(e.message, 5000); return; }
    if (!cases.length) { toast('No saved cases yet — use "Save as a case…" instead', 6000); return; }
    modal('Add these tables to a case', (b) => {
      b.append(el('p', null,
        'Every table here — rows, tags and notes included — is copied into the case you pick. '
        + (thenDiscard
          ? 'The quick look is discarded once the copy lands, and you go back to the case list.'
          : 'This quick look stays as it is; discard it afterwards if you are done with it.')));
      const list = el('div', 'session-list');
      for (const c of cases) {
        const row = el('div', 'row-actions session-row');
        row.append(el('span', 'session-name', c.name),
                   el('span', 'count', c.path));
        const go = el('button', 'btn ghost', 'Copy into this case');
        go.onclick = oneAtATime(go, async () => {
          const ids = S.sources.filter((s) => s.id > 0 && !s.error).map((s) => s.id);
          if (!ids.length) { toast('Nothing here to copy yet'); return; }
          setBusy(true);
          let res;
          try { res = await post('/api/case/copy_sources', { target_path: c.path, source_ids: ids }); }
          catch (e) { toast(e.message, 8000); return; }
          finally { setBusy(false); }
          $('modal').hidden = true;
          toast(`Copied ${res.copied.length} table${res.copied.length === 1 ? '' : 's'} into "${c.name}"`, 8000);
          if (thenDiscard) await tempDiscard({ confirm: false });
        });
        row.append(go);
        list.append(row);
      }
      b.append(list);
    }, { wide: true });
  })();
}

export async function tempDiscard({ confirm = true } = {}) {
  if (confirm && !(await confirmDialog(
    'Discard this quick look?\n\nIts tables, tags and notes are deleted. '
    + 'Anything you copied into a real case stays there.',
    { danger: true, okLabel: 'Discard' }))) return false;
  try { await switchCase(() => post('/api/case/discard', {})); }
  catch (e) { toast(e.message, 6000); return false; }
  paintTempBanner(false);
  showHome();
  await refreshCases();
  return true;
}

/* A quick look is deliberately OFF the case registry, so the home screen
   has no way back to it — navigating there mid-quick-look stranded the
   case (reported). The brand button now asks for one of the three real
   exits first; "Stay" is always available. */
export function openQuickLookExitDialog() {
  modal("This quick look isn't saved", (b) => {
    b.append(el('p', null,
      'Quick looks never appear in the case list, so there is no way back to this one '
      + 'from the home screen. Choose what happens to it first:'));
    const acts = el('div', 'row-actions');
    const save = el('button', 'btn primary', 'Save as a case…');
    // The save dialog replaces this one; Cancel there comes back here.
    save.onclick = () => tempSaveAs({
      onSaved: async () => { showHome(); await refreshCases(); },
      onCancel: openQuickLookExitDialog,
    });
    const copy = el('button', 'btn ghost', 'Add to an existing case…');
    copy.onclick = () => tempCopyModal({ thenDiscard: true });
    const discard = el('button', 'btn ghost danger', 'Discard');
    discard.onclick = () => tempDiscard();
    const stay = el('button', 'btn ghost', 'Stay here');
    stay.onclick = () => { $('modal').hidden = true; };
    acts.append(save, copy, discard, stay);
    b.append(acts);
  });
}

function wireTempBanner() {
  $('tempSaveBtn').onclick = () => tempSaveAs();
  $('tempCopyBtn').onclick = () => tempCopyModal();
  $('tempDiscardBtn').onclick = () => tempDiscard();
}

export async function boot() {
  await Promise.all([loadSavedFilters(), loadHeaderNicknames(), loadTimelineTemplates(),
                     loadPlugins(), loadAppSettings()]);
  // The machine's saved look wins over this origin's cache (a quick-look
  // window on a free port is a different origin — see saveAppearance).
  syncAppearanceFromServer();
  const cur = await api('/api/case/current').catch(() => ({ open: false }));
  setCaseToken(cur.case_token);   // what every request from here on says it is about
  S.collapsedFolders = loadCollapsedFolders();
  if (cur.open) {
    setBrandLabel(cur.temp ? 'Quick look' : cur.name);
    paintTempBanner(!!cur.temp);
    showApp();
    await loadCaseSettings();
    await loadCaseTags();   // same reason as openCase: an empty case opens no table
    await loadSources();
    startJobsPoll(); // an import (or index build) from before a reload shows back up
  } else {
    showHome();
    await refreshCases();
  }
}

/* DOM wiring for this module, called once by main.js. Handlers can't
   fire during load, so the order these run in doesn't matter — the
   startup steps that DO depend on order live in main.js instead. */
export function wireHome() {
wireTempBanner();
$('btnHome').onclick = () => {
  if (S.tempCase) { openQuickLookExitDialog(); return; }
  showHome(); refreshCases();
};
// Drop files anywhere on the home screen that ISN'T a case row → a fresh
// quick-look case. Case rows stopPropagation on their own drop (above), so
// this only fires for the "no particular case" gesture.
const home = $('home');
home.addEventListener('dragover', (e) => {
  if (!isFileDrag(e)) return;
  e.preventDefault();
  home.classList.add('home-drop');
});
home.addEventListener('dragleave', (e) => { if (e.target === home) home.classList.remove('home-drop'); });
home.addEventListener('drop', (e) => {
  if (!isFileDrag(e)) return;
  e.preventDefault(); e.stopPropagation();
  home.classList.remove('home-drop');
  const files = [...e.dataTransfer.files];
  if (files.length) importDroppedFiles(files, null);   // null → new quick-look case
});
}
