/* The detail pane, its notes field, and the JSON/XML pretty-printing in it.

   Split out of the former single static/app.js — see CLAUDE.md. */
import { $, el, post, toast } from './core.js';
import { addExtractedColumn, openFlattenModal, quotePredicate } from './derived.js';
import { ellipsize, parseFilter, setColumnFilter, setSearchMode } from './filters.js';
import { clearPageCache, render, rowAt } from './grid.js';
import { writeClipboardText } from './grouping.js';
import { syncSearchExpansion } from './search.js';
import { clearAllFilters, sourceLabel } from './sources.js';
import { S } from './state.js';
import { updateFiltersButton } from './timeframe.js';
import { displayCell } from './tsformat.js';
import { contextMenu } from './ui.js';
import { rebuildView } from './view.js';

/* --------------------------------------------------------------- detail */

/* Nested JSON/XML field values get pretty-printed + syntax colored in the
   detail pane (grid cells stay plain — single-line, truncated, virtualized).
   Everything here builds DOM nodes directly (never innerHTML) since field
   values are untrusted forensic data that can contain HTML-looking text. */

export function tryParseJSON(v) {
  const s = v.trim();
  if (!s || (s[0] !== '{' && s[0] !== '[')) return null;
  try { return JSON.parse(s); } catch { return null; }
}

export function looksLikeXml(v) {
  const s = v.trim();
  return s.startsWith('<') && s.endsWith('>') && /<\/?[a-zA-Z_][\w:.-]*[^>]*>/.test(s);
}

export function prettyXml(xml) {
  // Heuristic reflow, not a real parser: forensic XML fragments are often
  // malformed/truncated, so this must degrade gracefully rather than throw.
  const tags = xml.replace(/>\s*</g, '><').split(/(?=<)/).filter(Boolean);
  let out = '', depth = 0;
  for (const tag of tags) {
    const isClosing = tag.startsWith('</');
    const isVoid = /\/>$/.test(tag) || tag.startsWith('<?') || tag.startsWith('<!');
    if (isClosing) depth = Math.max(0, depth - 1);
    out += '  '.repeat(depth) + tag + '\n';
    if (!isClosing && !isVoid) depth++;
  }
  return out.trim();
}

/* The unhighlighted-but-readable fallback for XML the browser's own
   parser rejects — truncated fragments, mismatched tags, the shapes real
   evidence actually contains. Regex-based on purpose: it has to degrade
   rather than throw, which is exactly what a parser can't do. Nothing it
   emits carries a path, because there is no trustworthy structure to
   address in a document that didn't parse. */
export function appendXmlHighlighted(container, xmlText) {
  const tagRe = /<(\/?)([a-zA-Z_][\w:.-]*)((?:\s+[a-zA-Z_][\w:.-]*\s*=\s*"[^"]*")*)\s*(\/?)>/g;
  const attrRe = /([a-zA-Z_][\w:.-]*)(\s*=\s*)("[^"]*")/g;
  let last = 0, m;
  while ((m = tagRe.exec(xmlText))) {
    if (m.index > last) container.append(xmlText.slice(last, m.index));
    container.append('<' + m[1]);
    container.append(el('span', 'xtok-tag', m[2]));
    const attrs = m[3];
    if (attrs) {
      let aLast = 0, am;
      attrRe.lastIndex = 0;
      while ((am = attrRe.exec(attrs))) {
        if (am.index > aLast) container.append(attrs.slice(aLast, am.index));
        container.append(el('span', 'xtok-attr', am[1]));
        container.append(am[2]);
        container.append(el('span', 'xtok-attrval', am[3]));
        aLast = am.index + am[0].length;
      }
      container.append(attrs.slice(aLast));
    }
    container.append(m[4] + '>');
    last = m.index + m[0].length;
  }
  container.append(xmlText.slice(last));
}

/* ------------------ path-aware pretty-printing

   The pretty-printer builds from the *parsed* document rather than
   regexing over its serialized text, because every node it emits carries
   the path that addresses it (`data-path`) on the span. That path is what
   makes "right-click a field → add it as a column" a single click instead
   of a syntax the analyst has to learn and type: the same string the
   backend's json_field/xml_field operations take is already sitting on the
   node under the pointer.

   Everything appends DOM nodes and never innerHTML — field values are
   untrusted forensic data that routinely contains HTML-looking text. */

export function jsonPathStep(base, seg) {
  if (typeof seg === 'number') return `${base}[${seg}]`;
  return /^[^.[\]"']+$/.test(seg) ? `${base}.${seg}` : `${base}["${String(seg).replace(/(["\\])/g, '\\$1')}"]`;
}

/* A list of Name/Value pairs is addressed by name, not by index:
   `ExtendedProperties[Name='UserAgent'].Value`, which becomes a column
   called UserAgent. The twin of structparse._kv_shape, and it has to
   agree with it name for name — a path offered here on a list the backend
   reads positionally still extracts (the predicate walk doesn't care how
   the path was found), but the column it names has to be the one the
   Flatten picker would have offered. The rules and the reasons for them
   are in the comment above structparse._KV_NAME_KEYS: a Name or Key in
   every entry, all different strings, and something value-like beside at
   least one of them. */
const KV_NAME_KEYS = [/^@?name$/i, /^@?key$/i];

function kvNameOk(n) {
  return typeof n === 'string' && n.length > 0 && n.length <= 120
    && /[^ \t\n\r\x0b\x0c]/.test(n) && !/[\x00-\x1f\x7f\ud800-\udfff]/.test(n);
}

function kvValueLike(k) {
  const l = k.toLowerCase().replace(/^[@#]+/, '');
  return l === 'text' || l.includes('value');
}

function isDict(x) {
  return x !== null && typeof x === 'object' && !Array.isArray(x);
}

export function jsonKvShape(items) {
  if (!items.length || !items.every((x) => isDict(x) && Object.keys(x).length)) return null;
  for (const rx of KV_NAME_KEYS) {
    const key = Object.keys(items[0]).find((k) => rx.test(k));
    if (key === undefined) continue;
    const names = items.map((x) => (Object.prototype.hasOwnProperty.call(x, key) ? x[key] : undefined));
    if (!names.every(kvNameOk) || new Set(names).size !== names.length) continue;
    if (items.some((x) => Object.keys(x).some((k) => k !== key && kvValueLike(k)))) return key;
  }
  return null;
}

export function jsonKvStep(base, key, name) {
  return `${base}[${key}=${quotePredicate(name)}]`;
}

/* What the Name in a Name/Value entry stands for. The Name restates the
   predicate that selected its entry, so it isn't a field of its own; when
   the entry holds one thing beside it, the Name means that thing, and
   right-clicking "UserAgent" adds the UserAgent column the way
   right-clicking a key adds its value. Beside two (OldValue, NewValue) it
   would be a guess, so it means nothing. */
function kvNameTarget(keys, nameKey, entryPath, leafOf) {
  const others = keys.filter((k) => k !== nameKey);
  if (others.length !== 1) return null;
  return { path: jsonPathStep(entryPath, others[0]), value: leafOf(others[0]) };
}

export function jsonTokenClass(v) {
  if (v === null) return 'jtok-null';
  if (typeof v === 'boolean') return 'jtok-bool';
  if (typeof v === 'number') return 'jtok-num';
  return 'jtok-str';
}

/* A span the detail menu can act on: the path that addresses it, and the
   raw (unformatted) value it holds, so "filter to this" filters to what's
   in the data rather than to its pretty-printed rendering. */
export function fieldNode(cls, text, path, value, kind) {
  const n = el('span', cls, text);
  if (path) {
    n.dataset.path = path;
    n.dataset.structKind = kind;
    if (value !== undefined) n.dataset.value = value;
    n.classList.add('struct-node');
  }
  return n;
}

export function appendJsonNodes(container, value, path, indent, nameKey) {
  const pad = '  '.repeat(indent);
  const padIn = '  '.repeat(indent + 1);
  if (Array.isArray(value)) {
    if (!value.length) { container.append('[]'); return; }
    const kv = jsonKvShape(value);
    container.append('[\n');
    value.forEach((v, i) => {
      container.append(padIn);
      if (kv) appendJsonNodes(container, v, jsonKvStep(path, kv, v[kv]), indent + 1, kv);
      else appendJsonNodes(container, v, jsonPathStep(path, i), indent + 1);
      container.append(i < value.length - 1 ? ',\n' : '\n');
    });
    container.append(pad + ']');
    return;
  }
  if (value && typeof value === 'object') {
    const keys = Object.keys(value);
    if (!keys.length) { container.append('{}'); return; }
    const named = nameKey ? kvNameTarget(keys, nameKey, path, (k) => {
      const x = value[k];
      return x && typeof x === 'object' ? undefined : jsonLeafText(x);
    }) : null;
    container.append('{\n');
    keys.forEach((k, i) => {
      container.append(padIn);
      if (k === nameKey) {
        const np = named ? named.path : null;
        const nv = named ? named.value : undefined;
        container.append(fieldNode('jtok-key', JSON.stringify(k), np, nv, 'json'), ': ',
          fieldNode('jtok-str', JSON.stringify(value[k]), np, nv, 'json'));
        container.append(i < keys.length - 1 ? ',\n' : '\n');
        return;
      }
      const kp = jsonPathStep(path, k);
      const leaf = value[k];
      const scalar = !leaf || typeof leaf !== 'object';
      // The key carries the path too, so clicking either half of
      // `"user": "jacson"` means the same field.
      container.append(fieldNode('jtok-key', JSON.stringify(k), kp,
        scalar ? jsonLeafText(leaf) : undefined, 'json'));
      container.append(': ');
      appendJsonNodes(container, leaf, kp, indent + 1);
      container.append(i < keys.length - 1 ? ',\n' : '\n');
    });
    container.append(pad + '}');
    return;
  }
  container.append(fieldNode(jsonTokenClass(value), JSON.stringify(value), path,
    jsonLeafText(value), 'json'));
}

/* Mirrors structparse._leaf_text — what the extracted column would hold,
   which is what the filter/copy actions should act on. */
export function jsonLeafText(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

/* XML is rendered from a real parse when the browser can manage one, and
   falls back to the old heuristic reflow when it can't — forensic XML
   fragments are often malformed or truncated, and degrading to
   "unhighlighted but readable" beats showing nothing. Only the parsed path
   carries `data-path`; there is nothing trustworthy to address in a
   document that didn't parse. */
export function parseXmlDoc(text) {
  const body = text.trim();
  // Same guard as structparse.load_xml: a DOCTYPE is refused rather than
  // parsed. DOMParser won't expand external entities, but keeping the two
  // sides on the same rule means the detail pane never offers a path the
  // backend would then decline to extract.
  if (/<!DOCTYPE/i.test(body)) return null;
  const parse = (t) => {
    const doc = new DOMParser().parseFromString(t, 'application/xml');
    return doc.querySelector('parsererror') ? null : doc.documentElement;
  };
  return parse(body)
    || parse(`<winnow-fragment>${body.replace(/^<\?xml[^>]*\?>/, '')}</winnow-fragment>`);
}

export function xmlLocal(name) { return name.includes(':') ? name.split(':').pop() : name; }

export const XML_ID_ATTRS = ['name', 'key', 'id'];

/* The frontend twin of structparse._sibling_selectors — repeated elements
   that carry an identifying attribute are addressed by it rather than by
   position, so an EVTX `<Data Name="LogonType">` offers a path that means
   the same thing in every row. Kept in step with the backend: a path this
   produces has to be one xml_field can resolve. */
export function xmlSiblingSelectors(kids) {
  const groups = new Map();
  kids.forEach((c, i) => {
    const tag = xmlLocal(c.tagName);
    if (!groups.has(tag)) groups.set(tag, []);
    groups.get(tag).push(i);
  });
  const sel = new Array(kids.length).fill(0);
  for (const idxs of groups.values()) {
    let chosen = null;
    if (idxs.length > 1) {
      for (const want of XML_ID_ATTRS) {
        const real = idxs.map((i) => [...kids[i].attributes].find((a) => xmlLocal(a.name).toLowerCase() === want));
        const vals = real.map((a) => (a ? a.value : null));
        if (vals.every((v) => v !== null) && new Set(vals).size === vals.length) {
          chosen = { attr: xmlLocal(real[0].name), vals };
          break;
        }
      }
    }
    idxs.forEach((i, slot) => { sel[i] = chosen ? [chosen.attr, chosen.vals[slot]] : slot; });
  }
  return sel;
}

export function xmlStep(tag, sel) {
  if (Array.isArray(sel)) return `${tag}[@${sel[0]}='${sel[1]}']`;
  return sel === 0 ? tag : `${tag}[${sel}]`;
}

export function appendXmlNodes(container, node, path, indent) {
  const pad = '  '.repeat(indent);
  const tag = xmlLocal(node.tagName);
  const selector = path.endsWith(']') && /\[@[\w:.-]+='[^']*'\]$/.test(path);
  const kids = [...node.children];
  // A leaf element's text is its value, and the tag is what most people
  // aim at — so the opening tag carries it too, rather than only the text
  // run and the closing tag. Computed before anything is emitted because
  // the opening tag is written first.
  const leafText = kids.length ? undefined : (node.textContent || '').trim();
  container.append(pad + '<');
  container.append(fieldNode('xtok-tag', tag, path, leafText, 'xml'));
  for (const a of node.attributes) {
    const an = xmlLocal(a.name);
    container.append(' ');
    // An attribute that selected this element restates its own predicate;
    // it still renders, it just isn't offered as a separate field.
    const apath = selector && path.includes(`@${an}='${a.value}'`) ? null : `${path}@${an}`;
    container.append(fieldNode('xtok-attr', an, apath, a.value, 'xml'));
    container.append('=');
    container.append(fieldNode('xtok-attrval', `"${a.value}"`, apath, a.value, 'xml'));
  }
  if (!kids.length) {
    if (!leafText) { container.append('/>\n'); return; }
    container.append('>');
    container.append(fieldNode('xtok-text', leafText, path, leafText, 'xml'));
    container.append('</');
    container.append(fieldNode('xtok-tag', tag, path, leafText, 'xml'));
    container.append('>\n');
    return;
  }
  container.append('>\n');
  const sel = xmlSiblingSelectors(kids);
  kids.forEach((child, i) => {
    appendXmlNodes(container, child, `${path ? path + '/' : ''}${xmlStep(xmlLocal(child.tagName), sel[i])}`, indent + 1);
  });
  container.append(pad + '</');
  container.append(fieldNode('xtok-tag', tag, path, undefined, 'xml'));
  container.append('>\n');
}

/* ------------------ documents JSON.parse refuses

   A value that isn't one strict JSON document can still hold one, or
   something built the same way:

   - Python's repr of a list or dict, `[{'Name': 'UserAgent', 'Value':
     'Mozilla/5.0'}]` — single quotes, None/True/False — which is what a
     pipeline that went through Python on its way to a CSV leaves behind;
   - JSON a lax exporter wrote: a raw newline or tab inside a string, an
     unescaped backslash in a Windows path, NaN, a trailing comma;
   - a document inside other text: a log line's `[INFO] {...}`, a wrapper
     (`[ctx {...}]`), a note after it, two run together.

   JSON.parse refuses every one, so the pane showed each as a single
   unbroken line. This parser only finds the structure (brackets, strings,
   separators and a short list of bare words) and every token is shown as
   the cell writes it, never decoded and re-encoded: text selected in the
   layout still matches what a contains-filter searches for, and a 64-bit
   integer keeps all its digits.

   A document that is the whole value carries paths, like strict JSON
   does: structparse.load_json falls back to the same grammar
   (structparse.load_loose, the twin of looseValue/looseContainer below,
   which has to accept exactly what they do), so "Add as a column" on one
   of its nodes reads the same field out of every row. Each node is
   decoded for that (looseDecode) the way load_loose decodes it, so the
   path holds `UserAgent` where the cell says `'UserAgent'`. A document
   inside other text carries none: the backend reads a cell only when all
   of it is one document, so a path there would fill the column with
   nothing on every row, the same reason the unparseable-XML fallback
   isn't addressable either. */

// A hostile value must not turn a cursor move into a hang. Every `[` or
// `{` is a candidate and a failed one is retried a character later, so a
// long run of `[` would cost its length times the nesting depth; the
// budget counts every character and token the parser looks at, across all
// candidates, and past it the rest of the value is shown as text.
export const LOOSE_SCAN_BUDGET = 2000000;
export const LOOSE_MAX_DEPTH = 256;
// The longest whole-value document structparse.load_loose will read, and
// so the longest one given paths here (its LOOSE_MAX_CHARS).
export const LOOSE_MAX_CHARS = 1000000;

const LOOSE_CLOSERS = { '[': ']', '{': '}', '(': ')' };
const LOOSE_WORDS = {
  true: 'jtok-bool', false: 'jtok-bool', True: 'jtok-bool', False: 'jtok-bool',
  null: 'jtok-null', None: 'jtok-null',
  NaN: 'jtok-num', Infinity: 'jtok-num', '-Infinity': 'jtok-num',
  nan: 'jtok-num', inf: 'jtok-num', '-inf': 'jtok-num',
};
const LOOSE_NUMBER = /[-+]?(?:0[xX][\da-fA-F_]+|(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][-+]?\d+)?j?)/y;
const LOOSE_WORD = /-?[A-Za-z_]\w*/y;
const LOOSE_STRING_PREFIX = /[rRbBuU]{0,2}(?=["'])/y;

function looseSkipSpace(p) {
  while (p.i < p.s.length && ' \t\r\n'.includes(p.s[p.i])) { p.i++; p.budget--; }
}

/* One value at p.i: { leaf: tokenClass, text } or a container. null when
   what's there isn't one, with p.i somewhere past it. */
function looseValue(p, depth) {
  looseSkipSpace(p);
  if (p.i >= p.s.length || --p.budget < 0) return null;
  if (LOOSE_CLOSERS[p.s[p.i]]) return depth < LOOSE_MAX_DEPTH ? looseContainer(p, depth + 1) : null;
  const start = p.i;
  LOOSE_STRING_PREFIX.lastIndex = start;
  if (LOOSE_STRING_PREFIX.test(p.s)) {
    const open = LOOSE_STRING_PREFIX.lastIndex;
    const quote = p.s[open];
    for (let j = open + 1; j < p.s.length && --p.budget >= 0; j++) {
      if (p.s[j] === '\\') j++;
      else if (p.s[j] === quote) { p.i = j + 1; return { leaf: 'jtok-str', text: p.s.slice(start, p.i) }; }
    }
    return null;
  }
  for (const [re, cls] of [[LOOSE_NUMBER, 'jtok-num'], [LOOSE_WORD, null]]) {
    re.lastIndex = start;
    const m = re.exec(p.s);
    if (!m || /\w/.test(p.s[re.lastIndex] || '')) continue;
    const tokenClass = cls || LOOSE_WORDS[m[0]];
    if (!tokenClass) return null;
    p.i = re.lastIndex;
    return { leaf: tokenClass, text: m[0] };
  }
  return null;
}

/* A list, tuple or set ({ open, close, items }) or a dict/object
   ({ open, close, entries: [[keyText, value, keyNode], ...] }), opening
   at p.i.
   A `{` is a dict if its first element is followed by `:`. */
function looseContainer(p, depth) {
  const open = p.s[p.i];
  const close = LOOSE_CLOSERS[open];
  p.i++;
  const items = [];
  const entries = [];
  for (;;) {
    looseSkipSpace(p);
    if (p.s[p.i] === close) { p.i++; break; }
    const keyStart = p.i;
    const value = looseValue(p, depth);
    if (!value) return null;
    const keyText = p.s.slice(keyStart, p.i);
    looseSkipSpace(p);
    if (open === '{' && !items.length && p.s[p.i] === ':') {
      p.i++;
      const entry = looseValue(p, depth);
      if (!entry) return null;
      entries.push([keyText, entry, value]);
      looseSkipSpace(p);
    } else if (entries.length) {
      return null;
    } else {
      items.push(value);
    }
    if (p.s[p.i] === ',') { p.i++; continue; }
    if (p.s[p.i] === close) { p.i++; break; }
    return null;
  }
  return entries.length ? { open, close, entries } : { open, close, items };
}

/* JSON.parse takes the `[0]` of `items[0]`, and `{}` and `[{}]` are
   documents too, but none of them is worth turning one line into five.
   Worth laying out: a dict with an entry, or a list holding a non-empty
   dict or list. */
export function looseWorthShowing(node) {
  if (node.entries) return true;
  return node.items.some((n) => !n.leaf && (n.entries || n.items.length));
}

/* Every document in `s`, in order and never overlapping, as
   { start, end, node }. A candidate that doesn't parse (the `[INFO]`, or a
   wrapper with words inside it) is passed over one character at a time,
   so the document inside a failed wrapper is still found. A `(` is never
   a candidate on its own, since prose is full of them, but a tuple inside
   a list or dict reads as one. */
export function findLooseDocuments(s) {
  const docs = [];
  const p = { s, i: 0, budget: LOOSE_SCAN_BUDGET };
  for (let i = 0; i < s.length && p.budget > 0; i++) {
    if (s[i] !== '[' && s[i] !== '{') continue;
    p.i = i;
    const node = looseContainer(p, 1);
    if (!node || !looseWorthShowing(node)) continue;
    docs.push({ start: i, end: p.i, node });
    i = p.i - 1;
  }
  return docs;
}

/* What load_loose makes of a node, set on it as `value` (and on a leaf,
   `leafText`: what the column would hold, structparse._leaf_text's
   answer). Strings are decoded as JSON and Python both decode them, and
   an escape neither knows stays as written; a number json.loads could
   have read becomes a number, any other (hex, `1_000`) stays its own
   text, and so is a string to the Name/Value rules, as it is there. */
const LOOSE_WORD_VALUES = {
  true: true, True: true, false: false, False: false, null: null, None: null,
  NaN: NaN, nan: NaN, Infinity: Infinity, inf: Infinity, '-Infinity': -Infinity, '-inf': -Infinity,
};
const LOOSE_ESCAPES = { '\\': '\\', "'": "'", '"': '"', '/': '/', n: '\n', r: '\r', t: '\t', b: '\b', f: '\f' };
const LOOSE_INT = /^-?(?:0|[1-9]\d*)$/;
const LOOSE_FLOAT = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][-+]?\d+)?$/;

export function looseString(text) {
  const prefix = /^[rRbBuU]*/.exec(text)[0];
  const body = text.slice(prefix.length + 1, -1);
  if (/r/i.test(prefix)) return body;
  return body.replace(/\\(?:u([0-9a-fA-F]{4})|U([0-9a-fA-F]{8})|x([0-9a-fA-F]{2})|([\s\S]))/g,
    (all, u, cp, x, c) => {
      if (u) return String.fromCharCode(parseInt(u, 16));
      if (cp) return parseInt(cp, 16) <= 0x10ffff ? String.fromCodePoint(parseInt(cp, 16)) : all;
      if (x) return String.fromCharCode(parseInt(x, 16));
      return Object.prototype.hasOwnProperty.call(LOOSE_ESCAPES, c) ? LOOSE_ESCAPES[c] : all;
    });
}

function looseLeafText(v) {
  if (typeof v === 'number' && !Number.isFinite(v)) return Number.isNaN(v) ? 'nan' : (v > 0 ? 'inf' : '-inf');
  return jsonLeafText(v);
}

// A key that isn't a string (`{1: 'a'}`, `{None: 0}`) is named by its own
// text, which is also what the pane shows.
function looseKey(keyText, keyNode) {
  return keyNode.leaf === 'jtok-str' ? looseString(keyNode.text) : keyText;
}

export function looseDecode(node) {
  if (node.leaf === 'jtok-str') {
    node.value = node.leafText = looseString(node.text);
  } else if (node.leaf && Object.prototype.hasOwnProperty.call(LOOSE_WORD_VALUES, node.text)) {
    node.value = LOOSE_WORD_VALUES[node.text];
    node.leafText = looseLeafText(node.value);
  } else if (node.leaf) {
    const t = node.text;
    // An integer keeps every digit: the column holds Python's int, not a
    // double that rounded it.
    if (LOOSE_INT.test(t)) { node.value = Number(t); node.leafText = t === '-0' ? '0' : t; }
    else if (LOOSE_FLOAT.test(t)) { node.value = Number(t); node.leafText = String(node.value); }
    else { node.value = node.leafText = t; }
  } else if (node.entries) {
    node.value = Object.create(null);
    node.byKey = Object.create(null);
    for (const [keyText, entry, keyNode] of node.entries) {
      const k = looseKey(keyText, keyNode);
      node.value[k] = looseDecode(entry);
      node.byKey[k] = entry;
    }
  } else if (node.open === '{' && !node.items.length) {
    node.value = Object.create(null);
    node.byKey = Object.create(null);
  } else {
    node.value = node.items.map(looseDecode);
  }
  return node.value;
}

/* Lays a document out as the cell writes it. With `path`, a whole-value
   document that looseDecode has been run over: every node carries its
   path, Name/Value lists by name as in appendJsonNodes. */
export function appendLooseNodes(container, node, indent, path, nameKey) {
  if (node.leaf) {
    container.append(path ? fieldNode(node.leaf, node.text, path, node.leafText, 'json')
                          : el('span', node.leaf, node.text));
    return;
  }
  const parts = node.entries || node.items;
  if (!parts.length) { container.append(node.open + node.close); return; }
  const pad = '  '.repeat(indent);
  const padIn = '  '.repeat(indent + 1);
  const kv = path && !node.entries ? jsonKvShape(node.value) : null;
  const named = path && nameKey && node.entries
    ? kvNameTarget(Object.keys(node.byKey), nameKey, path, (k) => node.byKey[k].leafText) : null;
  container.append(node.open + '\n');
  parts.forEach((part, i) => {
    container.append(padIn);
    if (node.entries) {
      const [keyText, entry, keyNode] = part;
      const k = path ? looseKey(keyText, keyNode) : null;
      if (path && k === nameKey && entry.leaf) {
        const np = named ? named.path : null;
        const nv = named ? named.value : undefined;
        container.append(fieldNode('jtok-key', keyText, np, nv, 'json'), ': ',
          fieldNode(entry.leaf, entry.text, np, nv, 'json'));
      } else {
        const kp = path ? jsonPathStep(path, k) : null;
        container.append(fieldNode('jtok-key', keyText, kp, entry.leaf ? entry.leafText : undefined, 'json'), ': ');
        appendLooseNodes(container, entry, indent + 1, kp);
      }
    } else if (kv) {
      appendLooseNodes(container, part, indent + 1, jsonKvStep(path, kv, part.value[kv]), kv);
    } else {
      appendLooseNodes(container, part, indent + 1, path ? jsonPathStep(path, i) : null);
    }
    // A one-element tuple keeps the comma that makes it a tuple.
    const more = i < parts.length - 1 || (node.open === '(' && parts.length === 1);
    container.append(more ? ',\n' : '\n');
  });
  container.append(pad + node.close);
}

export function renderDetailContent(v) {
  const json = tryParseJSON(v);
  if (json !== null && typeof json === 'object') {
    const pre = el('pre', 'detail-pretty');
    appendJsonNodes(pre, json, '$', 0);
    return pre;
  }
  if (looksLikeXml(v)) {
    const root = parseXmlDoc(v);
    if (root) {
      const pre = el('pre', 'detail-pretty');
      // A synthetic fragment wrapper is ours, not the document's — its
      // children are what the analyst actually has, and what paths are
      // written against.
      if (root.tagName === 'winnow-fragment') {
        const kids = [...root.children];
        const sel = xmlSiblingSelectors(kids);
        kids.forEach((c, i) => appendXmlNodes(pre, c, xmlStep(xmlLocal(c.tagName), sel[i]), 0));
      } else {
        appendXmlNodes(pre, root, xmlLocal(root.tagName), 0);
      }
      return pre;
    }
    try {
      // Unparseable: the old heuristic reflow, highlighted but not
      // addressable.
      const pre = el('pre', 'detail-pretty');
      appendXmlHighlighted(pre, prettyXml(v));
      return pre;
    } catch { /* malformed fragment — fall through to plain text */ }
  }
  const docs = findLooseDocuments(v);
  if (docs.length) {
    const pre = el('pre', 'detail-pretty');
    // The whole value, give or take the whitespace load_json strips, is
    // what the backend can read a path out of.
    const blank = /^[ \t\r\n]*$/;
    if (docs.length === 1 && blank.test(v.slice(0, docs[0].start)) && blank.test(v.slice(docs[0].end))
        && docs[0].end - docs[0].start <= LOOSE_MAX_CHARS) {
      looseDecode(docs[0].node);
      appendLooseNodes(pre, docs[0].node, 0, '$');
      return pre;
    }
    let at = 0;
    for (const d of docs) {
      pre.append(v.slice(at, d.start));
      appendLooseNodes(pre, d.node, 0);
      at = d.end;
    }
    pre.append(v.slice(at));
    return pre;
  }
  return document.createTextNode(v);
}

/* --------------------------------------- saying that a row can be opened

   The grid's one undiscoverable gesture. A single click selects a cell;
   the pane opens on double-click or the detail hotkey, and until now the
   only place either was written down was a comment in this file. So the
   hovered row carries an affordance (grid.js paints it, style.css hides
   it until hover), and one quiet line sits beside the row count until the
   analyst has opened a row — after which it never appears again, because
   a hint that outlives its lesson is just chrome.

   Local to the browser profile like the keymap and the detail dock: it is
   a fact about a person, not about the case, and it must not travel in a
   session export. Storage that throws (private mode, a full quota) counts
   as "already seen" rather than "show it forever" — the analyst it would
   nag every boot is the one we can never stop nagging. */
const ROW_OPEN_SEEN = 'winnow.rowOpenSeen';

function rowOpenSeen() {
  try { return localStorage.getItem(ROW_OPEN_SEEN) === 'seen'; } catch { return true; }
}

/* Every path that opens a row goes through showDetail, so marking it
   there is what makes the hint answer "has this been done", not "was this
   particular affordance clicked". */
export function noteRowOpened() {
  if (rowOpenSeen()) return;
  try { localStorage.setItem(ROW_OPEN_SEEN, 'seen'); } catch { /* nothing to remember it with */ }
  syncRowOpenHint();
}

/* Called wherever the stats line it sits next to is written (view.js's
   writeStatsLine) and wherever that line is cleared — with no table open
   there is no row to open, and the hint would be advice about an empty
   grid. */
export function syncRowOpenHint() {
  $('rowOpenHint').hidden = rowOpenSeen() || !S.view || S.sourceId == null;
}

/* One field's "copy this value", revealed on hover (style.css). "Copy
   row" was the only copy the pane offered, so lifting a single SID or
   command line out of forty fields meant selecting it by hand across a
   wrapped, pretty-printed block — or copying the row and deleting the
   rest. The button carries no value of its own: the text lives in the
   row object already, and duplicating a multi-KB payload into a dataset
   attribute for every field would put the whole row in the DOM twice. */
export function copyFieldButton(column) {
  const b = el('button', 'dfield-copy', '⧉');
  b.type = 'button';
  b.title = `Copy ${column}`;
  // Not a tab stop: a wide table puts forty of these between the pane's
  // buttons and the note box, and tabbing to the note is the keyboard
  // path that actually exists here.
  b.tabIndex = -1;
  return b;
}

/* The detail pane only force-opens on double-click (activateRow's plain
   single-click path deliberately never calls showDetail) or the toggleDetail
   hotkey. Once it's open, though, cursor movement — click, arrow keys,
   ctrl/cmd-click — should keep it in sync with whatever row is now current,
   which is what this gates. */
export function maybeShowDetail(pos) {
  if (!$('detail').hidden) showDetail(pos);
}

export function showDetail(pos) {
  const r = rowAt(pos);
  const d = $('detail');
  if (!r) { hideDetailPane(); return; }
  d.hidden = false;
  $('detailResize').hidden = false;
  // Line numbers restart in every table of a merge, so on a merge the
  // title says which table this line is from.
  const cur = S.sources.find((s) => s.id === S.sourceId);
  const from = cur && cur.is_merge ? S.sources.find((s) => s.id === r.source_id) : null;
  $('detailTitle').textContent = from ? `Line ${r.rid} · ${sourceLabel(from)}` : `Line ${r.rid}`;
  // Opening a row is the gesture the grid had no way of announcing, so
  // the announcement stops the moment it has been used — from anywhere:
  // the gutter affordance, a double-click, or the hotkey.
  noteRowOpened();
  const dl = $('detailFields');
  // The row this pane was painted for. The pane only ever shows the
  // cursor row (maybeShowDetail keeps it there), but a field's Copy must
  // act on the row in front of the analyst rather than re-derive one from
  // state that something else may have moved since.
  dl.dataset.pos = pos;
  dl.replaceChildren();
  S.columns.forEach((c, i) => {
    const v = r.cells[i];
    if (v == null || v === '') return;
    // The column's name as the column writes it. It used to be
    // uppercased in CSS, which costs a beat per field in a two-column
    // list of forty of them, and made names that are already mixed case
    // in the evidence (TimeCreated, CommandLine) unrecognisable as the
    // same strings the header row shows.
    const dt = el('dt', null, c.name);
    dt.dataset.col = c.name;
    dl.append(dt);
    const dd = el('dd');
    // Which column a selection or a clicked node belongs to — the detail
    // menu's actions (filter, exclude, add as column) all need it, and the
    // <dd> is the only thing that still knows once you're deep inside a
    // pretty-printed document.
    dd.dataset.col = c.name;
    dd.append(copyFieldButton(c.name), renderDetailContent(displayCell(c.name, v)));
    dl.append(dd);
  });
  const note = $('noteInput');
  // r.note is the server's copy, which is behind the box while a save is
  // pending — see keepsUnsavedNote.
  if (!keepsUnsavedNote(note, r)) note.value = r.note || '';
  note.dataset.rid = r.rid;
  note.dataset.sourceId = r.source_id;
  $('noteStatus').textContent = '';
}

/* ------------------------------------------------- detail pane menu

   Right-clicking in the detail pane answers whichever of two questions the
   pointer is actually over, and often both at once:

   - text is selected → act on that substring (copy it, filter the column
     to it, search the table for it). A selection inside a JSON blob is a
     fragment, so it filters as *contains*, never as `=`: the cell it came
     from is a whole document and an exact match would return nothing.
   - the click landed on a node of a parsed JSON/XML document → act on that
     field (add it as a column, filter to its exact value, copy it). This
     is the path that makes extraction a click rather than a syntax to
     learn — `data-path` was put on the node by the pretty-printer.

   Both are scoped to the column the <dd> belongs to. */

export function detailSelectionText() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed) return '';
  const node = sel.anchorNode;
  const holder = node && (node.nodeType === 1 ? node : node.parentElement);
  // A selection that started outside the detail fields isn't ours.
  if (!holder || !holder.closest('#detailFields')) return '';
  return String(sel).trim();
}

export function detailMenuItems(ctx) {
  const items = [];
  const { column, selection, node } = ctx;

  if (selection) {
    const shown = ellipsize(selection);
    items.push({ header: `Selection in ${column}` });
    items.push({
      label: 'Copy',
      onclick: () => writeClipboardText(Promise.resolve(selection), 'Copied selection'),
    });
    items.push({
      label: `Filter ${column} to ${shown}`,
      title: 'Filters to rows whose value contains this text',
      onclick: () => filterByContains(column, selection),
    });
    items.push({
      label: `Filter ${column} to ${shown} only`,
      title: 'Drops every other filter and the search — the timeframe filter stays',
      onclick: () => filterByContains(column, selection, { only: true }),
    });
    items.push({
      label: `Exclude ${shown}`,
      onclick: () => filterByContains(column, selection, { exclude: true }),
    });
    items.push({
      label: `Search all columns for ${shown}`,
      onclick: () => searchForText(selection),
    });
  }

  if (node && node.dataset.path) {
    const path = node.dataset.path;
    const value = node.dataset.value;
    const kind = node.dataset.structKind;
    if (items.length) items.push('-');
    items.push({ header: ellipsize(path, 48), literal: true });
    items.push({
      label: 'Add as a column',
      title: `Adds a column holding ${path} from every row of "${column}"`,
      onclick: () => addExtractedColumn(column, path, kind),
    });
    if (value !== undefined && value !== '') {
      items.push({
        label: `Filter ${column} to this value`,
        title: 'Filters to rows whose document contains this value',
        onclick: () => filterByContains(column, value),
      });
      items.push({
        label: 'Copy value',
        onclick: () => writeClipboardText(Promise.resolve(value), 'Copied value'),
      });
    }
    items.push({ label: 'Copy path', onclick: () => writeClipboardText(Promise.resolve(path), 'Copied path') });
    // Single tables only, like the header menu's Flatten: a merge has no
    // table of its own to sample, and the server refuses one.
    if (S.sourceId >= 0) {
      items.push({
        label: 'Flatten this document into columns…',
        onclick: () => openFlattenModal(column),
      });
    }
  }
  return items;
}

/* Contains-filtering, as distinct from filterByValue's `=`. A selection or
   a field pulled out of a document is a *part* of the cell, so the filter
   that finds it again is the substring one. Reuses the same raw-filter
   syntax the header boxes take, and the same clearAllFilters seeding that
   makes `only` correct (the timeframe filter survives, grouping is
   stashed) rather than reimplementing either.

   The box only gets the text when it reads back as exactly this contains.
   Selected evidence is full of the box's syntax — `whoami | findstr` is an
   any-of list of two exact values, `=cmd` an equals, `>5` a comparison,
   `^HKLM` starts-with, `/usr/bin/` a regex, `*` and `""` not-empty and
   empty — and the box has no quoting for a contains (its quotes are for
   exact values only, see unquoteValue). So anything that doesn't
   round-trip, or that the box would trim or strip (edge whitespace, a
   line break), goes into the filter tree as a contains condition instead:
   the value picker's route for the line break the box can't hold, and the
   server escapes the value for LIKE there itself. */
export async function filterByContains(column, text, { only = false, exclude = false } = {}) {
  const value = String(text == null ? '' : text);
  if (!value) return;
  const op = exclude ? 'not_contains' : 'contains';
  const raw = (exclude ? '!' : '') + value;
  const cond = boxHoldsContains(op, value) ? null : { type: 'cond', column, op, value };
  const shown = ellipsize(value);
  const where = cond ? ` — applied under Filters ▾, as the filter box can't hold that text as a plain contains` : '';
  if (only) {
    await clearAllFilters(cond ? { column, cond } : { column, raw });
    toast(`Filtered ${column} to ${shown} · other filters cleared${where}`, cond ? 5000 : undefined);
    return;
  }
  // One filter per column either way: the box's text is overwritten, as
  // it always was, and so is a condition this put in the tree last time,
  // so the same gesture leaves the same filter whichever way the text had
  // to travel.
  setContainsTreeCond(column, cond);
  setColumnFilter(column, cond ? '' : raw);
  updateFiltersButton();
  await rebuildView();
  toast(`Filtered ${column} ${exclude ? 'not ' : ''}containing ${shown}${where}`, cond ? 5000 : undefined);
}

export function boxHoldsContains(op, value) {
  if (/[\r\n]/.test(value)) return false; // an <input> strips them
  const p = parseFilter((op === 'not_contains' ? '!' : '') + value);
  return !!p && p.op === op && p.value === value;
}

/* The condition filterByContains puts in the tree, found again the way the
   picker's node is (isPickerNode, filters.js): structurally, since the
   builder's SQL round-trip would drop a marker. A top-level contains or
   does-not-contain on the column whose text the box can't hold — one whose
   text it could hold can only have come from the builder, and is left
   alone. Added into the root when that is the plain AND group, and ANDed
   onto it otherwise, so a root the analyst turned into an OR (or a raw
   fragment) keeps its own meaning. */
export function setContainsTreeCond(column, cond) {
  const ours = (n) => !!n && n.type === 'cond' && n.column === column
    && (n.op === 'contains' || n.op === 'not_contains') && !boxHoldsContains(n.op, String(n.value ?? ''));
  const root = S.filterTree;
  if (root && root.type === 'group' && root.op === 'AND') {
    root.children = (root.children || []).filter((n) => !ours(n));
    if (cond) root.children.push(cond);
    return;
  }
  if (cond) S.filterTree = { type: 'group', op: 'AND', children: root ? [root, cond] : [cond] };
}

export async function searchForText(text) {
  const value = String(text || '').trim();
  if (!value) return;
  // 'contains' is the plain-substring mode (see #searchModeToggle) — a
  // selection lifted out of a document is literal text, so regex mode
  // would treat its dots and brackets as syntax.
  if (S.searchMode !== 'contains') await setSearchMode('contains');
  S.search = value;
  $('search').value = value;
  syncSearchExpansion(true);
  await rebuildView({ keepScroll: false });
  toast(`Searching for ${ellipsize(value)}`);
}

/* The note box saves 500 ms after the last keystroke, so for that long —
   and for as long as the POST then takes — the box is ahead of the
   server, and so of every row object a page fetch brings back. Two things
   follow, both in showDetail (keepsUnsavedNote):

   - a page that lands while the analyst types re-shows the same row
     (grid.js ensurePage, after anything that clears the row caches: a
     derived column finishing, an import, a tag written elsewhere), and
     setting the box to that row's `note` threw away what was typed since
     the last save — mid-sentence, and the rest of the sentence was then
     saved on its own. `noteUnsaved` is what says the box is ahead: set on
     a keystroke, cleared only once a save of exactly what the box holds,
     for the row it is bound to, has come back.
   - a cursor move rebinds the box to another row, and a save still
     waiting out its delay read the binding when it fired: it posted the
     new row's own note back to it, and what was typed on the old row in
     the last half second was lost. flushNoteSave sends it first, against
     the row it was typed on. */
let noteSaveTimer = null;
let noteUnsaved = false;

export function saveNote() {
  noteUnsaved = true;
  clearTimeout(noteSaveTimer);
  noteSaveTimer = setTimeout(writeNote, 500);
}

export function flushNoteSave() {
  if (noteSaveTimer === null) return;
  clearTimeout(noteSaveTimer);
  writeNote();
}

/* Whether showDetail should leave the note box's text alone for row `r`:
   only when the box is already that row's and holds typing the server
   hasn't confirmed. Any other row takes the box over, so a save still
   pending for the row it is leaving goes now, before the rebind. */
export function keepsUnsavedNote(note, r) {
  if (note.dataset.rid === String(r.rid) && note.dataset.sourceId === String(r.source_id)) return noteUnsaved;
  flushNoteSave();
  noteUnsaved = false;
  return false;
}

export async function writeNote() {
  noteSaveTimer = null;
  const note = $('noteInput');
  const rid = Number(note.dataset.rid);
  const sourceId = Number(note.dataset.sourceId);
  if (!rid) return;
  const value = note.value;
  await post('/api/note', { source_id: sourceId, rid, note: value });
  // Still this row's box, holding what was just saved: nothing is pending.
  // Otherwise the analyst has typed on since, or the box shows another row
  // now (a cursor move flushed this save), and "Saved" under it would be
  // about text this save didn't carry.
  const current = Number(note.dataset.rid) === rid && Number(note.dataset.sourceId) === sourceId
    && note.value === value;
  if (current) noteUnsaved = false;
  const r = rowAt(S.cursor);
  if (r && r.rid === rid) r.note = note.value;
  // Same rule as tagRowsAtPositions: under a grouping the row just patched
  // is a group-page object, and the flat cache still holds this rid with
  // the old note for dropGrouping to paint — the ✎ mark would go missing
  // on Ungroup exactly the way a tag stripe did. The mismatch arms cover
  // the 500 ms debounce: an Ungroup or a cursor move inside that window
  // means the object under the cursor is not the input's row, so nothing
  // above was patched and the cached copy of this rid is the stale one.
  if (!r || r.rid !== rid || S.groupByCols.length) clearPageCache();
  if (current) $('noteStatus').textContent = 'Saved';
  render();
}

/* ------------------------------------------------------------- detail pane */

/* Dock side + size are a browser-local UI preference, not case data — same
   rationale as the keymap/appearance blocks below (not saved in
   workspace/, doesn't travel with the case). */
export const DETAIL_KEY = 'winnow.detail';

export function loadDetailPrefs() {
  try { return { dock: 'bottom', size: null, ...JSON.parse(localStorage.getItem(DETAIL_KEY) || '{}') }; }
  catch { return { dock: 'bottom', size: null }; }
}

export function saveDetailPrefs() { localStorage.setItem(DETAIL_KEY, JSON.stringify(S.detailPrefs)); }

export function applyDetailPrefs() {
  const area = $('mainArea');
  const d = $('detail');
  area.dataset.dock = S.detailPrefs.dock;
  if (S.detailPrefs.size) {
    if (S.detailPrefs.dock === 'right') { d.style.width = S.detailPrefs.size + 'px'; d.style.height = ''; }
    else { d.style.height = S.detailPrefs.size + 'px'; d.style.width = ''; }
  } else {
    d.style.width = ''; d.style.height = '';
  }
  $('btnDetailDock').title = S.detailPrefs.dock === 'right' ? 'Dock to the bottom' : 'Dock to the right';
}

/* The pane and its resize handle hide together. Hiding never touches
   #noteInput's dataset (rid/sourceId): saveNote is a 500 ms debounce that
   reads them when it fires, so a note typed just before the pane went
   away on a page or table switch still posts against the row it was
   typed for — that row is still in the case. A case open is the one
   place that does blank the rid, in home.js before the /api/case/open
   POST, because a save that fired after the store swap would land the
   previous case's {source_id, rid} in the new one. Called on every page
   switch (syncTabChrome), on leaving a table (openSource, and the empty
   state loadSources lands on when the last tab closes) and on case open,
   as well as by the pane's own Close button and the `d` toggle. */
export function hideDetailPane() {
  $('detail').hidden = true;
  $('detailResize').hidden = true;
}

export function toggleDetailPane() {
  if ($('detail').hidden) { if (S.cursor >= 0 && rowAt(S.cursor)) showDetail(S.cursor); }
  else hideDetailPane();
}

/* DOM wiring for this module, called once by main.js. Handlers can't
   fire during load, so the order these run in doesn't matter — the
   startup steps that DO depend on order live in main.js instead. */
export function wireDetail() {
$('detail').addEventListener('contextmenu', (e) => {
  const dd = e.target.closest('#detailFields dd, #detailFields dt');
  if (!dd || !dd.dataset.col) return; // the note box and the header keep the browser's menu
  const ctx = {
    column: dd.dataset.col,
    selection: detailSelectionText(),
    node: e.target.closest('.struct-node'),
  };
  const items = detailMenuItems(ctx);
  if (!items.length) return;
  e.preventDefault();
  contextMenu(e, items);
});

$('btnDetailDock').onclick = () => {
  S.detailPrefs.dock = S.detailPrefs.dock === 'right' ? 'bottom' : 'right';
  S.detailPrefs.size = null; // switching axis — the old size doesn't mean anything on the new one
  applyDetailPrefs();
  saveDetailPrefs();
};

$('detailResize').addEventListener('mousedown', (e) => {
  e.preventDefault();
  const dock = S.detailPrefs.dock;
  const d = $('detail');
  const handle = $('detailResize');
  const startPos = dock === 'right' ? e.clientX : e.clientY;
  const startSize = dock === 'right' ? d.getBoundingClientRect().width : d.getBoundingClientRect().height;
  handle.classList.add('dragging');
  const move = (ev) => {
    const pos = dock === 'right' ? ev.clientX : ev.clientY;
    // Dragged inward from the edge the pane is docked against — right dock's
    // handle sits on its left edge, bottom dock's on its top edge, so in
    // both cases moving the handle *toward* that edge should *grow* the pane.
    const delta = startPos - pos;
    const size = Math.max(200, startSize + delta);
    if (dock === 'right') d.style.width = size + 'px';
    else d.style.height = size + 'px';
    S.detailPrefs.size = size;
  };
  const up = () => {
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', up);
    handle.classList.remove('dragging');
    saveDetailPrefs();
  };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
});

$('btnCloseDetail').onclick = () => hideDetailPane();

/* A field's Copy puts the DISPLAYED value on the clipboard — the same
   rule the grid's row and cell copies follow, so a datetime column's
   chosen format applies to a copy exactly as it does on screen rather
   than reverting to the stored text. What it does NOT carry across is
   this pane's pretty-printing: a JSON column copies as the document the
   row holds, not as the indented rendering of it, which is the same value
   the field menu's "Copy value" hands over. Delegated, since showDetail
   rebuilds the whole field list on every cursor move. */
$('detailFields').addEventListener('click', (e) => {
  const btn = e.target.closest('.dfield-copy');
  if (!btn) return;
  const column = btn.closest('dd').dataset.col;
  const r = rowAt(Number($('detailFields').dataset.pos));
  const i = S.columns.findIndex((c) => c.name === column);
  if (!r || i < 0) return;
  writeClipboardText(Promise.resolve(String(displayCell(column, r.cells[i]))), `Copied ${column}`);
});

$('btnCopyRow').onclick = () => {
  const r = rowAt(S.cursor);
  if (!r) return;
  const text = S.columns.map((c, i) => `${c.name}: ${r.cells[i] ?? ''}`).join('\n');
  writeClipboardText(Promise.resolve(text), 'Row copied');
};

$('noteInput').oninput = () => { $('noteStatus').textContent = 'Saving…'; saveNote(); };
}
