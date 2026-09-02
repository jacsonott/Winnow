/* The launch animation: winnowing, which is what the app is named for.

   Grain and chaff are tossed together from the left. The chaff — DFIR
   noise, event ids, ports, process names — is light enough that the wind
   carries it clean off the right edge. The grain is heavy: its arc breaks
   early and it drops out of the stream onto its place in the wordmark.
   Mid-flight the two are indistinguishable, which is the point; the
   separation is the whole idea of the tool.

   Three things it has to respect, because a splash screen that ignores
   any of them is an obstacle rather than a moment:

   - **Skippable.** Any key, click or scroll cuts to the settled wordmark
     and moves on. An analyst opening their fifth case of the day should
     never be waiting on us.
   - **Disable-able.** Settings → Appearance turns it off entirely, and
     `prefers-reduced-motion` skips straight to the final frame without
     being asked.
   - **Themed.** It reads the same appearance the rest of the app does, so
     a light-mode install gets ink-on-parchment rather than a black
     rectangle. Colours come from the Harvest palette either way.

   Deliberately standalone: it draws to its own canvas and imports nothing
   from the app, so it can run before the case list has loaded and can't
   be what breaks startup. */

import { $ } from './core.js';

const TOKENS = [
  '4624', '4688', 'TCP', 'SYN', 'svc.exe', '0x1A3F', '445', 'RDP', '::1', '8.8.8.8',
  'SHA1', 'GET /', 'LSASS', 'WMI', 'usn', '.evtx', '3389', 'regkey', 'ACK', 'NTFS',
  'A0F3', 'PID', '80', 'ESTAB', 'pwsh', 'curl', 'ttl=64', '5985', 'MFT', 'cmd.exe',
];

/* The animation wears whatever skin and mode the app is currently in,
   read live from the document's own custom properties rather than a
   palette of its own. Launching into Phosphor should not flash a wheat
   field first — the splash is the app's front door, not a separate brand.

   --ink is the app's deepest surface, --accent the colour it uses to mean
   "this matters", --dim its subordinate text. That maps onto background,
   grain and chaff exactly, in every skin, in both modes, including a
   custom accent the analyst picked themselves. */
/* The brand mark's three bars (static/icons/winnow-mark.svg), in unit
   coordinates — grain kept, chaff fading. Drawn as dots beside the
   wordmark so the logo and the file icon are visibly the same mark.
   The colors are the icon's own, not theme tokens: this is the brand,
   identical in every skin. */
export const MARK_BARS = [
  { x: 56 / 512, y: 44 / 512, w: 125 / 512, h: 424 / 512, color: [184, 132, 58] },
  { x: 237 / 512, y: 80 / 512, w: 94 / 512, h: 351 / 512, color: [138, 108, 51] },
  { x: 388 / 512, y: 117 / 512, w: 68 / 512, h: 279 / 512, color: [195, 201, 209] },
];

/* The mark's INK bounds inside its unit box — the bars span x 56..456,
   y 44..468 of 512. Alignment must use these, not the box: sizing the
   box to the text made the bars tower over the letters (the tallest bar
   is 83% of the box) and the box's empty margins padded the gap. */
export const MARK_INK = { x0: 56 / 512, x1: 456 / 512, y0: 44 / 512, y1: 468 / 512 };

function barAt(nx, ny) {
  for (const b of MARK_BARS) {
    if (nx >= b.x && nx <= b.x + b.w && ny >= b.y && ny <= b.y + b.h) return b;
  }
  return null;
}

function readPalette() {
  const cs = getComputedStyle(document.documentElement);
  const tok = (name, fallback) => (cs.getPropertyValue(name) || '').trim() || fallback;
  const bg = tok('--ink', '#0d0b08');
  return {
    bg,
    ink: tok('--text', '#cfc8b8'),
    chaff: toRgb(tok('--dim', '#8a8172')),
    // Each skin's light mode already darkens its own accent to stay legible
    // on a pale surface, so this needs no adjustment of its own.
    grain: toRgb(tok('--accent', '#e0a94a')),
  };
}

/* '#rgb', '#rrggbb' and 'rgb(r, g, b)' — the three forms a custom property
   can hold once a browser has resolved it. */
function toRgb(v) {
  if (v.startsWith('rgb')) {
    const n = v.slice(v.indexOf('(') + 1, v.indexOf(')')).split(',');
    return [parseInt(n[0], 10) || 0, parseInt(n[1], 10) || 0, parseInt(n[2], 10) || 0];
  }
  let h = v.replace('#', '');
  if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
  if (h.length < 6) return [128, 128, 128];
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}


const LAUNCH_START = 8;
const GRAIN_GRAVITY = 0.085;
const CHAFF_GRAVITY = 0.018;
const NOISE_FONT = '10px ui-monospace, monospace';
/* How long the completed wordmark stays up. Any key or click cuts it
   short, so this is a ceiling on the patient case, not a toll on anyone. */
const SETTLE_HOLD_MS = 2200;

const rand = (a, b) => a + Math.random() * (b - a);
const pick = (a) => a[(Math.random() * a.length) | 0];
const lerp = (a, b, t) => a + (b - a) * t;

let raf = null;
let onDone = null;

/* The settled wordmark, drawn once and static — the same dot field the
   animation ends on, so the home screen carries the mark the launch just
   assembled rather than a differently-shaped piece of text.

   Sampled rather than drawn as a font: the dots ARE the idea (grain,
   separated out), and rendering "WINNOW" in a bold monospace would be a
   picture of the wrong thing. */
export function drawWordmark(canvas, { text = 'WINNOW', color, fontSize = 44 } = {}) {
  const DPR = Math.min(window.devicePixelRatio || 1, 2);
  const pad = Math.round(fontSize * 0.25);
  const off = document.createElement('canvas');
  const octx = off.getContext('2d');
  const font = `700 ${fontSize}px ui-monospace, "JetBrains Mono", Menlo, monospace`;
  octx.font = font;
  const w = Math.ceil(octx.measureText(text).width) + pad * 2;
  const h = Math.ceil(fontSize * 1.35);
  off.width = w;
  off.height = h;
  const o2 = off.getContext('2d');
  o2.font = font;
  o2.fillStyle = '#fff';
  o2.textAlign = 'center';
  o2.textBaseline = 'middle';
  o2.fillText(text, w / 2, h / 2);

  // The three-bar brand mark leads the word, in the icon's own colors —
  // the same dots, so mark and word read as one object. Sized and
  // positioned by INK, not boxes: the tallest bar's ink matches the
  // letters' ink height, their vertical centers coincide, and the gap
  // is measured from the bars' last pixel to the letters' first.
  const tm = o2.measureText(text);
  const inkH = (tm.actualBoundingBoxAscent + tm.actualBoundingBoxDescent) || fontSize * 0.72;
  const inkCY = h / 2 + ((tm.actualBoundingBoxDescent - tm.actualBoundingBoxAscent) / 2 || 0);
  const markBox = inkH / (MARK_INK.y1 - MARK_INK.y0);
  const markW = markBox * (MARK_INK.x1 - MARK_INK.x0);
  const markX = -markBox * MARK_INK.x0;                       // ink starts at 0
  const markY = inkCY - inkH / 2 - markBox * MARK_INK.y0;     // ink centers on the letters
  const gap = Math.round(fontSize * 0.3);
  const total = Math.ceil(markW + gap + w);

  canvas.width = total * DPR;
  canvas.height = h * DPR;
  canvas.style.width = total + 'px';
  canvas.style.height = h + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  ctx.clearRect(0, 0, total, h);

  const stride = Math.max(2, Math.round(fontSize / 22));
  const img = o2.getImageData(0, 0, w, h).data;
  const r = Math.max(0.9, stride * 0.42);
  for (let y = 0; y < markBox; y += stride) {
    for (let x = 0; x < markBox; x += stride) {
      const b = barAt(x / markBox, y / markBox);
      if (!b) continue;
      ctx.fillStyle = `rgb(${b.color.join(',')})`;
      ctx.beginPath();
      ctx.arc(markX + x, markY + y, r, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  ctx.fillStyle = color;
  for (let y = 0; y < h; y += stride) {
    for (let x = 0; x < w; x += stride) {
      if (img[(y * w + x) * 4 + 3] > 128) {
        ctx.beginPath();
        ctx.arc(markW + gap + x, y, r, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }
  return { width: total, height: h };
}

/* Why the last launch did or didn't play, for Settings → Launch animation.
   The reports this exists for were "it's enabled but I never see it" —
   and the two silent reasons (the OS asking for reduced motion; the
   focus click on a freshly opened window counting as a skip) leave no
   trace anywhere the analyst looks. Kept in localStorage (per browser,
   like the setting itself). */
export const SPLASH_LAST_KEY = 'winnow.splash.last';
export function recordSplash(result, reason) {
  try { localStorage.setItem(SPLASH_LAST_KEY, JSON.stringify({ result, reason, at: Date.now() })); } catch { /* full/blocked */ }
}
export function lastSplash() {
  try { return JSON.parse(localStorage.getItem(SPLASH_LAST_KEY) || 'null'); } catch { return null; }
}

export function reducedMotion() {
  // Plain call, not `matchMedia?.()` — esprima (ES2017) can't parse an
  // optional CALL, and tests/test_static_syntax.py is what stands between
  // a syntax slip and a blank app.
  const mm = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)');
  return !!(mm && mm.matches);
}

/* Three states, not two. `false` is off. `'always'` — what the Settings
   checkbox writes when the analyst ticks it themselves — plays regardless
   of the OS hint. Anything else (the default `true`, or unset) is
   "on, but honour reduced-motion": Windows reports reduced motion whenever
   Animation effects are off, which performance policies and VM images do
   as a matter of course, so this default alone had the animation quietly
   never playing on exactly the machines this tool lives on. */
export function splashEnabled(appearance) {
  if (appearance && appearance.splash === false) { recordSplash('skipped', 'off in Settings'); return false; }
  if (appearance && appearance.splash === 'always') return true;
  if (reducedMotion()) { recordSplash('skipped', 'system asks for reduced motion'); return false; }
  return true;
}

/* Runs the animation over the whole viewport and resolves when it's done
   or skipped. Always resolves — a splash that can hang is a splash that
   can stop the app from ever appearing. */
export function runSplash() {
  return new Promise((resolve) => {
    const root = $('splash');
    const canvas = $('splashCanvas');
    if (!root || !canvas || !canvas.getContext) { resolve(); return; }

    const colors = readPalette();
    root.hidden = false;
    root.style.background = colors.bg;
    $('splashTagline').style.color = colors.ink;

    let finished = false;
    const startedAt = performance.now();
    const finish = (reason) => {
      if (finished) return;
      finished = true;
      recordSplash(reason ? 'skipped' : 'played', reason || '');
      if (raf) { cancelAnimationFrame(raf); raf = null; }
      window.removeEventListener('keydown', skip, true);
      window.removeEventListener('mousedown', skip, true);
      window.removeEventListener('wheel', skip, true);
      window.removeEventListener('touchstart', skip, true);
      root.classList.add('splash-out');
      // Matches the CSS fade; the app is already behind it by then.
      setTimeout(() => { root.hidden = true; root.classList.remove('splash-out'); resolve(); }, 420);
    };
    // A skip inside the first moments isn't a choice — it's the click that
    // focused a just-opened app window, or scroll inertia carried in from
    // the previous one. Real impatience arrives later.
    const SKIP_GRACE_MS = 700;
    const skip = (e) => {
      if (performance.now() - startedAt < SKIP_GRACE_MS) return;
      finish(`skipped by ${e && e.type ? e.type : 'input'}`);
    };
    onDone = () => finish('ended by the app');

    window.addEventListener('keydown', skip, true);
    window.addEventListener('mousedown', skip, true);
    window.addEventListener('wheel', skip, true, { passive: true });
    window.addEventListener('touchstart', skip, true, { passive: true });

    start(canvas, colors, () => {
      // Hold on the finished wordmark before handing over. The grain
      // settling is the payoff, and cutting away the moment the last one
      // lands throws it away — the eye needs time to read WINNOW as a word
      // rather than as the debris it just watched arrive. Skippable
      // throughout, so this costs an impatient analyst nothing.
      setTimeout(() => finish(), SETTLE_HOLD_MS);
    });
  });
}

/* Skip from outside — used when the app is ready before the animation is,
   so a slow case list never leaves someone watching grain settle. */
export function endSplash() { if (onDone) onDone(); }

function start(canvas, colors, done) {
  const ctx = canvas.getContext('2d');
  const DPR = Math.min(window.devicePixelRatio || 1, 2);
  let W = 0, H = 0;

  function resize() {
    W = window.innerWidth;
    H = window.innerHeight;
    canvas.width = W * DPR;
    canvas.height = H * DPR;
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  }
  resize();

  // Sample the mark + wordmark into a field of landing spots. The grain
  // settles into the same composite the case menu shows: the three-bar
  // brand mark leading the word, bar points carrying the icon's own
  // colors (see MARK_BARS) while the letters take the skin's accent.
  function computeTargets() {
    const off = document.createElement('canvas');
    off.width = W; off.height = H;
    const octx = off.getContext('2d');
    const fontSize = Math.max(48, Math.min(W * 0.13, 190));
    octx.fillStyle = '#fff';
    octx.textAlign = 'left';
    octx.textBaseline = 'middle';
    octx.font = `700 ${fontSize}px ui-monospace, "JetBrains Mono", Menlo, monospace`;
    // Same ink-based layout as drawWordmark: the tallest bar's ink
    // matches the letters' ink height, centers coincide, and the gap is
    // ink-to-ink — box-based sizing left the bars towering over the
    // letters with a padded gap (they visibly disagreed on screen).
    const tm = octx.measureText('WINNOW');
    const inkH = (tm.actualBoundingBoxAscent + tm.actualBoundingBoxDescent) || fontSize * 0.72;
    const inkCY = H * 0.44 + ((tm.actualBoundingBoxDescent - tm.actualBoundingBoxAscent) / 2 || 0);
    const markBox = inkH / (MARK_INK.y1 - MARK_INK.y0);
    const markW = markBox * (MARK_INK.x1 - MARK_INK.x0);
    const gap = fontSize * 0.3;
    const left = (W - (markW + gap + tm.width)) / 2;
    octx.fillText('WINNOW', left + markW + gap, H * 0.44);
    const stride = Math.max(3, Math.round(fontSize / 26));
    const img = octx.getImageData(0, 0, W, H).data;
    const pts = [];
    for (let y = 0; y < H; y += stride) {
      for (let x = 0; x < W; x += stride) {
        if (img[(y * W + x) * 4 + 3] > 128) pts.push({ x, y });
      }
    }
    const markX = left - markBox * MARK_INK.x0;
    const markTop = inkCY - inkH / 2 - markBox * MARK_INK.y0;
    for (let y = 0; y < markBox; y += stride) {
      for (let x = 0; x < markBox; x += stride) {
        const b = barAt(x / markBox, y / markBox);
        if (b) pts.push({ x: markX + x, y: markTop + y, rgb: b.color });
      }
    }
    for (let i = pts.length - 1; i > 0; i--) {
      const j = (Math.random() * (i + 1)) | 0;
      [pts[i], pts[j]] = [pts[j], pts[i]];
    }
    return pts.length > 780 ? pts.slice(0, 780) : pts;
  }

  const targets = computeTargets();

  const grains = targets.map((target) => {
    const startX = -rand(30, 130);
    const startY = H * rand(0.52, 0.72);
    // Thrown just hard enough that the arc peaks above the landing spot, so
    // the last thing a grain does is fall ONTO the word rather than into it.
    const apexMargin = rand(30, 80) + (target.x / W) * 60;
    const drop = Math.max(30, startY - target.y + apexMargin);
    const vy0 = -Math.sqrt(2 * GRAIN_GRAVITY * drop);
    const flightFrames = ((-vy0 / GRAIN_GRAVITY) + rand(6, 16)) | 0;
    return {
      x: startX, y: startY,
      vx: (target.x - startX) * rand(0.91, 0.96) / flightFrames,
      vy: vy0,
      target, token: pick(TOKENS),
      launchFrame: (LAUNCH_START + rand(0, 45)) | 0,
      flightFrames,
      stiff: rand(0.008, 0.014), drag: rand(0.16, 0.22), maxFall: rand(1.6, 2.4),
      homeAge: 0, age: 0, phase: 'wait', r: rand(1.1, 2.1),
    };
  });

  let chaff = [];
  const chaffCount = Math.min(1100, Math.round(targets.length * 1.5));
  for (let i = 0; i < chaffCount; i++) {
    chaff.push({
      x: -rand(30, 160), y: H * rand(0.5, 0.74),
      vx: rand(2.8, 6.5), vy: -rand(2.6, 4.8),
      windAccel: rand(0.015, 0.04),
      launched: false,
      launchFrame: (LAUNCH_START + rand(0, 110)) | 0,
      fadeDelay: (170 + Math.random() * 80) | 0,
      age: 0, alpha: 1, token: pick(TOKENS),
    });
  }

  const CHAFF_RGB = `rgb(${colors.chaff.join(',')})`;
  let frame = 0;
  let settledOnce = false;

  function step() {
    ctx.clearRect(0, 0, W, H);
    frame++;
    ctx.font = NOISE_FONT;
    ctx.textAlign = 'center';
    ctx.fillStyle = CHAFF_RGB;

    for (let i = chaff.length - 1; i >= 0; i--) {
      const c = chaff[i];
      if (!c.launched) {
        if (frame >= c.launchFrame) c.launched = true;
        else continue;
      }
      c.age++;
      c.vy += CHAFF_GRAVITY + (Math.random() - 0.5) * 0.02;
      c.vx += c.windAccel;
      c.x += c.vx;
      c.y += c.vy;
      if (c.age > c.fadeDelay) c.alpha -= 0.02;
      if (Math.random() < 0.02) c.token = pick(TOKENS);
      if (c.alpha <= 0 || c.x > W + 140 || c.y < -80 || c.y > H + 60) { chaff.splice(i, 1); continue; }
      ctx.globalAlpha = Math.max(c.alpha, 0);
      ctx.fillText(c.token, c.x, c.y);
    }
    ctx.globalAlpha = 1;

    let settled = 0;
    for (const p of grains) {
      if (p.phase === 'wait') {
        if (frame >= p.launchFrame) p.phase = 'fly';
        else continue;
      }
      p.age++;
      if (p.phase === 'fly') {
        p.vy += GRAIN_GRAVITY;
        p.x += p.vx;
        p.y += p.vy;
        if (Math.random() < 0.02) p.token = pick(TOKENS);
        if (p.age > p.flightFrames || (p.vy > 0 && p.y > p.target.y)) p.phase = 'home';
      } else if (p.phase === 'home') {
        p.homeAge++;
        // Damped spring with a capped pull: far from its spot a grain feels
        // a steady tug, never a distance-proportional yank, so it drifts in
        // rather than snapping.
        let ax = (p.target.x - p.x) * p.stiff;
        let ay = (p.target.y - p.y) * p.stiff;
        const MAX = 0.35;
        ax = Math.max(-MAX, Math.min(MAX, ax));
        ay = Math.max(-MAX, Math.min(MAX, ay));
        p.vx = (p.vx + ax) * (1 - p.drag);
        p.vy = (p.vy + ay) * (1 - p.drag);
        if (p.vy > p.maxFall) p.vy = p.maxFall;
        p.x += p.vx;
        p.y += p.vy;
        const dx = p.target.x - p.x, dy = p.target.y - p.y;
        if ((dx * dx + dy * dy < 2.25 && Math.abs(p.vx) + Math.abs(p.vy) < 0.5) || p.homeAge > 240) {
          p.x = p.target.x; p.y = p.target.y; p.phase = 'locked';
        }
      } else {
        settled++;
      }

      if (p.phase === 'fly') {
        ctx.fillStyle = CHAFF_RGB;
        ctx.fillText(p.token, p.x, p.y);
      } else {
        const mix = p.phase === 'locked' ? 1 : Math.min(1, p.homeAge / 45);
        // The token dissolves as the grain condenses, rather than swapping
        // in a single frame.
        if (mix < 0.5) {
          ctx.globalAlpha = 1 - mix * 2;
          ctx.fillStyle = CHAFF_RGB;
          ctx.fillText(p.token, p.x, p.y);
        }
        const to = p.target.rgb || colors.grain;   // bar points keep the icon's colors
        const rr = Math.round(lerp(colors.chaff[0], to[0], mix));
        const gg = Math.round(lerp(colors.chaff[1], to[1], mix));
        const bb = Math.round(lerp(colors.chaff[2], to[2], mix));
        ctx.globalAlpha = 0.3 + 0.7 * Math.min(1, mix * 1.6);
        ctx.fillStyle = `rgb(${rr},${gg},${bb})`;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r * (0.55 + 0.45 * Math.min(1, mix * 1.4)), 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = 1;
      }
    }

    if (!settledOnce && grains.length && settled === grains.length) {
      settledOnce = true;
      $('splashTagline').classList.add('visible');
      done();
    }
    if (chaff.length || settled < grains.length) raf = requestAnimationFrame(step);
    else raf = null;
  }

  raf = requestAnimationFrame(step);
}
