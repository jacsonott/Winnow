/* Widget recipes — what the guided editor, the one-click menu entries and
   the starter board all build widgets FROM. A recipe is a template
   (row count, top values, over time…) plus its picks (table, column,
   value…); `widgetFrom` turns that into the widget the board stores:
   the SQL to run, how to render it, and a DRILL — the table and the
   conditions that select the rows the number came from, so clicking the
   widget can open exactly those rows. The picks ride along as `build`,
   which is what lets the editor reopen a widget guided instead of as a
   wall of SQL.

   Pure: no DOM, no fetches. dashboard.js does the asking and the saving. */

import { S } from './state.js';

/* SQL spelling for a column name and a string literal. Column names are
   quoted with double quotes (the store's own q()); a value is a single-
   quoted literal. Neither is ever interpolated unquoted. */
export const qcol = (c) => '"' + String(c).replace(/"/g, '""') + '"';
export const qval = (v) => "'" + String(v).replace(/'/g, "''") + "'";
const like = (v) => qval('%' + String(v).replace(/[%_\\]/g, (ch) => '\\' + ch) + '%') + " ESCAPE '\\'";

const BUCKET_FMT = { day: '%Y-%m-%d', hour: '%Y-%m-%d %H' };

/* `needs` is what the editor asks for beyond the table; `title` is the
   auto title, with the column filled in. */
export const WIDGET_TEMPLATES = [
  { id: 'blank', label: 'Blank — write my own SQL', needs: [] },
  { id: 'count', label: 'Row count', render: 'stat', needs: [],
    title: (p) => 'Row count',
    sql: (t) => `SELECT COUNT(*) AS n FROM ${t}`,
    drill: (t) => ({ table: t }) },
  // An empty value is a real pick — "how many rows have nothing here?" —
  // and needs the empty-aware op on the drill side, since equals '' is a
  // condition the server drops.
  { id: 'countwhere', label: 'Count of rows matching a value', render: 'stat', needs: ['column', 'value', 'match'],
    title: (p) => (p.value === '' ? `${p.column} is empty` : `${p.column} ${p.match === 'contains' ? 'contains' : '='} ${p.value}`),
    sql: (t, p) => (p.value === ''
      ? `SELECT COUNT(*) AS n FROM ${t}\nWHERE ${qcol(p.column)} = '' OR ${qcol(p.column)} IS NULL`
      : `SELECT COUNT(*) AS n FROM ${t}\nWHERE ${qcol(p.column)} ${p.match === 'contains' ? 'LIKE ' + like(p.value) : '= ' + qval(p.value)}`),
    drill: (t, p) => ({ table: t, where: [p.value === ''
      ? { column: p.column, op: 'empty', value: '' }
      : { column: p.column, op: p.match === 'contains' ? 'contains' : 'equals', value: p.value }] }) },
  { id: 'distinct', label: 'Distinct count of a column', render: 'stat', needs: ['column'],
    title: (p) => `Distinct ${p.column}`,
    sql: (t, p) => `SELECT COUNT(DISTINCT ${qcol(p.column)}) AS n FROM ${t}\nWHERE ${qcol(p.column)} <> ''`,
    drill: (t, p) => ({ table: t, column: p.column, where: [{ column: p.column, op: 'not_empty', value: '' }] }) },
  { id: 'top', label: 'Top values of a column (bar chart)', render: 'bar', needs: ['column'], span: 2,
    title: (p) => `Top ${p.column}`,
    sql: (t, p) => `SELECT ${qcol(p.column)} AS label, COUNT(*) AS n\nFROM ${t}\nWHERE ${qcol(p.column)} <> ''\nGROUP BY ${qcol(p.column)} ORDER BY n DESC LIMIT 10`,
    drill: (t, p) => ({ table: t, column: p.column }) },
  { id: 'rare', label: 'Rarest values of a column (list)', render: 'list', needs: ['column'], span: 2,
    title: (p) => `Rarest ${p.column}`,
    sql: (t, p) => `SELECT ${qcol(p.column)} AS label, COUNT(*) AS n\nFROM ${t}\nWHERE ${qcol(p.column)} <> ''\nGROUP BY ${qcol(p.column)} ORDER BY n ASC LIMIT 10`,
    drill: (t, p) => ({ table: t, column: p.column }) },
  { id: 'time', label: 'Events over time (histogram)', render: 'histogram', needs: ['column', 'bucket'], span: 2,
    title: (p) => `${p.column} over time`,
    // TS_NORMALIZE is NULL for a value it can't read; those rows would be
    // one bucket labelled "null" that no timeframe can reopen, so they
    // are left out of the chart rather than charted as a lie.
    sql: (t, p) => `SELECT strftime('${BUCKET_FMT[p.bucket] || BUCKET_FMT.day}', TS_NORMALIZE(${qcol(p.column)})) AS bucket, COUNT(*) AS n\nFROM ${t}\nWHERE ${qcol(p.column)} <> '' AND TS_NORMALIZE(${qcol(p.column)}) IS NOT NULL\nGROUP BY bucket ORDER BY bucket`,
    drill: (t, p) => ({ table: t, column: p.column, bucket: p.bucket || 'day' }) },
  { id: 'window', label: 'Activity window (first / last)', render: 'kv', needs: ['column'], span: 2,
    title: (p) => 'Activity window',
    sql: (t, p) => `SELECT 'First' AS k, MIN(TS_NORMALIZE(${qcol(p.column)})) AS v FROM ${t} WHERE ${qcol(p.column)} <> ''\nUNION ALL SELECT 'Last', MAX(TS_NORMALIZE(${qcol(p.column)})) FROM ${t} WHERE ${qcol(p.column)} <> ''`,
    drill: (t, p) => ({ table: t }) },
];

export function templateById(id) {
  return WIDGET_TEMPLATES.find((t) => t.id === id) || WIDGET_TEMPLATES[0];
}

/* The SQL a recipe produces — null for blank. */
export function recipeSql(build) {
  const t = templateById(build.template);
  if (!t.sql || !build.table) return null;
  if (t.needs.includes('column') && !build.column) return null;
  if (t.needs.includes('value') && build.value == null) return null;   // '' is a pick; undefined is not
  return t.sql(build.table, build);
}

/* A whole widget from a recipe: the SQL, the render kind, the drill, and
   the recipe itself (as `build`) so the editor can reopen it guided. */
export function widgetFrom(build, extra = {}) {
  const t = templateById(build.template);
  const sql = recipeSql(build);
  if (!sql) return null;
  const w = {
    title: extra.title || t.title(build),
    source: 'sql',
    render: extra.render || t.render,
    span: extra.span || t.span || 1,
    query: { sql },
    build: { template: build.template, table: build.table },
    drill: t.drill(build.table, build),
  };
  for (const k of ['column', 'value', 'match', 'bucket']) if (build[k]) w.build[k] = build[k];
  if (extra.sub) w.sub = extra.sub;
  return w;
}

/* The table reference for a source in this case. Widgets reference tables
   by name (src_N), the same thing the SQL pane uses. */
export const tableOf = (sourceId) => `src_${sourceId}`;

/* Columns for a table reference: a case table's own columns, or for a
   portable {{…}} placeholder the header set it stands for (fetched once
   by dashboard.js into S.headerSets). Types are known only for case
   tables; placeholder columns get a name-based guess so the time
   templates can offer something. */
/* A widget's SQL is `FROM src_<id>`, and a derived column's values are not
   there — they live in the drv_<id> sidecar, which run_sql does not join.
   Offering one produces a card whose body reads "no such column", so it is
   filtered out at the source of every column list a widget is built from. */
const widgetable = (c) => !c.derived;

export function columnsForTable(table) {
  const m = /^src_(\d+)$/.exec(table || '');
  if (m) {
    const src = (S.sources || []).find((s) => s.id === Number(m.group ? m.group(1) : m[1]));
    return src ? src.columns.filter(widgetable).map((c) => ({ name: c.name, type: c.type })) : [];
  }
  const ph = /^\{\{(?:all:)?([^}]+)\}\}$/.exec(table || '');
  if (!ph || !S.headerSets) return [];
  let key = ph[1].trim();
  if (key.toLowerCase().startsWith('header_set:')) key = key.slice('header_set:'.length).trim();
  else key = (S.headerSets.shorthands || {})[key.toLowerCase()] || key;
  const set = (S.headerSets.sets || []).find((s) => s.name === key);
  if (!set) return [];
  return set.columns.map((name) => ({ name, type: /time|date|created|modified/i.test(name) ? 'datetime' : 'text' }));
}

/* The bucket a histogram label came from, as a timeframe: '2026-03-14'
   is the whole day, '2026-03-14 08' the whole hour. Null for anything
   else (a label the widget's own SQL shaped differently). */
export function bucketRange(label) {
  const s = String(label || '').trim().replace('T', ' ');
  let m = /^(\d{4}-\d{2}-\d{2}) (\d{2})$/.exec(s);
  if (m) return { start: `${m[1]} ${m[2]}:00:00`, end: `${m[1]} ${m[2]}:59:59` };
  m = /^(\d{4}-\d{2}-\d{2})$/.exec(s);
  if (m) return { start: `${m[1]} 00:00:00`, end: `${m[1]} 23:59:59` };
  return null;
}
