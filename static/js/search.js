/* The toolbar search box and the search-all-tables job.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, MOD_ENTER, api, el, post, toast } from './core.js';
import { addViewCountWidget } from './dashboard.js';
import { openFilterBuilder } from './filterbuilder.js';
import { renderAdvancedChips, renderTermChips, updateSearchHint } from './filters.js';
import { applyPreset, matchingSavedFilters } from './savedfilters.js';
import { openSettings } from './settings.js';
import { loadSources, sourceGlyph, sourceLabel } from './sources.js';
import { S, dashboardCreatorMode } from './state.js';
import { saveCurrentViewAsTable } from './subset.js';
import { openSavedFiltersModal, openTimeRangeModal } from './timeframe.js';
import { markModalAction, confirmDialog, currentModalAction, dropdownMenu, modal } from './ui.js';
import { rebuildView } from './view.js';
import { runScan } from './watchlist.js';

/* Checked against the tables the dialog's scope row names: the table in
   the grid by default, every real table in the case when nothing is open
   or "Every table" is asked for (searchAllScope here, resolved on the
   server by resolve_search_all_scope — a merged table in scope means its
   member tables). Plain contains-mode only — same as the grid's default
   search — not regex. Clicking a result opens that table with the same
   terms already applied via Advanced search, rather than inventing a
   separate cross-table results view.

   Two ways to build the term list, sharing one results pane:
   "Paste a list" (default) — a multi-line textarea, one term per line,
   OR'd together — the list-of-IOCs/hostnames/hashes use case, and lets you
   see more than the first line unlike a single-line input. "Advanced" is
   the original AND/OR/NOT chip builder, for anything needing mixed
   connectors or an exclusion — still available, just not the default.

   Explicit "Search" button rather than live-as-you-type: this is a
   COUNT(*) per table in scope, with a background FTS build potentially
   kicked off per table too (see search_all_sources) — and the scope is
   every table in the case whenever the analyst says so — not the cheap
   single-open-table filter the main grid's search bar is. Firing that on
   every keystroke while someone's still typing a hostname is real,
   avoidable backend load, not just a UX annoyance — so nothing here runs
   until the button (or Enter, or Cmd/Ctrl+Enter in the textarea) says to.

   The sweep itself runs as a server-side job (Store.start_search_all_job),
   polled from here. Every piece of this pane's state — the typed terms, the
   mode toggle, the job id, the hits so far — lives in S.searchAll rather
   than in the modal's closure, which is what makes closing the modal
   mid-sweep safe: the poll keeps running, the results keep accumulating,
   and reopening rebuilds the pane exactly where it was. On a 42 GB merge
   this sweep is minutes long; making the analyst sit and watch it was the
   real problem, not the sweep's own cost. */

/* Lazily created so a session that never searches carries no state. */
export function searchAllState() {
  if (!S.searchAll) {
    S.searchAll = {
      mode: 'paste',                                        // 'paste' | 'advanced'
      chipTerms: [{ term: '', connector: 'AND', exclude: false }],
      pasteText: '',
      jobId: null,
      running: false,
      scanned: 0,
      total: 0,
      hits: [],
      error: null,
      terms: [],       // the terms the current results were produced from
      scope: null,     // {mode: 'all'|'current'|'pick', ids, names} — normalised by searchAllScope
      ranScope: null,  // the scope the current results were produced from (see terms)
      seen: false,     // whether the analyst has looked at the finished results
    };
  }
  return S.searchAll;
}

/* Terms from whichever builder is active, in the shape the API wants. */
export function searchAllTerms(st) {
  if (st.mode === 'advanced') return st.chipTerms.filter((t) => t.term.trim());
  return st.pasteText.split('\n').map((l) => l.trim()).filter(Boolean)
    .map((term) => ({ term, connector: 'OR', exclude: false }));
}

/* ------------------------------------------------------------- scope */

/* The sweep used to be one thing: every table in the case. It still
   defaults to that on a case with nothing open, but "search this table for
   forty hostnames" was only reachable by sweeping the whole case and then
   reading one row of the results — minutes of scanning for an answer about
   one file.

   Three scopes, all of them the same job and the same results pane:
   'all' (source_ids: null on the wire — the server's own default),
   'current' (the table in the grid, re-read at every start so it follows
   the tab rather than pinning whatever was open when the pane was built),
   and 'pick' (an explicit list of real table ids).

   The scope lives on S.searchAll beside the terms for the same reason they
   do: the pane's state has to survive the modal being closed mid-sweep. */

/* The table in the grid, if it is one this case still has. S.sourceId can
   name a source from a previous case (loadSources documents why), and can
   be null outright when a case has no open tab. */
export function searchAllOpenSource() {
  return S.sources.find((s) => s.id === S.sourceId && !s.error) || null;
}

/* A Choose… scope over `ids`, each id remembered by the name its table
   has right now — see searchAllLivePickIds for why the name is kept. */
export function searchAllPickScope(ids) {
  const names = {};
  for (const id of ids) {
    const src = S.sources.find((s) => s.id === id);
    if (src) names[id] = src.name;
  }
  return { mode: 'pick', ids: [...ids], names };
}

/* The picked ids that still name the table they were picked from. SQLite
   reuses a source id once the table holding it is dropped, so a pick keeps
   the name each id had when it was ticked and drops an id whose live table
   is a different file — the same id-and-name rule subsetParentLabel
   follows, for the same reason. Without it, dropping a table and importing
   another that takes its id silently scopes the sweep to a file nobody
   chose. */
export function searchAllLivePickIds(sc) {
  const names = (sc && sc.names) || {};
  return ((sc && sc.ids) || []).filter((id) => S.sources.some(
    (s) => s.id === id && !s.error && (names[id] === undefined || s.name === names[id])));
}

/* A pick gone stale wholesale: it named tables, and not one of them is
   still in this case. It widens back to every table rather than pointing
   at nothing — and the scope row says so, because a three-table check
   quietly becoming a whole-case sweep is minutes an analyst didn't ask
   for. */
export function searchAllPickStale(st) {
  const sc = st.scope;
  return !!(sc && sc.mode === 'pick' && sc.ids.length && !searchAllLivePickIds(sc).length);
}

/* The chosen scope, normalised: a 'current' scope with no table open, or a
   'pick' whose tables have since all been dropped, answers 'all' rather
   than pointing at nothing. Assigns the default on first use — "this
   table" when there is one, since someone who opens this from a table
   nearly always means that table, and the whole-case sweep is one click
   away.

   A pick the analyst has emptied by hand is left alone: it is a scope
   half-built, not a stale one, and turning it into "every table" behind
   them is how a quick check becomes a four-minute sweep. Starting with it
   empty is refused in startSearchAll instead. */
export function searchAllScope(st) {
  if (!st.scope) st.scope = { mode: searchAllOpenSource() ? 'current' : 'all', ids: [] };
  const sc = st.scope;
  if (sc.mode === 'current' && !searchAllOpenSource()) return { mode: 'all', ids: [] };
  if (searchAllPickStale(st)) return { mode: 'all', ids: [] };
  return sc;
}

/* What goes on the wire: null for the whole case, otherwise the ids to
   scan. A merge id is sent AS the merge — the server expands it to the
   member tables its rows actually live in (a merge has no table of its
   own) and reports that expansion back, so the pane can say so rather
   than quietly answering about different tables than the one named. */
export function searchAllScopeIds(st) {
  const sc = searchAllScope(st);
  if (sc.mode === 'all') return null;
  if (sc.mode === 'current') {
    const src = searchAllOpenSource();
    return src ? [src.id] : null;
  }
  return searchAllLivePickIds(sc);
}

/* The REAL tables the current scope covers, for painting the table chips —
   null meaning "every one of them". A merge resolves to its members here
   too, so picking a merged table lights up the tables it is made of. */
export function searchAllScopeSelection(st) {
  const ids = searchAllScopeIds(st);
  if (!ids) return null;
  const out = new Set();
  for (const id of ids) {
    const src = S.sources.find((s) => s.id === id);
    if (src && src.is_merge) (src.member_source_ids || []).forEach((m) => out.add(m));
    else out.add(id);
  }
  return out;
}

/* A run's scope in words — for the badge's tooltip, the finished toast and
   the results pane. Reads the scope the RESULTS came from, never the one
   the scope row has since been switched to. */
export function searchAllRanScopeLabel(scope) {
  const ids = scope && scope.source_ids;
  if (!ids) return 'every table';
  if (!ids.length) return 'no tables';
  if (ids.length === 1) {
    const src = S.sources.find((s) => s.id === ids[0]);
    return src ? sourceLabel(src) : `table ${ids[0]}`;
  }
  return `${ids.length} tables`;
}

export let searchAllPollTimer = null;

/* Polls the job to completion regardless of whether the modal is open —
   that's the whole point. Repaints the modal's results pane only if it
   happens to be showing (searchAllRepaint is a no-op otherwise). */
export function pollSearchAll() {
  clearTimeout(searchAllPollTimer);
  searchAllPollTimer = setTimeout(async () => {
    const st = S.searchAll;
    if (!st || !st.running || st.jobId == null) return;
    const polled = st.jobId;
    let job;
    try {
      job = await api(`/api/search_all/job?job_id=${polled}`);
    } catch (e) {
      // A 404 means the job asked for is not on the server — which is the
      // ordinary end of a superseded poll now that starting a scoped run
      // while a sweep is going is a thing the dialog invites: the start
      // replaces the case's one job, so a GET already in flight for the
      // old one comes back 404 while the new one is starting fine. Only
      // the job we are still following gets to end the run here.
      if (!S.searchAll || S.searchAll !== st || st.jobId !== polled) return;
      // Otherwise the job genuinely no longer exists (the server restarted,
      // or the case was closed/switched underneath us), and this has to
      // clear `running`, or the badge sticks at "Search all… n/m" forever
      // with nothing left to advance it.
      st.running = false;
      st.error = e.status === 404
        ? 'The search job is no longer on the server (it restarted, or the case was closed). Run the search again.'
        : e.message;
      updateSearchAllButton();
      searchAllRepaint();
      return;
    }
    if (!S.searchAll || S.searchAll !== st || st.jobId !== job.job_id) return; // superseded mid-flight
    st.scanned = job.scanned;
    st.total = job.total;
    st.hits = job.hits;
    st.error = job.error;
    // The scope the server actually ran, merge expansion and all — it is
    // what these hits describe, so it is kept with them.
    if (job.scope) st.ranScope = job.scope;
    if (job.done || job.cancelled) {
      st.running = false;
      // Asked of the modal's ACTION, not its title: the title names the
      // scope now, and a dialog identified by a literal string stops being
      // recognised the moment that string is allowed to vary.
      const open = !$('modal').hidden && currentModalAction() === 'openSearchAll';
      st.seen = open;
      if (!open && !job.cancelled) {
        const where = st.ranScope && st.ranScope.source_ids
          ? `Search of ${searchAllRanScopeLabel(st.ranScope)}`
          : 'Search all';
        toast(job.hits.length
          ? `${where} finished — ${job.hits.length} table${job.hits.length === 1 ? '' : 's'} matched. Reopen "Search all" to see them.`
          : `${where} finished — no matches.`, 6000);
      }
    }
    updateSearchAllButton();
    searchAllRepaint();
    if (st.running) pollSearchAll();
  }, 400);
}

/* Badge on the toolbar button so a sweep running behind a closed modal is
   still visible, and a finished-but-unread one invites you back. */
export function updateSearchAllButton() {
  const btn = $('btnSearchAll');
  if (!btn) return;
  const st = S.searchAll;
  // "Search all… 2/3" on a run that covered one table is a badge lying
  // about what it counted, so the wording follows the run's own scope.
  const scoped = !!(st && st.ranScope && st.ranScope.source_ids);
  if (st && st.running) {
    const pct = st.total ? ` ${st.scanned}/${st.total}` : '';
    btn.textContent = `${scoped ? 'Searching…' : 'Search all…'}${pct}`;
    btn.setAttribute('aria-busy', 'true');
    btn.title = scoped
      ? `Searching ${searchAllRanScopeLabel(st.ranScope)} in the background — click to watch or refine it`
      : 'Search running in the background — click to watch or refine it';
  } else if (st && !st.seen && st.hits.length) {
    btn.textContent = `${scoped ? 'Search' : 'Search all'} (${st.hits.length})`;
    btn.removeAttribute('aria-busy');
    btn.title = `${st.hits.length} table(s) matched — click to see them`;
  } else {
    btn.textContent = 'Search all';
    btn.removeAttribute('aria-busy');
    btn.title = 'Search this case — every table, or just the one you are on';
  }
}

/* Set by openSearchAllModal while its pane is on screen; cleared when the
   modal closes or is replaced. Lets the poller repaint without knowing
   anything about the modal's internals. */
export let searchAllRepaint = () => {};
/* Same reason as setRowH in core.js: modal() clears this hook from ui.js. */
export function setSearchAllRepaint(fn) { searchAllRepaint = fn; }

export async function startSearchAll() {
  const st = searchAllState();
  const terms = searchAllTerms(st);
  if (!terms.length) {
    // Emptying the box clears the pane — but not out from under a sweep
    // that is still running: this branch used to drop the job id and the
    // partial hits on the floor with the server still scanning, leaving
    // nothing on screen that could stop it. Stopping a sweep is the Stop
    // button's job, and it is deliberate there.
    if (st.running) { toast('Enter a term, or use Stop to end the search that is running'); return; }
    st.hits = []; st.terms = []; st.error = null; st.jobId = null; st.ranScope = null;
    updateSearchAllButton();
    searchAllRepaint();
    return;
  }
  const sourceIds = searchAllScopeIds(st);
  if (sourceIds && !sourceIds.length) { toast('Tick at least one table to search'); return; }
  // One search job per case: this one replaces whatever is still running,
  // and a whole-case sweep can be four minutes in. Killing that silently
  // to answer a quick one-table question is the kind of loss you only
  // notice afterwards, so it is asked rather than assumed.
  if (st.running) {
    const where = st.ranScope && st.ranScope.source_ids
      ? `The search of ${searchAllRanScopeLabel(st.ranScope)}`
      : 'The sweep of every table';
    const done = st.total ? ` (${st.scanned} of ${st.total} tables counted)` : '';
    const ok = await confirmDialog(
      `${where} is still running${done}. Only one search runs at a time, so starting this one stops it.`,
      { okLabel: 'Stop it and search' });
    if (!ok) return;
  }
  st.terms = terms.map((t) => ({ ...t }));
  // Provisional: the server answers with the scope it resolved (a merge
  // expanded to its members), and the poll keeps it current.
  st.ranScope = { requested: sourceIds, source_ids: sourceIds, merges: [] };
  st.hits = [];
  st.error = null;
  st.scanned = 0;
  st.total = 0;
  st.seen = true;
  try {
    const job = await post('/api/search_all/start', { terms, source_ids: sourceIds });
    st.jobId = job.job_id;
    st.running = true;
    // A poll of the job this start just superseded can have landed while
    // the POST was in flight; this run has not failed, whatever it said.
    st.error = null;
    if (job.scope) st.ranScope = job.scope;
  } catch (e) {
    st.running = false;
    // A 404 on *this* endpoint means the route doesn't exist, not that
    // something wasn't found: static/ is served from disk (no-cache, so a
    // reload picks up new JS immediately) while server.py's routes are
    // whatever was imported when the process started. A frontend newer than
    // the running server lands exactly here, and the bare "Not Found" that
    // used to surface read like "your search matched nothing".
    st.error = e.status === 404
      ? 'This build of the page needs a newer server than the one running — restart server.py and reload.'
      : e.message;
  }
  updateSearchAllButton();
  searchAllRepaint();
  if (st.running) pollSearchAll();
}

/* "1,000+" rather than a precise number the server never computed —
   `capped` means the count stopped at SEARCH_ALL_COUNT_CAP instead of
   scanning every matching row. */
export function searchAllCountLabel(d) {
  return d.capped
    ? `${d.match_count.toLocaleString()}+ matches`
    : `${d.match_count.toLocaleString()} match${d.match_count === 1 ? '' : 'es'}`;
}

/* One results row. With `term`, it's that term's own count inside `hit`'s
   table and opening it searches for just that term; without, it's the
   table's total and opening it carries the whole query across. Both share
   this so the open behaviour can't drift between the two. */
export function searchAllHitRow(st, hit, term) {
  // The label goes through the live source record so a nickname shows here
  // too; the job's own hit.name (the file name) is the fallback for a
  // source dropped since the sweep ran.
  const hitSrc = S.sources.find((s) => s.id === hit.source_id);
  const hitName = hitSrc ? sourceLabel(hitSrc) : hit.name;
  const r = el('div', 'search-all-row' + (term ? ' search-all-term-row' : ''));
  r.append(
    el('span', 'search-all-name', term ? term.term : hitName),
    el('span', 'search-all-count', searchAllCountLabel(term || hit)),
  );
  const openBtn = el('button', 'btn ghost', 'Open ↦');
  openBtn.title = term
    ? `Open ${hitName} filtered to "${term.term}"`
    : `Open ${hitName} filtered to every term`;
  openBtn.onclick = async () => {
    const src = S.sources.find((s) => s.id === hit.source_id);
    if (src && !src.is_open) await post(`/api/source/${hit.source_id}/open`, { open: true });
    $('modal').hidden = true;
    await loadSources(hit.source_id);
    S.searchMode = 'advanced';
    // The terms the *results* came from, not whatever's since been typed
    // into the box — those are what this row's count describes.
    S.searchTerms = term
      ? [{ term: term.term, connector: 'AND', exclude: false }]
      : st.terms.map((t) => ({ ...t }));
    S.advCollapsed = null;  // a pasted IOC list can be hundreds of terms — let the bar auto-collapse
    document.querySelectorAll('#searchModeToggle button').forEach((btn) => btn.setAttribute('aria-pressed', String(btn.dataset.mode === 'advanced')));
    renderAdvancedChips();
    syncSearchExpansion(true);
    updateSearchHint();
    await rebuildView({ keepScroll: false });
  };
  r.append(openBtn);
  return r;
}

export function openSearchAllModal() {
  markModalAction('openSearchAll');
  const st = searchAllState();
  st.seen = true;
  updateSearchAllButton();

  // "Search tables", not "Search all tables": the dialog decides how much
  // of the case it covers now, so a title that claims all of it would be
  // wrong two thirds of the time. Nothing identifies this dialog by its
  // title any more — the poller asks currentModalAction() instead.
  modal('Search tables', (b) => {
    b.append(el('p', 'fb-help',
      'Runs in the background — you can close this and keep working.'));

    /* Scope row: Every table / This table / Choose…, with the tables it
       covers as chips under it. Above the builder, because it changes what
       the terms below it will be asked of. */
    const scopeRow = el('div', 'search-all-scope');
    scopeRow.append(el('span', 'search-all-scope-label', 'Search'));
    const seg = el('div', 'vp-seg');   // the value picker's scope idiom
    const scopeBtns = {};
    for (const [mode, label, title] of [
      ['all', 'Every table',
       'Every open and closed table in this case. A merged table is covered through the tables its rows live in.'],
      ['current', 'This table',
       'Only the table open in the grid — all of its rows, not just the ones the current filters leave.'],
      ['pick', 'Choose…', 'Tick the tables to search below.'],
    ]) {
      const btn = el('button', 'btn ghost', label);
      btn.title = title;
      btn.onclick = () => {
        st.scope = mode === 'pick'
          ? searchAllPickScope([...(searchAllScopeSelection(st) || allPickIds())])
          : { mode, ids: [] };
        paintScope();
      };
      scopeBtns[mode] = btn;
      seg.append(btn);
    }
    scopeRow.append(seg);
    const scopeNote = el('span', 'search-all-scope-note');
    scopeRow.append(scopeNote);
    b.append(scopeRow);

    const picks = el('div', 'search-all-picks');
    b.append(picks);
    // A long case would push the builder off the bottom of the dialog, so
    // the tail is folded behind a count until it is asked for.
    let picksExpanded = false;
    const PICK_CHIPS = 10;

    const modeToggle = el('div', 'search-mode-toggle');
    const pasteBtn = el('button', 'btn ghost', 'Paste a list');
    const advBtn = el('button', 'btn ghost', 'Advanced (AND / OR / NOT)');
    modeToggle.append(pasteBtn, advBtn);
    b.append(modeToggle);

    const textarea = el('textarea', 'search-all-paste');
    textarea.rows = 8;
    textarea.spellcheck = false;
    textarea.placeholder = 'One term per line — e.g. a list of hostnames, hashes or other IOCs.\nMatches any line (OR).';
    b.append(textarea);

    const chips = el('div', 'advanced-search-bar search-all-terms');
    b.append(chips);

    const searchActs = el('div', 'row-actions');
    // A pasted IOC list usually EXISTS as a file already (an intel feed
    // export, a colleague's indicator list) — read it straight in rather
    // than round-tripping through an editor.
    const impLabel = el('label', 'btn ghost', 'Import terms from file…');
    const impInput = el('input');
    impInput.type = 'file';
    impInput.accept = '.txt,.csv,.list,.ioc,text/plain';
    impInput.hidden = true;
    impInput.onchange = async () => {
      const f = impInput.files[0];
      if (!f) return;
      let text;
      try { text = await f.text(); } catch (e) { toast('Could not read that file: ' + e.message, 6000); return; }
      impInput.value = '';   // same file again re-fires onchange
      // One term per line; blank lines and #-comments (routine in shared
      // IOC lists) are noise, not indicators.
      const incoming = text.split(/\r?\n/).map((l) => l.trim())
        .filter((l) => l && !l.startsWith('#'));
      if (!incoming.length) { toast('No terms in that file'); return; }
      const have = new Set(st.pasteText.split('\n').map((l) => l.trim()).filter(Boolean));
      const merged = [...new Set([...have, ...incoming])];   // dedupes the file against itself too
      const added = { length: merged.length - have.size };
      st.pasteText = merged.join('\n');
      st.mode = 'paste';                 // the terms land in the paste pane
      textarea.value = st.pasteText;
      syncMode();
      const dupes = incoming.length - added.length;
      toast(added.length
        ? `${added.length} term${added.length === 1 ? '' : 's'} imported${dupes > 0 ? ` · ${dupes} duplicate${dupes === 1 ? '' : 's'} skipped` : ''}`
        : 'All of those terms are already in the list');
    };
    impLabel.append(impInput);
    // The terms you're about to sweep for are usually exactly the IOCs
    // worth watching as new data lands — add them to the watchlist in one
    // click (it dedupes against what's already there, then scans for the
    // new ones in the background — runScan's jobs-panel row stands for it).
    const wlBtn = el('button', 'btn ghost', 'Add to watchlist');
    wlBtn.title = 'Add these terms to the case watchlist and scan every table for them';
    wlBtn.onclick = async () => {
      const terms = searchAllTerms(st).filter((t) => !t.exclude).map((t) => t.term.trim()).filter(Boolean);
      if (!terms.length) { toast('Enter a term or two first'); return; }
      try {
        const r = await post('/api/watchlist/import', { text: terms.join('\n'), kind: 'other' });
        if (r.added_ids && r.added_ids.length) runScan({ watchlistIds: r.added_ids });
        const dupes = terms.length - r.added;
        toast(r.added
          ? `${r.added} added to the watchlist${dupes > 0 ? ` · ${dupes} already there` : ''}`
          : 'All of those are already on the watchlist', 5000);
      } catch (e) { toast('Could not add to the watchlist: ' + e.message, 6000); }
    };
    const searchBtn = el('button', 'btn', `Search  ${MOD_ENTER}`);
    const cancelBtn = el('button', 'btn ghost', 'Stop');
    cancelBtn.title = 'Stop the sweep — tables already counted keep their results';
    cancelBtn.onclick = async () => {
      if (st.jobId == null) return;
      try { await post(`/api/search_all/cancel?job_id=${st.jobId}`, {}); } catch { /* already gone */ }
    };
    const progress = el('span', 'search-all-progress');
    searchActs.append(searchBtn, impLabel, wlBtn, cancelBtn, progress);
    b.append(searchActs);

    const results = el('div', 'search-all-results');
    b.append(results);

    textarea.value = st.pasteText;
    textarea.oninput = () => { st.pasteText = textarea.value; };

    /* Every real table the sweep can reach: chips are over REAL tables,
       never merges, because that is what the server scans — a merge in
       scope is shown as its members lit up. Open tables first: they are
       the ones the analyst is working in. */
    function pickSources() {
      return S.sources.filter((x) => !x.is_merge && !x.error)
        .slice().sort((a, x) => (x.is_open ? 1 : 0) - (a.is_open ? 1 : 0));
    }
    function allPickIds() { return pickSources().map((x) => x.id); }

    function scopeNoteText(sc, sel) {
      const n = pickSources().length;
      if (sc.mode === 'all') {
        // A pick whose tables have all left the case answers 'all' — which
        // is a wider sweep than the one that was ticked, so it is named
        // rather than left to be discovered by the clock.
        const stale = searchAllPickStale(st)
          ? ' — the tables you chose are no longer in this case' : '';
        return `${n} table${n === 1 ? '' : 's'} in this case${stale}`;
      }
      if (sc.mode === 'current') {
        const src = searchAllOpenSource();
        if (!src) return '';
        if (src.is_merge) {
          const m = (src.member_source_ids || []).length;
          return `${sourceGlyph(src)}${sourceLabel(src)} · merge of ${m} table${m === 1 ? '' : 's'}`;
        }
        return `${sourceLabel(src)} · ${(src.row_count || 0).toLocaleString()} rows`;
      }
      return `${sel ? sel.size : 0} of ${n} table${n === 1 ? '' : 's'}`;
    }

    function paintScope() {
      const sc = searchAllScope(st);
      for (const [mode, btn] of Object.entries(scopeBtns)) {
        btn.setAttribute('aria-pressed', String(sc.mode === mode));
      }
      const sel = searchAllScopeSelection(st);
      scopeNote.textContent = scopeNoteText(sc, sel);
      picks.replaceChildren();
      const all = pickSources();
      const shown = picksExpanded ? all : all.slice(0, PICK_CHIPS);
      for (const src of shown) {
        const chip = el('button', 'search-all-pick', sourceGlyph(src) + sourceLabel(src));
        chip.type = 'button';
        chip.setAttribute('aria-pressed', String(!sel || sel.has(src.id)));
        chip.title = src.is_open ? 'Open in a tab' : 'Closed — searched anyway';
        // Clicking a chip from Every table / This table means "start from
        // what is in scope now and take this one out (or put it in)",
        // which is a Choose… scope with that edit already made.
        chip.onclick = () => {
          const ids = new Set(sel || allPickIds());
          if (ids.has(src.id)) ids.delete(src.id); else ids.add(src.id);
          st.scope = searchAllPickScope([...ids]);
          paintScope();
        };
        picks.append(chip);
      }
      if (all.length > shown.length) {
        const more = el('button', 'search-all-pick', `+ ${all.length - shown.length} more`);
        more.type = 'button';
        more.onclick = () => { picksExpanded = true; paintScope(); };
        picks.append(more);
      }
    }

    /* What the results on screen are an answer ABOUT — the scope the job
       ran with, not the one the row above has since been switched to (the
       same rule st.terms follows). Without it, a rescope between a sweep
       and reading its results leaves the pane describing tables nobody
       searched. */
    function ranScopeLine() {
      const rs = st.ranScope;
      if (!rs) return null;
      const line = rs.source_ids
        ? `Searched ${searchAllRanScopeLabel(rs)}.`
        : 'Searched every table in this case.';
      const merges = (rs.merges || []).map((m) => {
        const n = (m.member_source_ids || []).length;
        return `${m.name} is a merged table: its ${n} member table${n === 1 ? '' : 's'} `
             + `${n === 1 ? 'was' : 'were'} searched, and the results name ${n === 1 ? 'it' : 'them'}.`;
      });
      return [line, ...merges].join(' ');
    }

    /* The cap, said out loud. It is the right trade at any scope — an
       exact count is a full scan of every matching row on a table whose
       trigram index is not built yet, and scoping to one table does not
       make that table smaller — but "1,000+" invites "show me", and the
       answer to that is a button already on the row. */
    function cappedNote() {
      // A table's own count and a term's own count hit the cap separately
      // — SEARCH_ALL_COUNT_CAP applies to each — so the note names
      // whichever of them actually stopped, rather than telling someone
      // whose table total is exact that it stopped at a thousand.
      let table = 0;
      let term = 0;
      for (const h of st.hits) {
        if (h.capped) table = Math.max(table, h.match_count);
        for (const t of h.terms || []) if (t.capped) term = Math.max(term, t.match_count);
      }
      if (!table && !term) return null;
      const what = table && term ? 'per table, and per term within it'
        : (table ? 'per table' : 'per term, per table');
      return `Counts stop at ${Math.max(table, term).toLocaleString()} ${what}`
           + ' — "Open ↦" for the exact number, in the grid.';
    }

    function paintResults() {
      // The poller holds this closure and fires whether or not the pane is
      // still on screen; once the modal body has been replaced these nodes
      // are detached and there's nothing to paint.
      if (!results.isConnected) return;
      // Startable while a sweep runs: scoping to one table is exactly what
      // someone does mid-sweep, and startSearchAll asks before it takes
      // the job slot rather than being blocked from asking at all.
      cancelBtn.hidden = !st.running;
      progress.textContent = st.running
        ? (st.total ? `Scanning ${st.scanned} of ${st.total} table${st.total === 1 ? '' : 's'}…` : 'Starting…')
        : '';

      results.replaceChildren();
      if (st.error) { results.append(el('div', 'note-status', 'Search failed: ' + st.error)); return; }
      const scopeLine = st.terms.length ? ranScopeLine() : null;
      if (scopeLine) results.append(el('div', 'search-all-scope-ran', scopeLine));
      if (!st.hits.length) {
        results.append(el('div', 'note-status',
          st.running ? 'No matches yet…' : (st.terms.length ? 'No matches.' : '')));
        return;
      }
      // Partial results while running are worth showing (a hit on the table
      // you care about often lands early), so this renders whatever's in
      // st.hits and just keeps the progress line alongside it.
      for (const h of st.hits) {
        results.append(searchAllHitRow(st, h));
        // One row per term that matched this table, indented under it —
        // the point of a pasted IOC list is knowing *which* indicators hit
        // where, which a single summed count per table can't tell you.
        // Absent (server sends []) for an Advanced query, where the terms
        // constrain each other and a standalone per-term count would
        // describe a query nobody ran.
        for (const t of h.terms || []) {
          results.append(searchAllHitRow(st, h, t));
        }
      }
      const cap = cappedNote();
      if (cap) results.append(el('div', 'search-all-scope-ran', cap));
    }
    searchAllRepaint = paintResults;

    function syncMode() {
      pasteBtn.setAttribute('aria-pressed', String(st.mode === 'paste'));
      advBtn.setAttribute('aria-pressed', String(st.mode === 'advanced'));
      textarea.hidden = st.mode !== 'paste';
      chips.hidden = st.mode !== 'advanced';
    }
    // Switching builder mode no longer auto-runs: with a real background job
    // that would abandon a sweep in progress just because you glanced at the
    // other tab. The Search button is the only thing that starts one.
    pasteBtn.onclick = () => { st.mode = 'paste'; syncMode(); setTimeout(() => textarea.focus(), 0); };
    advBtn.onclick = () => { st.mode = 'advanced'; syncMode(); };
    syncMode();

    searchBtn.onclick = () => startSearchAll();
    textarea.onkeydown = (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); startSearchAll(); }
    };
    renderTermChips(chips, st.chipTerms, startSearchAll, { liveInput: false });
    paintScope();
    paintResults();
    setTimeout(() => textarea.focus(), 0);
  }, { wide: true });
}

/* --------------------------------------------------------- collapsible search */

/* Collapsed by default: a bare icon button in place of the box, per the
   spec that Contains/Regex/Advanced mode buttons shouldn't be visible
   clutter when there's nothing to search for yet. "Expanded" is a UI
   state independent of content — clicking the icon or pressing / opens
   the box (and mode buttons) even before anything's typed, so the user
   can pick a mode first; it only auto-collapses again once both the box
   loses focus AND there's no content left to show. */
export function hasSearchContent() {
  if (S.searchMode === 'advanced') return S.searchTerms.some((t) => (t.term || '').trim());
  return !!S.search;
}

export function syncSearchExpansion(forceExpand = false) {
  const expanded = forceExpand || hasSearchContent();
  $('btnSearchToggle').hidden = expanded;
  $('searchModeToggle').hidden = !expanded;
  $('searchWrap').hidden = !expanded || S.searchMode === 'advanced';
  $('advancedSearchBar').hidden = !expanded || S.searchMode !== 'advanced';
}

export function expandSearch() {
  syncSearchExpansion(true);
  if (S.searchMode === 'advanced') {
    // When the term list is collapsed there's no input — focus the
    // summary chip instead (Enter/Space expands it from there).
    const i = $('advancedSearchBar').querySelector('input') || $('advancedSearchBar').querySelector('.adv-summary');
    if (i) { i.focus(); if (i.select) i.select(); }
  } else {
    $('search').focus(); $('search').select();
  }
}

export function collapseSearchIfEmpty() {
  setTimeout(() => {
    const active = document.activeElement;
    const within = active && (active.closest('.search-wrap') || active.closest('#searchModeToggle')
      || active.closest('#advancedSearchBar') || active === $('btnSearchToggle'));
    if (within || hasSearchContent()) return;
    syncSearchExpansion(false);
  }, 0);
}

/* DOM wiring for this module, called once by main.js. Handlers can't
   fire during load, so the order these run in doesn't matter — the
   startup steps that DO depend on order live in main.js instead. */
export function wireSearch() {
$('btnSearchToggle').onclick = expandSearch;

$('btnFilters').onclick = () => dropdownMenu($('btnFilters'), () => {
  const items = [
    { label: 'Filter builder…', onclick: openFilterBuilder },
    { label: 'Saved filters…', onclick: openSavedFiltersModal },
    // The WHOLE view as it stands — filters, search, timeframe; a
    // select-all's unchecked rows included — copied into a new table
    // (subset.js). The row menu's scope-worded item is where a selection
    // is subtracted; this one never is, and the title says so.
    { label: 'Save this view as a table…',
      title: 'Every row this view shows — unchecked rows included — becomes a new table in the case, badged as a subset of this one; tags and notes stay here',
      onclick: () => saveCurrentViewAsTable() },
  ];
  if (dashboardCreatorMode() && S.sourceId != null && S.sourceId >= 0) {
    items.push({ label: 'Add to dashboard: count of this view',
      title: 'A number on a dashboard — the rows this view shows now — that reopens the view when clicked',
      onclick: () => addViewCountWidget() });
  }
  // The suggestion banner's chips, relocated: saved filters matching the
  // open table's columns apply straight from here (the button's accent
  // ring is what says they exist — see updateFiltersButton).
  const src = S.sources.find((x) => x.id === S.sourceId);
  if (!src) return items;
  const { exact, similar } = matchingSavedFilters(src.columns.map((c) => c.name));
  if (!exact.length && !similar.length) return items;
  items.push('-', { header: 'For this table' });
  for (const p of exact) {
    items.push({ label: p.name, onclick: () => applyPreset(p) });
  }
  for (const p of similar) {
    const colText = (p.col_names || []).join(', ');
    items.push({ label: `${p.name} (similar)`, onclick: async () => {
      if (await confirmDialog(`"${p.name}" was built for a different column set (${colText}). Apply anyway?`)) applyPreset(p);
    } });
  }
  return items;
});

$('btnTimeRange').onclick = openTimeRangeModal;

$('btnSettings').onclick = openSettings;

$('btnSearchAll').onclick = openSearchAllModal;
}
