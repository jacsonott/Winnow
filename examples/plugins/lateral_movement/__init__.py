"""Lateral-movement plugin for Winnow.

The reference example for the plugin system's **custom UI** hooks — and,
since v2, for `req.storage` (per-plugin workspace persistence) and for
shipping **defaults** the way the app ships filter defaults: a JSON file
of movement-event definitions shaped against real KAPE triage output,
bound to tables by required columns.

A *movement event* is one way a table describes hosts touching hosts:
{src_col, dst_col, label_col?, actor_col?, time_col?, conditions[],
fails?, session?}. The tab draws any number of (source, event) selections
at once — several event types out of one EVTX export, plus a firewall
table, plus a netflow table — from one feed (`feed`): every matching row
folded to a hop between two HOSTS, the account it moved as, and the
minute it happened.

Folding is what the views stand on. One machine is written a handful of
ways across an export — `WKSTN-17.corp.local` in Computer, `WKSTN-17
(10.1.0.117)` or `- (10.1.0.117)` in RemoteHost, a bare `10.1.0.117` on
RDP and share events — and v2 drew each spelling as its own node, so a
host that received a logon and then made one was two unconnected dots.
`fold_host` strips the field prefix, the bracketed address, UNC slashes
and the domain, folds case, and maps an address to the name the rows put
beside it (across every selection, so a flow export's bare addresses
fold to the names the event log gives them).

The backend goes through Store.run_sql on purpose: it opens its own
read-only connection, so a big aggregation never holds the shared
connection's lock (invariant #4), and it inherits the SQL pane's
defense-in-depth statement checks — and its TS_NORMALIZE() — for free.
"""

import collections
import datetime as _dt
import json
import os
import re

from winnow import defaults
from winnow.store import q, raw_epoch_sql, union_all

PLUGIN = {
    "name": "lateral-movement",
    "version": "3.0.0",
    "description": "Follow lateral movement through a case: hops between hosts in time order, by account, "
                   "with credential switches, RDP sessions and paths to the hosts that matter.",
}

WINNOW_API_VERSION = 15   # winnow.openFiltered options: "Open its rows" filters on every spelling

MAX_SELECTIONS = 20
FEED_ROWS = 250_000      # (event, hosts, account, minute) rows across every selection
PREVIEW_TOP = 12         # busiest links the event builder lists
PREVIEW_ROWS = 50_000    # grouped rows a preview reads before saying "or more"
PREVIEW_HOPS = 20_000    # of them sent back, for the tab to compare with what it draws
VALUES_TOP = 40          # suggestions per condition column
SPELLINGS = 12           # raw spellings kept per host, for tooltips and "Open its rows"
_DEFAULTS_FILE = os.path.join(os.path.dirname(__file__), "defaults.json")

OPS = {
    "equals": lambda col, ph: f"{col} = {ph}",
    "not_equals": lambda col, ph: f"{col} != {ph}",
    "contains": lambda col, ph: f"instr(lower({col}), lower({ph})) > 0",
    "not_contains": lambda col, ph: f"instr(lower({col}), lower({ph})) = 0",
}


def register(api):
    api.register_tab(
        id="graph",
        label="Lateral movement",
        entry="ui/tab.js",
        description=(
            "Movement events (logons, RDP, share access, netflow) from any "
            "number of tables: hops between hosts over time, a host matrix, "
            "accounts ranked by what is unusual about them, and paths "
            "between hosts."
        ),
    )
    api.register_api("feed", feed, methods=["POST"])
    api.register_api("preview", preview, methods=["POST"])
    api.register_api("values", values, methods=["POST"])
    api.register_api("defs", defs, methods=["GET", "POST"])


# ---------------------------------------------------------------- defaults


def _shipped_defaults():
    with open(_DEFAULTS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("events", [])


def _header_sets():
    """The app's shipped header-set nicknames (EvtxECmd, MFTECmd, ...) —
    a movement event ties to one by name, so it only offers itself on a
    table that actually IS that artifact, the same way a filter default
    binds to its header set. {name: [columns]}."""
    return {name: cols for name, cols in defaults.headers()["nicknames"]}


def defs(req):
    """GET  -> {shipped: [...], saved: [...]}
    POST {saved: [...]} -> same — replaces the analyst's saved definitions
    (machine-level, via req.storage; survives case switches and updates,
    exactly like the app's own saved filters)."""
    if req.method == "GET":
        saved = (req.storage.get() if req.storage else {}).get("saved", [])
        return {"shipped": _shipped_defaults(), "saved": saved,
                "header_sets": _header_sets()}
    saved = (req.body or {}).get("saved")
    if not isinstance(saved, list):
        raise ValueError("Body must be {saved: [event definitions]}")
    for d in saved:
        _validate_def_shape(d)
    if req.storage is None:
        raise ValueError("No storage available")
    req.storage.set({"saved": saved})
    # header_sets too: the tab replaces its whole copy with this response,
    # and a reply without them left every header-set binding (and the
    # builder's "Offer it on" choice) resolving against nothing until the
    # next GET.
    return {"shipped": _shipped_defaults(), "saved": saved,
            "header_sets": _header_sets()}


def _validate_def_shape(d):
    if not isinstance(d, dict) or not d.get("name"):
        raise ValueError("Every definition needs a name")
    for k in ("src_col", "dst_col"):
        if not d.get(k):
            raise ValueError(f"Definition {d.get('name')!r} needs {k}")
    hs = d.get("header_set")
    if hs and hs not in _header_sets():
        # Same failure mode as an unresolvable filter header_set — a
        # definition bound to a set that doesn't exist would never appear
        # on any table, silently.
        raise ValueError(f"Definition {d['name']!r} names an unknown header set {hs!r}")
    for c in d.get("conditions") or []:
        if c.get("op") not in set(OPS) | {"in", "not_in"}:
            raise ValueError(f"Unknown condition op {c.get('op')!r}")
        if not c.get("column"):
            raise ValueError("Every condition needs a column")
    _validate_session(d.get("session"), d["name"])


def _validate_session(rule, name):
    """`session` pairs the rows that open a session with the ones that
    close it — {column, open: [values], close: [values]}, the shipped RDP
    event's 21/25 and 24 — so the tab can draw a session as a span."""
    if rule is None:
        return
    if (not isinstance(rule, dict) or not rule.get("column")
            or not isinstance(rule.get("open"), list) or not isinstance(rule.get("close"), list)
            or not rule["open"] or not rule["close"]):
        raise ValueError(f"Definition {name!r}: session needs a column and open and close values")


# ------------------------------------------------------------- the SQL


def _derived_names(src):
    return {c["name"] for c in src["columns"] if c.get("derived")}


def _from_clause(src):
    base = f"{q(src['table_name'])} s"
    if not _derived_names(src):
        return base
    return f"{base} LEFT JOIN {q('drv_' + str(src['id']))} d ON d.rid = s.rid"


def _col(src, name):
    if src.get("is_merge"):
        return f"s.{q(name)}"
    return f"{'d' if name in _derived_names(src) else 's'}.{q(name)}"


def _scope(req, src):
    """A plain source is its table (plus derived sidecar); a merge is a
    UNION ALL of its members aliased `s` (invariant #9)."""
    if not src.get("is_merge"):
        return _from_clause(src)
    branches = []
    for mid in src["member_source_ids"]:
        m = req.store.get_source(mid)
        sel = req.store.merge_select_list(src, m)  # NULL-pads columns a member lacks
        branches.append(f"SELECT s.rid AS rid, {sel} FROM {_from_clause(m)}")
    return "(" + union_all(branches) + ") s"


def _lit(value):
    return "'" + str(value).replace("'", "''") + "'"


def _conditions_sql(src, conditions):
    parts = []
    names = {c["name"] for c in src["columns"]}
    for c in conditions or []:
        col_name, op, value = c.get("column"), c.get("op"), c.get("value", "")
        if col_name not in names:
            raise ValueError(f"No column {col_name!r} in {src['name']}")
        col = _col(src, col_name)
        if op in ("in", "not_in"):
            vals = [v.strip() for v in str(value).split(",") if v.strip()]
            if not vals:
                raise ValueError(f"'{op}' needs a comma-separated list")
            parts.append(f"{col} {'NOT ' if op == 'not_in' else ''}IN ({', '.join(_lit(v) for v in vals)})")
        elif op in OPS:
            parts.append(OPS[op](col, _lit(value)))
        else:
            raise ValueError(f"Unknown condition op {op!r}")
    return parts


def _normalized_bounds(req, start, end):
    """The timeframe's bounds in the shape TS_NORMALIZE(col) compares
    against — the core runs both through the same function before its
    range check, and so must this. Compared verbatim, any bound the
    Timeframe dialog accepts in another spelling (1/5/2024 8:00, a 'T'
    separator) cut the views to a different row set from the grid while
    the status line said "timeframe applied". A bound that doesn't parse
    is dropped, which is what the core does with it too."""
    if not (start or end):
        return None, None
    row = req.store.run_sql(
        f"SELECT TS_NORMALIZE({_lit(start or '')}), TS_NORMALIZE({_lit(end or '')})", limit=1)["rows"]
    lo, hi = (row[0][0], row[0][1]) if row else (None, None)
    return (lo if start else None), (hi if end else None)


# EVTX "not present" spellings, and loopback — a host talking to itself
# (127.0.0.1, ::1, LOCAL, '- (127.0.0.1)') is not movement. fold_host
# catches the rest once the spellings are folded; this keeps the bulk of
# them out of the GROUP BY.
_NOISE = "('', '-', '- (-)', '- (127.0.0.1)', '- (::1)', 'LOCAL', 'localhost', '127.0.0.1', '::1')"


def _source(req, source_id):
    try:
        return req.store.get_source(int(source_id))
    except (TypeError, ValueError, KeyError):
        raise ValueError(f"No table {source_id!r}")


def _prepare(req, sel, start=None, end=None):
    """One selection as SQL pieces, validated against its table. `cond` is
    the event's own conditions; `where` adds the endpoint-noise filter and
    the timeframe on top. feed and preview both build on this, so the
    builder's preview counts exactly the rows the views draw."""
    if req.store is None:
        raise ValueError("Open a case first")
    src = _source(req, sel.get("source_id"))
    names = {c["name"] for c in src["columns"]}
    sc_name, dc_name = sel.get("src_col"), sel.get("dst_col")
    roles = {r: sel.get(f"{r}_col") or None for r in ("label", "actor", "time")}
    session = sel.get("session") or None
    _validate_session(session, sel.get("name") or "this event")
    for col in (sc_name, dc_name, *[c for c in roles.values() if c], *([session["column"]] if session else [])):
        if col not in names:
            raise ValueError(f"No column {col!r} in {src['name']}")
    if sc_name == dc_name:
        raise ValueError("Source and destination must be different columns")
    cond = _conditions_sql(src, sel.get("conditions"))
    sc, dc = _col(src, sc_name), _col(src, dc_name)
    where = cond + [f"{sc} NOT IN {_NOISE}", f"{dc} NOT IN {_NOISE}", f"{sc} != {dc}"]
    tcol = _col(src, roles["time"]) if roles["time"] else None
    if tcol and start:
        where.append(f"TS_NORMALIZE({tcol}) >= {_lit(start)}")
    if tcol and end:
        where.append(f"TS_NORMALIZE({tcol}) <= {_lit(end)}")
    def opt(role):
        return _col(src, roles[role]) if roles[role] else "NULL"
    # Minutes since the epoch: the views place hops to the minute, and a
    # GROUP BY on it is what keeps a million logons a feed, not a dump.
    minute = f"CAST({raw_epoch_sql(tcol)} / 60 AS INTEGER)" if tcol else "NULL"
    select = (f"{sc} AS src, {dc} AS dst, {opt('label')} AS u, {opt('actor')} AS a, "
              f"{_col(src, session['column']) if session else 'NULL'} AS sv, {minute} AS m, COUNT(*) AS n")
    return {"src": src, "cond": cond, "where": where, "select": select,
            "strip": bool(sel.get("strip_prefix")), "session": session}


def _grouped(req, p, k):
    # By position, not by alias: a GROUP BY name resolves to the table's
    # own column before the result's alias, and a flow export may well
    # have a column called src.
    return (f"SELECT {int(k)} AS k, {p['select']} FROM {_scope(req, p['src'])}"
            f" WHERE {' AND '.join(p['where'])} GROUP BY 1, 2, 3, 4, 5, 6, 7")


# ---------------------------------------------------------------- folding

_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_BRACKETED = re.compile(r"^(.*?)\s*\(([^()]*)\)$")
_HOST_NOISE = {"", "-", "LOCAL", "LOCALHOST", "N/A"}
_LOOPBACK = {"127.0.0.1", "::1"}
_FIELD_PREFIX = re.compile(r"^[A-Za-z ]+:\s+")


def split_host(raw, strip_prefix=False):
    """'WKSTN-17 (10.1.0.117)' -> ('WKSTN-17', '10.1.0.117'); with
    strip_prefix, 'TargetServerName: FILESRV' -> ('FILESRV', None) — EVTX
    payload fields carry their field name in the value."""
    v = str(raw or "").strip()
    if strip_prefix and ": " in v:
        v = v.split(": ", 1)[1].strip()
    name, ip = v, None
    m = _BRACKETED.match(v)
    if m:
        name, ip = m.group(1).strip(), m.group(2).strip()
    if name.startswith("\\\\"):
        name = name[2:]
    return name, ip


def is_address(n):
    return bool(_IPV4.match(n)) or ":" in n


def norm_name(name):
    """Upper case, the domain gone, noise spellings empty; an address is
    kept whole."""
    n = str(name or "").strip().upper()
    if n in _HOST_NOISE:
        return ""
    return n if is_address(n) else n.split(".", 1)[0]


def norm_account(raw):
    """'Target: CORP\\\\user' -> 'CORP\\\\USER'. Windows account names are
    case-insensitive, and '-' is EVTX for "none"."""
    v = _FIELD_PREFIX.sub("", str(raw or "").strip()).upper()
    return "" if v == "-" else v


def address_names(endpoints):
    """{address: name} from (raw, strip_prefix, weight) endpoints: each
    address goes to the name rows write beside it most often."""
    seen = collections.defaultdict(collections.Counter)
    for raw, strip, n in endpoints:
        name, ip = split_host(raw, strip)
        nn = norm_name(name)
        if ip and nn and not is_address(nn):
            seen[ip.upper()][nn] += n
    return {ip: c.most_common(1)[0][0] for ip, c in seen.items()}


def fold_host(raw, strip_prefix, ipmap):
    """One host however it is spelled; '' for one that names nothing."""
    name, ip = split_host(raw, strip_prefix)
    n = norm_name(name)
    if not n and ip:
        n = norm_name(ip)
    if n and is_address(n) and n in ipmap:
        n = ipmap[n]
    return "" if n in _LOOPBACK else n


class _Folder:
    """fold_host and norm_account, each worked out once per distinct
    spelling. A feed is hundreds of thousands of rows but only a few
    thousand ways of writing a host, and folding every row afresh was
    most of the feed's time."""

    def __init__(self, endpoints, ipmap_hint=None):
        # endpoints: (raw, strip_prefix, rows) — summed per spelling first
        weights = collections.Counter()
        for raw, strip, n in endpoints:
            weights[(raw, bool(strip))] += n
        self.ipmap = dict(ipmap_hint or {})
        self.ipmap.update(address_names((raw, strip, n) for (raw, strip), n in weights.items()))
        self._hosts, self._accounts = {}, {}

    def host(self, raw, strip):
        key = (raw, bool(strip))
        h = self._hosts.get(key)
        if h is None:
            h = self._hosts[key] = fold_host(raw, strip, self.ipmap)
        return h

    def account(self, raw):
        a = self._accounts.get(raw)
        if a is None:
            a = self._accounts[raw] = norm_account(raw)
        return a


def _pair_sessions(rows, rule):
    """Sessions as (s, d, u, opened, closed) from (s, d, u, value, minute)
    rows in time order: an opening value starts one, a closing value ends
    the open one with the same hosts and account. A second logon (the
    first opening value) on a key that is still open ends the earlier
    session with no close logged; closed is None for those, and for any
    left open at the end."""
    opens, closes = [str(v) for v in rule["open"]], [str(v) for v in rule["close"]]
    out, open_ = [], {}
    # within a minute, open before close: a session that lasted under a
    # minute reads as one, rather than a close for nothing and an open
    # that never ends
    for s, d, u, v, m in sorted(rows, key=lambda r: (r[4], 0 if str(r[3]) in opens else 1)):
        v, key = str(v), (s, d, u)
        if v == opens[0] and key in open_:
            out.append((s, d, u, open_.pop(key), None))
        if v in opens:
            open_.setdefault(key, m)
        elif v in closes and key in open_:
            out.append((s, d, u, open_.pop(key), m))
    out.extend((s, d, u, m, None) for (s, d, u), m in open_.items())
    return out


def build_feed(rows, sels, ipmap_hint=None):
    """The views' feed from grouped rows (k, src, dst, u, a, sv, m, n).

    Hosts fold (fold_host) against one address map built from every
    selection's rows; a hop is (k, src, dst, account, minute) with a row
    count. `explicit` is each row whose acting account differs from the
    account it names (a 4648: who used whose credentials), and `sessions`
    pairs a session rule's opening and closing rows. Hosts and accounts
    come back as lists, the hops pointing into them by index, and every
    minute relative to t0, the earliest."""
    fold = _Folder(((raw, sels[r[0]].get("strip_prefix"), r[7]) for r in rows for raw in (r[1], r[2])),
                   ipmap_hint)
    per = [{"rows": 0, "drawn": 0, "empty": 0, "self": 0, "untimed": 0} for _ in sels]
    hops, explicit = collections.Counter(), set()
    spell = collections.defaultdict(collections.Counter)
    uspell = collections.defaultdict(collections.Counter)
    sess_rows = collections.defaultdict(list)
    for k, rs, rd, ru, ra, sv, m, n in rows:
        sel, st = sels[k], per[k]
        st["rows"] += n
        strip = sel.get("strip_prefix")
        s, d = fold.host(rs, strip), fold.host(rd, strip)
        if not s or not d:
            st["empty"] += n
            continue
        if s == d:
            st["self"] += n
            continue
        if m is None:
            st["untimed"] += n
            continue
        st["drawn"] += n
        u = fold.account(ru)
        hops[(k, s, d, u, m)] += n
        spell[s][(k, 0, str(rs))] += n
        spell[d][(k, 1, str(rd))] += n
        if u:
            uspell[u][(k, str(ru))] += n
        if sel.get("actor_col"):
            a = fold.account(ra)
            if a and u and a != u:
                explicit.add((k, s, d, a, u, m))
        if sel.get("session"):
            sess_rows[k].append((s, d, u, sv, m))
    sessions = [(k, *x) for k, rs in sess_rows.items() for x in _pair_sessions(rs, sels[k]["session"])]

    hosts = sorted({h for (_, s, d, _, _) in hops for h in (s, d)})
    users = sorted({""} | {u for (_, _, _, u, _) in hops} | {a for (_, _, _, a, _, _) in explicit})
    H = {h: i for i, h in enumerate(hosts)}
    U = {u: i for i, u in enumerate(users)}
    t0 = min((m for (_, _, _, _, m) in hops), default=0)
    t1 = max((m for (_, _, _, _, m) in hops), default=0)
    return {
        "t0": t0 * 60,                    # epoch seconds of the earliest minute
        "span": t1 - t0 + 1,              # minutes
        "hosts": hosts,
        "users": users,                   # "" is a hop with no account (a flow export)
        "hops": sorted(([k, H[s], H[d], U[u], m - t0, n] for (k, s, d, u, m), n in hops.items()),
                       key=lambda h: (h[4], h[0])),
        "explicit": sorted(([k, H[s], H[d], U[a], U[u], m - t0] for (k, s, d, a, u, m) in explicit),
                           key=lambda x: x[5]),
        "sessions": sorted(([k, H[s], H[d], U[u], a - t0, None if b is None else b - t0]
                            for (k, s, d, u, a, b) in sessions), key=lambda x: x[4]),
        # How each host and account is written, commonest first: "seen as"
        # in tooltips, and the values "Open its rows" filters the table on.
        "spellings": {H[h]: [[k, role, raw, n] for (k, role, raw), n in spell[h].most_common(SPELLINGS)]
                      for h in hosts},
        "account_spellings": {U[u]: [[k, raw, n] for (k, raw), n in uspell[u].most_common(4)]
                              for u in users if u},
        "ipmap": fold.ipmap,
        "per": per,
    }


# ------------------------------------------------------------------- feed


def feed(req):
    """POST /api/plugin/lateral_movement/feed
    body: {selections: [{source_id, src_col, dst_col, label_col?, actor_col?,
                         time_col?, strip_prefix?, session?, conditions?}],
           time?: {start?, end?}}   # the case timeframe filter, verbatim
    -> build_feed's dict, plus `truncated` when the grouped rows passed
    FEED_ROWS (the earliest are kept: narrow the timeframe for the rest).

    Every view in the tab reads this one answer; a selection's index is
    its `k` on every hop, which is how the tab colours by event type."""
    if req.store is None:
        raise ValueError("Open a case first")
    b = req.body or {}
    selections = b.get("selections") or []
    if not selections:
        raise ValueError("Pick at least one movement event")
    if len(selections) > MAX_SELECTIONS:
        raise ValueError(f"At most {MAX_SELECTIONS} movement events at once")
    time_range = b.get("time") or {}
    start, end = _normalized_bounds(req, time_range.get("start") or None,
                                    time_range.get("end") or None)
    branches = [_grouped(req, _prepare(req, sel, start, end), k) for k, sel in enumerate(selections)]
    res = req.store.run_sql(union_all(branches) + " ORDER BY 7", limit=FEED_ROWS)
    out = build_feed(res["rows"], selections)
    out["truncated"] = res["truncated"]
    return out


def preview(req):
    """POST /api/plugin/lateral_movement/preview
    body: {selection: {...as feed's...}, ipmap?: {address: name}}
    -> {matched, hops, links, hosts, links_capped, first, last, untimed,
        top: [{src, dst, raw_src, raw_dst, n, labels}],
        folded: [[address, name], ...], addresses: [...], bare,
        drawn: [[src, dst, account, minute, n], ...], drawn_capped}

    What the event builder shows while an analyst defines an event: how
    many rows its conditions match (`matched`), how many of those draw a
    hop once hosts fold and empty, loopback and self endpoints drop out
    (`hops`), the links and hosts that makes, and the busiest links. The
    tab passes the address map of what it already draws, so a flow
    export's addresses fold to the names the event logs give them here
    as they will once it is saved; `drawn` (absolute minutes, folded
    names) is how the tab tells which hops are new to it. No case
    timeframe: this is about the definition, not about today's window."""
    body = req.body or {}
    sel = body.get("selection") or {}
    p = _prepare(req, sel)
    scope = _scope(req, p["src"])
    cond = " AND ".join(p["cond"]) or "1"
    matched = req.store.run_sql(f"SELECT COUNT(*) FROM {scope} WHERE {cond}", limit=1)["rows"][0][0]
    res = req.store.run_sql(_grouped(req, p, 0), limit=PREVIEW_ROWS)
    rows = res["rows"]
    hint = {str(k).upper(): str(v) for k, v in (body.get("ipmap") or {}).items()}
    fold = _Folder(((raw, p["strip"], r[7]) for r in rows for raw in (r[1], r[2])), hint)
    links, hosts, drawn = {}, set(), collections.Counter()
    folded, addresses = {}, set()
    first = last = None
    untimed = bare = 0
    for _k, rs, rd, ru, _a, _sv, m, n in rows:
        s, d = fold.host(rs, p["strip"]), fold.host(rd, p["strip"])
        if not s or not d or s == d:
            continue
        for raw, h in ((rs, s), (rd, d)):
            name, ip = split_host(raw, p["strip"])
            a = norm_name(name) or norm_name(ip or "")
            if a and is_address(a):
                if a != h:
                    folded[a] = h
                else:
                    addresses.add(a)
        hosts.update((s, d))
        u = fold.account(ru)
        L = links.setdefault((s, d), {"src": s, "dst": d, "raw_src": str(rs), "raw_dst": str(rd),
                                      "n": 0, "labels": set()})
        L["n"] += n
        if u:
            L["labels"].add(u)
        else:
            bare += n
        if m is None:
            untimed += n
            continue
        drawn[(s, d, u, m)] += n
        first = m if first is None else min(first, m)
        last = m if last is None else max(last, m)
    top = sorted(links.values(), key=lambda L: (-L["n"], L["src"], L["dst"]))
    def stamp(m):
        return None if m is None else _dt.datetime.fromtimestamp(m * 60, _dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "matched": matched,
        "hops": sum(L["n"] for L in links.values()),
        "links": len(links),
        "hosts": len(hosts),
        "links_capped": res["truncated"],
        "first": stamp(first),
        "last": stamp(last),
        "untimed": untimed,
        "bare": bare,
        "top": [{**{k: v for k, v in L.items() if k != "labels"},
                 "labels": len(L["labels"]) if sel.get("label_col") else None}
                for L in top[:PREVIEW_TOP]],
        "folded": sorted(folded.items()),
        "addresses": sorted(addresses - set(folded)),
        "drawn": [[s, d, u, m, n] for (s, d, u, m), n in list(drawn.items())[:PREVIEW_HOPS]],
        "drawn_capped": len(drawn) > PREVIEW_HOPS,
    }


def values(req):
    """POST /api/plugin/lateral_movement/values  body: {source_id, column}
    -> {values: [{value, n}], truncated}
    The commonest values of one column, for a condition's suggestions —
    the hard part of "EventId is 4624" on a fresh export is knowing that
    the logon type lives in PayloadData2 and reads "LogonType 3"."""
    if req.store is None:
        raise ValueError("Open a case first")
    b = req.body or {}
    src = _source(req, b.get("source_id"))
    column = b.get("column")
    if column not in {c["name"] for c in src["columns"]}:
        raise ValueError(f"No column {column!r} in {src['name']}")
    col = _col(src, column)
    res = req.store.run_sql(
        f"SELECT {col} AS v, COUNT(*) AS n FROM {_scope(req, src)}"
        f" WHERE {col} IS NOT NULL AND {col} != '' GROUP BY 1 ORDER BY n DESC, v",
        limit=VALUES_TOP)
    return {"values": [{"value": "" if r[0] is None else str(r[0]), "n": r[1]} for r in res["rows"]],
            "truncated": res["truncated"]}
