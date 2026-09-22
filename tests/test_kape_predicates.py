"""What the KAPE board's numbers are counted OVER — the predicates, not
the layout.

An EventId only means something inside a channel. 4625 is a failed logon
in Security; the same four digits in another channel is a different event
that happens to share a number, and a KAPE collection carries dozens of
channels. The board's logon histogram has always said `Channel='Security'`
and four of its stats did not, so a card reading "3 failed logons" could
not be reconciled with the chart beside it. These tests state the rule
instead of the card list: every question that counts a Security-channel
EventId names the channel, in its SQL and in the drill that opens its
rows — so a card added later cannot quietly reintroduce the defect.

Written against the board's QUESTIONS rather than its card titles: a
widget's own query, and each cell of a `signals` widget, which is the
same kind of thing — one number, one SQL, one drill."""

from __future__ import annotations

import re

import pytest

from winnow import defaults

EVTX_COLS = dict(defaults.headers()["nicknames"])["Event logs (EvtxECmd)"]

# Events the Security channel is the only honest source for. 4624/4625/
# 4648 are logons, 4688 is a process create, 1102 is "the log was
# cleared" and 4719 is "auditing was changed" — the two an intruder
# produces on the way out.
SECURITY_IDS = {"4624", "4625", "4648", "4688", "1102", "4719"}

CHANNEL_COND = {"column": "Channel", "op": "equals", "value": "Security"}


def _kape():
    return next(p for p in defaults.profiles() if p["name"] == "KAPE triage")


def _questions():
    """(label, sql, drill) for everything on the board that counts rows."""
    out = []
    for w in _kape()["dashboard"]:
        for q in [w, *(w.get("cells") or [])]:
            sql = (q.get("query") or {}).get("sql")
            if sql:
                out.append((q.get("title") or q.get("label") or "(unnamed)", sql, q.get("drill")))
    return out


def _event_ids(sql: str) -> set[str]:
    ids = set(re.findall(r"EventId\s*=\s*'(\w+)'", sql))
    for group in re.findall(r"EventId\s+IN\s*\(([^)]*)\)", sql, re.I):
        ids |= set(re.findall(r"'(\w+)'", group))
    return ids


def _conds(drill):
    out = list((drill or {}).get("where") or [])

    def walk(n):
        if not n:
            return
        if n.get("type") == "cond":
            out.append({k: n[k] for k in ("column", "op", "value")})
        for c in n.get("children", []):
            walk(c)

    walk((drill or {}).get("tree"))
    return out


def test_every_security_event_count_names_the_channel():
    """The shape rule, checked over the whole board rather than over a
    list of card titles — the list is what went stale last time."""
    checked = 0
    for label, sql, drill in _questions():
        ids = _event_ids(sql)
        if not ids or not ids <= SECURITY_IDS:
            continue            # RDP (21/24/25), Defender (1006…) — other channels
        assert re.search(r"Channel\s*=\s*'Security'", sql), label
        assert CHANNEL_COND in _conds(drill), (label, drill)
        checked += 1
    assert checked >= 6, checked


# ------------------------------------------------------- against rows

def _ev(**kw):
    d = {c: "" for c in EVTX_COLS}
    d.update(kw)
    return [d[c] for c in EVTX_COLS]


# Every Security-channel event the board counts, once in Security and
# once in a channel that is not — the second of each pair is the row the
# old predicates counted and the histogram never did.
ROWS = [
    _ev(TimeCreated="2026-03-14 08:00:00", EventId="4624", Channel="Security", UserName="jsmith", RemoteHost="10.0.0.9"),
    _ev(TimeCreated="2026-03-14 08:01:00", EventId="4625", Channel="Security", UserName="admin"),
    _ev(TimeCreated="2026-03-14 08:02:00", EventId="4648", Channel="Security", UserName="admin"),
    _ev(TimeCreated="2026-03-14 08:03:00", EventId="1102", Channel="Security", UserName="admin"),
    _ev(TimeCreated="2026-03-14 08:04:00", EventId="4719", Channel="Security", UserName="admin"),
    _ev(TimeCreated="2026-03-14 08:05:00", EventId="4688", Channel="Security", UserName="admin"),
    _ev(TimeCreated="2026-03-14 09:00:00", EventId="4624", Channel="Microsoft-Windows-Bits-Client/Operational", UserName="decoy", RemoteHost="10.9.9.9"),
    _ev(TimeCreated="2026-03-14 09:01:00", EventId="4625", Channel="Microsoft-Windows-Bits-Client/Operational", UserName="decoy"),
    _ev(TimeCreated="2026-03-14 09:02:00", EventId="4648", Channel="Application", UserName="decoy"),
    _ev(TimeCreated="2026-03-14 09:03:00", EventId="1102", Channel="Microsoft-Windows-DNS-Client/Operational", UserName="decoy"),
    _ev(TimeCreated="2026-03-14 09:04:00", EventId="4719", Channel="System", UserName="decoy"),
    _ev(TimeCreated="2026-03-14 09:05:00", EventId="4688", Channel="Microsoft-Windows-Sysmon/Operational", UserName="decoy"),
]


@pytest.fixture
def logs(store, write_csv):
    sid = store.ingest_csv(write_csv([EVTX_COLS] + ROWS, "evtx.csv"), name="evtx", build_fts=False)["id"]
    return store, sid


def _drill_rows(store, sid, drill):
    children = [{"type": "cond", **c} for c in (drill.get("where") or [])]
    if drill.get("tree"):
        children.append(drill["tree"])
    sql = store.spec_sql(sid, {"source_id": sid, "filter_tree": {"type": "group", "op": "AND", "children": children}})
    res = store.run_sql(sql, limit=1000)
    return [dict(zip(res["columns"], r)) for r in res["rows"]]


def test_the_decoy_channel_rows_are_counted_by_nothing(logs):
    """One row per Security event in a channel that is not Security. The
    numbers on the board must not move when they are there — and neither
    must the rows a click opens, or the drill would contradict the card
    it came from."""
    store, sid = logs
    checked = 0
    for label, sql, drill in _questions():
        ids = _event_ids(sql)
        if not ids or not ids <= SECURITY_IDS:
            continue
        rows = _drill_rows(store, sid, drill)
        assert rows, label                       # the Security rows ARE opened
        assert all(r["Channel"] == "Security" for r in rows), (label, rows)
        assert "decoy" not in {r["UserName"] for r in rows}, label
        checked += 1
    assert checked >= 6, checked


def test_the_logon_histogram_and_the_logon_counts_agree(logs):
    """The reconciliation the fix is for: the failed-logon number is a
    slice of the chart's own population, so the chart's total covers it."""
    store, sid = logs
    qs = {label: sql for label, sql, _ in _questions()}
    hist = next(s for lbl, s in qs.items() if "strftime" in s and "4625" in s)
    total = sum(r[-1] for r in store.dashboard_widget_preview("sql", {"sql": hist})["rows"])
    failed = next(s for lbl, s in qs.items() if _event_ids(s) == {"4625"})
    (n,) = store.dashboard_widget_preview("sql", {"sql": failed})["rows"][0]
    assert n == 1 and total == 3           # 4624 + 4625 + 4648, Security only


# --------------------------------------------------- one Defender population

DEFENDER_ROWS = [
    _ev(TimeCreated="2026-03-14 10:00:00", EventId="1116", Channel="Microsoft-Windows-Windows Defender/Operational",
        Provider="Microsoft-Windows-Windows Defender", MapDescription="Malware detected"),
    _ev(TimeCreated="2026-03-14 10:05:00", EventId="1117", Channel="Microsoft-Windows-Windows Defender/Operational",
        Provider="Microsoft-Windows-Windows Defender", MapDescription="Action taken"),
    # Real-time protection switched off: an alert the four-id count never
    # saw, and the one an intruder produces on purpose.
    _ev(TimeCreated="2026-03-14 10:07:00", EventId="5001", Channel="Microsoft-Windows-Windows Defender/Operational",
        Provider="Microsoft-Windows-Windows Defender", MapDescription="Real-time protection disabled"),
    # Routine: a signature update is not an alert and is in neither.
    _ev(TimeCreated="2026-03-14 10:09:00", EventId="2000", Channel="Microsoft-Windows-Windows Defender/Operational",
        Provider="Microsoft-Windows-Windows Defender", MapDescription="Signature update"),
]


def _defender():
    return [(label, sql, drill) for label, sql, drill in _questions() if "Windows Defender" in sql]


def test_the_defender_number_and_the_defender_list_ask_one_question():
    """There are two Defender cards — how many, and the most recent few.
    They counted different populations, so the board showed "2" beside a
    list of three. The ids are the question; both cards ask it."""
    asked = _defender()
    assert len(asked) == 2, [a[0] for a in asked]
    ids = {frozenset(_event_ids(sql)) for _, sql, _ in asked}
    assert len(ids) == 1, {label: sorted(_event_ids(sql)) for label, sql, _ in asked}
    assert len(next(iter(ids))) == 17
    drilled = {frozenset(c["value"]) for _, _, drill in asked
               for c in _conds(drill) if c["column"] == "EventId"}
    assert drilled == ids, drilled


@pytest.fixture
def defender(store, write_csv):
    sid = store.ingest_csv(write_csv([EVTX_COLS] + DEFENDER_ROWS, "evtx.csv"), name="evtx", build_fts=False)["id"]
    return store, sid


def test_the_defender_number_is_the_length_of_the_defender_list(defender):
    store, sid = defender
    count_sql = next(sql for _, sql, _ in _defender() if "COUNT(*)" in sql)
    list_sql = next(sql for _, sql, _ in _defender() if "COUNT(*)" not in sql)
    (n,) = store.dashboard_widget_preview("sql", {"sql": count_sql})["rows"][0]
    listed = store.dashboard_widget_preview("sql", {"sql": list_sql})["rows"]
    assert n == 3 and len(listed) == 3          # the signature update is in neither
    for _, _, drill in _defender():
        opened = _drill_rows(store, sid, drill)
        assert sorted(r["EventId"] for r in opened) == ["1116", "1117", "5001"]
