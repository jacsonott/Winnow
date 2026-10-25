"""store.py's JSON/JSONL ingest path: the three flatten modes (none/full/
depth-N), arrays always staying as a JSON-string blob regardless of depth,
ragged/varying-shape records, and .jsonl vs a single .json document."""

from __future__ import annotations

import json
import sys

import pytest

from winnow.store import _flatten_json, _json_leaf_text


RECORDS = [
    {"id": 1, "user": {"name": "alice", "address": {"city": "Springfield", "zip": "11111"}}, "tags": ["a", "b"]},
    {"id": 2, "user": {"name": "bob"}, "tags": []},
    {"id": 3, "user": {"name": "carol", "address": {"city": "Shelbyville"}}, "note": "only on this record"},
]


@pytest.fixture
def json_array_file(tmp_path):
    path = tmp_path / "data.json"
    path.write_text(json.dumps(RECORDS))
    return str(path)


@pytest.fixture
def jsonl_file(tmp_path):
    path = tmp_path / "data.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in RECORDS))
    return str(path)


def test_flatten_json_none_mode_keeps_nested_values_as_json_text():
    flat = _flatten_json(RECORDS[0], max_depth=0)
    assert flat["id"] == "1"
    assert json.loads(flat["user"]) == RECORDS[0]["user"]
    assert json.loads(flat["tags"]) == ["a", "b"]


def test_flatten_json_full_mode_unfolds_every_nested_object():
    flat = _flatten_json(RECORDS[0], max_depth=None)
    assert flat == {
        "id": "1",
        "user.name": "alice",
        "user.address.city": "Springfield",
        "user.address.zip": "11111",
        "tags": '["a", "b"]',  # arrays never unfold, at any depth
    }


def test_flatten_json_depth_1_unfolds_one_level_only():
    flat = _flatten_json(RECORDS[0], max_depth=1)
    assert flat["user.name"] == "alice"
    assert json.loads(flat["user.address"]) == {"city": "Springfield", "zip": "11111"}
    assert "user.address.city" not in flat


def test_json_leaf_text_handles_bool_none_and_containers():
    assert _json_leaf_text(None) == ""
    assert _json_leaf_text(True) == "true"
    assert _json_leaf_text(False) == "false"
    assert _json_leaf_text(42) == "42"
    assert json.loads(_json_leaf_text({"a": 1})) == {"a": 1}
    assert json.loads(_json_leaf_text([1, 2])) == [1, 2]


def test_ingest_json_none_mode_one_column_per_top_level_key(store, json_array_file):
    rec = store.ingest_json(json_array_file, name="none.json", flatten_mode="none", build_fts=False)
    assert rec["row_count"] == 3
    assert [c["name"] for c in rec["columns"]] == ["id", "user", "tags", "note"]
    view = store.build_view(rec["id"], {"source_id": rec["id"], "filters": [], "sort": [{"column": "id", "dir": "asc"}]})
    rows = store.fetch_rows(view["view_id"], 0, 10)["rows"]
    assert json.loads(rows[0]["cells"][1]) == {"name": "alice", "address": {"city": "Springfield", "zip": "11111"}}
    assert rows[2]["cells"][3] == "only on this record"  # note, present only on record 3
    assert rows[0]["cells"][3] == ""  # note, absent on record 1 -> padded empty


def test_ingest_json_full_mode_unfolds_objects_but_not_arrays(store, json_array_file):
    rec = store.ingest_json(json_array_file, name="full.json", flatten_mode="full", build_fts=False)
    colnames = [c["name"] for c in rec["columns"]]
    assert colnames == ["id", "user.name", "user.address.city", "user.address.zip", "tags", "note"]
    view = store.build_view(rec["id"], {"source_id": rec["id"], "filters": [], "sort": [{"column": "id", "dir": "asc"}]})
    rows = store.fetch_rows(view["view_id"], 0, 10)["rows"]
    by_col = dict(zip(colnames, rows[0]["cells"]))
    assert by_col["user.name"] == "alice"
    assert by_col["user.address.city"] == "Springfield"
    assert by_col["tags"] == '["a", "b"]'
    # Record 2 has no address at all -> padded empty, not an error
    by_col_r2 = dict(zip(colnames, rows[1]["cells"]))
    assert by_col_r2["user.address.city"] == ""


def test_ingest_json_depth_limits_flattening(store, json_array_file):
    rec = store.ingest_json(json_array_file, name="depth1.json", flatten_mode="depth", flatten_depth=1, build_fts=False)
    colnames = [c["name"] for c in rec["columns"]]
    assert "user.address" in colnames
    assert "user.address.city" not in colnames


def test_ingest_jsonl_streams_one_record_per_line(store, jsonl_file):
    rec = store.ingest_json(jsonl_file, name="data.jsonl", flatten_mode="full", build_fts=False)
    assert rec["row_count"] == 3
    assert "user.name" in [c["name"] for c in rec["columns"]]


def test_ingest_json_single_object_becomes_one_row(store, tmp_path):
    path = tmp_path / "single.json"
    path.write_text(json.dumps({"a": 1, "b": {"c": 2}}))
    rec = store.ingest_json(str(path), flatten_mode="full", build_fts=False)
    assert rec["row_count"] == 1
    assert [c["name"] for c in rec["columns"]] == ["a", "b.c"]


def test_ingest_json_empty_array_raises(store, tmp_path):
    path = tmp_path / "empty.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="no records"):
        store.ingest_json(str(path))


def test_preview_json_file_matches_ingest_columns_and_reports_record_count(store, json_array_file):
    preview = store.preview_json_file(json_array_file, flatten_mode="full")
    assert preview["record_count"] == 3
    assert preview["columns"] == ["id", "user.name", "user.address.city", "user.address.zip", "tags", "note"]
    assert len(preview["sample_rows"]) == 3


def test_pathologically_deep_json_ingests_instead_of_recursing_out(store, tmp_path):
    """A 2,000-level nested document used to walk _flatten_json off the
    interpreter stack (RecursionError). Full flatten now caps at 64
    object levels and stringifies the remainder — same convention as
    "depth N" mode's leftovers. Before Python 3.12 json itself stopped
    near 1,000 levels, so there the read is retried under a raised
    recursion limit, which is process-wide and must be put back."""
    limit = sys.getrecursionlimit()
    p = tmp_path / "deep.json"
    p.write_text('{"a":' * 2000 + '1' + '}' * 2000, encoding="utf-8")
    rec = store.ingest_json(str(p), flatten_mode="full", build_fts=False)
    assert rec["row_count"] == 1
    assert len(rec["columns"]) == 1
    name = rec["columns"][0]["name"]
    assert name.count(".") == 64    # capped unfold, remainder is one cell
    assert sys.getrecursionlimit() == limit


# Deeper than json nests on any Python (3.12 and 3.13 stop near 10,000).
TOO_DEEP = 1_000_000


def test_json_nested_past_what_python_can_read_is_a_clear_error(store, tmp_path):
    """Past the depth the running Python's json can nest, the analyst used
    to get "maximum recursion depth exceeded while decoding a JSON object
    from a unicode string". It is a JSONDecodeError now, a ValueError that
    says what is wrong, and no source is left behind."""
    limit = sys.getrecursionlimit()
    p = tmp_path / "abyss.json"
    p.write_text("[" * TOO_DEEP + "]" * TOO_DEEP, encoding="utf-8")
    with pytest.raises(ValueError, match="Nested too deeply"):
        store.ingest_json(str(p), build_fts=False)
    assert store.list_sources() == []
    assert sys.getrecursionlimit() == limit


def test_a_jsonl_line_nested_past_python_is_one_bad_line(store, tmp_path):
    """A RecursionError is not a JSONDecodeError, so the JSONL reader's
    count-and-skip never saw it: one absurd line cost the whole file."""
    p = tmp_path / "abyss.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"n": 1}\n')
        f.write("[" * TOO_DEEP + "]" * TOO_DEEP + "\n")
        f.write('{"n": 2}\n')
    rec = store.ingest_json(str(p), build_fts=False)
    assert rec["row_count"] == 2
    assert rec["bad_records"] == 1
    assert rec["first_bad_line"] == 2


def test_more_keys_than_a_table_can_hold_fold_into_one_json_column(store, tmp_path):
    """SQLite caps a table at 2,000 columns, and one record with 2,100
    keys (attacker-chosen HTTP header names, an environment dump) failed
    the WHOLE import with "too many columns" — 50,000 good records lost
    to one. Past JSON_MAX_COLUMNS the remaining keys fold into one
    `Other fields (JSON)` column, per record, so every value is still in
    the table and the JSON extraction ops can lift any of them out."""
    from winnow.store import JSON_MAX_COLUMNS, JSON_OVERFLOW_COLUMN

    path = tmp_path / "wide.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "2024-01-01T00:00:00", "msg": "plain"}) + "\n")
        f.write(json.dumps({"ts": "2024-01-01T00:00:01", "msg": "wide",
                            "headers": {f"X-H{j}": f"v{j}" for j in range(2100)}}) + "\n")
    for preview in (False, True):
        if preview:
            out = store.preview_json_file(str(path), flatten_mode="full")
            cols, rows = out["columns"], out["sample_rows"]
        else:
            rec = store.ingest_json(str(path), flatten_mode="full", build_fts=False)
            assert rec["row_count"] == 2
            cols = [c["name"] for c in rec["columns"]]
            with store._reader() as ro:
                rows = [list(r)[1:] for r in ro.execute(
                    f'SELECT * FROM "{rec["table_name"]}" ORDER BY rid')]
        assert len(cols) == JSON_MAX_COLUMNS
        assert cols[:3] == ["ts", "msg", "headers.X-H0"] and cols[-1] == JSON_OVERFLOW_COLUMN
        plain, wide = rows
        assert plain[-1] == ""                                   # nothing overflowed on that record
        folded = json.loads(wide[-1])
        kept = JSON_MAX_COLUMNS - 1 - 2                          # header keys that got columns
        assert len(folded) == 2100 - kept
        assert folded["headers.X-H2099"] == "v2099"              # nothing dropped
        assert wide[cols.index("headers.X-H0")] == "v0"


# ------------------------------------------- what the file said, all of it

def _cells(store, rec):
    cols = [c["name"] for c in rec["columns"]]
    rows = store.db.execute(
        f"SELECT {', '.join(f'[{c}]' for c in cols)} FROM src_{rec['id']} ORDER BY rid").fetchall()
    return cols, [tuple(r) for r in rows]


def test_a_flattened_key_collision_keeps_both_values(store, tmp_path):
    """A nested `process.name` and a literal "process.name" key flatten to
    the same column, and the flatten's dict kept whichever came second —
    so whoever wrote the record chose which process name the analyst saw
    ("evil.exe" never reached the table). Both are kept, the second under
    a suffixed column, and the record is counted so the import can say so."""
    p = tmp_path / "ev.jsonl"
    p.write_text('{"process": {"name": "evil.exe"}, "process.name": "svchost.exe"}\n'
                 '{"process": {"name": "b.exe"}}\n', encoding="utf-8")
    rec = store.ingest_json(str(p), flatten_mode="full", build_fts=False)
    assert _cells(store, rec) == (["process.name", "process.name_1"],
                                  [("evil.exe", "svchost.exe"), ("b.exe", "")])
    assert rec["key_collisions"] == 1
    prev = store.preview_json_file(str(p), flatten_mode="full")
    assert prev["columns"] == ["process.name", "process.name_1"]
    assert prev["key_collisions"] == 1


def test_duplicate_keys_in_one_object_are_both_kept(store, tmp_path):
    """json.loads keeps the LAST of two equal keys, so the first value of a
    duplicated key never reached the table. Both land; a container that is
    stored as JSON text keeps both keys verbatim."""
    p = tmp_path / "dup.jsonl"
    p.write_text('{"user": "alice", "user": "root", "n": {"a": 1, "a": 2}}\n', encoding="utf-8")
    rec = store.ingest_json(str(p), flatten_mode="none", build_fts=False)
    assert _cells(store, rec) == (["user", "user_1", "n"], [("alice", "root", '{"a": 1, "a": 2}')])
    assert rec["key_collisions"] == 1
    rec = store.ingest_json(str(p), flatten_mode="full", build_fts=False)
    assert _cells(store, rec) == (["user", "user_1", "n.a", "n.a_1"], [("alice", "root", "1", "2")])


def test_a_suffix_never_lands_on_a_key_the_record_already_has():
    flat = _flatten_json(json.loads('{"a.b": "x", "a.b_1": "y"}') | {"a": {"b": "z"}}, max_depth=None)
    assert flat == {"a.b": "x", "a.b_1": "y", "a.b_2": "z"}


@pytest.mark.parametrize("ext", [".jsonl", ".json"])
def test_numbers_keep_the_text_the_file_wrote(store, tmp_path, ext):
    """Numbers went through float(): 1.10 became 1.1, 1e5 became 100000.0,
    1E400 became inf and nineteen significant digits became seventeen —
    the cell no longer said what the evidence said. And an integer past
    Python's 4,300-digit conversion limit raised out of json.loads and
    failed the whole file. A number is kept as the literal it was written
    as, inside stringified containers too."""
    huge = "9" * 5000
    doc = ('{"n": 1.10, "e": 1e5, "big": 1E400, "f": 1700000000.123456789, "neg0": -0.0, "z": -0,'
           ' "nan": NaN, "huge": ' + huge + ', "arr": [1.10, 2E3, {"x": 0.50}], "s": "1.10"}')
    p = tmp_path / f"nums{ext}"
    p.write_text(doc + "\n", encoding="utf-8")
    rec = store.ingest_json(str(p), flatten_mode="none", build_fts=False)
    cols, rows = _cells(store, rec)
    assert dict(zip(cols, rows[0])) == {
        "n": "1.10", "e": "1e5", "big": "1E400", "f": "1700000000.123456789", "neg0": "-0.0",
        "z": "-0", "nan": "NaN", "huge": huge, "arr": '[1.10, 2E3, {"x": 0.50}]', "s": "1.10"}
    assert json.loads(dict(zip(cols, rows[0]))["arr"]) == [1.1, 2000.0, {"x": 0.5}]


def test_stringified_containers_still_match_json_dumps():
    """The literal-preserving writer has to produce exactly what
    json.dumps(ensure_ascii=False) did for everything that is not a
    number — the stored text of every existing import is that shape."""
    from winnow.store import _json_loads
    for doc in ['{"a": [1, 2.5, "x", null, true, false, {}, []], "b": {"c": "é\\n\\"q\\""}}',
                '[[[["deep"]]]]', '{}', '[]', '{"k": {"": ""}}']:
        assert _json_leaf_text(_json_loads(doc)) == json.dumps(json.loads(doc), ensure_ascii=False)


def test_containers_past_the_recursion_depth_keep_the_same_text():
    """Below 200 levels the writer goes iterative; what it writes there
    must be what it writes above, numbers and duplicate keys included."""
    from winnow.store import _json_loads
    inner = '{"a": [2.5, "x", null, {"k": 1}], "b": {}}'
    doc = "[" * 500 + inner + "]" * 500
    assert _json_leaf_text(_json_loads(doc)) == json.dumps(json.loads(doc), ensure_ascii=False) == doc
    literal = "[" * 500 + '{"n": 1.10, "n": 1E400}' + "]" * 500
    assert _json_leaf_text(_json_loads(literal)) == literal


def test_a_lone_surrogate_escape_costs_nothing(store, tmp_path):
    """"\\udc80" is legal JSON that decodes to a string SQLite can't hold
    as UTF-8: the batch's insert raised "surrogates not allowed" and the
    import stopped — in a key, before a single row. The escape is kept as
    text, the way the file wrote it, so nothing is lost."""
    from winnow.store import BATCH

    path = tmp_path / "ev.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for i in range(1, BATCH + 11):
            if i == BATCH + 5:
                f.write('{"id": %d, "msg": "evil \\udc80 here", "nested": {"k": "\\ud800"}}\n' % i)
            else:
                f.write(json.dumps({"id": i, "msg": f"m{i}"}) + "\n")
    rec = store.ingest_json(str(path), build_fts=False)
    assert rec["row_count"] == BATCH + 10
    v = store.build_view(rec["id"], {"source_id": rec["id"]})
    cells = store.fetch_rows(v["view_id"], BATCH + 4, 1)["rows"][0]["cells"]
    assert cells[1] == "evil \\udc80 here"
    assert json.loads(cells[2]) == {"k": "\ud800"}       # still the JSON it was

    keyed = tmp_path / "k.jsonl"
    keyed.write_text('{"a": 1, "\\ud800x": 2}\n{"a": 3}\n', encoding="utf-8")
    rec = store.ingest_json(str(keyed), build_fts=False)
    assert rec["row_count"] == 2
    assert [c["name"] for c in rec["columns"]] == ["a", "\\ud800x"]


def test_surrogate_escaping_and_literal_text_hold_together(store, tmp_path):
    """Two fixes meet in _json_leaf_text and _flatten_json: a lone surrogate
    is written back as its escape (utf8_text), and a container keeps its
    numbers' literal text and every duplicated key. Neither undoes the
    other. A key whose surrogate escapes to the same text as a key that
    literally reads \\ud800 is a collision like any other: both values
    kept, the record counted."""
    p = tmp_path / "both.jsonl"
    p.write_text('{"n": {"k": "\\ud800", "x": 1.10, "x": 2E3}, "\\ud800": 1, "\\\\ud800": 2}\n',
                 encoding="utf-8")
    rec = store.ingest_json(str(p), flatten_mode="none", build_fts=False)
    cols, rows = _cells(store, rec)
    assert cols == ["n", "\\ud800", "\\ud800_1"]
    assert rows == [('{"k": "\\ud800", "x": 1.10, "x": 2E3}', "1", "2")]
    assert json.loads(rows[0][0])["k"] == "\ud800"
    assert rec["key_collisions"] == 1
