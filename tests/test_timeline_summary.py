"""The Timeline's per-artefact-shape summary: the Body column says what
happened ("Special privileges assigned  svc_backup · WKSTN-4471 · id
4672") instead of pipe-joining the whole source row, which put the
timestamp back on screen a second time and left MapDescription — the only
part a human reads — seventh field in.

Three things this pins, because all three are the fix rather than
decoration: the summary comes from defaults/headers.json and every column
it names really belongs to that header set, the timestamp column never
appears in the summary, and a shape nothing describes still renders
exactly the join it rendered before.
"""

from __future__ import annotations

import pytest

from winnow import defaults
from winnow.store import TL_DETAIL_MAX, TL_LEAD_MAX, _tl_details, _tl_lead

EVTX_COLUMNS = dict(defaults.headers()["nicknames"])["Event logs (EvtxECmd)"]


def _evtx_row(**over) -> list[str]:
    row = dict.fromkeys(EVTX_COLUMNS, "")
    row.update(over)
    return [row[c] for c in EVTX_COLUMNS]


@pytest.fixture
def evtx(store, write_csv):
    """An EvtxECmd-shaped table with two tagged rows: one the tool mapped
    (MapDescription filled in) and one it didn't, which is the common case
    the fallback lead exists for."""
    path = write_csv(
        [
            EVTX_COLUMNS,
            _evtx_row(RecordNumber="1", EventRecordId="100000", TimeCreated="2026-03-03 00:00:00",
                      EventId="4672", Level="Information", Channel="Security",
                      Provider="Microsoft-Windows-Security-Auditing", ProcessId="6868",
                      ThreadId="2766", Computer="WKSTN-4471", UserId="S-1-5-21-2447-1",
                      MapDescription="Special privileges assigned", UserName="svc_backup"),
            _evtx_row(RecordNumber="2", EventRecordId="100001", TimeCreated="2026-03-03 00:12:24",
                      EventId="7045", Level="Information", Channel="System",
                      Provider="Service Control Manager", Computer="WKSTN-4471"),
        ],
        name="evtx.csv",
    )
    rec = store.ingest_csv(path, name="evtx.csv", build_fts=False)
    tag = store.list_tags()[0]["id"]
    store.set_tags(rec["id"], [1, 2], tag, True)
    return store, rec["id"]


def _rows(store, **kw):
    res = store.build_timeline(**kw)
    return store.fetch_timeline_rows(res["view_id"], 0, 50)["rows"]


# ------------------------------------------------------------- the builders


def test_lead_takes_the_first_column_the_row_actually_filled_in():
    # Empty, not NULL, is what EvtxECmd writes for an unmapped event — the
    # reason this isn't COALESCE.
    assert _tl_lead("", None, "Service Control Manager") == "Service Control Manager"
    assert _tl_lead("Special privileges assigned", "Provider") == "Special privileges assigned"
    assert _tl_lead("", "", "") == ""


def test_lead_and_details_collapse_embedded_newlines_and_padding():
    # A row of the Timeline is exactly one row tall; a newline in an EVTX
    # Payload or an ESXi Message would render as a gap it has no room for.
    assert _tl_lead("logon\n  failed\t for user") == "logon failed for user"
    assert _tl_details("", "", "a\nb") == "a b"


def test_long_values_are_capped_so_the_details_stay_on_screen():
    lead = _tl_lead("x" * (TL_LEAD_MAX * 2))
    assert len(lead) == TL_LEAD_MAX and lead.endswith("…")
    detail = _tl_details("", "", "y" * (TL_DETAIL_MAX * 2))
    assert len(detail) == TL_DETAIL_MAX and detail.endswith("…")


def test_details_drop_blanks_label_the_cryptic_ones_and_join_with_a_dot():
    out = _tl_details("Special privileges assigned",
                      "", "svc_backup", "", "", "", "WKSTN-4471", "id", "4672")
    assert out == "svc_backup · WKSTN-4471 · id 4672"


def test_details_never_repeat_the_lead_or_each_other():
    # The lead falls back, so on a row whose first lead column was empty
    # the lead IS one of the detail columns.
    assert _tl_details("KeyName-value", "", "KeyName-value", "", "other") == "other"
    assert _tl_details("lead", "", "same", "", "same") == "same"


def test_every_shipped_summary_names_columns_of_its_own_header_set():
    # defaults.headers() raises on a typo — this is the assertion that a
    # wrong column name can't reach an analyst, since it would otherwise
    # render nothing on every row of that shape and say nothing about why.
    head = defaults.headers()
    by_name = dict(head["nicknames"])
    assert head["summaries"], "no shape ships a summary any more?"
    for name, spec in head["summaries"].items():
        known = set(by_name[name])
        assert set(spec["lead"]) <= known
        assert {c for c, _ in spec["details"]} <= known


# ------------------------------------------------------------ the built row


def test_summary_leads_with_what_happened_and_names_the_subject(evtx):
    store, _ = evtx
    rows = _rows(store)

    mapped = rows[0]
    assert mapped["lead"] == "Special privileges assigned"
    assert mapped["detail"] == "svc_backup · WKSTN-4471 · id 4672"

    # Nothing mapped this one, so the provider carries the line rather than
    # the row starting with a blank.
    unmapped = rows[1]
    assert unmapped["lead"] == "Service Control Manager"
    assert unmapped["detail"] == "WKSTN-4471 · id 7045"


def test_the_summary_survives_a_tag_filter(evtx):
    # The detail labels are bound parameters sitting in the SELECT list,
    # ahead of the type-label and tag-filter parameters in the same branch.
    # Get that order wrong and the labels come out as tag ids.
    store, _ = evtx
    rows = _rows(store, tag_ids=[store.list_tags()[0]["id"]])
    assert len(rows) == 2
    assert rows[0]["detail"] == "svc_backup · WKSTN-4471 · id 4672"


def test_the_summary_never_repeats_the_timestamp_column(evtx):
    store, _ = evtx
    for r in _rows(store):
        assert r["ts"] and r["ts"] not in r["lead"] and r["ts"] not in r["detail"]
        assert "2026-03-03" not in r["lead"] + r["detail"]


def test_the_raw_row_is_still_on_the_row(evtx):
    store, _ = evtx
    row = _rows(store)[0]
    # body is unchanged: every column, pipe-joined, timestamp included.
    assert row["body"].startswith("1 | 100000 | 2026-03-03 00:00:00 | 4672 | Information | ")
    assert row["body"].count(" | ") == len(EVTX_COLUMNS) - 1


def test_an_unknown_csv_shape_keeps_exactly_the_old_join(store, write_csv):
    path = write_csv(
        [["Timestamp", "EventId", "User"], ["2026-01-05T10:00:00", "4624", "alice"]],
        name="mystery.csv",
    )
    rec = store.ingest_csv(path, name="mystery.csv", build_fts=False)
    store.set_tags(rec["id"], [1], store.list_tags()[0]["id"], True)

    row = _rows(store)[0]
    assert row["lead"] == "" and row["detail"] == ""
    assert row["body"] == "2026-01-05T10:00:00 | 4624 | alice"


def test_body_columns_chosen_by_the_analyst_win_over_the_shipped_summary(evtx):
    store, sid = evtx
    row = _rows(store, configs={sid: {"body_columns": ["EventId", "Computer"]}})[0]
    assert row["body"] == "4672 | WKSTN-4471"
    assert row["lead"] == "" and row["detail"] == ""


def test_a_derived_column_does_not_stop_the_shape_being_recognised(evtx):
    store, sid = evtx
    store.add_derived_column(sid, "TC (derived)", "TimeCreated", "iso8601")
    assert any(c.get("derived") for c in store.get_source(sid)["columns"])
    # The header-set match runs against the file's own columns, so the
    # analyst's addition can't cost the table its summary.
    row = _rows(store)[0]
    assert row["lead"] == "Special privileges assigned"


def test_a_summary_column_used_as_the_timestamp_drops_out_of_the_summary(store, write_csv):
    # $J leads with UpdateReasons and details the name — but an analyst who
    # points the Timeline at a different column must not then see it twice.
    cols = dict(defaults.headers()["nicknames"])["USN journal $J (MFTECmd)"]
    row = dict.fromkeys(cols, "")
    row.update({"Name": "report.docx", "ParentPath": ".\\Users\\jdoe", "EntryNumber": "4711",
                "UpdateTimestamp": "2026-03-03 01:00:00", "UpdateReasons": "FileCreate|Close"})
    path = write_csv([cols, [row[c] for c in cols]], name="usn.csv")
    rec = store.ingest_csv(path, name="usn.csv", build_fts=False)
    store.set_tags(rec["id"], [1], store.list_tags()[0]["id"], True)

    normal = _rows(store)[0]
    assert normal["lead"] == "FileCreate|Close"
    assert normal["detail"] == "report.docx · .\\Users\\jdoe · entry 4711"

    # Reading the table by its Name column (nonsense as a timestamp, but it
    # is what the config says) takes Name out of the details.
    odd = _rows(store, configs={rec["id"]: {"timestamp_column": "Name"}})[0]
    assert odd["detail"] == ".\\Users\\jdoe · entry 4711"
