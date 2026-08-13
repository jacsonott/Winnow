/* Theme engine v2 for the round-2 style demos — same job as theme.js, but
   driven from a Settings modal instead of always-visible header controls
   (per feedback on round 1: theme/accent belong in Settings, not the bar),
   plus an Auto mode that follows the OS, and a density toggle.

   Each style page sets window.TL_MOCK_ID / TL_DEFAULT_ACCENT /
   TL_DEFAULT_THEME before including this script, so switching between
   wildly different skins doesn't drag one style's accent onto another —
   each remembers its own choice independently.

   Expected DOM, all inside the Settings modal:
     #settingsBtn                     — opens it (e.g. an item in the "⋯" menu)
     #settingsScrim / #settingsClose  — modal shell
     #themeSegmented button[data-theme="dark|light|auto"]
     .accent-swatch[data-accent="#rrggbb"], #accentCustom (input type=color)
     #densitySegmented button[data-density="comfortable|compact"] (optional) */
(function () {
  const ID = window.TL_MOCK_ID || 'default';
  const THEME_KEY = `tl_mock2_${ID}_theme`;
  const ACCENT_KEY = `tl_mock2_${ID}_accent`;
  const DENSITY_KEY = `tl_mock2_${ID}_density`;
  const DEFAULT_ACCENT = window.TL_DEFAULT_ACCENT || '#d2a04a';
  const DEFAULT_THEME = window.TL_DEFAULT_THEME || 'dark';

  function contrastFg(hex) {
    const c = hex.replace('#', '');
    const r = parseInt(c.substr(0, 2), 16), g = parseInt(c.substr(2, 2), 16), b = parseInt(c.substr(4, 2), 16);
    const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
    return lum > 0.6 ? '#14181d' : '#ffffff';
  }

  function resolveAuto() {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  let mode = DEFAULT_THEME;

  function paintThemeMode() {
    const resolved = mode === 'auto' ? resolveAuto() : mode;
    document.documentElement.setAttribute('data-theme', resolved);
    document.querySelectorAll('#themeSegmented button').forEach((b) => {
      b.setAttribute('aria-pressed', String(b.dataset.theme === mode));
    });
  }

  function setThemeMode(next) {
    mode = next;
    localStorage.setItem(THEME_KEY, mode);
    paintThemeMode();
  }

  function applyAccent(hex) {
    document.documentElement.style.setProperty('--accent', hex);
    document.documentElement.style.setProperty('--accent-fg', contrastFg(hex));
    localStorage.setItem(ACCENT_KEY, hex);
    document.querySelectorAll('.accent-swatch').forEach((el) => {
      el.setAttribute('aria-pressed', String((el.dataset.accent || '').toLowerCase() === hex.toLowerCase()));
    });
    const custom = document.getElementById('accentCustom');
    if (custom) custom.value = hex;
  }

  function applyDensity(d) {
    document.documentElement.setAttribute('data-density', d);
    localStorage.setItem(DENSITY_KEY, d);
    document.querySelectorAll('#densitySegmented button').forEach((b) => {
      b.setAttribute('aria-pressed', String(b.dataset.density === d));
    });
  }

  function init() {
    setThemeMode(localStorage.getItem(THEME_KEY) || DEFAULT_THEME);
    applyAccent(localStorage.getItem(ACCENT_KEY) || DEFAULT_ACCENT);
    applyDensity(localStorage.getItem(DENSITY_KEY) || 'comfortable');

    document.querySelectorAll('#themeSegmented button').forEach((b) => {
      b.addEventListener('click', () => setThemeMode(b.dataset.theme));
    });
    document.querySelectorAll('.accent-swatch').forEach((el) => {
      el.addEventListener('click', () => applyAccent(el.dataset.accent));
    });
    const custom = document.getElementById('accentCustom');
    if (custom) custom.addEventListener('input', (e) => applyAccent(e.target.value));
    document.querySelectorAll('#densitySegmented button').forEach((b) => {
      b.addEventListener('click', () => applyDensity(b.dataset.density));
    });

    if (window.matchMedia) {
      window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
        if (mode === 'auto') paintThemeMode();
      });
    }

    const settingsBtn = document.getElementById('settingsBtn');
    const settingsScrim = document.getElementById('settingsScrim');
    const settingsClose = document.getElementById('settingsClose');
    const open = () => { settingsScrim.hidden = false; };
    const close = () => { settingsScrim.hidden = true; };
    if (settingsBtn) settingsBtn.addEventListener('click', open);
    if (settingsClose) settingsClose.addEventListener('click', close);
    if (settingsScrim) settingsScrim.addEventListener('click', (e) => { if (e.target === settingsScrim) close(); });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && settingsScrim && !settingsScrim.hidden) close();
    });
  }

  document.addEventListener('DOMContentLoaded', init);
})();
