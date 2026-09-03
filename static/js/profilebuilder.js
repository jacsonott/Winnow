/* The profile builder — "how I analyze this kind of case", assembled in
   one dialog instead of snapshotted from whatever happens to be on
   screen. Pick the plugins, pick the dashboards (from the machine-wide
   library or the open case), and declare the variables an analyst must
   fill in when a case of this type is created.

   A profile is a TEMPLATE: it carries variable definitions (name, label,
   what it's for, whether it's required), never their values, because the
   values belong to a case and travel with it. Secrets belong in neither —
   Settings → Environment is for those.

   Saved profiles live per machine (workspace/plugin_bundles.json); the
   shipped ones (KAPE triage…) are read-only, so the builder opens a
   COPY of one rather than editing it in place. */
import { api, el, post, toast } from './core.js';
import { settingsSection } from './settings.js';
import { S } from './state.js';
import { markModalAction, modal } from './ui.js';

// The store's own rule (winnow/store.py VARIABLE_NAME_RE) — checked here
// so a typo is a message in the builder rather than a variable that
// silently never appears on the case.
const VAR_NAME_RE = /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/;

function labeled(parent, label, node, help) {
  parent.append(el('label', null, label), node);
  if (help) parent.append(el('p', 'fb-help', help));
  return node;
}

function input(value = '', placeholder = '') {
  const i = el('input', 'confirm-input');
  i.value = value;
  i.placeholder = placeholder;
  return i;
}

/* One variable row: what to call it, how to label it at case creation,
   what it's for, and whether creating the case is blocked without it. */
function variableRow(def, onRemove) {
  const row = el('div', 'pb-var');
  const name = input(def.name || '', 'name (engagement)');
  name.classList.add('pb-var-name');
  const label = input(def.label || '', 'label shown to the analyst');
  const desc = input(def.description || '', 'what it is for (optional)');
  const dflt = input(def.default || '', 'default (optional)');
  const req = el('input');
  req.type = 'checkbox';
  req.checked = !!def.required;
  const reqLbl = el('label', 'pb-req');
  reqLbl.append(req, el('span', null, 'Required at case creation'));
  const del = el('button', 'btn ghost pb-var-del', '✕');
  del.title = 'Remove this variable';
  del.onclick = () => { row.remove(); onRemove(); };
  row.append(name, label, desc, dflt, reqLbl, del);
  row.read = () => ({
    name: name.value.trim(),
    label: label.value.trim(),
    description: desc.value.trim(),
    default: dflt.value.trim(),
    required: req.checked,
  });
  return row;
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
    b.append(el('p', 'fb-help',
      'A profile is a kind of case: the plugins it needs, the dashboards it opens with, and the '
      + 'values an analyst must supply when the case is created. Applying it later sets all of that '
      + 'in one step.'));

    const nameIn = input(copying ? `${existing.name} (copy)` : (editing ? existing.name : ''), 'e.g. Ransomware triage');
    const descIn = input((existing && existing.description) || '', 'what this profile is for (optional)');
    const head = el('div', 'pb-head');
    labeled(head, 'Profile name', nameIn);
    labeled(head, 'Description', descIn);
    b.append(head);

    // ---------------------------------------------------------- plugins
    const secP = settingsSection(b, 'Plugins', { open: true });
    const pluginBox = el('div', 'pb-list');
    secP.append(el('p', 'fb-help',
      'Applying the profile turns exactly these on for the case, and everything else off.'), pluginBox);
    const pluginChecks = new Map();
    try {
      const info = await api('/api/plugins');
      const chosen = new Set(existing ? existing.plugins || []
        : (info.plugins || []).filter((p) => p.enabled).map((p) => p.fs_name));
      if (!(info.plugins || []).length) {
        pluginBox.append(el('div', 'note-status', 'No plugins installed — Settings → Plugins installs them.'));
      }
      for (const p of info.plugins || []) {
        const row = el('label', 'pb-row');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.checked = chosen.has(p.fs_name);
        pluginChecks.set(p.fs_name, cb);
        const name = el('span', 'pb-name', p.name || p.fs_name);
        if (p.bundled) name.append(el('span', 'bundle-shipped', 'example'));
        row.append(cb, name, el('span', 'pb-desc', p.description || ''));
        pluginBox.append(row);
      }
      // A profile can name a plugin this machine doesn't have; keep it
      // rather than dropping it silently on the next save.
      for (const fs of (existing && existing.plugins) || []) {
        if (pluginChecks.has(fs)) continue;
        const row = el('label', 'pb-row');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.checked = true;
        pluginChecks.set(fs, cb);
        row.append(cb, el('span', 'pb-name', fs), el('span', 'pb-desc', 'not installed on this machine'));
        pluginBox.append(row);
      }
    } catch (e) {
      pluginBox.append(el('div', 'note-status', 'Could not read the plugin list: ' + e.message));
    }

    // ------------------------------------------------------- dashboards
    const secD = settingsSection(b, 'Dashboards', { open: true });
    const dashBox = el('div', 'pb-list');
    secD.append(el('p', 'fb-help',
      'Boards the profile creates in the case. Saved boards come from the dashboard library '
      + '(Dashboards → “Save to library…”); boards in the open case can be picked straight from it.'), dashBox);
    const dashChecks = [];        // { entry, cb }
    const seen = new Set();
    try {
      const [lib, caseBoards] = await Promise.all([
        api('/api/dashboard_library').catch(() => []),
        api('/api/dashboards').catch(() => []),
      ]);
      const chosenNames = new Set(((existing && existing.dashboards) || []).map((d) => d.name.toLowerCase()));
      const add = (entry, checked, note) => {
        if (seen.has(entry.name.toLowerCase())) return;
        seen.add(entry.name.toLowerCase());
        const row = el('label', 'pb-row');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.checked = checked;
        row.append(cb, el('span', 'pb-name', entry.name),
          el('span', 'pb-desc', `${entry.widget_count} widget${entry.widget_count === 1 ? '' : 's'} · ${note}`));
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
        chosenNames.has(d.name.toLowerCase()), 'saved board');
      for (const d of caseBoards) add({ kind: 'case', id: d.id, name: d.name, widget_count: d.widget_count },
        chosenNames.has(d.name.toLowerCase()), 'in the open case');
      if (!dashChecks.length) {
        dashBox.append(el('div', 'note-status',
          'No dashboards yet — build one in a case, then “Save to library…” to offer it here.'));
      }
    } catch (e) {
      dashBox.append(el('div', 'note-status', 'Could not read dashboards: ' + e.message));
    }
    // A profile saved from a dashboard carries one unnamed board of its
    // own; keep it unless the analyst clears it here.
    let keepOwnBoard = null;
    if (existing && (existing.dashboard || []).length) {
      const row = el('label', 'pb-row');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.checked = true;
      row.append(cb, el('span', 'pb-name', `${existing.name} (the profile’s own board)`),
        el('span', 'pb-desc', `${existing.dashboard.length} widgets · applied under the profile’s name`));
      dashBox.append(row);
      keepOwnBoard = { cb, widgets: existing.dashboard };
    }

    // -------------------------------------------------------- variables
    const secV = settingsSection(b, 'Variables', { open: true });
    const varBox = el('div', 'pb-vars');
    secV.append(el('p', 'fb-help',
      'Values a case of this type carries — the engagement name, a backend URL, a link to the '
      + 'scoping document. Required ones are asked for in the New case dialog and must be filled '
      + 'in before the case is created. Plugins read them as req.variables. Case data, so they '
      + 'travel with the file: never a token or password (those go in Settings → Environment).'), varBox);
    const rows = [];
    const addVar = (def) => {
      const row = variableRow(def, () => {
        const i = rows.indexOf(row);
        if (i > -1) rows.splice(i, 1);
      });
      rows.push(row);
      varBox.append(row);
      return row;
    };
    for (const d of (existing && existing.variables) || []) addVar(d);
    const addBtn = el('button', 'btn ghost', '＋ Add variable');
    addBtn.onclick = () => addVar({}).querySelector('.pb-var-name').focus();
    secV.append(addBtn);

    // ----------------------------------------------------------- saving
    const acts = el('div', 'row-actions pb-actions');
    const save = el('button', 'btn', editing ? 'Save changes' : 'Create profile');
    const cancel = el('button', 'btn ghost', 'Cancel');
    cancel.onclick = () => { document.getElementById('modal').hidden = true; };
    save.onclick = async () => {
      const name = nameIn.value.trim();
      if (!name) { toast('Name the profile'); nameIn.focus(); return; }
      const variables = rows.map((r) => r.read()).filter((v) => v.name || v.label || v.description);
      const bad = variables.find((v) => !VAR_NAME_RE.test(v.name));
      if (bad) {
        toast(`“${bad.name || '(blank)'}” is not a variable name — a letter, then letters, digits, _ . or -`, 6000);
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
        };
        const rec = await post('/api/plugin_bundles', body);
        toast(`Profile “${rec.name}” saved — apply it from the Plugin bundles menu, or pick it as the `
          + 'case type when creating a case', 7000);
        document.getElementById('modal').hidden = true;
        if (onSaved) onSaved(rec);
      } catch (e) {
        toast('Could not save: ' + e.message, 6000);
        save.disabled = false;
        save.textContent = editing ? 'Save changes' : 'Create profile';
      }
    };
    acts.append(save, cancel);
    b.append(acts);
    setTimeout(() => { if (!nameIn.value) nameIn.focus(); }, 0);
  }, { wide: true });
}

/* Convenience for the case menu: build a profile from what the analyst is
   looking at right now (this case's plugins, its boards, its variables). */
export function openProfileBuilderFromCase() {
  const plugins = [...new Set((S.plugins || []).filter((p) => p.enabled).map((p) => p.fs_name))];
  const variables = (S.caseVariables || []).map((v) => ({
    name: v.name, label: '', description: v.description || '', required: !!v.required, default: '' }));
  openProfileBuilder({ name: '', plugins, variables, dashboards: [], dashboard: [] });
}
