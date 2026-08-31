/* File associations — the Settings panel that registers Winnow with the
   OS, and the one-time after-import offer. All the policy lives server
   side (winnow/assoc.py's catalogue: which types exist, which may be
   made the default); this module renders what /api/assoc/types says and
   posts back choices. On macOS the panel says why it's empty instead of
   pretending. */

import { $, api, el, post, toast } from './core.js';
import { confirmDialog, modal } from './ui.js';

async function types() {
  return api('/api/assoc/types');
}

const PLATFORM_LABEL = { windows: 'Windows', linux: 'Linux' };

export function buildAssocPanel(b) {
  const note = el('p', null,
    'Put Winnow in your system’s Open With menu for the file types it reads. '
    + 'Registration is per-user — no admin rights — and undoing it here removes it again.');
  const list = el('div', 'assoc-list');
  b.append(note, list);

  async function paint() {
    let info;
    try { info = await types(); } catch (e) {
      list.textContent = 'Could not read association state: ' + e.message;
      return;
    }
    list.textContent = '';
    if (!PLATFORM_LABEL[info.platform]) {
      list.append(el('p', 'fb-help',
        'Not available on this platform — file associations need a Windows or Linux desktop. '
        + 'On macOS, associations require an app bundle, which a run-from-source tool doesn’t have.'));
      return;
    }
    for (const t of info.types) {
      const row = el('div', 'assoc-row');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.checked = t.registered;
      cb.onchange = async () => {
        try {
          await post(cb.checked ? '/api/assoc/register' : '/api/assoc/unregister', { exts: [t.ext] });
          toast(cb.checked ? `Winnow added to Open With for ${t.ext}` : `Winnow removed for ${t.ext}`);
        } catch (e) {
          toast(`Could not update ${t.ext}: ` + e.message, 6000);
        }
        paint();
      };
      const lbl = el('label', 'assoc-label');
      lbl.append(cb, el('span', 'assoc-ext', t.ext), el('span', 'assoc-desc', t.label
        + (t.source !== 'builtin' ? ` (${t.source} plugin)` : '')));
      row.append(lbl);
      if (t.default_ok) {
        const defBtn = el('button', 'btn ghost', t.default ? 'Default ✓' : 'Make default');
        defBtn.disabled = t.default;
        defBtn.onclick = async () => {
          try {
            const r = await post('/api/assoc/default', { exts: [t.ext] });
            if ((r.userchoice || []).length) {
              // Windows keeps the final say via its hash-protected
              // UserChoice key — tell the analyst the honest next step
              // rather than claiming a victory Explorer will ignore.
              toast(`Windows guards the default for ${t.ext} — right-click a ${t.ext} file, `
                + 'choose Open With → Choose another app, pick Winnow and tick Always.', 12000);
            } else {
              toast(`Winnow is now the default for ${t.ext}`);
            }
          } catch (e) {
            toast(`Could not set default for ${t.ext}: ` + e.message, 6000);
          }
          paint();
        };
        row.append(defBtn);
      } else {
        row.append(el('span', 'assoc-handleronly', 'Open With only'));
      }
      list.append(row);
    }
  }
  paint();
}

/* On launch: when the catalogue has grown extensions nobody was ever
   asked about — a Winnow update adding a type, or a newly installed
   plugin — ask once whether Winnow should be their default. Only on a
   machine that actually uses associations (something is registered);
   any answer, including Not now, is recorded and only genuinely NEW
   extensions raise this again. */
export async function maybeOfferDefaultPrompt() {
  let info;
  try { info = await types(); } catch { return; }
  if (!PLATFORM_LABEL[info.platform]) return;
  if (!info.types.some((t) => t.registered)) return;
  const fresh = info.types.filter((t) => t.default_ok && !t.prompted && !t.default);
  if (!fresh.length) return;
  const exts = fresh.map((t) => t.ext);
  const names = exts.join(', ');
  await new Promise((resolve) => {
    modal('New file types', (b) => {
      b.append(el('p', null,
        `Winnow can now open ${names} files. Should it become the DEFAULT app for them `
        + '(double-click opens Winnow), just appear in the Open With menu, or leave them alone? '
        + 'You can change any of this later in Settings → File associations.'));
      const acts = el('div', 'row-actions');
      const finish = async (route) => {
        try {
          if (route) {
            const r = await post(route, { exts });
            if ((r.userchoice || []).length) {
              toast(`Windows guards the default for ${r.userchoice.join(', ')} — right-click such a file, `
                + 'choose Open With → Choose another app, pick Winnow and tick Always.', 12000);
            }
          }
          await post('/api/assoc/prompted', { exts });
        } catch (e) {
          toast('Could not update associations: ' + e.message, 6000);
        }
        $('modal').hidden = true;
        resolve();
      };
      const def = el('button', 'btn', 'Make Winnow the default');
      def.onclick = () => finish('/api/assoc/default');
      const handler = el('button', 'btn ghost', 'Open With only');
      handler.onclick = () => finish('/api/assoc/register');
      const skip = el('button', 'btn ghost', 'Not now');
      skip.onclick = () => finish(null);
      acts.append(def, handler, skip);
      b.append(acts);
    });
  });
}

/* After an import: offer — once per type, ever — to register Winnow for
   the types the analyst demonstrably opens with it. Answering the offer
   (either way) records it server-side, so a "no" is never re-asked and
   the offer never fires on a machine where Settings already decided. */
export async function maybeOfferAssociation(filenames) {
  let info;
  try { info = await types(); } catch { return; }
  if (!PLATFORM_LABEL[info.platform]) return;
  const exts = [...new Set(filenames
    .map((n) => (/\.[A-Za-z0-9_]+$/.exec(n || '') || [''])[0].toLowerCase())
    .filter(Boolean))];
  const offer = info.types.filter((t) => exts.includes(t.ext) && !t.registered && !t.asked);
  if (!offer.length) return;
  const names = offer.map((t) => t.ext).join(', ');
  const yes = await confirmDialog(
    `Add Winnow to ${PLATFORM_LABEL[info.platform]}’s Open With menu for ${names} files? `
    + 'You can change this any time in Settings → File associations.',
    { okLabel: 'Add to Open With', cancelLabel: 'No thanks' });
  try {
    if (yes) {
      await post('/api/assoc/register', { exts: offer.map((t) => t.ext) });
      toast(`Winnow registered for ${names}`);
    } else {
      await post('/api/assoc/asked', { exts: offer.map((t) => t.ext) });
    }
  } catch (e) {
    toast('Could not update associations: ' + e.message, 6000);
  }
}
