"""Configure tables…' live preview: Store.timeline_preview and
POST /api/timeline/preview.

The dialog shows a few rows of a table the way the Timeline will show them
under settings that are not saved yet. The thing worth pinning is that the
preview and the Timeline cannot disagree — the summary for a known shape,
the timestamp column dropped from it, picked body columns replacing it,
every column joined for a shape nothing describes, the fallback timestamp,
a derived timestamp — so most tests here build the Timeline under the same
settings and compare the two row for row.
"""

from __future__ import annotations

import threading

import pytest

from winnow import defaults

EVTX_COLUMNS = dict(defaults.headers()["nicknames"])["Event logs (EvtxECmd)"]


def _evtx_row(**over) -> list[str]:
    row = dict.fromkeys(EVTX_COLUMNS, "")
    row.update(over)
    return [row[c] for c in EVTX_COLUMNS]


@pytest.fixture
def evtx(store, write_csv):
    """An EvtxECmd-shaped table, rows 2 and 3 tagged, row 1 not."""
    path = write_csv([
        EVTX_COLUMNS,
        _evtx_row(RecordNumber="1", TimeCreated="2026-03-03 00:00:01", EventId="4624",
                  Provider="Microsoft-Windows-Security-Auditing", Computer="WKSTN-1",
                  MapDescription="Successful logon", UserName="alice"),
        _evtx_row(RecordNumber="2", TimeCreated="2026-03-03 00:05:00", EventId="4672",
                  Provider="Microsoft-Windows-Security-Auditing", Computer="WKSTN-4471",
                  MapDescription="Special privileges assigned", UserName="svc_backup"),
        _evtx_row(RecordNumber="3", TimeCreated="2026-03-03 00:01:00", EventId="7045",
                  Provider="Service Control Manager", Computer="WKSTN-4471"),
    ], name="evtx.csv")
    sid = store.ingest_csv(path, name="evtx.csv", build_fts=False)["id"]
    store.set_tags(sid, [2, 3], store.list_tags()[0]["id"], True)
    return store, sid


@pytest.fixture
def plain(store, write_csv):
    """A shape nothing in defaults/headers.json describes. Nothing tagged."""
    path = write_csv([
        ["Logged", "Epoch", "Process", "User"],
        ["2026-03-10 10:00:00", "1700000000", "evil.exe", "alice"],
        ["2026-03-11 10:00:00", "1700086400", "cmd.exe", "bob"],
        ["2026-03-12 10:00:00", "1700172800", "net.exe", "carol"],
        ["2026-03-13 10:00:00", "1700259200", "ps.exe", "dave"],
    ], name="proc.csv")
    return store, store.ingest_csv(path, name="proc.csv", build_fts=False)["id"]


def _timeline(store, sid, cfg):
    res = store.build_timeline(configs={sid: cfg} if cfg else None)
    rows = store.fetch_timeline_rows(res["view_id"], 0, 50)["rows"]
    return [r for r in rows if r["source_id"] == sid]


def _shape(rows):
    return [(r["rid"], r["ts"], r["type_label"], r["body"], r["lead"], r["detail"]) for r in rows]


# ------------------------------------------------------- reads as the Timeline

@pytest.mark.parametrize("cfg", [
    {},
    {"timestamp_column": "TimeCreated", "body_columns": ["EventId", "UserName"], "type_label": "Security"},
    {"timestamp_column": None, "body_columns": [], "type_label": ""},
])
def test_the_preview_reads_exactly_as_the_timeline(evtx, cfg):
    store, sid = evtx
    pv = store.timeline_preview(sid, cfg)
    assert _shape([r for r in pv["rows"] if r["tagged"]]) == _shape(_timeline(store, sid, cfg))
    # The untagged row it topped up with reads as it will once tagged.
    store.set_tags(sid, [1], store.list_tags()[0]["id"], True)
    assert _shape(pv["rows"]) == _shape(_timeline(store, sid, cfg))


def test_tagged_rows_come_first_and_in_timeline_order(evtx):
    """The Timeline holds tagged rows only, so those are what the preview
    shows — and in the Timeline's order (row 3 happened before row 2)."""
    store, sid = evtx
    pv = store.timeline_preview(sid, {}, limit=2)
    assert [r["rid"] for r in pv["rows"]] == [3, 2]
    # Carrying their tags, as a Timeline row does, for the dots.
    assert [r["tags"] for r in pv["rows"]] == [[store.list_tags()[0]["id"]]] * 2
    assert all(r["tagged"] for r in pv["rows"])


def test_an_untagged_table_previews_its_first_rows(plain):
    store, sid = plain
    pv = store.timeline_preview(sid, {})
    assert [r["rid"] for r in pv["rows"]] == [1, 2, 3]
    assert not any(r["tagged"] or r["tags"] for r in pv["rows"])


def test_fewer_tagged_rows_than_asked_are_topped_up_without_repeats(plain):
    store, sid = plain
    store.set_tags(sid, [4], store.list_tags()[0]["id"], True)
    pv = store.timeline_preview(sid, {})
    assert sorted(r["rid"] for r in pv["rows"]) == [1, 2, 4]
    assert [r["tagged"] for r in pv["rows"] if r["rid"] == 4] == [True]


# --------------------------------------------- what the body falls back to

def test_no_body_columns_on_a_known_shape_is_its_summary(evtx):
    store, sid = evtx
    pv = store.timeline_preview(sid, {})
    assert pv["body"] == "summary"
    assert pv["summary_shape"] == "Event logs (EvtxECmd)"
    assert pv["timestamp_column"] == "TimeCreated"
    by_rid = {r["rid"]: r for r in pv["rows"]}
    assert by_rid[2]["lead"] == "Special privileges assigned"
    assert by_rid[2]["detail"] == "svc_backup · WKSTN-4471 · id 4672"
    # The timestamp is never repeated in the summary.
    assert "2026-03-03" not in by_rid[2]["detail"]


def test_picked_body_columns_replace_the_summary(evtx):
    store, sid = evtx
    pv = store.timeline_preview(sid, {"body_columns": ["UserName", "EventId"]})
    assert pv["body"] == "columns" and pv["summary_shape"] is None
    assert pv["shape"] == "Event logs (EvtxECmd)"   # what the pick replaced
    assert {r["rid"]: r["body"] for r in pv["rows"]}[2] == "svc_backup | 4672"
    assert not any(r["lead"] or r["detail"] for r in pv["rows"])


def test_an_unknown_shape_with_no_body_columns_joins_every_column(plain):
    store, sid = plain
    pv = store.timeline_preview(sid, {})
    assert pv["body"] == "all" and pv["summary_shape"] is None and pv["shape"] is None
    assert pv["rows"][0]["body"] == "2026-03-10 10:00:00 | 1700000000 | evil.exe | alice"
    assert pv["body_shown"] == pv["body_total"] == 4


def test_a_body_column_the_table_lacks_is_dropped_like_the_timeline_drops_it(plain):
    store, sid = plain
    pv = store.timeline_preview(sid, {"body_columns": ["Nope", "User"]})
    assert pv["body"] == "columns"
    assert pv["rows"][0]["body"] == "alice"


# ---------------------------------------------------------------- timestamp

def test_the_fallback_timestamp_is_named(plain):
    store, sid = plain
    pv = store.timeline_preview(sid, {"timestamp_column": "NotAColumn"})
    assert pv["timestamp_column"] == "Logged"
    assert pv["rows"][0]["ts"] == "2026-03-10 10:00:00"


def test_a_derived_timestamp_is_read_off_the_derived_column(plain):
    store, sid = plain
    res = store.add_derived_column(sid, "When", "Epoch", "unix_epoch", {})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    cfg = {"timestamp_column": "When", "body_columns": ["Process"]}
    pv = store.timeline_preview(sid, cfg)
    assert pv["timestamp_column"] == "When"
    assert pv["rows"][0]["ts"] == "2023-11-14 22:13:20"
    store.set_tags(sid, [1, 2, 3], store.list_tags()[0]["id"], True)
    assert _shape(store.timeline_preview(sid, cfg)["rows"]) == _shape(_timeline(store, sid, cfg))


def test_an_empty_type_is_the_tables_own_name(plain):
    store, sid = plain
    assert {r["type_label"] for r in store.timeline_preview(sid, {"type_label": ""})["rows"]} == {"proc.csv"}
    assert {r["type_label"] for r in store.timeline_preview(sid, {"type_label": "Procs"})["rows"]} == {"Procs"}


# ------------------------------------------------------------------ limits

def test_the_row_count_is_clamped(plain):
    store, sid = plain
    assert len(store.timeline_preview(sid, {}, limit=0)["rows"]) == 1
    assert len(store.timeline_preview(sid, {}, limit=10_000)["rows"]) == 4   # the whole table, capped at 10


def test_the_preview_does_not_wait_for_the_writer_lock(evtx):
    """A pure read (invariant #4): it runs on the reader pool, so a long
    build or import holding the writer lock can't freeze the dialog. Held
    for the whole read, as tests/test_concurrency.py does — a deterministic
    deadlock against a preview that took the lock, not a race."""
    store, sid = evtx
    result: dict = {}

    def worker():
        try:
            result["value"] = store.timeline_preview(sid, {})
        except BaseException as e:  # noqa: BLE001 — reraised below
            result["error"] = e

    with store.lock:
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(5.0)
        if t.is_alive():
            pytest.fail("timeline_preview blocked on Store.lock")
    if "error" in result:
        raise result["error"]
    assert len(result["value"]["rows"]) == 3


# ------------------------------------------------------------------ the route

def test_the_route_previews_unsaved_settings(client, evtx):
    store, sid = evtx
    r = client.post("/api/timeline/preview", json={
        "source_id": sid, "timestamp_column": "TimeCreated",
        "body_columns": ["UserName"], "type_label": "Security"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["body"] == "columns"
    assert [(row["type_label"], row["body"]) for row in body["rows"]] == [
        ("Security", "alice"), ("Security", ""), ("Security", "svc_backup")]
    # Nothing was saved: the preview is not a template write.
    assert client.get("/api/timeline_templates").json() == []


def test_the_route_needs_the_client_header(client, evtx):
    from fastapi.testclient import TestClient

    import server

    _store, sid = evtx
    bare = TestClient(server.app)
    assert bare.post("/api/timeline/preview", json={"source_id": sid}).status_code == 403


def test_a_merge_is_a_400_and_a_missing_table_a_404(client, store, write_csv):
    ids = []
    for n in range(2):
        p = write_csv([["A", "B"], [f"{n}", "x"]], name=f"m{n}.csv")
        ids.append(store.ingest_csv(p, name=f"m{n}.csv", build_fts=False)["id"])
    merge = store.create_merge("both", ids)
    r = client.post("/api/timeline/preview", json={"source_id": merge["id"]})
    assert r.status_code == 400
    assert "merged table" in r.json()["detail"]
    r = client.post("/api/timeline/preview", json={"source_id": 9999})
    assert r.status_code == 404
    assert "no longer in this case" in r.json()["detail"]
