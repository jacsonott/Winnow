/* The home screen: the case registry, opening/creating cases, and boot().

   Split out of the former single static/app.js — see CLAUDE.md. */
import { applyBundle } from './bundles.js';
import { $, api, el, post, setBusy, toast } from './core.js';
import { loadPlugins } from './importer.js';
import { startJobsPoll } from './jobs.js';
import { resetPluginTabMounts } from './plugins.js';
import { loadAppSettings, loadCaseSettings, loadHeaderNicknames, loadSavedFilters } from './savedfilters.js';
import { updateSearchAllButton } from './search.js';
import { clearViewStateStash, applyPageTabsSize, loadSources } from './sources.js';
import { showGridTab } from './sql.js';
import { openSettings } from './settings.js';
import { drawWordmark } from './splash.js';
import { S } from './state.js';
import { fmtBytes } from './tables.js';
import { updateTimeRangeButton } from './timeframe.js';
import { loadTimelineTemplates } from './timeline.js';
import { confirmDialog, modal, promptDialog } from './ui.js';

/* -------------------------------------------------------------- home screen */

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
   affordance, because there's no server left to serve one. */
export async function shutdownWinnow() {
  const go = await confirmDialog(
    'Shut down the Winnow server?\n\nEverything is already saved in the case file on disk — tags, notes and '
    + 'imports are never lost. This page (and any other tab using this server) will stop working until you '
    + 'start Winnow again.',
    { okLabel: 'Shut down', cancelLabel: 'Keep running', danger: true });
  if (!go) return;
  try { await post('/api/shutdown', {}); } catch { /* the server may drop before the response lands */ }
  const note = el('div', 'shutdown-note');
  note.append(
    el('div', 'shutdown-note-title', 'Winnow is off'),
    el('div', null, 'The server has shut down. You can close this tab — start Winnow again with "python server.py".'),
  );
  document.body.replaceChildren(note);
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

export async function openCase(path, opts = {}) {
  let res;
  try {
    res = await post('/api/case/open', { path, force: !!opts.force });
  } catch (e) {
    if (e.status === 409 && e.detail && e.detail.error === 'case_in_use') {
      const go = await confirmDialog(describeCaseHolder(e.detail.holder), {
        okLabel: 'Open anyway', cancelLabel: 'Don\u2019t open', danger: true,
      });
      if (!go) return;
      return openCase(path, { force: true });
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
  S.tabOrder = [];
  // Unlike a tab switch within the *same* case (where the timeframe filter
  // deliberately survives — see clearAllFilters()/applyPreset()), a
  // different case is a different investigation; a timeframe pinned in
  // the last one has no reason to silently carry over into this one.
  S.timeRange = { enabled: false, column: null, start: '', end: '' };
  updateTimeRangeButton();
  // Same reasoning for the timeline: its view_id belongs to the Store
  // instance that just closed, and its tag-id checkboxes belong to the
  // previous case's tag_defs — neither means anything here.
  S.timeline = { view: null, pages: new Map(), pending: new Set(), reqId: S.timeline.reqId + 1, tagFilter: null };
  // sql_tabs is a per-case table, so the previous case's tabs (and the
  // in-memory results keyed by their ids) don't describe this one. Left
  // empty rather than reloaded here — showSqlTab loads lazily.
  S.sqlTabs = [];
  S.sqlTabId = null;
  S.sqlResults.clear();
  // A Search-all job belongs to the Store instance the server just closed;
  // its results reference source ids from the previous case.
  S.searchAll = null;
  updateSearchAllButton();
  // A mounted plugin tab's UI was built from the previous case's data —
  // tear the mounts down so the next activation rebuilds against this one.
  resetPluginTabMounts();
  // The effective plugin set is per-case (case_settings overrides beat the
  // machine default), and the server reloaded its registry when this case
  // opened — refetch so tabs/formats/panel reflect THIS case's plugins.
  await loadPlugins();
  if (S.activeTab !== 'grid') showGridTab();
  setBrandLabel(res.name);
  showApp();
  // ts_format lives in the case file, so it's per-case state like sql_tabs
  // — reload it rather than carrying the last case's setting over.
  await loadCaseSettings();
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
    const list = el('div', 'session-list');
    list.style.maxHeight = '42vh';
    list.style.overflow = 'auto';
    b.append(list);
    const status = el('div', 'note-status', '');
    b.append(status);

    const actions = el('div', 'row-actions');
    const useBtn = el('button', 'btn', filesMode ? 'Add selected' : 'Use this folder');
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
        res = await api(`/api/browse_dir?path=${encodeURIComponent(path || '')}${filesMode ? '&files=true' : ''}`);
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
      paint();
    }

    function paint() {
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
        for (const f of listing.files || []) {
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
          row.append(cb, el('span', 'session-name', f.name), el('span', 'count', fmtBytes(f.size)));
          frag.append(row);
        }
        if (!(listing.files || []).length && !listing.dirs.length) {
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
   state, same pattern openImportPreview's onConfirm/onCancel already use. */
export function openNewCaseModal(state = {}) {
  modal('New case', (b) => {
    const nameInput = fieldInput(state.name || '');
    nameInput.placeholder = 'Case name';
    const nameRow = el('div', 'row-actions');
    nameRow.append(nameInput);
    b.append(el('label', null, 'Name'), nameRow);

    const groupInput = fieldInput(state.group || '');
    groupInput.placeholder = 'Group (optional) — e.g. an IR engagement name';
    groupInput.setAttribute('list', 'home-new-case-groups');
    const groupRow = el('div', 'row-actions');
    groupRow.append(groupInput, groupOptionsDatalist('home-new-case-groups'));
    b.append(el('label', null, 'Group'), groupRow);

    let chosenDir = state.chosenDir || S.casesDir || 'cases';
    const pathInput = fieldInput(state.path || `${chosenDir}/${slugify(state.name || '')}.db`);
    pathInput.style.fontFamily = 'var(--mono)';
    let pathTouched = state.pathTouched || false;
    pathInput.oninput = () => { pathTouched = true; };
    nameInput.oninput = () => { if (!pathTouched) pathInput.value = `${chosenDir}/${slugify(nameInput.value)}.db`; };
    const browseBtn = el('button', 'btn ghost', 'Browse…');
    browseBtn.onclick = () => {
      const snapshot = {
        name: nameInput.value, group: groupInput.value, path: pathInput.value,
        chosenDir, pathTouched,
        caseType: typeSel.value,
      };
      openFolderBrowser(
        chosenDir,
        (dir) => openNewCaseModal({
          ...snapshot, chosenDir: dir, pathTouched: false, path: `${dir}/${slugify(snapshot.name)}.db`,
        }),
        () => openNewCaseModal(snapshot),
      );
    };
    const pathRow = el('div', 'row-actions');
    pathRow.append(pathInput, browseBtn);
    b.append(el('label', null, 'Case file path'), pathRow);

    // Case type: a plugin bundle applied right after the case opens, so
    // a Triage case starts with the triage plugins on — no settings trip.
    const typeSel = el('select');
    typeSel.style.cssText = 'flex:1;background:var(--ink);color:var(--text);border:1px solid var(--line-2);padding:6px 8px;font:inherit';
    const noneOpt = el('option', null, 'None — machine defaults');
    noneOpt.value = '';
    typeSel.append(noneOpt);
    api('/api/plugin_bundles').then((bundles) => {
      for (const bd of bundles) {
        const o = el('option', null, `${bd.name} (${bd.plugins.length} plugin${bd.plugins.length === 1 ? '' : 's'})`);
        o.value = String(bd.id);
        typeSel.append(o);
      }
      if (state.caseType) typeSel.value = state.caseType;
    }).catch(() => {});
    const typeRow = el('div', 'row-actions');
    typeRow.append(typeSel);
    b.append(el('label', null, 'Case type'), typeRow);


    const actions = el('div', 'row-actions');
    const create = el('button', 'btn', 'Create case');
    create.onclick = async () => {
      const name = nameInput.value.trim();
      if (!name) { toast('Name the case first'); return; }
      const path = pathInput.value.trim();
      if (!path) { toast('Give the case file a path'); return; }
      try {
        await post('/api/cases', { path, name, group: groupInput.value.trim(), notes: '' });
      } catch (e) {
        toast('Could not create case: ' + e.message, 6000);
        return;
      }
      $('modal').hidden = true;
      await openCase(path); // shared with the home screen's "open" flow — same brand-label/view-cache handling
      if (typeSel.value) {
        try {
          await applyBundle({ id: Number(typeSel.value) });
        } catch (e) {
          toast('Case created, but the case-type bundle failed to apply: ' + e.message, 6000);
        }
      }
    };
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { $('modal').hidden = true; };
    actions.append(create, cancel);
    b.append(actions);
  });
}

/* First run of this Winnow instance (no cases_dir configured, no cases
   yet): ask where case files should live instead of silently defaulting
   to ./cases. Asked once — either answer configures the instance. */
export async function maybeOfferStorageDir() {
  let prefs;
  try {
    prefs = await api('/api/prefs');
  } catch { return; }
  S.casesDir = prefs.cases_dir || null;
  if (!prefs.first_run) return;
  const pick = await confirmDialog(
    'First run — where should Winnow keep its case files? '
    + 'The default is a "cases" folder next to the server. You can pick any '
    + 'folder on this machine instead.',
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
      const res = await post('/api/prefs', { cases_dir: 'cases' });
      S.casesDir = res.cases_dir;
    } catch { /* next launch asks again — fine */ }
  }
}

export async function openExistingCasePrompt() {
  const path = await promptDialog('Path to an existing case .db file:');
  if (!path || !path.trim()) return;
  const trimmed = path.trim();
  const name = trimmed.split(/[\\/]/).pop().replace(/\.db$/i, '');
  try {
    await post('/api/cases', { path: trimmed, name, group: '', notes: '' });
  } catch (e) {
    toast('Could not register case: ' + e.message, 6000);
    return;
  }
  await openCase(trimmed);
}

export function openEditCaseModal(c) {
  modal('Edit case', (b) => {
    const nameInput = fieldInput(c.name);
    const nameRow = el('div', 'row-actions');
    nameRow.append(nameInput);
    b.append(el('label', null, 'Name'), nameRow);

    const groupInput = fieldInput(c.group || '');
    groupInput.setAttribute('list', 'home-edit-case-groups');
    const groupRow = el('div', 'row-actions');
    groupRow.append(groupInput, groupOptionsDatalist('home-edit-case-groups'));
    b.append(el('label', null, 'Group'), groupRow);

    const notesArea = el('textarea');
    notesArea.rows = 4;
    notesArea.value = c.notes || '';
    notesArea.placeholder = 'Notes about this case…';
    notesArea.style.width = '100%';
    b.append(el('label', null, 'Notes'), notesArea);

    b.append(el('div', 'note-status', c.path));

    const actions = el('div', 'row-actions');
    const save = el('button', 'btn', 'Save');
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
  await api(`/api/cases/${c.id}`, { method: 'DELETE' });
  refreshCases();
}

export async function deleteCaseFile(c) {
  if (!(await confirmDialog(
    `Permanently delete the case file for "${c.name}"?\n\n${c.path}\n\nThis cannot be undone — all tags, notes and imported data in it will be lost.`,
    { danger: true, okLabel: 'Delete permanently' },
  ))) return;
  try {
    await api(`/api/cases/${c.id}?delete_file=true`, { method: 'DELETE' });
  } catch (e) {
    toast('Could not delete: ' + e.message, 6000);
    return;
  }
  refreshCases();
}

export function renderCaseRow(c) {
  const row = el('div', 'home-case-row' + (c.exists === false ? ' missing' : ''));
  const main = el('div', 'home-case-main');
  const nameRow = el('div', 'home-case-name');
  nameRow.append(el('span', null, (c.exists === false ? '⚠ ' : '') + c.name));
  if (c.group) nameRow.append(el('span', 'home-case-group-badge', c.group));
  main.append(nameRow);
  const stats = c.exists === false
    ? `${c.path} — file not found`
    : c.error
      ? `${c.path} — ${c.error}`
      : `${c.path} · ${(c.source_count || 0).toLocaleString()} source${c.source_count === 1 ? '' : 's'} `
        + `· ${(c.row_count || 0).toLocaleString()} rows` + (c.last_opened ? ` · opened ${c.last_opened}` : '');
  main.append(el('div', 'home-case-meta', stats));
  if (c.notes) main.append(el('div', 'home-case-notes', c.notes));
  if (c.exists !== false) main.onclick = () => openCase(c.path);
  row.append(main);

  // Always four button slots, in the same order, even when an action
  // doesn't apply to this row (a missing case can't be Opened or have its
  // file Deleted) — .home-case-actions is a plain flex row with no fixed
  // column widths, so a row with fewer buttons used to pack them flush
  // right instead of leaving Edit/Remove from list in their usual columns,
  // visibly zig-zagging as soon as a missing case sat next to a real one.
  // visibility:hidden (not omitting the element) keeps the slot's width
  // reserved without making it paintable or clickable; disabled backs
  // that up and drops it from tab order.
  const actions = el('div', 'home-case-actions');
  const openBtn = el('button', 'btn ghost', 'Open');
  if (c.exists !== false) {
    openBtn.onclick = () => openCase(c.path);
  } else {
    openBtn.disabled = true;
    openBtn.style.visibility = 'hidden';
  }
  const edit = el('button', 'btn ghost', 'Edit');
  edit.onclick = () => openEditCaseModal(c);
  const remove = el('button', 'btn ghost', 'Remove from list');
  remove.onclick = () => removeCaseFromList(c);
  const del = el('button', 'btn ghost', 'Delete file…');
  if (c.exists !== false) {
    del.title = 'Permanently delete the case file from disk';
    del.onclick = () => deleteCaseFile(c);
  } else {
    del.disabled = true;
    del.style.visibility = 'hidden';
  }
  actions.append(openBtn, edit, remove, del);
  row.append(actions);
  return row;
}

export const HOME_STALE_MS = 30 * 24 * 60 * 60 * 1000;

 // 30 days

export function isStaleCase(c) {
  // Never-opened cases (just created, or from before last_opened existed)
  // aren't "stale" — they need attention, not hiding.
  if (!c.last_opened) return false;
  const t = Date.parse(c.last_opened);
  return !Number.isNaN(t) && (Date.now() - t) > HOME_STALE_MS;
}

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
    color: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || '#d9a441',
  });
  head.append(el('div', 'home-head-spacer'));
  const newBtn = el('button', 'btn', '+ New case');
  newBtn.onclick = openNewCaseModal;
  const openBtn = el('button', 'btn ghost', 'Open existing case file…');
  openBtn.onclick = openExistingCasePrompt;
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
  head.append(newBtn, openBtn, setBtn, offBtn);
  inner.append(head);

  if (!S.cases.length) {
    inner.append(el('div', 'home-empty', 'No cases yet — create one to get started.'));
    home.append(inner);
    return;
  }

  const searchRow = el('div', 'home-search-row');
  const search = el('input', 'home-search');
  search.type = 'search';
  search.placeholder = 'Search cases or groups…';
  search.value = S.homeSearch;
  searchRow.append(search);
  inner.append(searchRow);

  const listWrap = el('div', 'home-case-list');
  inner.append(listWrap);

  function renderList() {
    listWrap.replaceChildren();
    const q = S.homeSearch.trim().toLowerCase();
    const matches = (c) => !q || c.name.toLowerCase().includes(q) || (c.group || '').toLowerCase().includes(q);
    // Most-recently-opened first; a case that's never been opened (no
    // last_opened) sorts after every case that has been, newest first.
    const sorted = [...S.cases].sort((a, b) => (b.last_opened || '').localeCompare(a.last_opened || ''));
    const filtered = sorted.filter(matches);
    const visible = S.homeShowOlder ? filtered : filtered.filter((c) => !isStaleCase(c));
    const hiddenCount = filtered.length - visible.length;
    if (!visible.length) {
      listWrap.append(el('div', 'home-empty', q ? 'No cases match that search.' : 'No cases to show.'));
    } else {
      for (const c of visible) listWrap.append(renderCaseRow(c));
    }
    if (hiddenCount > 0) {
      const toggle = el('button', 'btn ghost home-show-older',
        `Show ${hiddenCount.toLocaleString()} case${hiddenCount === 1 ? '' : 's'} not opened in over 30 days…`);
      toggle.onclick = () => { S.homeShowOlder = true; renderList(); };
      listWrap.append(toggle);
    }
  }
  search.oninput = () => { S.homeSearch = search.value; renderList(); };
  renderList();

  home.append(inner);
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
  $('tempBanner').hidden = !on;
}

function wireTempBanner() {
  $('tempSaveBtn').onclick = async () => {
    const name = await promptDialog('Save this as a case named:');
    if (!name || !name.trim()) return;
    let res;
    try { res = await post('/api/case/save_as', { name: name.trim() }); }
    catch (e) { toast(e.message, 6000); return; }
    paintTempBanner(false);
    setBrandLabel(res.name);
    toast(`Saved — "${res.name}" is on the home screen now`, 6000);
  };

  $('tempCopyBtn').onclick = async () => {
    let cases;
    try { cases = (await api('/api/cases')).filter((c) => !c.missing); }
    catch (e) { toast(e.message, 5000); return; }
    if (!cases.length) { toast('No saved cases yet — use "Save as a case…" instead', 6000); return; }
    modal('Add these tables to a case', (b) => {
      b.append(el('p', null,
        'Every table here — rows, tags and notes included — is copied into the case you pick. '
        + 'This quick look stays as it is; discard it afterwards if you are done with it.'));
      const list = el('div', 'session-list');
      for (const c of cases) {
        const row = el('div', 'row-actions session-row');
        row.append(el('span', 'session-name', c.name),
                   el('span', 'count', c.path));
        const go = el('button', 'btn ghost', 'Copy into this case');
        go.onclick = async () => {
          const ids = S.sources.filter((s) => s.id > 0 && !s.error).map((s) => s.id);
          if (!ids.length) { toast('Nothing here to copy yet'); return; }
          setBusy(true);
          let res;
          try { res = await post('/api/case/copy_sources', { target_path: c.path, source_ids: ids }); }
          catch (e) { toast(e.message, 8000); return; }
          finally { setBusy(false); }
          $('modal').hidden = true;
          toast(`Copied ${res.copied.length} table${res.copied.length === 1 ? '' : 's'} into "${c.name}"`, 8000);
        };
        row.append(go);
        list.append(row);
      }
      b.append(list);
    }, { wide: true });
  };

  $('tempDiscardBtn').onclick = async () => {
    if (!(await confirmDialog(
      'Discard this quick look?\n\nIts tables, tags and notes are deleted. '
      + 'Anything you copied into a real case stays there.',
      { danger: true, okLabel: 'Discard' }))) return;
    try { await post('/api/case/discard', {}); }
    catch (e) { toast(e.message, 6000); return; }
    paintTempBanner(false);
    showHome();
    await refreshCases();
  };
}

export async function boot() {
  await Promise.all([loadSavedFilters(), loadHeaderNicknames(), loadTimelineTemplates(),
                     loadPlugins(), loadAppSettings()]);
  const cur = await api('/api/case/current').catch(() => ({ open: false }));
  if (cur.open) {
    setBrandLabel(cur.temp ? 'Quick look' : cur.name);
    paintTempBanner(!!cur.temp);
    showApp();
    await loadCaseSettings();
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
$('btnHome').onclick = () => { showHome(); refreshCases(); };
}
