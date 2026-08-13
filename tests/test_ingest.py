"""store.py ingest path: sanitize_columns, infer_type, ragged rows,
delimiter sniffing, column_types override, _quick_hash, batched commits."""

from __future__ import annotations

from store import BATCH, infer_type, sanitize_columns


def test_sanitize_columns_dedups_and_fills_blanks():
    out = sanitize_columns(["Name", "", "Name", None, "Name"])
    assert out == ["Name", "col_2", "Name_1", "col_4", "Name_2"]
    assert len(set(n.lower() for n in out)) == len(out)


def test_sanitize_columns_handles_preexisting_dedup_pattern():
    # A CSV that already went through another tool's own "_1" dedup pass —
    # the naive first-seen-only algorithm collides here (see the function's
    # own docstring); this is the regression case for that fix.
    out = sanitize_columns(["Name", "Name", "Name_1"])
    assert len(set(n.lower() for n in out)) == 3


def test_sanitize_columns_rejects_rid_as_a_header():
    # "rid" is the reserved primary-key column name every src_<id> table
    # already has — a CSV header literally named "rid" must not collide.
    out = sanitize_columns(["rid", "Name"])
    assert out[0] == "rid_1"


def test_sanitize_columns_renames_fts5_reserved_names():
    # "rank" and "rowid" (case-insensitive) are FTS5-reserved column names —
    # CREATE VIRTUAL TABLE ... USING fts5(...) rejects them outright, which
    # used to blow up build_fts() well after the source table and its rows
    # were already committed. Regression test for that fix.
    out = sanitize_columns(["Rank", "RowId", "Name"])
    assert out == ["Rank_1", "RowId_1", "Name"]


def test_ingest_with_a_column_literally_named_rank(store, write_csv):
    # End-to-end: this used to raise sqlite3.OperationalError("reserved
    # fts5 column name: Rank") out of ingest_csv, after the source row and
    # its data were already committed — a plausible header (log rank/score
    # columns are common) that failed the whole import.
    path = write_csv([
        ["Rank", "Name"],
        ["1", "Alice"],
        ["2", "Bob"],
    ])
    rec = store.ingest_csv(path, name="ranked.csv")
    assert rec["row_count"] == 2
    assert store.wait_for_fts(rec["id"], timeout=5)
    assert store.get_source(rec["id"])["has_fts"] == 1
    assert [c["name"] for c in rec["columns"]] == ["Rank_1", "Name"]


def test_infer_type_number_vs_datetime_vs_text():
    assert infer_type(["1", "2", "3.5", "-4"]) == "number"
    assert infer_type(["2024-01-05 13:22:01", "2024-01-06T09:00:00"]) == "datetime"
    assert infer_type(["1/6/2024", "2/7/2024"]) == "datetime"
    assert infer_type(["svchost.exe", "cmd.exe"]) == "text"
    # 80% threshold: mostly dates with one stray non-date value still counts.
    # (DATE_RE's ISO branch requires a trailing space/T — a bare "YYYY-MM-DD"
    # with nothing after it doesn't match; the US slash branch has no such
    # requirement, so that's what this case uses.)
    assert infer_type(["1/5/2024", "1/6/2024", "1/7/2024", "1/8/2024", "N/A"]) == "datetime"
    assert infer_type(["2024-01-05", "2024-01-06"]) == "text"  # bare ISO date, no time part -> not recognized
    assert infer_type([]) == "text"
    assert infer_type(["", None, ""]) == "text"


def test_ingest_basic_shape(store, write_csv):
    path = write_csv([
        ["Timestamp", "EventId", "User"],
        ["2024-01-05 13:22:01", "4624", "ACME\\jacson"],
        ["2024-01-05 13:23:11", "4688", "ACME\\admin"],
    ])
    rec = store.ingest_csv(path, name="mini.csv")
    assert rec["row_count"] == 2
    assert rec["ragged_rows"] == 0
    assert [c["name"] for c in rec["columns"]] == ["Timestamp", "EventId", "User"]
    assert rec["columns"][0]["type"] == "datetime"
    assert rec["columns"][1]["type"] == "number"
    assert rec["columns"][2]["type"] == "text"
    # The trigram index build is backgrounded (see test_search.py for that
    # behavior specifically) — wait for it before asserting has_fts here.
    assert store.wait_for_fts(rec["id"], timeout=5)
    assert store.get_source(rec["id"])["has_fts"] == 1
    assert rec["is_open"] is True


def test_source_table_is_never_mutated_by_reingest(store, write_csv):
    # Invariant #1: each import gets its own src_<id> table; importing the
    # same file again creates a second, independent source rather than
    # touching the first one's data.
    path = write_csv([
        ["A", "B"],
        ["1", "2"],
    ])
    rec1 = store.ingest_csv(path, name="a.csv")
    rec2 = store.ingest_csv(path, name="a.csv")
    assert rec1["id"] != rec2["id"]
    assert rec1["table_name"] != rec2["table_name"]
    assert store.get_source(rec1["id"])["row_count"] == 1


def test_ragged_rows_are_padded_not_dropped(store, write_csv):
    path = write_csv([
        ["A", "B", "C"],
        ["1", "2", "3"],
        ["short", "row"],           # 2 cells, header wants 3 -> padded
        ["over", "long", "row", "extra"],  # 4 cells -> trimmed to 3
    ])
    rec = store.ingest_csv(path, name="ragged.csv")
    assert rec["row_count"] == 3
    assert rec["ragged_rows"] == 2
    spec = {"source_id": rec["id"], "filters": [], "sort": [{"column": "A", "dir": "asc"}]}
    view = store.build_view(rec["id"], spec)
    rows = store.fetch_rows(view["view_id"], 0, 10)["rows"]
    by_a = {r["cells"][0]: r["cells"] for r in rows}
    assert by_a["short"] == ["short", "row", ""]  # padded with an empty cell, not dropped
    assert by_a["over"] == ["over", "long", "row"]  # trimmed to header width


def test_delimiter_sniffing_semicolon(store, write_csv):
    path = write_csv([["A;B"], ["1;2"], ["3;4"]], name="semis.csv")
    # write_csv writes real CSV rows, so build the semicolon file directly
    # instead of relying on the csv writer to produce ';'-delimited text.
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write("A;B\n1;2\n3;4\n")
    rec = store.ingest_csv(path, name="semis.csv")
    assert [c["name"] for c in rec["columns"]] == ["A", "B"]
    assert rec["row_count"] == 2


def test_has_header_false_treats_first_row_as_data(store, write_csv):
    path = write_csv([["1", "2"], ["3", "4"]])
    rec = store.ingest_csv(path, name="noheader.csv", has_header=False)
    assert rec["row_count"] == 2
    assert rec["columns"][0]["name"] == "col_1"
    spec = {"source_id": rec["id"], "filters": [], "sort": []}
    view = store.build_view(rec["id"], spec)
    rows = store.fetch_rows(view["view_id"], 0, 10)["rows"]
    values = sorted(r["cells"][0] for r in rows)
    assert values == ["1", "3"]  # the "1,2" row is data, not consumed as a header


def test_column_types_override(store, write_csv):
    path = write_csv([
        ["Code"],
        ["007"],
        ["042"],
    ])
    # Without an override this infers as "number" (NUM_RE matches "007").
    auto = store.ingest_csv(path, name="auto.csv")
    assert auto["columns"][0]["type"] == "number"
    forced = store.ingest_csv(path, name="forced.csv", column_types=["text"])
    assert forced["columns"][0]["type"] == "text"


def test_quick_hash_stable_and_sensitive(store, write_csv, tmp_path):
    p1 = write_csv([["A"], ["1"]], name="one.csv")
    p2 = write_csv([["A"], ["1"]], name="one_copy.csv")
    p3 = write_csv([["A"], ["2"]], name="different.csv")
    assert store._quick_hash(p1) == store._quick_hash(p2)  # same content -> same hash
    assert store._quick_hash(p1) != store._quick_hash(p3)  # different content -> different hash


def test_multi_batch_ingest_commits_across_chunk_boundary(store, tmp_path):
    # Exercises BATCH-sized chunked commits (ingest_csv's docstring: each
    # chunk is its own short transaction) — enough rows to cross at least
    # one BATCH boundary.
    n = BATCH + 5_000
    path = tmp_path / "big.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write("Id,Value\n")
        for i in range(n):
            f.write(f"{i},row{i}\n")
    rec = store.ingest_csv(str(path), name="big.csv", build_fts=False)
    assert rec["row_count"] == n
    assert rec["ragged_rows"] == 0
    spec = {"source_id": rec["id"], "filters": [{"column": "Id", "op": "equals", "value": str(n - 1)}], "sort": []}
    view = store.build_view(rec["id"], spec)
    rows = store.fetch_rows(view["view_id"], 0, 10)["rows"]
    assert len(rows) == 1
    assert rows[0]["cells"][1] == f"row{n - 1}"
