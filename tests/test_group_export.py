"""Exporting from an expanded group writes what the grid shows.

A small group of a table that holds every row (unfiltered, or only
sorted) is never materialised: it is paged, tagged and exported straight
off the source table (`group_virtual`), and its export has its own code,
`Store._export_virtual_group_csv_rows`. Until these tests only its header
line had ever run (test_silent_wrong_answers.py), so a wrong row, a tag on
the wrong line or a note dropped there would have reached a report with
every test green.

Each test holds that export to two things it has to agree with: the rows
the grid pages for the same group, and the export of the same rows reached
another way (a filter, or a group big enough to be materialised), which
goes through the other export path.
"""

from __future__ import annotations

import csv
import io

import pytest

from winnow.store import Store

CHANNELS = ["Security", "PowerShell", "System"]


@pytest.fixture
def case(store, write_csv):
    """45 rows over three channels, with a column the layout hides, two
    tags that overlap, notes with a comma, a quote and a newline, and
    cells and a tag name that need the formula-injection prefix."""
    rows = [["When", "Channel", "User", "Msg"]]
    for i in range(45):
        msg = f"=HYPERLINK(\"x{i}\")" if i % 9 == 0 else f"event {i}"
        rows.append([f"2026-03-{14 + i // 15:02d} 08:{i % 60:02d}:00", CHANNELS[i % 3], f"u{i % 4}", msg])
    sid = store.ingest_csv(write_csv(rows, "events.csv"), name="events", build_fts=False)["id"]
    store.save_layout(sid, {"order": ["Msg", "Channel", "When"], "columns": {"User": {"hidden": True}}})
    keep = store.upsert_tag(None, "Keep", "#ff0000", "1")["id"]
    odd = store.upsert_tag(None, "@odd", "#0000ff", "2")["id"]
    store.set_tags(sid, [1, 4, 7, 10, 22, 31], keep, True)
    store.set_tags(sid, [4, 13, 22, 40], odd, True)
    store.set_note(sid, 7, 'saw "this", twice\nsecond line')
    store.set_note(sid, 13, "=not a formula")
    store.set_note(sid, 2, "a PowerShell row")
    return sid


def _csv(store, view_id, tagged_only=False):
    return "".join(store.export_view_csv(view_id, tagged_only=tagged_only))


def _rows(text):
    return list(csv.reader(io.StringIO(text)))


def _grid(store, sid, view_id):
    """The group as the grid pages it, laid out as an export line: Line,
    Tags, Note, then the export's columns. Formula prefixes aside (the
    export adds them; the grid shows the stored value), this is what the
    analyst was looking at when they pressed Export."""
    names = {t["id"]: t["name"] for t in store.list_tags()}
    cols = [c["name"] for c in store.get_source(sid)["columns"]]
    shown = ["Msg", "Channel", "When"]
    page = store.fetch_rows(view_id, 0, 10_000)["rows"]
    return [[str(r["rid"]), "; ".join(names[t] for t in r["tags"]), r["note"] or "",
             *[r["cells"][cols.index(c)] for c in shown]] for r in page]


def _unprefixed(rows):
    return [[c[1:] if c[:2] in ("'=", "'@", "'+", "'-") else c for c in r] for r in rows]


def _group(store, sid, column, value, spec=None, path=None):
    root = store.build_view(sid, {"source_id": sid, **(spec or {})})["view_id"]
    return store.expand_group(root, column, value, path)["view_id"]


@pytest.mark.parametrize("parent", [{}, {"sort": [{"column": "Msg", "dir": "desc"}]}],
                         ids=["unfiltered parent", "sorted parent"])
@pytest.mark.parametrize("channel", CHANNELS)
def test_a_small_group_exports_the_rows_the_grid_shows(store, case, parent, channel):
    vid = _group(store, case, "Channel", channel, parent)
    assert store._views[vid]["kind"] == "group_virtual"
    exported = _rows(_csv(store, vid))
    assert exported[0] == ["Line", "Tags", "Note", "Msg", "Channel", "When"]
    assert _unprefixed(exported[1:]) == _grid(store, case, vid)
    assert len(exported) - 1 == 15


def test_tags_notes_and_prefixes_land_on_their_own_rows(store, case):
    vid = _group(store, case, "Channel", "Security")
    by_line = {r[0]: r for r in _rows(_csv(store, vid))[1:]}
    assert by_line["1"][1:3] == ["Keep", ""]
    assert by_line["4"][1] == "Keep; @odd"            # both tags, in tag order
    assert by_line["7"][2] == 'saw "this", twice\nsecond line'
    assert by_line["13"][1:3] == ["'@odd", "'=not a formula"]   # made safe where they lead the cell
    assert by_line["10"][3] == "'=HYPERLINK(\"x9\")"   # a cell made safe on this path too
    assert "2" not in by_line                         # a PowerShell row's note stays out of it


@pytest.mark.parametrize("tagged_only", [False, True], ids=["every row", "tagged rows only"])
def test_the_same_bytes_as_a_filter_for_the_same_rows(store, case, tagged_only):
    """A filter reaches the same rows through a materialised view and the
    other export path, so the two files can be compared whole."""
    group_csv = _csv(store, _group(store, case, "Channel", "Security"), tagged_only)
    # Built after: a new view of the same table retires the group's parent.
    filtered = store.build_view(case, {"source_id": case,
                                       "filters": [{"column": "Channel", "op": "equals", "value": "Security"}]})
    assert store._views[filtered["view_id"]].get("kind") not in ("group_virtual", "root_virtual")
    assert group_csv == _csv(store, filtered["view_id"], tagged_only)


def test_tagged_rows_only_keeps_exactly_the_tagged_rows(store, case):
    vid = _group(store, case, "Channel", "Security")
    lines = [r[0] for r in _rows(_csv(store, vid, tagged_only=True))[1:]]
    assert lines == ["1", "4", "7", "10", "13", "22", "31", "40"]


def test_tagged_rows_only_of_a_group_with_no_tags_is_just_the_header(store, case):
    vid = _group(store, case, "Channel", "System")
    assert _rows(_csv(store, vid, tagged_only=True)) == [["Line", "Tags", "Note", "Msg", "Channel", "When"]]


@pytest.mark.parametrize("tagged_only", [False, True], ids=["every row", "tagged rows only"])
def test_a_group_too_big_for_the_fast_path_exports_the_same(store, case, monkeypatch, tagged_only):
    """Over GROUP_MATERIALIZE_THRESHOLD the same group is materialised
    and exported by the other path; the file must not change with it."""
    small = _group(store, case, "Channel", "Security")
    small_csv = _csv(store, small, tagged_only)
    monkeypatch.setattr(Store, "GROUP_MATERIALIZE_THRESHOLD", 3)
    big = _group(store, case, "Channel", "Security")
    assert store._views[big]["kind"] == "group"
    assert _csv(store, big, tagged_only) == small_csv


def test_a_nested_group_exports_only_its_own_rows(store, case):
    """A group inside a group: the outer level's value is part of which
    rows belong, and has to be part of the export's query too."""
    vid = _group(store, case, "User", "u1", path=[{"column": "Channel", "value": "Security"}])
    assert store._views[vid]["kind"] == "group_virtual"
    exported = _rows(_csv(store, vid))[1:]
    assert [r[0] for r in exported] == ["10", "22", "34"]
    assert _unprefixed(exported) == _grid(store, case, vid)
    group_csv = _csv(store, vid)
    filtered = store.build_view(case, {"source_id": case, "filters": [
        {"column": "Channel", "op": "equals", "value": "Security"},
        {"column": "User", "op": "equals", "value": "u1"}]})
    assert group_csv == _csv(store, filtered["view_id"])


def test_a_day_of_a_datetime_column_exports_that_day(store, case):
    """Grouping a timestamp groups by day; the export has to compare by
    day too, or it matches no row at all."""
    vid = _group(store, case, "When", "2026-03-15")
    assert store._views[vid]["kind"] == "group_virtual"
    exported = _rows(_csv(store, vid))[1:]
    assert len(exported) == 15
    assert all(r[5].startswith("2026-03-15") for r in exported)
    assert _unprefixed(exported) == _grid(store, case, vid)
