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
does constantly. A number-typed sort column is ordered by value, like the
grid's numeric sort, never as text ("10" < "9"); a cell that isn't a
number wins neither end (`_order_keys`).

**The description is rendered server-side, per row.** `{ColumnName}` takes
that row's value (so the first row shows the first event's user),
`{count}` the group's row count, `{which}` the literal First/Last, and
`{which:number}` the row's position in its group instead (1 for the first,
the group size for the last). Unknown
placeholders are a ValueError naming the offender — a template typo should
fail the preview, not silently emit `{Usre}` into 400 descriptions. Any
column of the source can be named: the windowed pass projects whatever the
template references on top of the grouped/ordered/carried columns
(`_template_columns`), so a column need not be dragged into Include
columns just to appear in the description. The one exception is a column
named like one of the pass's own aliases (`_reserved_aliases`): that is
never projected on the template's behalf, so `{count}` and the Sum column
keep reading the window's numbers rather than the file's.

**Distinct values are a second, separate pass** (`_group_values`). A
column under List values, or named by `{values:Column}`, becomes each
group's distinct non-blank values, sorted, in one cell. It cannot ride the
bookend window: SQLite refuses DISTINCT inside a window aggregate, and a
plain group_concat OVER would build the whole list once per ROW. So it is
one GROUP BY per column over the same filtered rows, keyed back onto the
bookends by their group values, and capped at MAX_LISTED_VALUES per cell
(the rest counted, not dropped silently). The preview asks only for the
groups it shows.

The filter helpers (_where/_esc_like/OPERATORS) are copied from the pivot
example rather than imported — plugins are deliberately standalone
(plugin_api gives them no way to import each other), and each example is
meant to be readable alone.
"""

import json
import re

from winnow.store import q

PLUGIN = {
    "name": "first-last",
    "version": "1.0.0",
    "description": "Bookend each group of events: first and last row per group, with a templated description, as a new table.",
}

WINNOW_API_VERSION = 12   # winnow.dropdownMenu: the click-to-place menu under a field

MAX_GROUP_COLS = 6
# Two output rows per group; beyond this the result stops being a summary.
MAX_GROUPS = 100_000
PREVIEW_GROUPS = 8
# "Copy result" returns the whole result to the clipboard; past this it
# stops being something a clipboard (or the analyst) wants.
MAX_COPY_ROWS = 20_000
# A List values cell names at most this many values and counts the rest:
# past a few dozen a cell stops being read, and a column of ids grouped by
# host would otherwise put a whole column into every bookend.
MAX_LISTED_VALUES = 50
# The description used when the analyst leaves the box empty.
DEFAULT_TEMPLATE = "{which} of {count}"
# The auto-created timeline tag's colour — fixed so every bookend tag
# reads the same across cases (amber, matching the app's accent family).
TIMELINE_TAG_COLOR = "#d2a04a"

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
    api.register_api("rows", rows, methods=["POST"])
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
        raise ValueError(f"No table {source_id}")


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
    branches = []
    for mid in src["member_source_ids"]:
        m = req.store.get_source(mid)
        sel = req.store.merge_select_list(src, m)  # NULL-pads columns a member lacks
        branches.append(f"SELECT {int(m['id'])} AS source_id, s.rid AS rid, {sel} FROM {_from_clause(m)}")
    return "(" + " UNION ALL ".join(branches) + ") s"


def _col(src, name):
    if src.get("is_merge"):
        return f"s.{q(name)}"  # everything sits in the union subquery's alias
    return f"{'d' if name in _derived_names(src) else 's'}.{q(name)}"


def _esc_like(value):
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


NUM_RE_SQL = r"^-?\d+(\.\d+)?$"
_NUM_RE = re.compile(NUM_RE_SQL)


def _is_number_column(src, col):
    """Whether ingest typed this column as a number. Not _numeric() — that
    returns the cast expression, and any non-empty string is truthy."""
    return any(c["name"] == col and c.get("type") == "number" for c in src["columns"])


def _numeric(src, col):
    return _numeric_of(_col(src, col))


def _numeric_of(ident):
    return f"(CASE WHEN {ident} REGEXP '{NUM_RE_SQL}' THEN CAST({ident} AS REAL) ELSE NULL END)"


def _order_keys(ident, number):
    """(first-window key, last-window key) for the sort column, each with
    its direction. A number column is ordered by value, as the grid's
    numeric sort orders it: its cells are TEXT, so "10" < "9" < "100", and
    a group of record ids came back First=10, Last=9. A cell that isn't a
    number (blank, "-", "n/a") must not win either end, so it sorts LAST
    both ways. SQLite sorts NULL first ascending and last descending, so
    ascending-NULLs-last is spelled `-v DESC` (negation flips the order
    exactly and leaves NULL NULL). That evaluates the REGEXP-guarded cast
    once per row per window: a separate `v IS NULL` key would run it twice
    and cost about twice as much, and `NULLS LAST` needs SQLite 3.30."""
    if not number:
        return f"{ident} ASC", f"{ident} DESC"
    v = _numeric_of(ident)
    return f"-{v} DESC", f"{v} DESC"


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
    sums = body.get("sum_columns") or []
    if not isinstance(sums, list):
        raise ValueError("sum_columns must be a list of column names")
    listed = body.get("value_columns") or []
    if not isinstance(listed, list):
        raise ValueError("value_columns must be a list of column names")
    _check_columns(src, group_cols + [sort_col] + carry + sums + listed)
    for c in sums:
        # SUM over text is 0 in SQLite, and a 0 that looks like a real
        # total is the kind of number that ends up in a report. _numeric
        # builds a safe-cast EXPRESSION, not a predicate — the column's own
        # declared type is the question here.
        if not _is_number_column(src, c):
            raise ValueError(f"{c} is not a number column — nothing to sum")
    for f in body.get("filters") or []:
        _check_columns(src, [f.get("column")])
    where, params = _where(src, body.get("filters"))
    tag_clause = _tag_where(src, body.get("tags"))
    if tag_clause:
        where = f"{where} AND {tag_clause}" if where else f" WHERE {tag_clause}"
    template = body.get("template") or DEFAULT_TEMPLATE
    # {values:Column} takes any column, like {Column} does, so it is checked
    # here by name rather than waiting for _render to find nothing.
    for col in _template_value_columns(template):
        if col not in {c["name"] for c in src["columns"]}:
            raise ValueError(f"{{values:{col}}} names no column of {src['name']}")
    row_json = bool(body.get("row_json"))
    return src, group_cols, sort_col, carry, sums, listed, where, params, template, row_json


def _sum_alias(col):
    """The output column's name. Prefixed so it cannot collide with a
    carried column of the same name sitting beside it."""
    return f"Sum of {col}"


# Group-wide aggregates a Total-up column carries besides its sum. Only the
# sum becomes an output column; min and max ride the same window for the
# {min:Column} / {max:Column} placeholders, so asking for either costs no
# extra pass and needs nothing new under Total up.
AGGREGATES = {"sum": "SUM", "min": "MIN", "max": "MAX"}


def _agg_alias(kind, col):
    return _sum_alias(col) if kind == "sum" else f"{kind.capitalize()} of {col}"


def _reserved_aliases(sums):
    """The names the windowed pass gives its own outputs. A source column
    that happens to share one must not be projected beside it: the inner
    SELECT would then carry the name twice — the file's, then the
    window's — and SQLite labels the outer SELECT *'s second copy
    `group_n:1`, so dict(zip(cols, row)) would hand _render and the Sum
    column the file's value in place of the group's, silently."""
    return {"rn_first", "rn_last", "group_n"} | {
        _agg_alias(kind, c) for c in sums for kind in AGGREGATES}


def _template_columns(template, src, sums=()):
    """The source columns a description template names as plain `{Name}`
    placeholders, in first-mention order. Tokenised exactly as _render
    does (a `{` runs to the next `}`), so what gets projected is what
    _render will look up; anything that is not a column — {which},
    {count}, the colon forms, a typo — is left for _render to resolve or
    refuse by name. A column named like one of the pass's own aliases is
    skipped (see _reserved_aliases): the template route must not shadow
    the window's outputs, so `{group_n}` reads the pass's count, not the
    file's column. The same column grouped or carried still shadows — a
    pre-existing edge, left as is; headers shaped like that are rare."""
    names = {c["name"] for c in src["columns"]} - _reserved_aliases(sums)
    out, i, n = [], 0, len(template or "")
    while i < n:
        if template[i] != "{":
            i += 1
            continue
        j = template.find("}", i + 1)
        if j == -1:
            break
        key = template[i + 1:j]
        if key in names and key not in out:
            out.append(key)
        i = j + 1
    return out


def _bookend_rows(req, src, group_cols, sort_col, carry, where, params, limit,
                  row_json=False, sums=(), template=""):
    """One windowed pass: rank each row inside its group both directions,
    keep rank 1 of each. Selected values are the *row's own* — the first
    row's user, not the group's."""
    # Every column the template or output might need, deduped, stable order
    # — or the whole row when the JSON cell is requested. The template's
    # own columns ride along so {Col} resolves for ANY column, not only
    # one that happens to be grouped, ordered or carried; they are
    # projected for _render alone and never become output columns.
    pool = ([c["name"] for c in src["columns"]] if row_json
            else group_cols + [sort_col] + carry + _template_columns(template, src, sums))
    needed = []
    for c in pool:
        if c not in needed:
            needed.append(c)
    sel = ", ".join(f"{_col(src, c)} AS {q(c)}" for c in needed)
    part = ", ".join(_col(src, c) for c in group_cols)
    # Totals ride the window that is already partitioned by the group, so
    # they cost no extra pass — the same reason group_n does. Every row of
    # the group carries the total, and the bookends we keep are rows of it.
    # _numeric's guarded cast, not a bare CAST: a number column can still
    # hold the odd "-" or "n/a", and CAST turns those into 0, which is a
    # wrong total rather than a missing one.
    # The sum is always an output column; min and max only exist for the
    # {min:}/{max:} placeholders, and each window term re-runs the
    # REGEXP-guarded cast per row, so they are added only when the
    # template actually names one.
    wanted = {"sum"} | {k for k in ("min", "max") if f"{{{k}:" in (template or "")}
    sum_sel = "".join(
        f", {fn}({_numeric(src, c)}) OVER (PARTITION BY {part}) AS {q(_agg_alias(kind, c))}"
        for c in sums for kind, fn in AGGREGATES.items() if kind in wanted)
    # Direction must be stated PER COLUMN: "ORDER BY ts, rid DESC" flips only
    # rid, leaving the last-window ranked by ascending time — every group's
    # "Last" would be its earliest row with the biggest rid. On a merge,
    # rid alone collides across members, so (source_id, rid) is the
    # deterministic tie-break.
    number = _is_number_column(src, sort_col)
    first_key, last_key = _order_keys(_col(src, sort_col), number)
    tie_cols = ["s.source_id", "s.rid"] if src.get("is_merge") else ["s.rid"]
    order_asc = ", ".join([first_key] + [f"{t} ASC" for t in tie_cols])
    order_desc = ", ".join([last_key] + [f"{t} DESC" for t in tie_cols])
    sql = (
        f"SELECT {sel},"
        f" ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY {order_asc}) AS rn_first,"
        f" ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY {order_desc}) AS rn_last,"
        f" COUNT(*) OVER (PARTITION BY {part}) AS group_n"
        f"{sum_sel}"
        f" FROM {_scope(req, src)}{where}"
    )
    outer = (
        f"SELECT * FROM ({sql}) WHERE rn_first = 1 OR rn_last = 1"
        f" ORDER BY {_order_keys(q(sort_col), number)[0]}, rn_first"
    )
    res = req.store.run_sql(_inline(outer, params), limit=limit)
    cols = res["columns"]
    return [dict(zip(cols, r)) for r in res["rows"]], res["truncated"]


def _values_alias(col):
    """The List values output column's name, prefixed like the Sum column
    so it cannot collide with a carried column of the same name."""
    return f"Values of {col}"


def _template_value_columns(template):
    """The columns `{values:Column}` names, in first-mention order —
    tokenised the way _render does it."""
    out, i, n = [], 0, len(template or "")
    while i < n:
        if template[i] != "{":
            i += 1
            continue
        j = template.find("}", i + 1)
        if j == -1:
            break
        key = template[i + 1:j]
        if key.startswith("values:"):
            col = key.split(":", 1)[1].strip()
            if col not in out:
                out.append(col)
        i = j + 1
    return out


# Joins a group's values inside SQLite and splits them back apart in
# Python. A unit separator, because commas, tabs and newlines all turn up
# in real cells; this one does not.
_VALUE_SEP = "\x1f"


def _group_values(req, src, group_cols, where, params, cols, bookends):
    """{column: {group key: cell}} — each group's distinct, non-blank values
    of each column, sorted (numbers by value), at most MAX_LISTED_VALUES
    named and the rest counted. A group key is the tuple of the bookend's
    own group values, so it matches the row it is attached to whatever the
    group columns hold, NULL included.

    Only the groups in `bookends` are needed. A handful (the preview) are
    asked for by value, which keeps the preview from listing every group in
    the table; a whole result reads every group, and a cap hit there is an
    error rather than a silently empty cell."""
    if not cols or not bookends:
        return {}
    keys = {tuple(r.get(g) for g in group_cols) for r in bookends}
    gexprs = [_col(src, g) for g in group_cols]
    kcols = ", ".join(f"k{i}" for i in range(len(group_cols)))
    restrict, rparams = "", []
    if len(keys) <= 4 * PREVIEW_GROUPS:
        ors = []
        for key in keys:
            parts = []
            for expr, v in zip(gexprs, key):
                if v is None:
                    parts.append(f"{expr} IS NULL")
                else:
                    parts.append(f"{expr} = ?")
                    rparams.append(v)
            ors.append("(" + " AND ".join(parts) + ")")
        restrict = "(" + " OR ".join(ors) + ")"
    out = {}
    for col in cols:
        ident = _col(src, col)
        conds = [f"{ident} IS NOT NULL", f"{ident} != ''"] + ([restrict] if restrict else [])
        scoped = f"{where} AND " if where else " WHERE "
        number = _is_number_column(src, col)
        # Sorted in SQL too, so the values a capped cell names are the
        # first ones in order rather than whichever came up first.
        order = (f"({_numeric_of('v')} IS NULL), {_numeric_of('v')}, v" if number
                 else "v COLLATE NOCASE, v")
        sel = ", ".join(f"{e} AS k{i}" for i, e in enumerate(gexprs))
        sql = (
            f"SELECT {kcols}, group_concat(v, char(31)), MAX(n) FROM ("
            f"SELECT {kcols}, v, COUNT(*) OVER (PARTITION BY {kcols}) AS n,"
            f" ROW_NUMBER() OVER (PARTITION BY {kcols} ORDER BY {order}) AS rn"
            f" FROM (SELECT DISTINCT {sel}, {ident} AS v FROM {_scope(req, src)}"
            f"{scoped}{' AND '.join(conds)})"
            f") WHERE rn <= {MAX_LISTED_VALUES} GROUP BY {kcols}"
        )
        res = req.store.run_sql(_inline(sql, params + rparams), limit=MAX_GROUPS)
        cells = {}
        for row in res["rows"]:
            key = tuple(row[:len(group_cols)])
            joined, total = row[len(group_cols)], row[len(group_cols) + 1]
            vals = [] if joined is None else str(joined).split(_VALUE_SEP)
            vals.sort(key=_value_sort_key(number))
            cell = ", ".join(vals)
            if total and total > len(vals):
                cell += f" … (+{total - len(vals):,} more)"
            cells[key] = cell
        if res["truncated"] and not keys.issubset(cells):
            raise ValueError(f"More than {MAX_GROUPS:,} groups — too many to list "
                             f"{col} for; narrow the grouping or add a filter")
        out[col] = cells
    return out


def _value_sort_key(number):
    """The SQL ORDER BY above, again in Python — group_concat's order is
    not one SQLite promises."""
    def key(v):
        if number:
            return (0, float(v), v) if _NUM_RE.match(v) else (1, 0.0, v)
        return (v.casefold(), v)
    return key


def _render(template, row, which, sums=()):
    """{ColumnName} → the row's value, {count} → group size, {which} →
    First/Last/Only, {which:number} → the row's position in its group,
    {sum:Column} / {min:Column} / {max:Column} → that Total-up column's
    group total, smallest and largest, formatted like the Sum column,
    {values:Column} → the group's distinct values of any column (attached
    by _attach_values). Unknown names raise, naming the offender.

    The colon form is the namespace for anything computed rather than
    read: a plain name is always a field, so no column name can shadow a
    function and no function can shadow a column."""
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
            elif key == "which:number":
                # rn_first IS the position: 1 for the first, the group size
                # for the last. Not group_n for the last — a number column's
                # non-number cells sort after it, and then it is not Nth.
                out.append(str(row.get("rn_first", "")))
            elif key.startswith("values:"):
                out.append(row.get(("values", key.split(":", 1)[1].strip()), ""))
            elif key == "count":
                out.append(str(row.get("group_n", "")))
            elif ":" in key and key.split(":", 1)[0] in AGGREGATES:
                kind, col = key.split(":", 1)
                col = col.strip()
                if col not in sums:
                    raise ValueError(
                        f"{{{key}}} needs {col!r} under Total up first"
                        + (f" — totals available: {', '.join(sums)}" if sums else ""))
                out.append(_fmt_sum(row.get(_agg_alias(kind, col))))
            elif key in row:
                val = row.get(key)
                out.append("" if val is None else str(val))
            else:
                raise ValueError(f"Unknown placeholder {{{key}}} — use a column name, {{count}}, {{which}}, "
                                 f"{{which:number}}, {{values:Column}}, {{sum:Column}}, {{min:Column}} "
                                 f"or {{max:Column}}")
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


ROW_JSON_COLUMN = "Row (JSON)"


def _fmt_sum(v):
    """Whole numbers without a trailing .0 — a count of bytes should read
    as bytes. SUM is REAL so that mixed ints and decimals add up."""
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else f"{float(v):g}"


def _attach_values(bookends, group_cols, listed):
    """Hang each group's List values cells (from _group_values) on its
    bookends, under a tuple key no column name can be."""
    for r in bookends:
        key = tuple(r.get(g) for g in group_cols)
        for col, cells in listed.items():
            r[("values", col)] = cells.get(key, "")


def _header(sort_col, carry, sums, listed, row_json):
    """The output columns, in the order _emit fills them. The description
    sits before the JSON cell: it is the line an analyst reads, and the
    JSON is the long cell they open when they need it."""
    return ([sort_col] + carry + [_sum_alias(c) for c in sums]
            + [_values_alias(c) for c in listed] + ["Description"]
            + ([ROW_JSON_COLUMN] if row_json else []))


def _emit(rows, sort_col, carry, template, json_cols=None, sums=(), listed=()):
    """The output rows: one per bookend. A single-row group is both its own
    first and its last — emitted once, labelled Only (a story with one
    event has no separate ending, and "First of 1" read as if a Last had
    gone missing). `json_cols` non-None adds a cell with the bookend's
    ENTIRE row as a JSON object — the synthetic window columns
    (rn_first/rn_last/group_n) and the attached value lists never appear
    in it."""
    out = []
    for r in rows:
        labels = []
        if r.get("rn_first") == 1 and r.get("rn_last") == 1:
            labels.append("Only")
        elif r.get("rn_first") == 1:
            labels.append("First")
        elif r.get("rn_last") == 1:
            labels.append("Last")
        for which in labels:
            desc = _render(template, r, which, sums)
            row = ([r.get(sort_col, "")]
                   + [("" if r.get(c) is None else str(r.get(c))) for c in carry]
                   + [_fmt_sum(r.get(_sum_alias(c))) for c in sums]
                   + [r.get(("values", c), "") for c in listed]
                   + [desc])
            if json_cols is not None:
                row.append(json.dumps({c: r.get(c) for c in json_cols}, ensure_ascii=False))
            out.append(row)
    return out


# ---------------------------------------------------------------- routes


def meta(req):
    return {
        "operators": [{"id": k, "label": v[0], "value_kind": v[1]} for k, v in OPERATORS.items()],
        "limits": {"groups": MAX_GROUPS, "group_cols": MAX_GROUP_COLS, "preview_groups": PREVIEW_GROUPS},
        "placeholders": ["which", "which:number", "count", "values:<column>",
                         "sum:<column>", "min:<column>", "max:<column>"],
        "which_values": ["First", "Last", "Only"],
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


def _result(req, body, limit, *, whole=False):
    """What preview, rows and create share: validate, run the windowed
    pass, list the distinct values it asks for, render. `whole` is create's
    rule that a result cut short by `limit` is refused, not written."""
    (src, group_cols, sort_col, carry, sums, listed,
     where, params, template, row_json) = _validated(req, body)
    bookends, truncated = _bookend_rows(req, src, group_cols, sort_col, carry, where, params,
                                        limit=limit, row_json=row_json, sums=sums, template=template)
    if whole and truncated:
        raise ValueError(f"More than {MAX_GROUPS:,} groups — narrow the grouping or add a filter")
    wanted = list(dict.fromkeys(listed + _template_value_columns(template)))
    _attach_values(bookends, group_cols,
                   _group_values(req, src, group_cols, where, params, wanted, bookends))
    json_cols = [c["name"] for c in src["columns"]] if row_json else None
    return {
        "src": src, "group_cols": group_cols, "where": where, "params": params,
        "columns": _header(sort_col, carry, sums, listed, row_json),
        "rows": _emit(bookends, sort_col, carry, template, json_cols, sums, listed),
        "truncated": truncated,
    }


def preview(req):
    """POST .../preview — the first PREVIEW_GROUPS groups' bookends, plus the
    total group count, so 'Create table' says what it will make before it
    makes it."""
    res = _result(req, req.body or {}, PREVIEW_GROUPS * 2 + 2)
    src = res["src"]
    part = ", ".join(_col(src, c) for c in res["group_cols"])
    count_sql = f"SELECT COUNT(*) FROM (SELECT 1 FROM {_scope(req, src)}{res['where']} GROUP BY {part})"
    total_groups = req.store.run_sql(_inline(count_sql, res["params"]), limit=1)["rows"][0][0]
    return {"columns": res["columns"], "rows": res["rows"], "total_groups": total_groups}


def rows(req):
    """POST .../rows — the ENTIRE result as rows, for "Copy result": the
    preview shows only the first few groups, and copying should hand over
    what Create would have made, not what happened to be on screen."""
    res = _result(req, req.body or {}, MAX_COPY_ROWS)
    return {"columns": res["columns"], "rows": res["rows"], "truncated": res["truncated"]}


def create(req):
    """POST .../create — run the whole thing and land it as a new source.

    `timeline_tag` (optional, a tag name) puts the bookends on the unified
    Timeline: the Timeline is exactly "every tagged row across the case",
    so a tag IS the membership. The tag is found or created by name and
    applied to every row of the new table through set_tags — the public
    path that routes _apply_tag_change (invariant #7), so undo works on it
    like any other tag write. Rids are contiguous from 1 on every ingest
    path (invariant #2), which is what makes range(1, n+1) exact."""
    body = req.body or {}
    res = _result(req, body, MAX_GROUPS * 2, whole=True)
    src, header, out_rows = res["src"], res["columns"], res["rows"]
    # The table's label as the analyst knows it — its nickname when it has
    # one, as everywhere else in the app.
    name = (body.get("name") or "").strip() or f"First-Last of {src.get('nickname') or src['name']}"
    rec = req.store.ingest_rows(header, out_rows, name=name)
    out = {"source": {"id": rec["id"], "name": rec["name"], "row_count": rec["row_count"]}}
    tag_name = (body.get("timeline_tag") or "").strip()
    if tag_name:
        tag = next((t for t in req.store.list_tags() if t["name"] == tag_name), None)
        if tag is None:
            tag = req.store.upsert_tag(None, tag_name, TIMELINE_TAG_COLOR, None)
        req.store.set_tags(rec["id"], list(range(1, rec["row_count"] + 1)), tag["id"], True)
        out["timeline_tag"] = {"id": tag["id"], "name": tag["name"]}
    return out
