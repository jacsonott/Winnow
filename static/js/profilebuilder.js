/* The profile builder — "how I analyze this kind of case", assembled in
   one dialog instead of snapshotted from whatever happens to be on
   screen. It takes the merge builder's shape: two panes, the definition
   on the left and what it does on the right. Left, what the profile turns
   on: its name, the plugins, the boards (from the machine-wide library or
   the open case). Right, what a case of it gets: the variables an analyst
   must fill in, one sentence saying what applying it does, and the values
   prompt as the analyst will see it — so nobody has to work out from the
   ticks what Apply will do.

   A profile is a TEMPLATE: it carries variable definitions (name, label,
   what it's for, whether it's required), never their values, because the
   values belong to a case and travel with it. Secrets belong in neither —
   Settings → Environment is for those.

   Saved profiles live per machine (workspace/plugin_bundles.json); the
   shipped ones (KAPE triage…) are read-only, so the builder opens a
   COPY of one rather than editing it in place. */
import { api, el, post, toast } from './core.js';
import { variablePromptFields } from './settings.js';
import { S } from './state.js';
import { markModalAction, modal } from './ui.js';

// The store's own rule (winnow/store.py VARIABLE_NAME_RE) — checked here
// so a typo is a message in the builder rather than a variable that
// silently never appears on the case.
const VAR_NAME_RE = /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/;
// Non-breaking spaces in the tail, so a wrap never strands "or -" on a line of its own.
const VAR_NAME_RULE = 'a letter, then letters, digits, _\u00a0.\u00a0or\u00a0-';

const plural = (n, one, many = `${one}s`) => (n === 1 ? one : many);
const widgetCount = (n) => `${n} ${plural(n, 'widget')}`;

let fieldSeq = 0;

/* A caption above its box (.dlg-field), tied to it by for/id so a click
   on the words lands in the box. `after` goes under the box (a hint). */
function field(label, control, after) {
  const f = el('div', 'dlg-field');
  if (!control.id) control.id = `pbField${++fieldSeq}`;
  const l = el('label', null, label);
  l.htmlFor = control.id;
  f.append(l, control);
  if (after) f.append(after);
  return f;
}

function input(value = '', placeholder = '') {
  const i = el('input', 'confirm-input');
  i.type = 'text';
  i.autocomplete = 'off';
  i.value = value;
  if (placeholder) i.placeholder = placeholder;
  return i;
}

/* A dim section head, one line under it saying what the section is for,
   then its contents. `key` is a stable handle (data-sec) that doesn't
   depend on the wording of the head. */
function section(key, title, sub, ...kids) {
  const s = el('div', 'pb-sec');
  s.dataset.sec = key;
  s.append(el('div', 'pb-sec-head', title));
  if (sub) s.append(el('div', 'pb-sub', sub));
  s.append(...kids);
  return s;
}

/* One ticked-or-not line in the Plugins or Dashboards list: the box, the
   name with a dim line under it, and a chip saying where it comes from. */
function checkRow(name, sub, checked, chip, cls = '') {
  const row = el('label', 'pb-row');
  const cb = el('input');
  cb.type = 'checkbox';
  cb.checked = checked;
  const cell = el('span', 'pb-cell');
  cell.append(el('span', 'pb-name', name));
  if (sub) cell.append(el('span', 'pb-desc', sub));
  row.append(cb, cell, chip ? el('span', `pb-chip${cls ? ' ' + cls : ''}`, chip) : el('span'));
  return { row, cb };
}

/* One variable, as a card: what to call it and how to label it at case
   creation (side by side), what it's for, its default, and whether
   creating the case is blocked without it. Every box has a caption, so
   nothing rests on placeholder text. `flag` shows the name's problem, if
   it has one, under the Name box. */
function variableCard(def, onRemove) {
  const card = el('div', 'pb-var');
  const name = input(def.name || '', 'e.g. engagement');
  name.classList.add('pb-var-name');
  name.spellcheck = false;
  const label = input(def.label || '', 'e.g. Engagement name');
  const desc = input(def.description || '');
  const dflt = input(def.default || '');
  const req = el('input');
  req.type = 'checkbox';
  req.checked = !!def.required;
  const hint = el('small', 'pb-hint warn');

  const del = el('button', 'btn ghost pb-var-del', '✕');
  del.type = 'button';
  del.title = 'Remove this variable';
  del.setAttribute('aria-label', 'Remove this variable');
  del.onclick = () => { card.remove(); onRemove(); };

  const ids = el('div', 'dlg-pair');
  ids.append(field('Name', name, hint), field('Label', label));
  const top = el('div', 'pb-var-top');
  top.append(ids, del);
  const reqLbl = el('label', 'pb-req');
  reqLbl.append(req, el('span', null, 'Required at case creation'));
  const last = el('div', 'dlg-pair');
  last.append(field('Default', dflt), reqLbl);
  card.append(top, field('What it’s for', desc), last);

  card.read = () => ({
    name: name.value.trim(),
    label: label.value.trim(),
    description: desc.value.trim(),
    default: dflt.value.trim(),
    required: req.checked,
  });
  card.flag = (problem) => {
    hint.textContent = problem;
    name.classList.toggle('bad', !!problem);
  };
  return card;
}

/* What is wrong with one variable's name, given how many times each name
   is used — the refusals Save makes, said under the box they are about. A
   card with nothing in it is not a variable yet, so it has no problem. */
function nameProblem(v, uses) {
  if (!(v.name || v.label || v.description)) return '';
  if (!v.name) return `Needs a name: ${VAR_NAME_RULE}`;
  if (!VAR_NAME_RE.test(v.name)) return `Not a variable name: ${VAR_NAME_RULE}`;
  if ((uses.get(v.name.toLowerCase()) || 0) > 1) return 'Another variable has this name';
  return '';
}

/* `existing` is a saved profile to edit, or a shipped one to start from
   (shipped profiles are read-only, so that saves as a new name). */
export function openProfileBuilder(existing = null, { onSaved } = {}) {
  markModalAction('openProfileBuilder');
  const copying = !!(existing && existing.shipped);
  const editing = !!(existing && existing.name && !copying);
  const title = copying ? `New profile from “${existing.name}”`
    : editing ? `Edit profile — ${existing.name}` : 'New profile';
  modal(title, async (b) => {
    const root = el('div', 'pb');
    root.append(el('p', 'fb-help',
      'A kind of case, set up in one step: the plugins it turns on, the boards it opens with '
      + 'and the values it asks for.'));
    const panes = el('div', 'pb-panes');
    const left = el('div', 'pb-pane');
    const right = el('div', 'pb-pane pb-result');
    panes.append(left, right);

    // ------------------------------------- left: what the profile turns on
    left.append(el('div', 'pb-pane-head', 'What the profile turns on'));
    const nameIn = input(copying ? `${existing.name} (copy)` : (editing ? existing.name : ''), 'e.g. Ransomware triage');
    nameIn.id = 'pbName';
    const nameHint = el('small', 'pb-hint');
    const descIn = input((existing && existing.description) || '', 'What this profile is for (optional)');
    const head = el('div', 'pb-head');
    head.append(field('Profile name', nameIn, nameHint), field('Description', descIn));
    left.append(head);

    const pluginBox = el('div', 'pb-list');
    left.append(section('plugins', 'Plugins',
      'Applying the profile turns the ticked ones on for the case, and every other plugin off.', pluginBox));
    const dashBox = el('div', 'pb-list');
    left.append(section('dashboards', 'Dashboards',
      'Created in the case, each under its own name. A board’s “Save to library…” adds it here.', dashBox));

    // ---------------------------------------- right: what a case gets
    const summary = el('div', 'pb-summary');
    summary.setAttribute('aria-live', 'polite');
    const notes = el('ul', 'pb-notes');
    right.append(el('div', 'pb-pane-head', 'What a case gets'), summary, notes);

    const varBox = el('div', 'pb-vars');
    const addBtn = el('button', 'btn ghost pb-add', '＋ Add variable');
    addBtn.type = 'button';
    right.append(section('variables', 'Variables',
      'Values a case of this type carries. Never a token or password: those go in Settings → Environment.',
      varBox, addBtn));
    // The values prompt itself, built by the prompt's own code and made
    // inert, so the preview cannot drift from what an analyst is asked.
    const preview = el('div', 'pb-preview');
    preview.inert = true;
    right.append(section('preview', 'Asked when a case is created',
      'What the analyst sees. Required ones (*) must be filled in before the case is created.', preview));

    // ----------------------------------------------------------- saving
    const acts = el('div', 'row-actions pb-actions');
    const save = el('button', 'btn primary', editing ? 'Save changes' : 'Create profile');
    // Until the plugin and dashboard lists are in, Save would write a
    // profile with neither — so it waits for them, as it did when it was
    // only drawn after them.
    save.disabled = true;
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { document.getElementById('modal').hidden = true; };
    acts.append(save, cancel);

    root.append(panes, acts);
    b.append(root);

    // ------------------------------------------------- the live state
    const pluginChecks = new Map();   // fs_name -> checkbox
    const installed = new Set();      // fs_names this machine has
    const dashChecks = [];            // { entry, cb }
    let keepOwnBoard = null;          // { cb, widgets } — a profile's unnamed board
    let others = [];                  // every profile on the machine, for the name hint
    let loaded = false;
    const rows = [];

    // The variables Save keeps: a card counts once anything that names
    // or explains it is filled in.
    const readVars = () => rows.map((r) => r.read()).filter((v) => v.name || v.label || v.description);

    function nameNote() {
      const n = nameIn.value.trim().toLowerCase();
      if (!n) return ['', false];
      // Profiles are saved BY NAME, so a taken one is overwritten and a
      // renamed edit lands beside the original. Say so before Save.
      const mine = others.find((o) => !o.shipped && o.name.toLowerCase() === n
        && !(editing && o.id === existing.id));
      if (mine) return [`Saving replaces your profile “${mine.name}”.`, true];
      if (editing && n !== existing.name.toLowerCase()) {
        return [`Saves as a new profile; “${existing.name}” stays as it is.`, false];
      }
      if (others.some((o) => o.shipped && o.name.toLowerCase() === n)) {
        return ['A shipped profile has this name too; both will be listed.', false];
      }
      return ['', false];
    }

    function paintSummary(vars) {
      summary.replaceChildren();
      if (!loaded) { summary.textContent = 'Reading the plugins and dashboards…'; return; }
      const on = [...pluginChecks].filter(([fs, cb]) => cb.checked && installed.has(fs)).length;
      const boards = dashChecks.filter((d) => d.cb.checked).length
        + (keepOwnBoard && keepOwnBoard.cb.checked ? 1 : 0);
      const indicators = ((existing && existing.watchlist) || []).length;
      const required = vars.filter((v) => v.required).length;
      const bits = [];
      if (on) bits.push(['turns on ', on, ` ${plural(on, 'plugin')}`]);
      else bits.push([installed.size ? 'turns every plugin off' : 'turns on no plugins']);
      bits.push(boards ? ['opens ', boards, ` ${plural(boards, 'dashboard')}`] : ['opens no dashboards']);
      if (indicators) bits.push(['seeds ', indicators, ` watchlist ${plural(indicators, 'indicator')}`]);
      if (vars.length) {
        bits.push(['asks for ', vars.length, ` ${plural(vars.length, 'value')}`,
          ...(required ? [' (', required, ' required)'] : [])]);
      } else {
        bits.push(['asks for nothing']);
      }
      bits.forEach((parts, i) => {
        if (i) summary.append(' · ');
        parts.forEach((p, j) => {
          if (typeof p === 'number') summary.append(el('b', null, String(p)));
          else summary.append(i === 0 && j === 0 ? p[0].toUpperCase() + p.slice(1) : p);
        });
      });
    }

    function paintNotes() {
      notes.replaceChildren();
      const missing = [...pluginChecks].filter(([fs, cb]) => cb.checked && !installed.has(fs)).map(([fs]) => fs);
      if (loaded && missing.length) {
        notes.append(el('li', null, `${missing.join(', ')} ${missing.length === 1 ? 'isn’t' : 'aren’t'} `
          + 'installed here: kept in the profile, and skipped when it is applied on this machine.'));
      }
      const indicators = ((existing && existing.watchlist) || []).length;
      if (indicators) {
        notes.append(el('li', null, `Its watchlist (${indicators} ${plural(indicators, 'indicator')}) `
          + 'is kept as it is; the builder doesn’t edit one.'));
      }
      notes.hidden = !notes.children.length;
    }

    function paintPreview(vars) {
      const shown = vars.filter((v) => v.name || v.label);
      if (!shown.length) {
        preview.replaceChildren(el('p', 'pb-empty', 'Nothing: a case of this profile is created without asking.'));
        return;
      }
      preview.replaceChildren(...variablePromptFields(shown).fields);
    }

    function refresh() {
      const vars = readVars();
      const uses = new Map();
      for (const v of vars) if (v.name) uses.set(v.name.toLowerCase(), (uses.get(v.name.toLowerCase()) || 0) + 1);
      for (const r of rows) r.flag(nameProblem(r.read(), uses));
      const [note, warn] = nameNote();
      nameHint.textContent = note;
      nameHint.classList.toggle('warn', warn);
      paintSummary(vars);
      paintNotes();
      paintPreview(vars);
    }
    // One listener for the whole builder: every box and tick feeds the
    // same summary, so there's nothing to gain from wiring each one.
    root.addEventListener('input', refresh);
    root.addEventListener('change', refresh);

    // -------------------------------------------------------- variables
    const addVar = (def) => {
      const row = variableCard(def, () => {
        const i = rows.indexOf(row);
        if (i > -1) rows.splice(i, 1);
        refresh();
      });
      rows.push(row);
      varBox.append(row);
      return row;
    };
    for (const d of (existing && existing.variables) || []) addVar(d);
    addBtn.onclick = () => {
      addVar({}).querySelector('.pb-var-name').focus();
      refresh();
    };
    refresh();

    // ------------------------------------------ plugins and dashboards
    const [info, lib, caseBoards, all] = await Promise.all([
      api('/api/plugins').catch((e) => ({ error: e })),
      api('/api/dashboard_library').catch(() => []),
      api('/api/dashboards').catch(() => []),
      api('/api/plugin_bundles').catch(() => []),
    ]);
    others = Array.isArray(all) ? all : [];

    try {
      if (info.error) throw info.error;
      const chosen = new Set(existing ? existing.plugins || []
        : (info.plugins || []).filter((p) => p.enabled).map((p) => p.fs_name));
      if (!(info.plugins || []).length) {
        pluginBox.append(el('div', 'note-status', 'No plugins installed — Settings → Plugins installs them.'));
      }
      for (const p of info.plugins || []) {
        const { row, cb } = checkRow(p.name || p.fs_name, p.description || '', chosen.has(p.fs_name),
          p.bundled ? 'example' : '');
        if (p.description) row.title = p.description;
        pluginChecks.set(p.fs_name, cb);
        installed.add(p.fs_name);
        pluginBox.append(row);
      }
      // A profile can name a plugin this machine doesn't have; keep it
      // rather than dropping it silently on the next save.
      for (const fs of (existing && existing.plugins) || []) {
        if (pluginChecks.has(fs)) continue;
        const { row, cb } = checkRow(fs, 'named by the profile', true, 'not installed', 'warn');
        pluginChecks.set(fs, cb);
        pluginBox.append(row);
      }
    } catch (e) {
      pluginBox.append(el('div', 'note-status', 'Could not read the plugin list: ' + e.message));
    }

    // A profile saved from a dashboard carries one unnamed board of its
    // own; keep it unless the analyst clears it here. It leads the list,
    // since it is the one board that is the profile's and no one else's.
    if (existing && (existing.dashboard || []).length) {
      const { row, cb } = checkRow(existing.name,
        `${widgetCount(existing.dashboard.length)} · the profile’s own board, applied under its name`, true,
        'in this profile');
      dashBox.append(row);
      keepOwnBoard = { cb, widgets: existing.dashboard };
    }
    const seen = new Set();
    try {
      const chosenNames = new Set(((existing && existing.dashboards) || []).map((d) => d.name.toLowerCase()));
      const add = (entry, checked, chip) => {
        if (seen.has(entry.name.toLowerCase())) return;
        seen.add(entry.name.toLowerCase());
        const { row, cb } = checkRow(entry.name, widgetCount(entry.widget_count), checked, chip);
        dashBox.append(row);
        dashChecks.push({ entry, cb });
      };
      // The profile's own boards first, so editing shows what it has even
      // when the source board is long gone from this machine.
      for (const d of (existing && existing.dashboards) || []) {
        add({ kind: 'inline', name: d.name, widgets: d.widgets || [], widget_count: (d.widgets || []).length },
          true, 'in this profile');
      }
      for (const d of lib) add({ kind: 'library', id: d.id, name: d.name, widget_count: d.widget_count },
        chosenNames.has(d.name.toLowerCase()), 'library');
      for (const d of caseBoards) add({ kind: 'case', id: d.id, name: d.name, widget_count: d.widget_count },
        chosenNames.has(d.name.toLowerCase()), 'open case');
      if (!dashChecks.length && !keepOwnBoard) {
        dashBox.append(el('div', 'note-status',
          'No dashboards yet — build one in a case, then “Save to library…” to offer it here.'));
      }
    } catch (e) {
      dashBox.append(el('div', 'note-status', 'Could not read dashboards: ' + e.message));
    }
    loaded = true;
    save.disabled = false;
    refresh();

    save.onclick = async () => {
      const name = nameIn.value.trim();
      if (!name) { toast('Name the profile'); nameIn.focus(); return; }
      const variables = readVars();
      const bad = variables.find((v) => !VAR_NAME_RE.test(v.name));
      if (bad) {
        toast(`“${bad.name || '(blank)'}” is not a variable name — ${VAR_NAME_RULE}`, 6000);
        return;
      }
      const dupe = variables.map((v) => v.name.toLowerCase())
        .find((n, i, a) => a.indexOf(n) !== i);
      if (dupe) { toast(`Two variables are both named “${dupe}”`, 5000); return; }

      save.disabled = true;
      save.textContent = 'Saving…';
      try {
        const dashboards = [];
        for (const { entry, cb } of dashChecks) {
          if (!cb.checked) continue;
          let widgets = entry.widgets;
          if (!widgets) {
            const res = entry.kind === 'library'
              ? await api(`/api/dashboard_library/${entry.id}`)
              : await api(`/api/dashboards/${entry.id}`);
            widgets = res.widgets || [];
          }
          dashboards.push({ name: entry.name, widgets });
        }
        const body = {
          name,
          description: descIn.value.trim(),
          plugins: [...pluginChecks].filter(([, cb]) => cb.checked).map(([fs]) => fs),
          dashboards,
          variables,
          dashboard: keepOwnBoard && keepOwnBoard.cb.checked ? keepOwnBoard.widgets : [],
          // The builder has no watchlist editor, so the profile's starter
          // indicators ride through untouched rather than being dropped by
          // a dialog that never asked about them — a copy of KAPE triage
          // that silently lost its five IOCs looked like a bug from every
          // angle except this one.
          watchlist: (existing && existing.watchlist) || [],
          // Lineage. A copy of a shipped profile records which one and at
          // what version; editing that copy later must not erase the
          // answer, which is why the non-copying branch passes what the
          // record already carries rather than null.
          from_profile: copying ? existing.name : ((existing && existing.from_profile) || null),
          from_version: copying ? (existing.version || 1) : ((existing && existing.from_version) || null),
        };
        const rec = await post('/api/plugin_bundles', body);
        toast(`Profile “${rec.name}” saved — apply it from the profile manager, or pick it `
          + 'when creating a case', 7000);
        document.getElementById('modal').hidden = true;
        if (onSaved) onSaved(rec);
      } catch (e) {
        toast('Could not save: ' + e.message, 6000);
        save.disabled = false;
        save.textContent = editing ? 'Save changes' : 'Create profile';
      }
    };
  }, { wide: 'x', focus: (b) => { const n = b.querySelector('#pbName'); return n && !n.value ? n : null; } });
}

/* Convenience for the case menu: build a profile from what the analyst is
   looking at right now (this case's plugins, its boards, its variables). */
export function openProfileBuilderFromCase() {
  const plugins = [...new Set((S.plugins || []).filter((p) => p.enabled).map((p) => p.fs_name))];
  const variables = (S.caseVariables || []).map((v) => ({
    name: v.name, label: '', description: v.description || '', required: !!v.required, default: '' }));
  openProfileBuilder({ name: '', plugins, variables, dashboards: [], dashboard: [] });
}
