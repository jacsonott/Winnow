"""First/Last plugin — the bounds of each group of events, as a new table.

Group a table on any columns (Host + User, say), and get back two rows per
group: the first and the last event, ordered by a column you choose, each
carrying a templated description and whichever columns you want alongside.
The classic use is turning 40,000 logon events into 200 session bookends —
"First of 312 | WKSTN-014 | user: jsmith" — that read as a story and can be
tagged, put on the Timeline, and exported like any other rows.

Shape decisions worth knowing before editing:

**The result is a real table** (Store.ingest_rows), not a live view in the
tab. Bookends are findings: the analyst tags them, they belong on the
unified Timeline, and they should survive in the case file when it's handed
over — none of which a plugin tab's in-memory state can do. ingest_rows
gives every ingest convention (all-TEXT, contiguous rids, batched commits,
background FTS) for free, and invariant #1 holds untouched: the source
table is only ever read.

**First/last come from one window-function pass.** ROW_NUMBER() twice over
(PARTITION BY the group columns ORDER BY sort ASC/DESC, rid as the
tiebreak) plus COUNT() over the same partition — a single scan of the
filtered source, no N+1 per group. `rid` tie-breaking makes the answer
deterministic when timestamps collide, which EVTX at second resolution
does constantly.

**The description is rendered server-side, per row.** `{ColumnName}` takes
that row's value (so the first row shows the first event's user),
`{count}` the group's row count, `{which}` the literal First/Last. Unknown
placeholders are a ValueError naming the offender — a template typo should
fail the preview, not silently emit `{Usre}` into 400 descriptions.

The filter helpers (_where/_esc_like/OPERATORS) are copied from the pivot
example rather than imported — plugins are deliberately standalone
(plugin_api gives them no way to import each other), and each example is
meant to be readable alone.
"""

import json

from store import q

PLUGIN = {
    "name": "first-last",
    "version": "1.0.0",
    "description": "Bookend each group of events: first and last row per group, with a templated description, as a new table.",
}

WINNOW_API_VERSION = 1

MAX_GROUP_COLS = 6
# Two output rows per group; beyond this the result stops being a summary.
MAX_GROUPS = 100_000
PREVIEW_GROUPS = 8

OPERATORS = {
    "in": ("is any of", "many"),
    "not_in": ("is none of", "many"),
    "contains": ("contains", "one"),
    "not_contains": ("does not contain", "one"),
    "starts": ("starts with", "one"),
    "gt": ("> (numeric)", "one"),
    "lt": ("< (numeric)", "one"),
    "empty": ("is empty", "none"),
    "not_empty": ("is not empty", "none"),
}


def register(api):
    api.register_tab(
        id="firstlast",
        label="First/Last",
        entry="ui/tab.js",
        description=(
            "Group events (by host, user, anything) and produce the first and "
            "last row of each group with a templated description — logon "
            "sessions, tool-run windows, per-host activity bounds — as a new, "
            "taggable table."
        ),
    )
    api.register_api("meta", meta, methods=["GET"])
    api.register_api("values", values, methods=["POST"])
    api.register_api("preview", preview, methods=["POST"])
    api.register_api("create", create, methods=["POST"])


# --------------------------------------------------------------- helpers


def _source(req, body):
    if req.store is None:
        raise ValueError("Open a case first")
    try:
        source_id = int(body.get("source_id"))
    except (TypeError, ValueError):
        raise ValueError("source_id is required")
    try:
        return req.store.get_source(source_id)
    except KeyError:
        raise ValueError(f"No source {source_id}")


def _check_columns(src, columns):
    names = {c["name"] for c in src["columns"]}
    for col in columns:
        if col not in names:
            raise ValueError(f"No column {col!r} in {src['name']}")


def _derived_names(src):
    return {c["name"] for c in src["columns"] if c.get("derived")}


def _from_clause(src):
    base = f"{q(src['table_name'])} s"
    if not _derived_names(src):
        return base
    return f"{base} LEFT JOIN {q('drv_' + str(src['id']))} d ON d.rid = s.rid"


def _scope(req, src):
    """FROM text for reading this source's rows. A plain source is its
    table (plus sidecar); a merge is a UNION ALL of its members aliased
    `s`, carrying source_id, rid and every exposed column under plain
    names — so everything downstream references `s.<col>` uniformly
    (invariant #9: the merge path is part of the operation)."""
    if not src.get("is_merge"):
        return _from_clause(src)
    cols = [c["name"] for c in src["columns"]]
    branches = []
    for mid in src["member_source_ids"]:
        m = req.store.get_source(mid)
        sel = ", ".join(f"{_col(m, c)} AS {q(c)}" for c in cols)
        branches.append(f"SELECT {int(m['id'])} AS source_id, s.rid AS rid, {sel} FROM {_from_clause(m)}")
    return "(" + " UNION ALL ".join(branches) + ") s"


def _col(src, name):
    if src.get("is_merge"):
        return f"s.{q(name)}"  # everything sits in the union subquery's alias
    return f"{'d' if name in _derived_names(src) else 's'}.{q(name)}"


def _esc_like(value):
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


NUM_RE_SQL = r"^-?\d+(\.\d+)?$"


def _numeric(src, col):
    ident = _col(src, col)
    return f"(CASE WHEN {ident} REGEXP '{NUM_RE_SQL}' THEN CAST({ident} AS REAL) ELSE NULL END)"


def _where(src, filters):
    clauses, params = [], []
    for f in filters or []:
        col, op = f.get("column"), f.get("op", "in")
        if op not in OPERATORS:
            raise ValueError(f"Unknown filter operator {op!r}")
        ident = _col(src, col)
        kind = OPERATORS[op][1]
        if kind == "many":
            vals = list(f.get("values") or [])
            if not vals:
                continue
            marks = ",".join("?" * len(vals))
            clauses.append(f"{ident} IN ({marks})" if op == "in"
                           else f"({ident} NOT IN ({marks}) OR {ident} IS NULL)")
            params.extend(vals)
        elif kind == "one":
            val = f.get("value")
            if val in (None, ""):
                continue
            if op == "contains":
                clauses.append(f"{ident} LIKE ? ESCAPE '\\'"); params.append(f"%{_esc_like(val)}%")
            elif op == "not_contains":
                clauses.append(f"({ident} NOT LIKE ? ESCAPE '\\' OR {ident} IS NULL)")
                params.append(f"%{_esc_like(val)}%")
            elif op == "starts":
                clauses.append(f"{ident} LIKE ? ESCAPE '\\'"); params.append(f"{_esc_like(val)}%")
            else:
                try:
                    number = float(val)
                except (TypeError, ValueError):
                    raise ValueError(f"{OPERATORS[op][0]} needs a number, got {val!r}")
                clauses.append(f"{_numeric(src, col)} {'>' if op == 'gt' else '<'} ?")
                params.append(number)
        else:
            clauses.append(f"({ident} IS NULL OR {ident} = '')" if op == "empty"
                           else f"({ident} IS NOT NULL AND {ident} != '')")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _tag_where(src, tags):
    """The tag filter's WHERE clause, or None. Tags live in the row_tags
    sidecar (keyed source_id, rid) in the same case file run_sql reads, so
    membership is one IN-subquery — no join, no new privileges. Everything
    interpolated is a validated int, so nothing here touches `params`."""
    if not tags:
        return None
    mode = tags.get("mode")
    if src.get("is_merge"):
        sub = "SELECT 1 FROM row_tags rt WHERE rt.source_id = s.source_id AND rt.rid = s.rid"
        if mode == "any":
            return f"EXISTS ({sub})"
        if mode == "none":
            return f"NOT EXISTS ({sub})"
        if mode == "ids":
            try:
                ids = [int(i) for i in (tags.get("ids") or [])]
            except (TypeError, ValueError):
                raise ValueError("Tag ids must be integers")
            if not ids:
                return None
            return f"EXISTS ({sub} AND rt.tag_id IN ({','.join(str(i) for i in ids)}))"
        raise ValueError(f"Unknown tag filter mode {mode!r}")
    sub = f"SELECT rid FROM row_tags WHERE source_id = {int(src['id'])}"
    if mode == "any":
        return f"s.rid IN ({sub})"
    if mode == "none":
        return f"s.rid NOT IN ({sub})"
    if mode == "ids":
        try:
            ids = [int(i) for i in (tags.get("ids") or [])]
        except (TypeError, ValueError):
            raise ValueError("Tag ids must be integers")
        if not ids:
            return None
        return f"s.rid IN ({sub} AND tag_id IN ({','.join(str(i) for i in ids)}))"
    raise ValueError(f"Unknown tag filter mode {mode!r}")


def _inline(sql, params):
    """run_sql takes no parameters — inline them, skipping both quoted-span
    kinds (see the pivot example: the numeric guard embeds `?` in a string
    literal, and a CSV header like "Elevated?" puts one in an identifier)."""
    out, i, k, n = [], 0, 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            j = i + 1
            while j < n:
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            out.append(sql[i:j + 1])
            i = j + 1
            continue
        if ch == "?":
            if k >= len(params):
                raise ValueError("more placeholders than parameters")
            val = params[k]
            k += 1
            out.append(repr(float(val)) if isinstance(val, (int, float)) and not isinstance(val, bool)
                       else "'" + str(val).replace("'", "''") + "'")
            i += 1
            continue
        out.append(ch)
        i += 1
    if k != len(params):
        raise ValueError("parameter/placeholder mismatch")
    return "".join(out)


# ------------------------------------------------------------ the query


def _validated(req, body):
    """Everything preview and create share: the source, the group columns,
    the sort column, the carried columns, the compiled WHERE, the template."""
    src = _source(req, body)
    group_cols = body.get("group_by") or []
    if not isinstance(group_cols, list) or not group_cols:
        raise ValueError("Pick at least one column to group by")
    if len(group_cols) > MAX_GROUP_COLS:
        raise ValueError(f"Too many group columns ({len(group_cols)} > {MAX_GROUP_COLS})")
    sort_col = body.get("sort_column")
    if not sort_col:
        raise ValueError("Pick the column that orders each group (first/last are meaningless without one)")
    carry = body.get("columns") or []
    _check_columns(src, group_cols + [sort_col] + carry)
    for f in body.get("filters") or []:
        _check_columns(src, [f.get("column")])
    where, params = _where(src, body.get("filters"))
    tag_clause = _tag_where(src, body.get("tags"))
    if tag_clause:
        where = f"{where} AND {tag_clause}" if where else f" WHERE {tag_clause}"
    template = body.get("template") or "{which} of {count}"
    row_json = bool(body.get("row_json"))
    return src, group_cols, sort_col, carry, where, params, template, row_json


def _bookend_rows(req, src, group_cols, sort_col, carry, where, params, limit, row_json=False):
    """One windowed pass: rank each row inside its group both directions,
    keep rank 1 of each. Selected values are the *row's own* — the first
    row's user, not the group's."""
    # Every column the template or output might need, deduped, stable order
    # — or the whole row when the JSON cell is requested.
    pool = ([c["name"] for c in src["columns"]] if row_json
            else group_cols + [sort_col] + carry)
    needed = []
    for c in pool:
        if c not in needed:
            needed.append(c)
    sel = ", ".join(f"{_col(src, c)} AS {q(c)}" for c in needed)
    part = ", ".join(_col(src, c) for c in group_cols)
    # Direction must be stated PER COLUMN: "ORDER BY ts, rid DESC" flips only
    # rid, leaving the last-window ranked by ascending time — every group's
    # "Last" would be its earliest row with the biggest rid. On a merge,
    # rid alone collides across members, so (source_id, rid) is the
    # deterministic tie-break.
    sort_ref = _col(src, sort_col)
    tie_cols = ["s.source_id", "s.rid"] if src.get("is_merge") else ["s.rid"]
    order_asc = ", ".join([f"{sort_ref} ASC"] + [f"{t} ASC" for t in tie_cols])
    order_desc = ", ".join([f"{sort_ref} DESC"] + [f"{t} DESC" for t in tie_cols])
    sql = (
        f"SELECT {sel},"
        f" ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY {order_asc}) AS rn_first,"
        f" ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY {order_desc}) AS rn_last,"
        f" COUNT(*) OVER (PARTITION BY {part}) AS group_n"
        f" FROM {_scope(req, src)}{where}"
    )
    outer = (
        f"SELECT * FROM ({sql}) WHERE rn_first = 1 OR rn_last = 1"
        f" ORDER BY {q(sort_col)}, rn_first"
    )
    res = req.store.run_sql(_inline(outer, params), limit=limit)
    cols = res["columns"]
    return [dict(zip(cols, r)) for r in res["rows"]], res["truncated"]


def _render(template, row, which):
    """{ColumnName} → the row's value, {count} → group size, {which} →
    First/Last. Unknown names raise, naming the offender."""
    out, i, n = [], 0, len(template)
    while i < n:
        ch = template[i]
        if ch == "{":
            j = template.find("}", i + 1)
            if j == -1:
                raise ValueError("Unclosed { in the description template")
            key = template[i + 1:j]
            if key == "which":
                out.append(which)
            elif key == "count":
                out.append(str(row.get("group_n", "")))
            elif key in row:
                val = row.get(key)
                out.append("" if val is None else str(val))
            else:
                raise ValueError(f"Unknown placeholder {{{key}}} — use a column name, {{count}} or {{which}}")
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


ROW_JSON_COLUMN = "Row (JSON)"


def _emit(rows, sort_col, carry, template, json_cols=None):
    """The output rows: one per bookend. A single-row group is both its own
    first and its last — emitted once, labelled First (a story with one
    event has no separate ending). `json_cols` non-None adds a cell with
    the bookend's ENTIRE row as a JSON object — the synthetic window
    columns (rn_first/rn_last/group_n) never appear in it."""
    out = []
    for r in rows:
        labels = []
        if r.get("rn_first") == 1:
            labels.append("First")
        if r.get("rn_last") == 1 and r.get("rn_first") != 1:
            labels.append("Last")
        for which in labels:
            desc = _render(template, r, which)
            row = [r.get(sort_col, "")] + [("" if r.get(c) is None else str(r.get(c))) for c in carry]
            if json_cols is not None:
                row.append(json.dumps({c: r.get(c) for c in json_cols}, ensure_ascii=False))
            out.append(row + [desc])
    return out


# ---------------------------------------------------------------- routes


def meta(req):
    return {
        "operators": [{"id": k, "label": v[0], "value_kind": v[1]} for k, v in OPERATORS.items()],
        "limits": {"groups": MAX_GROUPS, "group_cols": MAX_GROUP_COLS, "preview_groups": PREVIEW_GROUPS},
        "placeholders": ["which", "count"],
    }


def values(req):
    body = req.body or {}
    src = _source(req, body)
    column = body.get("column")
    _check_columns(src, [column])
    limit = min(int(body.get("limit") or 500), 5000)
    sql = (f"SELECT {_col(src, column)} AS value, COUNT(*) AS n FROM {_scope(req, src)}"
           f" GROUP BY 1 ORDER BY n DESC")
    res = req.store.run_sql(sql, limit=limit)
    return {"values": [{"value": r[0], "count": r[1]} for r in res["rows"]],
            "truncated": res["truncated"]}


def preview(req):
    """POST .../preview — the first PREVIEW_GROUPS groups' bookends, plus the
    total group count, so 'Create table' says what it will make before it
    makes it."""
    body = req.body or {}
    src, group_cols, sort_col, carry, where, params, template, row_json = _validated(req, body)
    part = ", ".join(_col(src, c) for c in group_cols)
    count_sql = f"SELECT COUNT(*) FROM (SELECT 1 FROM {_scope(req, src)}{where} GROUP BY {part})"
    total_groups = req.store.run_sql(_inline(count_sql, params), limit=1)["rows"][0][0]

    rows, _ = _bookend_rows(req, src, group_cols, sort_col, carry, where, params,
                            limit=PREVIEW_GROUPS * 2 + 2, row_json=row_json)
    json_cols = [c["name"] for c in src["columns"]] if row_json else None
    header = [sort_col] + carry + ([ROW_JSON_COLUMN] if row_json else []) + ["Description"]
    return {"columns": header, "rows": _emit(rows, sort_col, carry, template, json_cols),
            "total_groups": total_groups}


def create(req):
    """POST .../create — run the whole thing and land it as a new source."""
    body = req.body or {}
    src, group_cols, sort_col, carry, where, params, template, row_json = _validated(req, body)
    rows, truncated = _bookend_rows(req, src, group_cols, sort_col, carry, where, params,
                                    limit=MAX_GROUPS * 2, row_json=row_json)
    if truncated:
        raise ValueError(f"More than {MAX_GROUPS:,} groups — narrow the grouping or add a filter")
    json_cols = [c["name"] for c in src["columns"]] if row_json else None
    out_rows = _emit(rows, sort_col, carry, template, json_cols)
    name = (body.get("name") or "").strip() or f"First-Last of {src['name']}"
    header = [sort_col] + carry + ([ROW_JSON_COLUMN] if row_json else []) + ["Description"]
    rec = req.store.ingest_rows(header, out_rows, name=name)
    return {"source": {"id": rec["id"], "name": rec["name"], "row_count": rec["row_count"]}}
