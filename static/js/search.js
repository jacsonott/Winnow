/* The toolbar search box and the search-all-tables job.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, api, el, post, toast } from './core.js';
import { openFilterBuilder } from './filterbuilder.js';
import { renderAdvancedChips, renderTermChips, updateSearchHint } from './filters.js';
import { openSettings } from './settings.js';
import { loadSources, sourceLabel } from './sources.js';
import { S } from './state.js';
import { openSavedFiltersModal, openTimeRangeModal } from './timeframe.js';
import { dropdownMenu, modal } from './ui.js';
import { rebuildView } from './view.js';

/* Checked against every real table in the case (plain contains-mode only —
   same as the grid's default search — not regex). Clicking a result opens
   that table with the same terms already applied via Advanced search,
   rather than inventing a separate cross-table results view.

   Two ways to build the term list, sharing one results pane:
   "Paste a list" (default) — a multi-line textarea, one term per line,
   OR'd together — the list-of-IOCs/hostnames/hashes use case, and lets you
   see more than the first line unlike a single-line input. "Advanced" is
   the original AND/OR/NOT chip builder, for anything needing mixed
   connectors or an exclusion — still available, just not the default.

   Explicit "Search" button rather than live-as-you-type: this hits every
   real table in the case (COUNT(*) per table, potentially a background FTS
   build kicked off per table too — see search_all_sources), not the cheap
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

export let searchAllPollTimer = null;

/* Polls the job to completion regardless of whether the modal is open —
   that's the whole point. Repaints the modal's results pane only if it
   happens to be showing (searchAllRepaint is a no-op otherwise). */
export function pollSearchAll() {
  clearTimeout(searchAllPollTimer);
  searchAllPollTimer = setTimeout(async () => {
    const st = S.searchAll;
    if (!st || !st.running || st.jobId == null) return;
    let job;
    try {
      job = await api(`/api/search_all/job?job_id=${st.jobId}`);
    } catch (e) {
      // There is only ever one poll chain (the clearTimeout above cancels
      // any previous one), so a 404 here is never a stale poller being
      // superseded — it means the job genuinely no longer exists: the
      // server restarted, or the case was closed/switched underneath us.
      // This has to clear `running`, otherwise the badge sticks at
      // "Search all… n/m" forever with nothing left to advance it.
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
    if (job.done || job.cancelled) {
      st.running = false;
      const open = !$('modal').hidden && $('modalTitle').textContent === 'Search all tables';
      st.seen = open;
      if (!open && !job.cancelled) {
        toast(job.hits.length
          ? `Search all finished — ${job.hits.length} table${job.hits.length === 1 ? '' : 's'} matched. Reopen "Search all" to see them.`
          : 'Search all finished — no matches.', 6000);
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
  if (st && st.running) {
    const pct = st.total ? ` ${st.scanned}/${st.total}` : '';
    btn.textContent = `Search all…${pct}`;
    btn.setAttribute('aria-busy', 'true');
    btn.title = 'Search running in the background — click to watch or refine it';
  } else if (st && !st.seen && st.hits.length) {
    btn.textContent = `Search all (${st.hits.length})`;
    btn.removeAttribute('aria-busy');
    btn.title = `${st.hits.length} table(s) matched — click to see them`;
  } else {
    btn.textContent = 'Search all';
    btn.removeAttribute('aria-busy');
    btn.title = 'Search every table in this case';
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
    st.hits = []; st.terms = []; st.error = null; st.jobId = null; st.running = false;
    updateSearchAllButton();
    searchAllRepaint();
    return;
  }
  st.terms = terms.map((t) => ({ ...t }));
  st.hits = [];
  st.error = null;
  st.scanned = 0;
  st.total = 0;
  st.seen = true;
  try {
    const job = await post('/api/search_all/start', { terms });
    st.jobId = job.job_id;
    st.running = true;
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
  const st = searchAllState();
  st.seen = true;
  updateSearchAllButton();

  modal('Search all tables', (b) => {
    b.append(el('p', 'fb-help',
      'Matches every open and closed table in this case. Runs in the background — you can close this and keep working.'));

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
    const searchBtn = el('button', 'btn', 'Search  ⌘⏎');
    const cancelBtn = el('button', 'btn ghost', 'Stop');
    cancelBtn.title = 'Stop the sweep — tables already counted keep their results';
    cancelBtn.onclick = async () => {
      if (st.jobId == null) return;
      try { await post(`/api/search_all/cancel?job_id=${st.jobId}`, {}); } catch { /* already gone */ }
    };
    const progress = el('span', 'search-all-progress');
    searchActs.append(searchBtn, cancelBtn, progress);
    b.append(searchActs);

    const results = el('div', 'search-all-results');
    b.append(results);

    textarea.value = st.pasteText;
    textarea.oninput = () => { st.pasteText = textarea.value; };

    function paintResults() {
      // The poller holds this closure and fires whether or not the pane is
      // still on screen; once the modal body has been replaced these nodes
      // are detached and there's nothing to paint.
      if (!results.isConnected) return;
      searchBtn.disabled = st.running;
      cancelBtn.hidden = !st.running;
      progress.textContent = st.running
        ? (st.total ? `Scanning ${st.scanned} of ${st.total} tables…` : 'Starting…')
        : '';

      results.replaceChildren();
      if (st.error) { results.append(el('div', 'note-status', 'Search failed: ' + st.error)); return; }
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

    searchBtn.onclick = startSearchAll;
    textarea.onkeydown = (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); startSearchAll(); }
    };
    renderTermChips(chips, st.chipTerms, startSearchAll, { liveInput: false });
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

$('btnFilters').onclick = () => dropdownMenu($('btnFilters'), [
  { label: 'Filter builder…', onclick: openFilterBuilder },
  { label: 'Saved filters…', onclick: openSavedFiltersModal },
]);

$('btnTimeRange').onclick = openTimeRangeModal;

$('btnSettings').onclick = openSettings;

$('btnSearchAll').onclick = openSearchAllModal;
}
