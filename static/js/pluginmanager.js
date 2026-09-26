/* The plugins manager — every installed plugin on the left, the whole of
   one on the right. Two panes rather than a list because a plugin has
   more to say about itself than a row can hold, and a Settings accordion
   had been answering that by saying almost nothing.

   What the old panel showed for a plugin that was switched OFF was its
   folder name, a badge and the word "off": seven rows reading `mft_usn`,
   `first_last`, `top_values`… with a four-option <select> as the widest
   thing on each. All seven ship a name, a version and a sentence of
   description — none of it reachable, because a disabled plugin is never
   imported and metadata only existed after an import. The only way to
   learn what one did was to run it, which is backwards for a decision
   about whether to run it. plugin_api.static_meta reads that metadata
   from the file instead (ast.literal_eval, nothing executed), and this is
   the surface built on the answer.

   Reuses the profile manager's .pm-* classes verbatim (bundles.js) — same
   shape, same scroll behaviour, same list/detail split. The .pl-* rules
   in style.css are only what a plugin needs that a profile doesn't.

   Scope is two controls here, not one select. The model has always been
   a machine default plus an optional per-case override, and one dropdown
   mixing them could spell every state except the useful one: drop my
   override and go back to following the machine. That's the Follow
   button, and the follow_case scope behind it. */
import { $, api, el, post, toast } from './core.js';
import { loadPlugins, openImportModal, pluginFormatById, queueFilesForFormat } from './importer.js';
import { renderSidebar } from './sources.js';
import { S } from './state.js';
import { markModalAction, modal } from './ui.js';
import { applyPluginListing, openInstallDialog } from './plugins.js';

/* A per-case scope is only offerable when a case is on screen. #home and
   #app are siblings and exactly one is visible (see CLAUDE.md), so that
   is the question — S.pluginsCaseOpen alone answers "does the server hold
   a Store", which stays true after showHome(). */
const caseScopeAvailable = () => !!S.pluginsCaseOpen && !$('app').hidden;

/* Which case "this case" is. The brand button is already the app's answer
   to that, so it is the same string. */
function thisCase() {
  const label = ($('brandLabel') && $('brandLabel').textContent || '').trim();
  return label && label !== 'Winnow' ? `“${label}”` : 'this case';
}

/* What the plugin API calls a thing, in words an analyst reads. Keyed by
   the register_* suffix, so it covers both halves: the runtime registry
   (a loaded plugin) and static_meta's `declares` (one that isn't). */
const KIND_WORDS = {
  tab: ['pinned tab', 'pinned tabs'],
  ingest_format: ['import format', 'import formats'],
  toolbar_panel: ['toolbar panel', 'toolbar panels'],
  page_panel: ['page panel', 'page panels'],
  dashboard: ['dashboard', 'dashboards'],
  row_action: ['row action', 'row actions'],
  api: ['route', 'routes'],
  table: ['case table', 'case tables'],
};

const kindWord = (kind, n) => {
  const w = KIND_WORDS[kind];
  if (!w) return `${kind.replace(/_/g, ' ')}${n === 1 ? '' : 's'}`;
  return n === 1 ? w[0] : w[1];
};

/* Reading order for a summary line: what an analyst came looking for
   first. Alphabetical put "routes" — the one kind that shows up nowhere
   in the UI — ahead of the tab and the import format, which are the two
   reasons anyone switches a plugin on. Anything unlisted sorts last. */
const KIND_ORDER = ['ingest_format', 'tab', 'toolbar_panel', 'page_panel',
                    'dashboard', 'row_action', 'table', 'api'];
const kindRank = (k) => {
  const i = KIND_ORDER.indexOf(k);
  return i === -1 ? KIND_ORDER.length : i;
};

/* Everything a LOADED plugin put in the registry, as [kind, [entries]].
   Each registered thing carries plugin_fs, so this is attribution by the
   identity that exists without importing rather than by display name —
   two plugins are free to call themselves the same thing. Ingest formats
   are the exception: they are indexed by the record's own id list, which
   is what pluginFormatById already resolves. */
function registered(p) {
  const out = [];
  const formats = (p.formats || []).map(pluginFormatById).filter(Boolean);
  if (formats.length) out.push(['ingest_format', formats.map((f) => ({ label: f.label, note: fmtMatches(f), format: f }))]);
  const mine = (list) => (list || []).filter((x) => x.plugin_fs === p.fs_name);
  for (const [kind, list] of [
    ['tab', mine(S.pluginTabs)],
    ['toolbar_panel', mine(S.pluginPanels)],
    ['page_panel', mine(S.pluginPagePanels)],
    ['dashboard', mine(S.pluginDashboards)],
    ['row_action', mine(S.pluginRowActions)],
  ]) {
    if (list.length) out.push([kind, list.map((x) => ({ label: x.label || x.local_id || x.id, note: x.description || '' }))]);
  }
  return out.sort(([a], [c]) => kindRank(a) - kindRank(c));
}

const fmtMatches = (f) => (f.extensions || []).concat(f.filename_patterns || []).join(', ')
  || 'no automatic matching';

/* The one line under a plugin's name in the list. A loaded plugin is
   described by what it actually registered; one that was never imported
   by what its source names — kinds only, never counts, because a count
   read out of source is a guess the moment a register call sits inside a
   loop. Both say the same kind of thing, and neither pretends to be the
   other. */
function summaryOf(p) {
  // The on/off tag beside the name already says which it is; a summary
  // that opened with "off ·" made this list say it twice, which is one
  // of the things the panel this replaces was doing.
  if (p.error) return 'failed to load';
  if (p.enabled) {
    const parts = registered(p).map(([kind, items]) => `${items.length} ${kindWord(kind, items.length)}`);
    return parts.join(' · ') || 'loaded — registers nothing';
  }
  const kinds = [...(p.declares || [])].sort((a, c) => kindRank(a) - kindRank(c));
  if (!kinds.length) return 'its source names nothing it would add';
  return 'adds ' + kinds.map((k) => kindWord(k, 2)).join(', ');
}

export function openPluginManager(opts = {}) {
  markModalAction('openPluginManager');
  // Not async: nothing here awaits. The listing is already in S (boot
  // loads it), so the panes paint at once and the two fetches below
  // repaint when they land — a plugins dialog that opened empty and
  // filled in a moment later would be the worse trade.
  modal('Plugins', (b) => {
    b.append(el('p', 'pm-desc',
      'Drop-in Python extensions, read from the folders named under the list. A plugin runs with '
      + 'the same privileges as Winnow itself, so only install ones you trust — a plugin that is '
      + 'off is never imported, and its code never runs. Changes take effect immediately, no restart.'));

    const wrap = el('div', 'pm pm-plugins');
    const listPane = el('div', 'pm-list');
    const detailPane = el('div', 'pm-detail');
    wrap.append(listPane, detailPane);
    b.append(wrap);

    let selected = opts.select || null;
    let profiles = [];

    // Profiles name the plugins they turn on, which is the other half of
    // "why is this on?" — and the answer a scope control can't give. Best
    // effort: a workspace file that won't read must not empty the pane.
    api('/api/plugin_bundles').then((r) => { profiles = r || []; renderDetail(); }).catch(() => {});

    function plugins() { return S.plugins || []; }

    function current() {
      const list = plugins();
      return list.find((p) => p.fs_name === selected) || list[0] || null;
    }

    async function setScope(p, scope) {
      try {
        applyPluginListing(await post('/api/plugins/toggle', { fs_name: p.fs_name, scope }));
      } catch (e) {
        toast('Could not change the plugin: ' + e.message, 6000);
      }
      renderAll();
    }

    function renderList() {
      listPane.replaceChildren();
      const list = plugins();
      if (!list.length) {
        listPane.append(el('div', 'note-status',
          'No plugins found in the folders below. Installing one copies it into the first of them.'));
      }
      const cur = current();
      for (const p of list) {
        const item = el('div', 'pm-item');
        item.setAttribute('aria-current', cur && p.fs_name === cur.fs_name ? 'true' : 'false');
        const name = el('div', 'pm-item-name');
        name.append(el('span', 'pm-item-label', p.name || p.fs_name));
        const state = el('span', 'pm-tag' + (p.enabled ? ' applied' : ''), p.enabled ? 'on' : 'off');
        name.append(state);
        if (p.error) name.append(el('span', 'pm-tag pl-tag-bad', 'error'));
        item.append(name, el('div', 'pm-item-sum', summaryOf(p)));
        item.onclick = () => { selected = p.fs_name; renderAll(); };
        listPane.append(item);
      }
      const acts = el('div', 'pm-list-acts');
      const add = el('button', 'btn ghost', 'Install a plugin…');
      add.title = 'Copy a .py file or a plugin folder into the plugins directory';
      // The install dialog REPLACES this modal (one #modalBody), so the
      // panes this closure holds are detached by the time it finishes —
      // repainting them would paint into nothing. Reopen instead, on the
      // plugin that just arrived.
      add.onclick = () => openInstallDialog((fs) => openPluginManager({ select: fs }));
      acts.append(add);
      listPane.append(acts);
      // The directories, at the bottom of the pane the install button is
      // in — they are the answer to "where does that copy land", and
      // belong beside the question rather than above the whole list.
      const dirs = el('div', 'pl-dirs');
      dirs.append(el('div', 'pl-dirs-head', 'Read from'));
      for (const d of S.pluginDirs || []) {
        const line = el('div', 'pl-dir', d);
        line.title = d;   // the pane is 260px; the path is usually longer
        dirs.append(line);
      }
      listPane.append(dirs);
    }

    function scopeRow(label, options, title) {
      const row = el('div', 'pl-scope-row');
      const l = el('span', 'pl-scope-label', label);
      if (title) l.title = title;
      const group = el('div', 'pl-scope');
      for (const o of options) {
        const btn = el('button', null, o.label);
        btn.setAttribute('aria-pressed', String(!!o.on));
        btn.title = o.title || '';
        btn.disabled = !!o.disabled;
        if (o.onclick) btn.onclick = o.onclick;
        group.append(btn);
      }
      row.append(l, group);
      return row;
    }

    function renderScope(p) {
      const box = el('div', 'pl-scopes');
      box.append(scopeRow('Everywhere', [
        { label: 'Off', on: !p.machine_enabled,
          title: 'Off by default in every case on this machine',
          onclick: () => setScope(p, 'off_all') },
        { label: 'On', on: !!p.machine_enabled,
          title: 'On by default in every case on this machine',
          onclick: () => setScope(p, 'on_all') },
      ], 'The machine-wide default. Picking either also drops this case’s override.'));

      if (caseScopeAvailable()) {
        const ov = p.case_override;
        box.append(scopeRow(`In ${thisCase()}`, [
          { label: 'Follow', on: ov == null,
            title: 'No override — this case follows the everywhere setting',
            onclick: () => setScope(p, 'follow_case') },
          { label: 'On', on: ov === true,
            title: 'On for this case, whatever the everywhere setting is',
            onclick: () => setScope(p, 'on_case') },
          { label: 'Off', on: ov === false,
            title: 'Off for this case, whatever the everywhere setting is',
            onclick: () => setScope(p, 'off_case') },
        ], 'Lives in the case file, so it travels with the case.'));
      } else if (p.case_override != null) {
        // A case the server still holds can carry an override while its
        // scopes are not on offer here. Saying so beats a control that
        // silently describes only half of what decided the state.
        box.append(el('div', 'pl-scope-note',
          `A case that is open but not on screen has this ${p.case_override ? 'on' : 'off'} — `
          + 'open that case to change it.'));
      }
      return box;
    }

    function section(title, count) {
      const sec = el('div', 'pm-sec');
      const head = el('div', 'pm-sec-head', title);
      if (count) head.append(el('span', 'pm-sec-count', count));
      const body = el('div', 'pm-sec-body');
      sec.append(head, body);
      detailPane.append(sec);
      return body;
    }

    /* "What it adds" for a plugin that is RUNNING: the registry's own
       answer, one row per thing, with the ingest formats carrying their
       file picker — the one file-picking path that can reach a target a
       format matches by bare-name pattern ("$MFT" has no extension for an
       accept attribute to allow). */
    function renderRegistered(p) {
      const entries = registered(p);
      const total = entries.reduce((n, [, items]) => n + items.length, 0);
      const body = section('What it adds', total ? `${total} thing${total === 1 ? '' : 's'}` : '');
      if (!entries.length) {
        body.append(el('div', 'pm-none', 'Loaded, and it registers nothing the UI shows.'));
        return;
      }
      for (const [kind, items] of entries) {
        for (const it of items) {
          const row = el('div', 'pl-reg');
          const left = el('div', 'pl-reg-main');
          left.append(el('div', 'pl-reg-label', it.label));
          if (it.note) left.append(el('div', 'pl-reg-note', it.note));
          row.append(left);
          if (it.format) {
            const pick = el('label', 'btn ghost pm-mini', 'Import files…');
            const inp = el('input');
            inp.type = 'file';
            inp.multiple = true;
            inp.hidden = true;   // no accept attribute on purpose — see above
            inp.onchange = () => {
              if (!inp.files.length) return;
              queueFilesForFormat(it.format, [...inp.files]);
              $('modal').hidden = true;
              openImportModal();
            };
            pick.append(inp);
            row.append(pick);
          }
          row.append(el('div', 'pl-reg-kind', kindWord(kind, 1)));
          body.append(row);
        }
      }
    }

    /* And for one that is NOT running: what its source names, which is a
       list of what it can add and never a count of what it did. Said in
       those words, because the difference is the whole reason one of
       these panes can be trusted and the other has to be hedged. */
    function renderDeclared(p) {
      const kinds = p.declares || [];
      const body = section('What it would add', kinds.length ? 'from its source' : '');
      if (!kinds.length) {
        body.append(el('div', 'pm-none',
          'Its source names no register_… calls. Switch it on to see what it registers.'));
        return;
      }
      body.append(el('div', 'pl-declared-lead',
        'It is not running, so this is read from the file rather than from the registry — '
        + 'what it can add, not how many:'));
      const pills = el('div');
      for (const k of [...kinds].sort((a, c) => kindRank(a) - kindRank(c))) {
        pills.append(el('span', 'pm-pill', kindWord(k, 2)));
      }
      body.append(pills);
    }

    function renderProvenance(p) {
      const body = section('Where it came from');
      const kv = el('dl', 'pl-kv');
      const pair = (k, v, cls) => {
        kv.append(el('dt', null, k), el('dd', cls || null, v));
      };
      pair('Folder', p.path || '—');
      pair('Entry point', p.entry || '—');
      const wants = p.api_wants;
      pair('Plugin API', wants == null
        ? `does not say — this build provides v${S.pluginApiVersion || '?'}`
        : `wants v${wants} · this build provides v${S.pluginApiVersion || '?'}`,
        wants != null && S.pluginApiVersion && wants > S.pluginApiVersion ? 'pl-bad' : null);
      if (p.readme) pair('README', p.readme);
      pair('Status', p.error ? 'failed to load' : p.enabled ? 'loaded — no errors' : 'not imported',
        p.error ? 'pl-bad' : null);
      body.append(kv);
    }

    function renderUsedBy(p) {
      const using = profiles.filter((b) => (b.plugins || []).includes(p.fs_name));
      if (!using.length) return;
      const body = section('Used by', `${using.length} profile${using.length === 1 ? '' : 's'}`);
      body.append(el('span', 'pl-usedby-lead', 'Applying these turns it on: '));
      for (const b of using) body.append(el('span', 'pm-pill', b.name));
    }

    function renderDetail() {
      detailPane.replaceChildren();
      const p = current();
      if (!p) return;
      const head = el('div', 'pm-head');
      const h = el('h3', null, p.name || p.fs_name);
      head.append(h);
      if (p.version) head.append(el('span', 'pm-tag', `v${p.version}`));
      if (p.bundled) head.append(el('span', 'pm-tag', 'example'));
      // The folder name, whenever it is not already the title. It is the
      // identity everything else keys on — profiles, the toggle route,
      // the case's overrides — so it is worth having on screen.
      if ((p.name || p.fs_name) !== p.fs_name) head.append(el('span', 'pl-fs', p.fs_name));
      const acts = el('div', 'pm-head-acts');
      acts.append(renderScope(p));
      head.append(acts);
      detailPane.append(head);

      detailPane.append(el('p', 'pm-desc', p.description
        || 'Its source declares no description — a plugin sets one in its PLUGIN = {…} block.'));

      if (p.error) {
        const err = el('div', 'pl-error');
        err.append(el('div', 'pl-error-head', 'This plugin did not load'), el('div', null, p.error));
        detailPane.append(err);
      }

      if (p.enabled && !p.error) renderRegistered(p);
      else renderDeclared(p);
      renderProvenance(p);
      renderUsedBy(p);

      const foot = el('div', 'pm-foot');
      foot.append(el('span', 'pm-foot-note', effectiveSentence(p)));
      detailPane.append(foot);
    }

    function renderAll() { renderList(); renderDetail(); }

    renderAll();
    // Refresh from the server in the background — cheap, and catches a
    // plugin someone dropped into the folder by hand since boot.
    loadPlugins().then(() => { renderSidebar(); renderAll(); }).catch(() => {});
  }, { wide: 'x' });
}

/* One sentence for the state the two scope controls add up to. The
   controls say what is set; this says what it means, which for an
   override is the part that is easy to read the wrong way round. */
function effectiveSentence(p) {
  const where = caseScopeAvailable() ? thisCase() : 'the open case';
  if (p.case_override === true) {
    return `On for ${where} only — every other case follows the everywhere setting `
      + `(${p.machine_enabled ? 'On' : 'Off'}).`;
  }
  if (p.case_override === false) {
    return `Off for ${where} only — every other case follows the everywhere setting `
      + `(${p.machine_enabled ? 'On' : 'Off'}).`;
  }
  return p.machine_enabled
    ? 'On in every case on this machine.'
    : 'Off in every case on this machine.';
}

/* Settings → Plugins is now one line and a button. The panel it replaces
   had grown into a list of seven near-identical rows — a four-option
   <select>, a folder name, an identical badge and a word repeating what
   the select just said — inside an accordion inside a shared dialog, and
   a plugin that was switched off showed nothing else at all. That is more
   structure than a settings section can hold, so it lives in its own
   two-pane manager (pluginmanager.js) and Settings keeps the summary. */
export function buildPluginsPanel(b) {
  const box = el('div', 'settings-line');
  b.append(box);

  function paint() {
    box.replaceChildren();
    const list = S.plugins || [];
    const on = list.filter((p) => p.enabled).length;
    const bad = list.filter((p) => p.error).length;
    const summary = !list.length
      ? 'No plugins installed'
      : `${on} on of ${list.length} installed`;
    const line = el('span', 'settings-line-text', summary);
    if (bad) line.append(el('span', 'settings-line-bad', ` · ${bad} failed to load`));
    const open = el('button', 'btn', 'Manage plugins…');
    open.onclick = () => openPluginManager();
    box.append(line, open);
    box.append(el('p', 'fb-help',
      'Drop-in Python extensions — a plugin runs with the same privileges as Winnow itself. '
      + 'The manager lists what each one adds and what it can reach before you switch it on.'));
  }

  paint();
  // Refresh from the server in the background — cheap, and catches a
  // plugin someone dropped into the folder by hand since boot.
  loadPlugins().then(paint).catch(() => {});
}
