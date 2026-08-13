/* Shared theme engine for the Winnow redesign mockups.
   Vanilla, no deps — consistent with the app's own no-CDN/no-build rule.
   Every mockup page wires the same three controls up to this:
     #themeToggle        — button, toggles data-theme on <html> between
                            "dark" and "light"
     .accent-swatch       — buttons with data-accent="#rrggbb" (presets)
     #accentCustom        — <input type="color"> for a fully custom accent
   Both persist to localStorage so switching between the three option
   pages keeps your choice. --accent-fg is computed from the accent's
   luminance so text drawn on an accent-colored button stays readable. */
(function () {
  const THEME_KEY = 'tl_mock_theme';
  const ACCENT_KEY = 'tl_mock_accent';
  const DEFAULT_ACCENT = '#d2a04a'; // the app's current brass

  function contrastFg(hex) {
    const c = hex.replace('#', '');
    const r = parseInt(c.substr(0, 2), 16), g = parseInt(c.substr(2, 2), 16), b = parseInt(c.substr(4, 2), 16);
    const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
    return lum > 0.6 ? '#14181d' : '#ffffff';
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

  function applyTheme(name) {
    document.documentElement.setAttribute('data-theme', name);
    localStorage.setItem(THEME_KEY, name);
    const toggle = document.getElementById('themeToggle');
    if (toggle) {
      toggle.setAttribute('aria-pressed', String(name === 'light'));
      toggle.textContent = name === 'light' ? '☀' : '◐'; // sun / half-moon
      toggle.title = name === 'light' ? 'Switch to dark mode' : 'Switch to light mode';
    }
  }

  function init() {
    applyTheme(localStorage.getItem(THEME_KEY) || 'dark');
    applyAccent(localStorage.getItem(ACCENT_KEY) || DEFAULT_ACCENT);

    const toggle = document.getElementById('themeToggle');
    if (toggle) toggle.addEventListener('click', () => {
      applyTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
    });

    document.querySelectorAll('.accent-swatch').forEach((el) => {
      el.addEventListener('click', () => applyAccent(el.dataset.accent));
    });

    const custom = document.getElementById('accentCustom');
    if (custom) custom.addEventListener('input', (e) => applyAccent(e.target.value));
  }

  document.addEventListener('DOMContentLoaded', init);
})();
