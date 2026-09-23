/* Profiles — "how I analyze this kind of case": the plugins a case of this
   kind needs, the boards it opens with, a starter watchlist, and the
   variables the case must carry. Managed from the M menu here, and picked
   by name from the new-case dialog's Profile select (home.js).

   Two things this module is deliberate about, both because applying a
   profile CHANGES THE OPEN CASE and used to say nothing first:

   - Nothing is applied until the sheet has said what it will do.
     Applying sets an explicit on/off override for EVERY installed plugin
     (so anything not in the profile is turned off), replaces boards BY
     NAME, seeds indicators and starts a scan over the case's tables.
     openProfileApplySheet reads those numbers out of the open case
     (GET .../plan) and offers each part as its own checkbox, so a board
     can be taken without the watchlist.
   - A shipped profile is read-only and a copy of one is never rewritten.
     A copy records which shipped profile it came from and at what
     version; when the shipped one moves on, the manager offers the diff
     and a button. Taking an update is always the button. */
import { $, api, el, post, toast } from './core.js';
import { loadPlugins } from './importer.js';
import { loadSources, renderPageTabs } from './sources.js';
import { openProfileBuilder } from './profilebuilder.js';
import { promptForVariables } from './settings.js';
import { confirmDialog, markModalAction, modal, promptDialog } from './ui.js';

export async function listBundles() {
  return api('/api/plugin_bundles');
}

/* Apply a profile to the open case. `parts` is the subset of
   plugins/boards/watchlist/variables the analyst agreed to — omitted
   means all four, which is what the new-case dialog's Profile select
   sends and what apply meant before the sheet existed. */
export async function applyBundle(bundle, parts = null) {
  const res = await post(`/api/plugin_bundles/${bundle.id}/apply`, parts ? { parts } : {});
  await loadPlugins();
  renderPageTabs();
  // A profile can add a named dashboard and seed the watchlist — reload the
  // case state so the sidebar's Dashboards section and the watchlist reflect
  // it without a manual refresh.
  await loadSources();
  const done = [];
  if ((res.parts || []).includes('plugins')) {
    done.push(`${res.enabled.length} plugin${res.enabled.length === 1 ? '' : 's'} on`);
  }
  const boards = (res.dashboard_applied ? 1 : 0) + (res.dashboards_applied || []).length;
  if (boards) done.push(`${boards} board${boards === 1 ? '' : 's'}`);
  if (res.watchlist_seeded) done.push(`${res.watchlist_seeded} indicator${res.watchlist_seeded === 1 ? '' : 's'}`);
  const missing = res.missing || [];
  toast(`Applied “${res.applied}”${done.length ? ' — ' + done.join(', ') : ''}`
    + (missing.length && (res.parts || []).includes('plugins')
      ? ` (${missing.length} in the profile not installed here: ${missing.join(', ')})` : ''), 6000);
  // Required variables the case doesn't have yet: ask now, in one dialog.
  const need = res.variables_missing || [];
  if (need.length) {
    const defs = (bundle.variables || (await listBundles()).find((b) => b.id === bundle.id)?.variables || [])
      .filter((d) => need.includes(d.name));
    if (defs.length) await promptForVariables(defs, { title: `“${res.applied}” needs a few values` });
  }
  return res;
}

/* ------------------------------------------------------------ describing */

function boardsOf(bd) {
  /* Every board a profile carries, in the order apply lands them: its own
     board (which takes the profile's name) first, then the extra named
     ones. One shape, so the manager and the summary count the same
     things. */
  const out = [];
  if ((bd.dashboard || []).length) out.push({ name: bd.name, widgets: bd.dashboard });
  for (const d of bd.dashboards || []) {
    if ((d.widgets || []).length) out.push({ name: d.name, widgets: d.widgets });
  }
  return out;
}

function widgetKind(w) {
  /* "kv · span 2", or "watchlist · stat" — what the card IS, in the
     vocabulary the widget editor uses. A non-SQL source leads, because
     "this one counts the watchlist" is the more surprising fact. */
  const bits = [];
  if (w.source && w.source !== 'sql') bits.push(w.source);
  bits.push(w.render || 'stat');
  const span = Number(w.span) || 1;
  if (span > 1) bits.push(`span ${span}`);
  return bits.join(' · ');
}

function countWidgets(bd) {
  return boardsOf(bd).reduce((n, b) => n + b.widgets.length, 0);
}

function summaryOf(bd) {
  /* The one line under a profile's name in the list. Counts, not names:
     the names are one click away in the pane beside it, and the list's
     job is to let an analyst tell five profiles apart at a glance. */
  const bits = [];
  const p = (bd.plugins || []).length;
  bits.push(`${p} plugin${p === 1 ? '' : 's'}`);
  const w = countWidgets(bd);
  if (w) bits.push(`${w} widget${w === 1 ? '' : 's'}`);
  const i = (bd.watchlist || []).length;
  if (i) bits.push(`${i} IOC${i === 1 ? '' : 's'}`);
  // Deliberately not the variable count as well: the line has to stay on
  // one line at 230px, and the variables are three inches to the right.
  return bits.join(' · ');
}

function section(parent, title, count, key) {
  const sec = el('div', 'pm-sec');
  // Which PART of a profile this is, independent of its heading — a board
  // section is headed with the board's name, and "Watchlist" is also the
  // start of a widget title on the shipped board.
  if (key) sec.dataset.sec = key;
  const head = el('div', 'pm-sec-head');
  head.append(el('span', null, title));
  if (count) head.append(el('span', 'pm-sec-count', count));
  const body = el('div', 'pm-sec-body');
  sec.append(head, body);
  parent.append(sec);
  return body;
}

function pill(text, title) {
  const n = el('span', 'pm-pill', text);
  if (title) n.title = title;
  return n;
}

/* ---------------------------------------------------------- the manager */

/* The two-pane profile manager: profiles on the left with a one-line
   summary, the WHOLE of the selected profile on the right — every plugin,
   every widget with its render kind and whether it re-runs on every open,
   the watchlist, the variables. The list it replaced showed one truncated
   row per profile, which pushed its own Apply button off the end. */
export function openPluginBundlesModal(opts = {}) {
  markModalAction('openPluginBundles');
  modal('Profiles', async (b) => {
    b.append(el('p', 'fb-help',
      'A profile is a kind of case: the plugins it needs, the boards it opens with, a starter '
      + 'watchlist and the values the case must carry. Applying one changes THIS case, and says '
      + 'what it will change before it does. Shipped profiles are read-only — “Copy to edit” makes '
      + 'one of your own.'));

    const wrap = el('div', 'pm');
    const listPane = el('div', 'pm-list');
    const detailPane = el('div', 'pm-detail');
    wrap.append(listPane, detailPane);
    b.append(wrap);

    let bundles = [];
    let settings = {};
    try {
      [bundles, settings] = await Promise.all([
        listBundles(),
        api('/api/case_settings').catch(() => ({})),
      ]);
    } catch (e) {
      listPane.append(el('div', 'note-status', 'Could not load profiles: ' + e.message));
      return;
    }
    let appliedAt = {};
    try { appliedAt = JSON.parse(settings.profiles_applied || '{}') || {}; } catch { appliedAt = {}; }
    const caseOpen = !$('app').hidden;
    const want = opts.select != null ? String(opts.select).toLowerCase() : null;
    let selected = (bundles.find((x) => x.name.toLowerCase() === want) || bundles[0] || {}).id;

    function reopen(select) {
      openPluginBundlesModal({ select: select != null ? select : selectedName() });
    }

    function selectedName() {
      return (bundles.find((x) => x.id === selected) || {}).name;
    }

    function renderList() {
      listPane.replaceChildren();
      if (!bundles.length) {
        listPane.append(el('div', 'note-status', 'No profiles yet.'));
      }
      for (const bd of bundles) {
        const item = el('div', 'pm-item');
        item.setAttribute('aria-current', bd.id === selected ? 'true' : 'false');
        const name = el('div', 'pm-item-name');
        name.append(el('span', 'pm-item-label', bd.name));
        if (bd.shipped) name.append(el('span', 'pm-tag', 'shipped'));
        if (appliedAt[bd.name]) name.append(el('span', 'pm-tag applied', 'applied'));
        if (bd.update_available) name.append(el('span', 'pm-tag update', 'update'));
        item.append(name, el('div', 'pm-item-sum', summaryOf(bd)));
        item.onclick = () => { selected = bd.id; renderList(); renderDetail(); };
        listPane.append(item);
      }
      const acts = el('div', 'pm-list-acts');
      const add = el('button', 'btn ghost', '＋ New profile…');
      add.title = 'Pick plugins and boards, and declare the variables a case of this type needs';
      add.onclick = () => openProfileBuilder(null, { onSaved: (rec) => reopen(rec.name) });
      const imp = el('label', 'btn ghost pm-mini', 'Import…');
      imp.title = 'Read a profile exported from another machine';
      const impInput = el('input');
      impInput.type = 'file';
      impInput.accept = '.json';
      impInput.hidden = true;
      impInput.onchange = async () => {
        if (!impInput.files[0]) return;
        const fd = new FormData();
        fd.append('file', impInput.files[0]);
        try {
          const rec = await api('/api/plugin_bundles/import', { method: 'POST', body: fd });
          toast(`Imported “${rec.name}”`, 5000);
          reopen(rec.name);
        } catch (e) {
          toast('Could not import: ' + e.message, 7000);
        }
      };
      imp.append(impInput);
      const snap = el('button', 'btn ghost pm-mini', 'Save current plugins…');
      snap.title = 'Snapshot the plugins currently enabled (for this case, if one is open) under a name';
      snap.onclick = async () => {
        const name = await promptDialog('Profile name (e.g. Triage, BEC):');
        if (!name || !name.trim()) return;
        let current;
        try {
          current = await api('/api/plugins');
        } catch (e) {
          toast('Could not read the plugin list: ' + e.message, 6000);
          return;
        }
        const enabled = (current.plugins || []).filter((p) => p.enabled).map((p) => p.fs_name);
        try {
          const rec = await post('/api/plugin_bundles', { name: name.trim(), plugins: enabled });
          reopen(rec.name);
        } catch (e) {
          toast('Could not save: ' + e.message, 6000);
        }
      };
      acts.append(add, imp, snap);
      listPane.append(acts);
    }

    function renderDetail() {
      detailPane.replaceChildren();
      const bd = bundles.find((x) => x.id === selected);
      if (!bd) {
        detailPane.append(el('div', 'note-status', 'Pick a profile on the left.'));
        return;
      }

      const head = el('div', 'pm-head');
      head.append(el('h3', null, bd.name));
      head.append(el('span', 'pm-tag', bd.shipped ? 'shipped · read-only' : 'mine'));
      const headActs = el('span', 'pm-head-acts');
      if (bd.shipped) {
        const copy = el('button', 'btn ghost', 'Copy to edit');
        copy.title = 'Start an editable profile from this one — it remembers where it came from';
        copy.onclick = () => openProfileBuilder(bd, { onSaved: (rec) => reopen(rec.name) });
        headActs.append(copy);
      } else {
        const edit = el('button', 'btn ghost', 'Edit…');
        edit.title = 'Edit this profile — plugins, boards and variables';
        edit.onclick = () => openProfileBuilder(bd, { onSaved: (rec) => reopen(rec.name) });
        headActs.append(edit);
      }
      const exp = el('button', 'btn ghost', 'Export…');
      exp.title = 'Save this profile as one JSON file, to import on another machine';
      exp.onclick = () => { window.location = `/api/plugin_bundles/${bd.id}/export`; };
      headActs.append(exp);
      if (!bd.shipped) {
        const del = el('button', 'btn ghost pm-del', '✕');
        del.title = 'Delete this profile (cases it was applied to keep their plugins)';
        del.onclick = async () => {
          if (!(await confirmDialog(`Delete profile “${bd.name}”?`, { danger: true, okLabel: 'Delete' }))) return;
          await api(`/api/plugin_bundles/${bd.id}`, { method: 'DELETE' });
          bundles = bundles.filter((x) => x.id !== bd.id);
          selected = (bundles[0] || {}).id;
          renderList();
          renderDetail();
        };
        headActs.append(del);
      }
      head.append(headActs);
      detailPane.append(head);

      if (bd.description) detailPane.append(el('p', 'pm-desc', bd.description));

      // Lineage: one line, and a button. Never a rewrite.
      if (bd.update_available) {
        const line = el('div', 'pm-update');
        line.append(el('span', null,
          `“${bd.update_available.name}” has changed since you copied it `
          + `(you took version ${bd.update_available.taken_at}, it is now ${bd.update_available.now}).`));
        const see = el('button', 'btn ghost pm-mini', 'See what changed');
        see.onclick = () => openProfileDiff(bd);
        line.append(see);
        detailPane.append(line);
      } else if (bd.from_profile) {
        detailPane.append(el('p', 'pm-lineage',
          `Copied from the shipped “${bd.from_profile}”`
          + (bd.from_version ? ` (version ${bd.from_version})` : '') + '.'));
      }

      const plugins = bd.plugins || [];
      const pBody = section(detailPane, 'Plugins',
        plugins.length ? `${plugins.length} on · all else off` : 'all off', 'plugins');
      if (!plugins.length) {
        pBody.append(el('span', 'pm-none', 'None — applying turns every plugin off for this case.'));
      }
      for (const fs of plugins) pBody.append(pill(fs));

      for (const board of boardsOf(bd)) {
        const live = board.widgets.filter((w) => w && w.live).length;
        const body = section(detailPane, `Board — ${board.name}`,
          `${board.widgets.length} widget${board.widgets.length === 1 ? '' : 's'}`
          + (live ? ` · ${live} live` : ''), 'board');
        const grid = el('div', 'pm-widgets');
        for (const w of board.widgets) {
          const row = el('div', 'pm-widget');
          row.append(el('span', 'pm-w-title', w.title || '(untitled)'));
          row.append(el('span', 'pm-w-kind', widgetKind(w)));
          row.append(el('span', w.live ? 'pm-w-run live' : 'pm-w-run', w.live ? 'every open' : 'cached'));
          grid.append(row);
        }
        body.append(grid);
      }
      if (!boardsOf(bd).length) {
        section(detailPane, 'Boards', 'none', 'board').append(
          el('span', 'pm-none', 'This profile creates no dashboards.'));
      }

      const wl = bd.watchlist || [];
      const wBody = section(detailPane, 'Watchlist',
        wl.length ? `${wl.length} indicator${wl.length === 1 ? '' : 's'}` : 'none', 'watchlist');
      if (!wl.length) wBody.append(el('span', 'pm-none', 'No starter indicators.'));
      for (const i of wl) wBody.append(pill(i.value || '', i.note || i.kind || ''));

      const vars = bd.variables || [];
      const req = vars.filter((v) => v.required).length;
      const vBody = section(detailPane, 'Variables',
        vars.length ? `${vars.length}${req ? ` · ${req} required` : ''}` : 'none', 'variables');
      if (!vars.length) vBody.append(el('span', 'pm-none', 'No values are asked for.'));
      for (const v of vars) {
        vBody.append(pill((v.label || v.name) + (v.required ? ' *' : ''), v.description || v.name));
      }

      const foot = el('div', 'pm-foot');
      const apply = el('button', 'btn', 'Apply to this case…');
      apply.disabled = !caseOpen;
      apply.title = caseOpen
        ? 'Shows exactly what will change in this case before anything happens'
        : 'Open a case first — a profile applies per case';
      apply.onclick = () => openProfileApplySheet(bd, { returnTo: () => reopen(bd.name) });
      foot.append(apply);
      foot.append(el('span', 'pm-foot-note', caseOpen
        ? 'Shows what will change before anything happens'
        : 'Open a case to apply a profile'));
      if (appliedAt[bd.name]) {
        foot.append(el('span', 'pm-foot-when', `Applied ${appliedAt[bd.name].replace('T', ' ')}`));
      }
      detailPane.append(foot);
    }

    renderList();
    renderDetail();
  }, { wide: 'x', tall: true });
}

/* ----------------------------------------------------------- the lineage */

/* What the shipped profile has that this copy does not — and the one
   button that takes it. The diff is the copy against the shipped profile
   as it stands NOW, which is the question being decided; it lists the
   analyst's own edits on the other side of each line so taking the update
   is not a surprise. */
export function openProfileDiff(bd) {
  markModalAction('openProfileDiff');
  modal(`“${bd.name}” vs the shipped profile`, async (b) => {
    let d;
    try {
      d = await api(`/api/plugin_bundles/${bd.id}/diff`);
    } catch (e) {
      b.append(el('div', 'note-status', 'Could not compare: ' + e.message));
      return;
    }
    b.append(el('p', 'fb-help',
      `You copied “${d.from}” at version ${d.taken_at}; it ships version ${d.now} now. `
      + 'Nothing below has been applied to your copy — taking it replaces the whole profile '
      + 'with the shipped one, including anything you changed.'));

    const lines = el('div', 'pm-diff');
    const row = (label, added, removed) => {
      if (!added.length && !removed.length) return;
      const r = el('div', 'pm-diff-row');
      r.append(el('span', 'pm-diff-label', label));
      const v = el('span', 'pm-diff-vals');
      if (added.length) v.append(el('span', 'pm-add', `+ ${added.join(', ')}`));
      if (removed.length) v.append(el('span', 'pm-rem', `− ${removed.join(', ')}`));
      r.append(v);
      lines.append(r);
    };
    if (d.board.was !== d.board.now) {
      const r = el('div', 'pm-diff-row');
      r.append(el('span', 'pm-diff-label', 'Board size'));
      r.append(el('span', 'pm-diff-vals', `${d.board.was} widgets → ${d.board.now}`));
      lines.append(r);
    }
    row('Plugins', d.plugins.added, d.plugins.removed);
    row('Widgets', d.board.added, d.board.removed);
    row('Extra boards', d.boards.added, d.boards.removed);
    row('Watchlist', d.watchlist.added, d.watchlist.removed);
    row('Variables', d.variables.added, d.variables.removed);
    if (!lines.childElementCount) {
      lines.append(el('div', 'note-status',
        'Nothing differs — the shipped profile changed something this comparison does not name '
        + '(a widget’s SQL, a description). Taking it is still how you get it.'));
    }
    b.append(lines);

    const acts = el('div', 'row-actions');
    const take = el('button', 'btn', 'Take the shipped version');
    take.onclick = async () => {
      if (!(await confirmDialog(
        `Replace “${bd.name}” with the shipped “${d.from}” (version ${d.now})? Your edits to this profile are lost.`,
        { danger: true, okLabel: 'Replace' }))) return;
      try {
        await post(`/api/plugin_bundles/${bd.id}/take_update`, {});
        toast(`“${bd.name}” now matches the shipped profile`, 5000);
        openPluginBundlesModal({ select: bd.name });
      } catch (e) {
        toast('Could not take the update: ' + e.message, 6000);
      }
    };
    const keep = el('button', 'btn ghost', 'Keep mine');
    keep.onclick = () => openPluginBundlesModal({ select: bd.name });
    acts.append(take, keep);
    b.append(acts);
  }, { wide: true });
}

/* ------------------------------------------------------- the apply sheet */

/* What applying this profile will do to THIS case, part by part, with the
   numbers read out of the case — and a checkbox per part, so a board can
   be taken without the watchlist. Nothing happens until Apply. */
export function openProfileApplySheet(bd, { returnTo } = {}) {
  markModalAction('openProfileApply');
  modal(`Apply “${bd.name}” to this case`, async (b) => {
    let plan;
    try {
      plan = await api(`/api/plugin_bundles/${bd.id}/plan`);
    } catch (e) {
      b.append(el('div', 'note-status', 'Could not work out what this would change: ' + e.message));
      return;
    }

    const edited = (plan.boards || []).filter((x) => x.replaces && x.replaces.edited);
    if (edited.length) {
      const warn = el('div', 'ap-warn');
      warn.append(el('span', null,
        `This case already has ${edited.length === 1 ? 'a' : ''} `
        + edited.map((x) => `“${x.name}”`).join(', ')
        + ` board${edited.length === 1 ? '' : 's'} that ${edited.length === 1 ? 'is' : 'are'} not `
        + 'this profile’s own copy. Applying replaces '
        + (edited.length === 1 ? 'it' : 'them') + '.'));
      b.append(warn);
    }

    const boxes = {};
    const part = (key, title, build, { enabled = true } = {}) => {
      const row = el('label', 'ap-part');
      row.dataset.part = key;
      const cb = el('input');
      cb.type = 'checkbox';
      cb.checked = enabled;
      cb.disabled = !enabled;
      boxes[key] = cb;
      const text = el('span', 'ap-text');
      text.append(el('h6', null, title));
      const d = el('span', 'ap-d');
      build(d);
      text.append(d);
      row.append(cb, text);
      if (!enabled) row.classList.add('ap-empty');
      b.append(row);
    };

    const p = plan.plugins;
    part('plugins', 'Plugins', (d) => {
      const bits = [];
      if (p.turn_on.length) {
        bits.push(el('span', null, 'Turn '), el('span', 'ap-on', 'on'), el('span', null, `: ${p.turn_on.join(', ')}`));
      }
      if (p.turn_off.length) {
        if (bits.length) bits.push(el('span', null, ' · '));
        bits.push(el('span', null, 'Turn '), el('span', 'ap-off', 'off'), el('span', null, `: ${p.turn_off.join(', ')}`));
      }
      if (!bits.length) bits.push(el('span', null, 'Every plugin is already where this profile wants it.'));
      for (const n of bits) d.append(n);
      d.append(el('br'));
      const tail = [];
      if (p.already_on.length) tail.push(`${p.already_on.join(', ')} already on`);
      tail.push(p.stay_off === 1 ? '1 other plugin stays off' : `${p.stay_off} other plugins stay off`);
      if (p.missing.length) tail.push(`${p.missing.join(', ')} not installed here`);
      if (p.pins) {
        tail.push(`all ${p.pins} pinned for this case, whatever the machine defaults become`);
      }
      d.append(el('span', null, tail.join(' · ') + '.'));
      /* Ticked even when nothing moves. Applying writes an explicit
         override for every installed plugin, and that write is the point:
         it is what stops a later machine-wide toggle taking a plugin out
         of this case. A part that unticked itself on a matching case
         would quietly make the sheet's apply mean something weaker than
         the one the new-case dialog sends. Only a machine with no
         plugins at all has nothing here to write. */
    }, { enabled: !!p.pins });

    part('boards', 'Boards', (d) => {
      if (!plan.boards.length) {
        d.append(el('span', null, 'This profile creates no boards.'));
        return;
      }
      for (const board of plan.boards) {
        const line = el('div');
        if (board.replaces) {
          line.append(el('span', 'ap-new', 'Replace'));
          line.append(el('span', null,
            ` “${board.name}” (${board.replaces.widget_count} widget`
            + `${board.replaces.widget_count === 1 ? '' : 's'} → ${board.widgets})`));
        } else {
          line.append(el('span', 'ap-new', 'Create'));
          line.append(el('span', null, ` “${board.name}” (${board.widgets} widget${board.widgets === 1 ? '' : 's'})`));
        }
        if (board.live) line.append(el('span', null, ` · ${board.live} run every time, the rest cache`));
        d.append(line);
      }
    }, { enabled: !!plan.boards.length });

    const wl = plan.watchlist;
    part('watchlist', 'Watchlist', (d) => {
      if (!wl.new.length && !wl.existing.length) {
        d.append(el('span', null, 'This profile seeds no indicators.'));
        return;
      }
      if (wl.new.length) {
        d.append(el('span', 'ap-new', `Add ${wl.new.length}`));
        d.append(el('span', null, ` indicator${wl.new.length === 1 ? '' : 's'}: ${wl.new.join(', ')}`));
      } else {
        d.append(el('span', null, 'Every indicator in this profile is already here'));
      }
      if (wl.existing.length) {
        d.append(el('span', null, ` · ${wl.existing.join(', ')} already here`));
      }
      if (wl.new.length) {
        d.append(el('br'));
        d.append(el('span', null,
          `Adding them starts a scan of ${wl.scan_tables} table${wl.scan_tables === 1 ? '' : 's'}.`));
      }
    }, { enabled: !!wl.new.length });

    part('variables', 'Variables', (d) => {
      if (!plan.variables.length) {
        d.append(el('span', null, 'This profile declares no variables.'));
        return;
      }
      plan.variables.forEach((v, i) => {
        if (i) d.append(el('span', null, ', '));
        d.append(el('span', null, v.label || v.name));
        /* A required variable with a declared default is NOT asked for:
           the apply creates the row carrying the default, so it is set
           by the time anything could prompt. Saying "not set" about it
           and then never asking is the one disagreement the sheet
           cannot afford. */
        if (v.required && !v.set && !v.default) d.append(el('span', 'ap-off', ' (required, not set)'));
        else if (v.required && !v.set) d.append(el('span', null, ` (required, defaults to “${v.default}”)`));
        else if (v.required) d.append(el('span', null, ' (required)'));
      });
      const ask = plan.variables.filter((v) => v.required && !v.set && !v.default).length;
      d.append(el('span', null, ask
        ? ' — you will be asked for these after applying.'
        : ' — definitions only; values you have already set are kept.'));
    }, { enabled: !!plan.variables.length });

    const acts = el('div', 'row-actions');
    const go = el('button', 'btn', 'Apply');
    go.onclick = async () => {
      const parts = Object.keys(boxes).filter((k) => boxes[k].checked);
      if (!parts.length) { toast('Nothing is ticked — there is nothing to apply'); return; }
      go.disabled = true;
      go.textContent = 'Applying…';
      try {
        await applyBundle(bd, parts);
        $('modal').hidden = true;
      } catch (e) {
        toast('Could not apply: ' + e.message, 6000);
        go.disabled = false;
        go.textContent = 'Apply';
      }
    };
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => {
      if (returnTo) returnTo(); else $('modal').hidden = true;
    };
    acts.append(go, cancel);
    acts.append(el('span', 'ap-note', 'Nothing is applied until you press Apply.'));
    b.append(acts);
  }, { wide: true });
}
