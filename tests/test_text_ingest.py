"""The raw-text importer (Store.ingest_text): one row per physical line,
the whole line in the one Message column — for logs whose extension no
built-in claims, and for anything the analyst forces through it.

Pins what the CSV path got wrong for a log: the first line is data, a
comma never splits a line, a wide line is never truncated, a stray quote
swallows nothing, blank lines survive (the row id is the line number),
and a binary file is refused rather than imported as mojibake.
"""
import os
import time

import pytest

from winnow.store import TEXT_COLUMN, Store, looks_binary

LOG = (
    "2026-03-14T08:00:01Z hostd[2098]: verbose Starting, commas, inside, the line\n"
    "\n"
    "2026-03-14T08:00:02Z hostd[2098]: a \"stray quote here\n"
    "2026-03-14T08:00:03Z hostd[2098]: still its own row\n"
    "last line without a newline"
)


def _write(tmp_path, name, text, mode="w", **kw):
    p = tmp_path / name
    with open(p, mode, **kw) as f:
        f.write(text)
    return str(p)


def test_one_row_per_line_in_one_column(store, tmp_path):
    rec = store.ingest_text(_write(tmp_path, "hostd.log", LOG), build_fts=False)
    assert [c["name"] for c in rec["columns"]] == [TEXT_COLUMN]
    assert rec["columns"][0]["type"] == "text"
    assert rec["row_count"] == 5
    rows = [tuple(r) for r in store.db.execute(f"SELECT rid, {TEXT_COLUMN} FROM src_{rec['id']} ORDER BY rid")]
    assert rows[0] == (1, "2026-03-14T08:00:01Z hostd[2098]: verbose Starting, commas, inside, the line")
    assert rows[1] == (2, "")                      # blank line kept: rid == line number
    assert rows[2][1].endswith('a "stray quote here')
    assert rows[3][1].endswith("still its own row")  # not swallowed by the quote
    assert rows[4] == (5, "last line without a newline")
    assert rec["ragged_rows"] == 0 and rec["suspect_quote_rows"] == 0
    assert rec["name"] == "hostd.log"


def test_crlf_is_stripped_and_utf16_bom_is_read(store, tmp_path):
    p = _write(tmp_path, "win.log", "one\r\ntwo\r\n", mode="w", newline="")
    rec = store.ingest_text(p, build_fts=False)
    assert [r[0] for r in store.db.execute(f"SELECT {TEXT_COLUMN} FROM src_{rec['id']} ORDER BY rid")] == ["one", "two"]
    p16 = _write(tmp_path, "ps.log", "eins\nzwei\n", mode="w", encoding="utf-16", newline="")   # the codec writes the BOM
    assert looks_binary(p16) is False   # UTF-16 has NULs but a BOM
    rec = store.ingest_text(p16, build_fts=False)
    assert [r[0] for r in store.db.execute(f"SELECT {TEXT_COLUMN} FROM src_{rec['id']} ORDER BY rid")] == ["eins", "zwei"]


def test_a_very_long_line_is_one_row(store, tmp_path):
    """The csv module's 128 KB field limit stopped an import; a line
    iterator has no such limit."""
    long = "x" * 300_000
    rec = store.ingest_text(_write(tmp_path, "big.out", f"a\n{long}\nb\n"), build_fts=False)
    assert rec["row_count"] == 3
    (n,) = store.db.execute(f"SELECT length({TEXT_COLUMN}) FROM src_{rec['id']} WHERE rid = 2").fetchone()
    assert n == 300_000


def test_binary_is_refused_and_empty_is_an_error(store, tmp_path):
    p = _write(tmp_path, "tool.exe", b"MZ\x90\x00\x03\x00\x00\x00", mode="wb")
    assert looks_binary(p) is True
    with pytest.raises(ValueError, match="binary"):
        store.ingest_text(p, build_fts=False)
    with pytest.raises(ValueError, match="empty"):
        store.ingest_text(_write(tmp_path, "empty.log", ""), build_fts=False)
    assert store.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0


def test_runs_as_a_background_job_with_byte_progress(store, tmp_path):
    p = _write(tmp_path, "auth.log.1", "".join(f"line {i}\n" for i in range(50_000)))
    job = store.start_ingest_job("text", p, options={"build_fts": False})
    assert job["kind"] == "text" and job["unit"] == "bytes" and job["units_total"] == os.path.getsize(p)
    for _ in range(200):
        j = store.list_ingest_jobs()[0]
        if j["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert j["status"] == "done", j.get("error")
    assert j["rows_done"] == 50_000 and j["units_done"] == j["units_total"]
    src = store.get_source(j["source_ids"][0])
    assert src["row_count"] == 50_000 and src["name"] == "auth.log.1"


def test_preview_lines(store):
    p = store.preview_text_lines("a,b\n\nc\n")
    assert p == {"delimiter": None, "columns": [TEXT_COLUMN], "sample_rows": [["a,b"], [""], ["c"]], "inferred_types": ["text"]}
    with pytest.raises(ValueError, match="empty"):
        store.preview_text_lines("")


# ------------------------------------------------------- folder import scan

def _tree(tmp_path):
    root = tmp_path / "bundle"
    (root / "var" / "log").mkdir(parents=True)
    (root / "a.csv").write_text("h\n1\n")
    (root / "var" / "log" / "hostd.log").write_text("l1\nl2\n")
    (root / "var" / "log" / "auth.log.1").write_text("l1\n")
    (root / "noext").write_text("plain\n")
    (root / "tool.exe").write_bytes(b"MZ\x00\x00")
    return str(root)


def test_scan_default_still_excludes_unknown_extensions(store, tmp_path):
    r = store.scan_import_directory(_tree(tmp_path))
    assert {m["rel_path"] for m in r["matched"]} == {"a.csv"}
    reasons = {e["rel_path"]: e["reason"] for e in r["excluded"]}
    assert reasons == {"var/log/hostd.log": "extension", "var/log/auth.log.1": "extension",
                       "noext": "extension", "tool.exe": "extension"}


def test_scan_star_admits_other_text_files_as_text_kind(store, tmp_path):
    root = _tree(tmp_path)
    r = store.scan_import_directory(root, extensions=[".csv", "*"])
    kinds = {m["rel_path"]: m["kind"] for m in r["matched"]}
    assert kinds == {"a.csv": "csv", "var/log/hostd.log": "text", "var/log/auth.log.1": "text", "noext": "text"}
    assert {(e["rel_path"], e["reason"]) for e in r["excluded"]} == {("tool.exe", "binary")}
    # include/exclude patterns still apply to files admitted this way
    r2 = store.scan_import_directory(root, extensions=["*"], exclude_patterns=["*.1"])
    assert "var/log/auth.log.1" not in {m["rel_path"] for m in r2["matched"]}


def test_scan_probes_for_binary_only_after_the_pattern_gates(store, tmp_path, monkeypatch):
    """The binary probe opens the file, so it runs last: a tree of build
    output excluded by pattern is never read, and a file the patterns
    exclude reports the pattern, not "binary"."""
    import winnow.store as st
    root = _tree(tmp_path)
    opened = []
    real = st.looks_binary
    monkeypatch.setattr(st, "looks_binary", lambda path: (opened.append(path), real(path))[1])
    r = store.scan_import_directory(root, extensions=["*"], exclude_patterns=["*.exe"])
    reasons = {e["rel_path"]: e["reason"] for e in r["excluded"]}
    assert reasons["tool.exe"] == "excluded by pattern: *.exe"
    assert not any(p.endswith("tool.exe") for p in opened)
    r = store.scan_import_directory(root, extensions=["*"], include_patterns=["*.log"])
    assert not any(p.endswith("tool.exe") for p in opened)
    # …and one nothing gates is still probed and refused
    r = store.scan_import_directory(root, extensions=["*"])
    assert ("tool.exe", "binary") in {(e["rel_path"], e["reason"]) for e in r["excluded"]}


# ------------------------------------------------------------------ routes

def test_unknown_extension_routes_to_text(client, tmp_path):
    import server

    assert server._ingest_kind_for_path("/x/hostd.log") == "text"
    assert server._ingest_kind_for_path("/x/auth.log.1") == "text"
    assert server._ingest_kind_for_path("/x/noext") == "text"
    assert server._ingest_kind_for_path("/x/notes.txt") == "csv"      # .txt keeps the delimited path
    assert server._ingest_kind_for_path("/x/a.csv") == "csv"
    assert server._ingest_kind_for_path("/x/a.jsonl") == "json"
    p = _write(tmp_path, "hostd.log", LOG)
    r = client.post("/api/ingest/preview/path", json={"path": p})
    assert r.status_code == 200, r.text
    assert r.json()["columns"] == [TEXT_COLUMN] and len(r.json()["sample_rows"]) == 5
    r = client.post("/api/ingest/jobs/path", json={"path": p, "build_fts": False})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "text"


def test_txt_can_be_forced_to_text_by_kind(client, tmp_path):
    p = _write(tmp_path, "export.txt", "a,b\n1,2\n")
    r = client.post("/api/ingest/preview/path", json={"path": p, "kind": "text"})
    assert r.json()["sample_rows"] == [["a,b"], ["1,2"]]
    r = client.post("/api/ingest/preview/path", json={"path": p})
    assert r.json()["columns"] == ["a", "b"]
