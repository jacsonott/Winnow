"""Directory import: Store.scan_import_directory's matching rules (extension
filter, include/exclude glob patterns, recursion, already-imported
detection, the result cap), plus the three new HTTP routes that back the
"Import a folder..." modal — /api/ingest/dir/scan, /api/ingest/json/path,
and /api/import_profiles."""

from __future__ import annotations

import csv
import json
import os

import pytest

from winnow import store as store_module


def _write(tmp_path, rel, content="a,b\n1,2\n"):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return str(path)


@pytest.fixture
def kape_tree(tmp_path):
    """A small KAPE-shaped output directory: two CSVs at the top level (one
    matching the noisy-Amcache-output pattern from the feature request), one
    nested under a subfolder, and one file with an unrecognized extension."""
    root = tmp_path / "kape_out"
    _write(root, "DESKTOP01_AmcacheParser_Amcache_UnassociatedFileEntries.csv")
    _write(root, "DESKTOP01_EvtxECmd_Output.csv")
    _write(root, "RegistryHives/SYSTEM.csv")
    _write(root, "readme.txt.bak")
    return str(root)


def test_scan_matches_recognized_extensions_by_default(kape_tree, store):
    r = store.scan_import_directory(kape_tree)
    matched = {m["rel_path"] for m in r["matched"]}
    assert matched == {
        "DESKTOP01_AmcacheParser_Amcache_UnassociatedFileEntries.csv",
        "DESKTOP01_EvtxECmd_Output.csv",
        "RegistryHives/SYSTEM.csv",
    }
    excluded = {(e["rel_path"], e["reason"]) for e in r["excluded"]}
    assert excluded == {("readme.txt.bak", "extension")}
    assert r["truncated"] is False


def test_scan_exclude_pattern_matches_by_filename(kape_tree, store):
    r = store.scan_import_directory(kape_tree, exclude_patterns=["*_Amcache_UnassociatedFileEntries.csv"])
    matched = {m["rel_path"] for m in r["matched"]}
    assert "DESKTOP01_AmcacheParser_Amcache_UnassociatedFileEntries.csv" not in matched
    assert "DESKTOP01_EvtxECmd_Output.csv" in matched
    reasons = dict((e["rel_path"], e["reason"]) for e in r["excluded"])
    assert reasons["DESKTOP01_AmcacheParser_Amcache_UnassociatedFileEntries.csv"] == (
        "excluded by pattern: *_Amcache_UnassociatedFileEntries.csv"
    )


def test_scan_exclude_pattern_with_slash_matches_relative_subpath(kape_tree, store):
    # A pattern containing '/' matches the rel_path, not the bare filename —
    # this is what lets a whole subfolder be excluded (RegistryHives/*)
    # without also matching a same-named file elsewhere in the tree.
    r = store.scan_import_directory(kape_tree, exclude_patterns=["RegistryHives/*"])
    matched = {m["rel_path"] for m in r["matched"]}
    assert "RegistryHives/SYSTEM.csv" not in matched
    assert "DESKTOP01_EvtxECmd_Output.csv" in matched

    # A bare-filename pattern (no '/') must NOT match a nested file just
    # because its rel_path happens to contain the pattern as a substring —
    # fnmatch requires a full match, and the target here is the filename.
    r2 = store.scan_import_directory(kape_tree, exclude_patterns=["SYSTEM.csv"])
    assert "RegistryHives/SYSTEM.csv" not in {m["rel_path"] for m in r2["matched"]}


def test_scan_include_patterns_narrow_to_matches_only(kape_tree, store):
    r = store.scan_import_directory(kape_tree, include_patterns=["*EvtxECmd*"])
    matched = {m["rel_path"] for m in r["matched"]}
    assert matched == {"DESKTOP01_EvtxECmd_Output.csv"}
    reasons = {e["reason"] for e in r["excluded"] if e["rel_path"].endswith("SYSTEM.csv")}
    assert reasons == {"no include pattern matched"}


def test_scan_pattern_matching_is_case_insensitive(kape_tree, store):
    r = store.scan_import_directory(kape_tree, exclude_patterns=["*AMCACHE_UNASSOCIATEDFILEENTRIES.CSV"])
    matched = {m["rel_path"] for m in r["matched"]}
    assert "DESKTOP01_AmcacheParser_Amcache_UnassociatedFileEntries.csv" not in matched


def test_scan_non_recursive_skips_subfolders(kape_tree, store):
    r = store.scan_import_directory(kape_tree, recursive=False)
    matched = {m["rel_path"] for m in r["matched"]}
    assert "RegistryHives/SYSTEM.csv" not in matched
    assert "DESKTOP01_EvtxECmd_Output.csv" in matched


def test_scan_flags_already_imported_files(kape_tree, store):
    target = os.path.join(kape_tree, "DESKTOP01_EvtxECmd_Output.csv")
    store.ingest_csv(target, build_fts=False)
    r = store.scan_import_directory(kape_tree)
    flags = {m["rel_path"]: m["already_imported"] for m in r["matched"]}
    assert flags["DESKTOP01_EvtxECmd_Output.csv"] is True
    assert flags["RegistryHives/SYSTEM.csv"] is False


def test_scan_json_files_are_classified_by_kind(tmp_path, store):
    root = tmp_path / "mixed"
    _write(root, "a.csv")
    (root / "b.json").write_text(json.dumps([{"x": 1}]))
    r = store.scan_import_directory(str(root))
    kinds = {m["rel_path"]: m["kind"] for m in r["matched"]}
    assert kinds == {"a.csv": "csv", "b.json": "json"}


def test_scan_extensions_override_narrows_recognized_set(tmp_path, store):
    root = tmp_path / "mixed_ext"
    _write(root, "a.csv")
    (root / "b.json").write_text(json.dumps([{"x": 1}]))

    # A narrower override excludes anything outside it, even though .json
    # is in the default recognized set.
    r = store.scan_import_directory(str(root), extensions=[".csv"])
    assert {m["rel_path"] for m in r["matched"]} == {"a.csv"}
    assert {(e["rel_path"], e["reason"]) for e in r["excluded"]} == {("b.json", "extension")}

    # A bare extension without a leading '.' still normalizes correctly.
    r2 = store.scan_import_directory(str(root), extensions=["csv", "json"])
    assert {m["rel_path"] for m in r2["matched"]} == {"a.csv", "b.json"}


def test_scan_caps_results_and_reports_truncated(tmp_path, store, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_SCAN_RESULTS", 3)
    root = tmp_path / "many"
    for i in range(10):
        _write(root, f"file_{i}.csv")
    r = store.scan_import_directory(str(root))
    assert len(r["matched"]) + len(r["excluded"]) <= 3
    assert r["truncated"] is True


def test_scan_unreadable_file_reports_reason_instead_of_raising(tmp_path, store):
    root = tmp_path / "broken"
    root.mkdir()
    broken_link = root / "dangling.csv"
    try:
        os.symlink(str(root / "does_not_exist.csv"), str(broken_link))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this filesystem")
    r = store.scan_import_directory(str(root))
    assert r["matched"] == []
    assert r["excluded"] == [{"path": str(broken_link), "rel_path": "dangling.csv", "reason": "unreadable"}]


# --------------------------------------------------------------- HTTP routes

def test_scan_dir_route(client, kape_tree):
    r = client.post("/api/ingest/dir/scan", json={"root": kape_tree})
    assert r.status_code == 200
    body = r.json()
    assert len(body["matched"]) == 3


def test_scan_dir_route_404s_a_non_directory(client, tmp_path):
    r = client.post("/api/ingest/dir/scan", json={"root": str(tmp_path / "nope")})
    assert r.status_code == 400


def test_ingest_json_path_route(client, tmp_path):
    path = tmp_path / "records.jsonl"
    path.write_text('{"a": 1}\n{"a": 2}\n')
    r = client.post("/api/ingest/json/path", json={"path": str(path), "name": "records"})
    assert r.status_code == 200
    rec = r.json()
    assert rec["row_count"] == 2
    assert rec["name"] == "records"


def test_ingest_json_path_route_400s_a_missing_file(client, tmp_path):
    r = client.post("/api/ingest/json/path", json={"path": str(tmp_path / "nope.json")})
    assert r.status_code == 400


def test_ingest_path_route_reuses_existing_csv_ingest(client, tmp_path):
    # Not new (the route already existed), but this feature is its first
    # real UI consumer — confirm the by-path CSV route still works exactly
    # like the JSON one now does, since the import modal will call both.
    path = tmp_path / "rows.csv"
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows([["a", "b"], ["1", "2"], ["3", "4"]])
    r = client.post("/api/ingest/path", json={"path": str(path), "name": "rows", "build_fts": False})
    assert r.status_code == 200
    assert r.json()["row_count"] == 2


def test_import_profile_routes_crud(client):
    assert client.get("/api/import_profiles").json() == []

    created = client.post("/api/import_profiles", json={
        "name": "KAPE", "exclude_patterns": ["*_Amcache_UnassociatedFileEntries.csv"],
    }).json()
    assert created["name"] == "KAPE"
    assert client.get("/api/import_profiles").json() == [created]

    updated = client.post("/api/import_profiles", json={
        "id": created["id"], "name": "KAPE (tuned)", "recursive": False,
    }).json()
    assert updated["id"] == created["id"]
    assert updated["name"] == "KAPE (tuned)"
    assert updated["recursive"] is False
    assert len(client.get("/api/import_profiles").json()) == 1

    r = client.delete(f"/api/import_profiles/{created['id']}")
    assert r.status_code == 200
    assert client.get("/api/import_profiles").json() == []


# ---------------------------------------------------------------- folders
#
# A directory import reproduces the on-disk tree as sidebar folders: the
# table is named by its basename, and nested files land in folders that
# mirror their path (replacing the old behaviour of concatenating the whole
# rel_path into the source name). Folder assignment happens in the ingest
# job worker, driven here through the same server-path route the UI uses.

def _run_dir_import(store, path, folder_path):
    job = store.start_ingest_job(
        "csv", path, name=os.path.basename(path),
        options={"build_fts": False, "folder_path": folder_path})
    done = store.wait_for_ingest_job(job["job_id"], timeout=30)
    assert done["status"] == "done", done.get("error")
    return done["source_ids"][0]


def test_directory_import_files_nested_tables_into_matching_folders(kape_tree, store):
    scan = store.scan_import_directory(kape_tree)
    matched = {m["rel_path"]: m for m in scan["matched"]}

    # a file at the scan root: basename name, no folder
    root_file = matched["DESKTOP01_EvtxECmd_Output.csv"]
    rid = _run_dir_import(store, root_file["path"], "")
    root_src = next(s for s in store.list_sources() if s["id"] == rid)
    assert root_src["name"] == "DESKTOP01_EvtxECmd_Output.csv"
    assert root_src["folder_id"] is None

    # a nested file: basename name, filed under a folder mirroring its dir
    nested = matched["RegistryHives/SYSTEM.csv"]
    slash = nested["rel_path"].rfind("/")
    base, folder_path = nested["rel_path"][slash + 1:], nested["rel_path"][:slash]
    nid = _run_dir_import(store, nested["path"], folder_path)
    nsrc = next(s for s in store.list_sources() if s["id"] == nid)
    assert nsrc["name"] == "SYSTEM.csv"                 # basename, NOT the rel_path
    folder = next(f for f in store.list_folders() if f["id"] == nsrc["folder_id"])
    assert folder["name"] == "RegistryHives" and folder["parent_id"] is None


def test_directory_import_shares_one_folder_across_files(tmp_path, store):
    """Two files in the same on-disk folder land in the SAME sidebar folder,
    not one folder per file."""
    p1 = _write(tmp_path, "Logs/one.csv")
    p2 = _write(tmp_path, "Logs/two.csv")
    id1 = _run_dir_import(store, str(p1), "Logs")
    id2 = _run_dir_import(store, str(p2), "Logs")
    srcs = {s["id"]: s for s in store.list_sources()}
    assert srcs[id1]["folder_id"] == srcs[id2]["folder_id"] is not None
    assert sum(f["name"] == "Logs" for f in store.list_folders()) == 1

