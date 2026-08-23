"""Pivot table plugin for Winnow — Excel's PivotTable over an ingested table.

Drag fields into Rows / Columns / Values / Filters and get a cross-tab with
subtotals and grand totals: "4624s per host per day", "distinct users per
process", "bytes transferred per destination". The tab is the plugin's own ES
module (register_tab); this file is the backend it queries (register_api).

Three things about the shape of this, since they're the parts that aren't
obvious:

**It aggregates the whole table, not the grid's current view.** A view is a
temp table in Winnow's scratch database, which `run_sql`'s read-only
connection to the case file can't see — and reaching for the private reader
that could is exactly what the plugin guide says not to do. That's why the
Filters area exists: it's the pivot's own filtering, the way Excel's is, and
it compiles into the same WHERE clause the aggregation runs under.

**Subtotals come from grouping sets, not from re-adding the leaves.** Ask for
a Count Distinct subtotal and summing the child cells gives you the wrong
answer whenever a value appears under more than one child. So the client
declares every grouping level it wants to draw and each one is aggregated
from the source independently — correct for every aggregation rather than
just the associative ones. It costs one GROUP BY per level (capped at
MAX_GROUP_SETS), which is the trade the correctness is worth.

**Sum/Average/numeric Min/Max go through the same guarded cast the rest of
Winnow uses.** Columns are stored as TEXT (evidence fidelity), and SQLite's
bare `CAST(text AS REAL)` turns "N/A" into 0.0 — indistinguishable from a
real zero in a total. NUM_RE gates the cast so junk becomes NULL and drops
out of the aggregate instead of quietly dragging an average down.
"""

from store import NUM_RE, q

PLUGIN = {
    "name": "pivot-table",
    "version": "1.0.0",
    "description": "Excel-style pivot tables: drag fields into rows, columns and values for cross-tabs with subtotals.",
}

WINNOW_API_VERSION = 1

# One GROUP BY per grouping level. Rows × columns levels stay small in
# practice (Excel's own field wells get unusable well before this), and the
# cap is what stops a pathological request turning into 40 table scans.
MAX_GROUP_SETS = 16
# Distinct groups returned per level. A pivot with more rows than this isn't
# readable anyway — the answer is another field in Filters, not more rows.
MAX_GROUPS = 20_000
# Rows shown by the drill-down ("show details" on a cell).
MAX_DETAIL_ROWS = 500

AGGREGATIONS = {
    # id: (label, needs_column)
    "count": ("Count", False),
    "count_distinct": ("Distinct count", True),
    "sum": ("Sum", True),
    "avg": ("Average", True),
    "min": ("Min", True),
    "max": ("Max", True),
}

# Filter operators, mirroring the header filter box's vocabulary so an
# analyst doesn't learn a second one. value_kind: 'one' | 'many' | 'none'.
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
        id="pivot",
        label="Pivot",
        entry="ui/tab.js",
        description=(
            "Excel-style pivot table: drag fields into Rows, Columns and Values "
            "for a cross-tab with subtotals, then click a cell to see the rows "
            "behind it."
        ),
    )
    api.register_api("meta", meta, methods=["GET"])
    api.register_api("aggregate", aggregate, methods=["POST"])
    api.register_api("values", values, methods=["POST"])
    api.register_api("detail", detail, methods=["POST"])


# --------------------------------------------------------------- helpers


def _source(req, body):
    """The validated source for this request. Merges are rejected: they have
    no single backing table, and `src_<id>` is what every query below needs."""
    if req.store is None:
        raise ValueError("Open a case first")
    try:
        source_id = int(body.get("source_id"))
    except (TypeError, ValueError):
        raise ValueError("source_id is required")
    if source_id < 0:
        raise ValueError("Pick a real table — merged sources aren't supported here")
    try:
        return req.store.get_source(source_id)
    except KeyError:
        raise ValueError(f"No source {source_id}")


def _check_columns(src, columns):
    """Every column name is analyst-supplied (it round-trips through the
    browser), so nothing reaches SQL that isn't a real column of this table."""
    names = {c["name"] for c in src["columns"]}
    for col in columns:
        if col not in names:
            raise ValueError(f"No column {col!r} in {src['name']}")


def _numeric(col):
    """CAST guarded by the same pattern ingest-time type inference uses, so
    non-numeric text aggregates as NULL rather than as 0.0."""
    ident = q(col)
    return f"(CASE WHEN {ident} REGEXP '{NUM_RE.pattern}' THEN CAST({ident} AS REAL) ELSE NULL END)"


def _agg_expr(measure, coltypes):
    """SQL for one Values field. Min/Max stay textual for non-numeric columns
    on purpose: the earliest and latest value of an ISO timestamp column is
    exactly what MIN()/MAX() over its text gives you, and casting it to a
    number would throw the whole column away."""
    agg = measure.get("agg", "count")
    if agg not in AGGREGATIONS:
        raise ValueError(f"Unknown aggregation {agg!r}")
    column = measure.get("column")
    if agg == "count":
        # Excel's Count is COUNTA — how many rows have a value in *this*
        # field, not how many rows there are. The difference is the whole
        # point of putting a specific field in Values: "Count of Bytes" over
        # a column that's half blank must not read the same as "Count of
        # Host". SQLite's COUNT(col) only skips NULL, and a missing value in
        # an ingested table is '' (ragged rows are padded, not nulled), so
        # the blank test has to be explicit.
        if not column:
            return "COUNT(*)"
        return f"SUM(CASE WHEN {q(column)} IS NOT NULL AND {q(column)} != '' THEN 1 ELSE 0 END)"
    if not column:
        raise ValueError(f"{AGGREGATIONS[agg][0]} needs a column")
    if agg == "count_distinct":
        return f"COUNT(DISTINCT {q(column)})"
    if agg in ("min", "max") and coltypes.get(column) != "number":
        return f"{agg.upper()}({q(column)})"
    return f"{agg.upper()}({_numeric(column)})"


def _where(filters, coltypes):
    """Compiles the Filters area into a WHERE clause plus its parameters.

    Values are bound, never interpolated — the identifiers are the only thing
    that goes into the SQL text, and those are validated against the source's
    real column list first.
    """
    clauses, params = [], []
    for f in filters or []:
        col, op = f.get("column"), f.get("op", "in")
        if op not in OPERATORS:
            raise ValueError(f"Unknown filter operator {op!r}")
        ident = q(col)
        kind = OPERATORS[op][1]
        if kind == "many":
            vals = [v for v in (f.get("values") or [])]
            if not vals:
                continue  # an empty selection filters nothing, like Excel's "(All)"
            marks = ",".join("?" * len(vals))
            # A NULL never matches IN/NOT IN, so the not_in arm spells out
            # that a missing value is *kept* — otherwise excluding one value
            # would silently drop every empty cell too.
            clauses.append(f"{ident} IN ({marks})" if op == "in"
                           else f"({ident} NOT IN ({marks}) OR {ident} IS NULL)")
            params.extend(vals)
        elif kind == "one":
            val = f.get("value")
            if val in (None, ""):
                continue
            if op == "contains":
                clauses.append(f"{ident} LIKE ?"); params.append(f"%{val}%")
            elif op == "not_contains":
                clauses.append(f"({ident} NOT LIKE ? OR {ident} IS NULL)"); params.append(f"%{val}%")
            elif op == "starts":
                clauses.append(f"{ident} LIKE ?"); params.append(f"{val}%")
            else:  # gt / lt — numeric, through the same guarded cast
                try:
                    number = float(val)
                except (TypeError, ValueError):
                    raise ValueError(f"{OPERATORS[op][0]} needs a number, got {val!r}")
                clauses.append(f"{_numeric(col)} {'>' if op == 'gt' else '<'} ?")
                params.append(number)
        else:
            clauses.append(f"({ident} IS NULL OR {ident} = '')" if op == "empty"
                           else f"({ident} IS NOT NULL AND {ident} != '')")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _inline(sql, params):
    """run_sql takes no parameters, so the bound values are inlined here —
    through SQLite's own ''-doubling for strings, and as plain literals for
    the numbers _where() has already coerced with float().

    Skipping over string literals is the load-bearing part, and it is not
    theoretical: the numeric guard embeds NUM_RE's pattern, which contains
    two `?` of its own. A naive scan reads those as placeholders and either
    runs off the end of the parameter list or — worse — silently shifts
    every subsequent value into the wrong slot. Winnow's own
    _inline_sql_params walks literals for exactly the same reason."""
    out, i, k, n = [], 0, 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":   # '' is an escaped quote, not the end
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


# ---------------------------------------------------------------- routes


def meta(req):
    """GET /api/plugin/pivot/meta -> the vocabulary the UI builds its pickers
    from, so the two can't drift apart."""
    return {
        "aggregations": [{"id": k, "label": v[0], "needs_column": v[1]} for k, v in AGGREGATIONS.items()],
        "operators": [{"id": k, "label": v[0], "value_kind": v[1]} for k, v in OPERATORS.items()],
        "limits": {"groups": MAX_GROUPS, "group_sets": MAX_GROUP_SETS, "detail_rows": MAX_DETAIL_ROWS},
    }


def aggregate(req):
    """POST /api/plugin/pivot/aggregate
    body: {source_id, values: [{column?, agg}], group_sets: [[col, ...], ...],
           filters: [...]}
    -> {sets: [{keys: [col, ...], rows: [[k1, .., m1, ..], ...], truncated}], ...}

    One entry per requested grouping level, in the order asked for. `[]` is
    the grand total (a single row of measures, no keys).
    """
    body = req.body or {}
    src = _source(req, body)
    coltypes = {c["name"]: c["type"] for c in src["columns"]}

    measures = body.get("values") or [{"agg": "count"}]
    for m in measures:
        if m.get("column"):
            _check_columns(src, [m["column"]])
    agg_sql = [_agg_expr(m, coltypes) for m in measures]

    group_sets = body.get("group_sets")
    if not isinstance(group_sets, list) or not group_sets:
        raise ValueError("group_sets is required")
    if len(group_sets) > MAX_GROUP_SETS:
        raise ValueError(f"Too many grouping levels ({len(group_sets)} > {MAX_GROUP_SETS})")
    for keys in group_sets:
        if not isinstance(keys, list):
            raise ValueError("each grouping level must be a list of column names")
        _check_columns(src, keys)

    for f in body.get("filters") or []:
        _check_columns(src, [f.get("column")])
    where, params = _where(body.get("filters"), coltypes)
    table = q(src["table_name"])

    out = []
    for keys in group_sets:
        key_sql = ", ".join(q(k) for k in keys)
        select = ", ".join(([key_sql] if keys else []) + agg_sql)
        sql = f"SELECT {select} FROM {table}{where}"
        if keys:
            sql += f" GROUP BY {', '.join(str(i + 1) for i in range(len(keys)))}"
        res = req.store.run_sql(_inline(sql, params), limit=MAX_GROUPS)
        out.append({"keys": keys, "rows": res["rows"], "truncated": res["truncated"]})
    return {"sets": out, "measures": measures, "row_count": src["row_count"]}


def values(req):
    """POST /api/plugin/pivot/values -> distinct values of one column, most
    common first. Backs the Filters area's checkbox list."""
    body = req.body or {}
    src = _source(req, body)
    column = body.get("column")
    _check_columns(src, [column])
    limit = min(int(body.get("limit") or 500), 5000)
    sql = (f"SELECT {q(column)} AS value, COUNT(*) AS n FROM {q(src['table_name'])}"
           f" GROUP BY 1 ORDER BY n DESC")
    res = req.store.run_sql(sql, limit=limit)
    return {"values": [{"value": r[0], "count": r[1]} for r in res["rows"]],
            "truncated": res["truncated"]}


def detail(req):
    """POST /api/plugin/pivot/detail -> the rows behind one cell.

    Excel's "Show Details". `cell` is the row/column field values that
    identify the cell; they're turned into equality filters and AND'ed onto
    whatever the Filters area already says.
    """
    body = req.body or {}
    src = _source(req, body)
    coltypes = {c["name"]: c["type"] for c in src["columns"]}
    cell = body.get("cell") or []
    for pair in cell:
        _check_columns(src, [pair.get("column")])

    filters = list(body.get("filters") or [])
    for f in filters:
        _check_columns(src, [f.get("column")])
    # A cell keyed on an empty value means "the rows where this column is
    # blank", which is a different question from "equals the empty string" —
    # `empty` covers both spellings the way the rest of Winnow does.
    for pair in cell:
        value = pair.get("value")
        filters.append({"column": pair["column"], "op": "empty"} if value in (None, "")
                       else {"column": pair["column"], "op": "in", "values": [value]})

    where, params = _where(filters, coltypes)
    cols = [c["name"] for c in src["columns"]]
    select = ", ".join(["rid"] + [q(c) for c in cols])
    sql = f"SELECT {select} FROM {q(src['table_name'])}{where} ORDER BY rid"
    res = req.store.run_sql(_inline(sql, params), limit=MAX_DETAIL_ROWS)
    return {"columns": ["Line"] + cols, "rows": res["rows"], "truncated": res["truncated"]}
