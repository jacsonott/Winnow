"""A delimiter the analyst types, for a log no reader claims.

A log with an extension nothing recognizes imports one line per row (raw
text). Its fields are often set off by something no preset covers: " | ",
" - ", "::". The import window's Custom delimiter takes any text:

- One character goes to the csv reader the presets use, so quotes group a
  field (an access log's "GET / HTTP/1.1" stays whole when split on a
  space).
- More than one character can't go to csv (it takes exactly one), so each
  line is split as written (store._LiteralRows). Quotes are ordinary
  characters there, so a stray one in a log line can't swallow the lines
  after it; a line ends at \\n alone, as in raw text, so a \\r inside a
  logged value can't forge a row; and a line with more fields than the
  first keeps the rest in its last column instead of losing it.

The preview and the import read the same way, and the import records the
delimiter it used, so a session re-import splits the same way again.
"""

from __future__ import annotations

import json
import time

import pytest

from winnow.store import check_delimiter

LOG = (
    "2026-11-02 08:14:03 | INFO | auth | session opened for alice\r\n"
    '2026-11-02 08:14:09 | WARN | auth | failed password for "admin\r\n'
    "2026-11-02 08:14:11 | WARN | auth | failed again | attempt 2\r\n"
    "\r\n"
    "2026-11-02 08:15:42 | INFO | web | user x\rforged | GET /\r\n"
    "2026-11-02 08:16:05 | ERROR | db\r\n"
)


def _write(tmp_path, name, text, encoding="utf-8"):
    p = tmp_path / name
    p.write_text(text, encoding=encoding, newline="")
    return str(p)


def _rows(store, rec):
    cols = [c["name"] for c in rec["columns"]]
    sel = ", ".join(f'"{c}"' for c in cols)
    return cols, [list(r) for r in store.db.execute(f'SELECT {sel} FROM "{rec["table_name"]}" ORDER BY rid')]


def _wait(store, job_id):
    for _ in range(400):
        j = next(j for j in store.list_ingest_jobs() if j["job_id"] == job_id)
        if j["status"] in ("done", "error"):
            return j
        time.sleep(0.025)
    raise AssertionError("ingest job never finished")


# ------------------------------------------------------------ the check

@pytest.mark.parametrize("bad, words", [
    ("", "empty"),
    ("\n", "line break"),
    ("a\r", "line break"),
    ('"', "double quote"),
    ("\0", "NUL"),
    ("|￿", "NUL"),
])
def test_a_delimiter_no_import_could_split_on_is_refused_in_words(bad, words):
    with pytest.raises(ValueError, match=words):
        check_delimiter(bad)


@pytest.mark.parametrize("ok", [None, ",", "\t", " ", "^", " | ", "::", "||", '"|"', "é"])
def test_any_other_text_is_a_delimiter(ok):
    check_delimiter(ok)


# ------------------------------------------------- longer than one character

def test_a_multi_character_delimiter_splits_each_line_as_written(store, tmp_path):
    p = _write(tmp_path, "app.log", LOG)
    rec = store.ingest_csv(p, delimiter=" | ", has_header=False, build_fts=False)
    cols, rows = _rows(store, rec)
    assert cols == ["col_1", "col_2", "col_3", "col_4"]
    assert rows == [
        ["2026-11-02 08:14:03", "INFO", "auth", "session opened for alice"],
        # A stray quote is an ordinary character: nothing after it is swallowed.
        ["2026-11-02 08:14:09", "WARN", "auth", 'failed password for "admin'],
        # More fields than the first line: the rest stays in the last column.
        ["2026-11-02 08:14:11", "WARN", "auth", "failed again | attempt 2"],
        # A blank line is kept, so the row number is still the line number.
        ["", "", "", ""],
        # A \r inside a line is the line's own text, not a row of its own.
        ["2026-11-02 08:15:42", "INFO", "web", "user x\rforged | GET /"],
        # Fewer fields: padded, and counted as ragged.
        ["2026-11-02 08:16:05", "ERROR", "db", ""],
    ]
    assert rec["row_count"] == 6
    assert rec["ragged_rows"] == 2   # the blank line and the short one
    assert rec["suspect_quote_rows"] == 0


def test_with_a_header_the_first_line_names_the_columns(store, tmp_path):
    p = _write(tmp_path, "app.log", "Time :: Level :: Message\n1 :: INFO :: a :: b\n2 :: WARN :: c\n")
    rec = store.ingest_csv(p, delimiter=" :: ", has_header=True, build_fts=False)
    cols, rows = _rows(store, rec)
    assert cols == ["Time", "Level", "Message"]
    assert rows == [["1", "INFO", "a :: b"], ["2", "WARN", "c"]]


def test_a_utf16_log_splits_the_same(store, tmp_path):
    p = _write(tmp_path, "app.log", "﻿a || b\r\nc || d\r\n", encoding="utf-16-le")
    rec = store.ingest_csv(p, delimiter=" || ", has_header=False, build_fts=False)
    assert _rows(store, rec)[1] == [["a", "b"], ["c", "d"]]


def test_the_preview_shows_the_table_the_import_builds(store, tmp_path):
    p = _write(tmp_path, "app.log", LOG)
    with open(p, encoding="utf-8", newline="") as f:
        text = f.read()
    prev = store.preview_csv_text(text, delimiter=" | ", has_header=False, name=p)
    rec = store.ingest_csv(p, delimiter=" | ", has_header=False, build_fts=False)
    cols, rows = _rows(store, rec)
    assert prev["delimiter"] == " | "
    assert prev["columns"] == cols
    assert prev["sample_rows"] == rows


# --------------------------------------------------------- one character

def test_one_character_follows_the_presets_rules(store, tmp_path):
    """A single character goes to csv, as Comma and Pipe do: a quoted field
    is one field however many spaces it holds. That is what makes Space
    work on an access log."""
    p = _write(tmp_path, "access.log",
               '10.0.0.1 - - "GET /a HTTP/1.1" 200\n10.0.0.2 - bob "POST /b HTTP/1.1" 404\n')
    rec = store.ingest_csv(p, delimiter=" ", has_header=False, build_fts=False)
    assert _rows(store, rec)[1] == [
        ["10.0.0.1", "-", "-", "GET /a HTTP/1.1", "200"],
        ["10.0.0.2", "-", "bob", "POST /b HTTP/1.1", "404"],
    ]
    p = _write(tmp_path, "caret.log", "a^b^c\n1^2^3\n")
    rec = store.ingest_csv(p, delimiter="^", has_header=True, build_fts=False)
    assert _rows(store, rec) == (["a", "b", "c"], [["1", "2", "3"]])


def test_a_bad_delimiter_is_refused_before_anything_is_made(store, tmp_path):
    p = _write(tmp_path, "app.log", LOG)
    with pytest.raises(ValueError, match="double quote"):
        store.ingest_csv(p, delimiter='"', build_fts=False)
    with pytest.raises(ValueError, match="line break"):
        store.preview_csv_text(LOG, delimiter="\n")
    assert store.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0


def test_the_delimiter_is_recorded_for_a_reimport(store, tmp_path):
    p = _write(tmp_path, "app.log", LOG)
    rec = store.ingest_csv(p, delimiter=" | ", has_header=False, build_fts=False)
    ingest = store.db.execute("SELECT ingest FROM sources WHERE id=?", (rec["id"],)).fetchone()[0]
    assert json.loads(ingest) == {"kind": "csv", "delimiter": " | ", "has_header": False}


def test_a_background_job_reads_it_the_same(store, tmp_path):
    p = _write(tmp_path, "app.log", LOG)
    job = store.start_ingest_job("csv", p, options={"build_fts": False, "delimiter": " | ", "has_header": False})
    j = _wait(store, job["job_id"])
    assert j["status"] == "done", j.get("error")
    src = store.get_source(j["source_ids"][0])
    assert src["row_count"] == 6 and len(src["columns"]) == 4


# ------------------------------------------------------------------ routes

def test_preview_and_import_by_path_take_a_custom_delimiter(client, store, tmp_path):
    p = _write(tmp_path, "app.log", LOG)
    r = client.post("/api/ingest/preview/path",
                    json={"path": p, "kind": "csv", "delimiter": " | ", "has_header": False})
    assert r.status_code == 200, r.text
    assert r.json()["columns"] == ["col_1", "col_2", "col_3", "col_4"]
    assert r.json()["sample_rows"][2][3] == "failed again | attempt 2"
    r = client.post("/api/ingest/jobs/path",
                    json={"path": p, "kind": "csv", "delimiter": " | ", "has_header": False, "build_fts": False})
    assert r.status_code == 200, r.text
    j = _wait(store, r.json()["job_id"])
    assert j["status"] == "done", j.get("error")
    assert store.get_source(j["source_ids"][0])["row_count"] == 6


def test_an_upload_keeps_a_delimiter_that_is_only_a_space(client, store, tmp_path):
    """A form field of one space has to arrive as a space, not as nothing
    (which would ask for a sniff)."""
    body = b"a b c\n1 2 3\n"
    r = client.post("/api/ingest/preview", files={"file": ("app.log", body)},
                    data={"kind": "csv", "delimiter": " ", "has_header": "true"})
    assert r.status_code == 200, r.text
    assert r.json()["delimiter"] == " " and r.json()["columns"] == ["a", "b", "c"]
    r = client.post("/api/ingest/jobs/upload", files={"file": ("app.log", body)},
                    data={"kind": "csv", "delimiter": " ", "has_header": "true", "build_fts": "false"})
    assert r.status_code == 200, r.text
    j = _wait(store, r.json()["job_id"])
    assert j["status"] == "done", j.get("error")
    assert [c["name"] for c in store.get_source(j["source_ids"][0])["columns"]] == ["a", "b", "c"]


def test_a_bad_delimiter_is_a_400_in_words_not_a_failed_job(client, store, tmp_path):
    p = _write(tmp_path, "app.log", LOG)
    r = client.post("/api/ingest/preview/path", json={"path": p, "kind": "csv", "delimiter": '"'})
    assert r.status_code == 400 and "double quote" in r.json()["detail"]
    r = client.post("/api/ingest/jobs/path", json={"path": p, "kind": "csv", "delimiter": "a\nb"})
    assert r.status_code == 400 and "line break" in r.json()["detail"]
    r = client.post("/api/ingest/jobs/upload", files={"file": ("app.log", LOG.encode())},
                    data={"kind": "csv", "delimiter": '"'})
    assert r.status_code == 400 and "double quote" in r.json()["detail"]
    assert store.list_ingest_jobs() == []


# ------------------------------------------------- folder import's profiles

def test_a_profile_keeps_how_other_text_files_are_split(client):
    created = client.post("/api/import_profiles", json={
        "name": "App logs", "extensions": [".csv", "*"], "text_delimiter": " | ", "text_has_header": True,
    }).json()
    assert created["text_delimiter"] == " | " and created["text_has_header"] is True
    assert client.get("/api/import_profiles").json() == [created]
    # Blank is one line per row again.
    updated = client.post("/api/import_profiles", json={
        "id": created["id"], "name": "App logs", "extensions": [".csv", "*"], "text_delimiter": "",
    }).json()
    assert updated["text_delimiter"] is None and updated["text_has_header"] is False
    r = client.post("/api/import_profiles", json={"name": "Bad", "text_delimiter": '"'})
    assert r.status_code == 400 and "double quote" in r.json()["detail"]
    assert [p["name"] for p in client.get("/api/import_profiles").json()] == ["App logs"]


def test_a_profile_saved_before_the_option_reads_as_one_line_per_row(client):
    from winnow import workspace as WS

    WS.import_profiles._save([{"id": 1, "name": "Old", "extensions": ["*"], "include_patterns": [],
                               "exclude_patterns": [], "recursive": True, "created_at": "2026-01-01"}])
    [old] = client.get("/api/import_profiles").json()
    assert old.get("text_delimiter") is None and not old.get("text_has_header")

