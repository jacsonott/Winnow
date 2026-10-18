"""URL decode — the first `decode`-family derived op. The pure function
first (what is decoded, what is kept as written), then that it is an
ordinary derived column in the store, merges and re-derive included."""

from __future__ import annotations

import csv

import pytest

from winnow import timeparse
from winnow.store import Store


def _write(path, rows, header=("Uri", "Other")):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return str(path)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "case.db-winnow"))
    yield s
    s.close()


def _values(store, source_id, name):
    src = store._source_lite(source_id)
    with store._reader() as ro:
        return [r[0] for r in ro.execute(
            f"SELECT {store._col_ref(src, name)} FROM {store._from_clause(src)} ORDER BY rid")]


def _decode(value, **params):
    op = timeparse.OPERATIONS["url_decode"]
    return op["parse"](value, timeparse.validate_params("url_decode", params), {})


# ------------------------------------------------------------ pure function

@pytest.mark.parametrize("value, want", [
    ("cmd%3Dwhoami%26x%3D%2Fetc%2Fpasswd", "cmd=whoami&x=/etc/passwd"),
    ("q=hello+world%21", "q=hello world!"),
    ("a%2Bb", "a+b"),                       # an encoded plus is a plus, not a space
    ("caf%C3%A9", "café"),                  # a multi-byte character spans several escapes
    ("%F0%9F%98%80", "😀"),
    ("%25252e", "%252e"),                   # one pass: double encoding comes out single
    ("x%00y", "x\x00y"),
    ("no escapes here", "no escapes here"),
    ("   ", "   "),                         # a copy, not a tidied version
])
def test_decodes_form_style_by_default(value, want):
    assert _decode(value) == want


def test_plus_stays_a_plus_when_switched_off():
    assert _decode("/a+b/c%20d", plus_as_space=False) == "/a+b/c d"
    assert _decode("/a+b/c%20d", plus_as_space="no") == "/a+b/c d"
    assert _decode("a%2Bb", plus_as_space=False) == "a+b"


@pytest.mark.parametrize("value", [
    "100%", "%zz", "%2", "50% off", "%u0041",       # not escapes at all
    "caf%E9", "caf%e9",                             # Latin-1, not UTF-8 — the hex's case kept
    "%E2%82",                                       # a character cut short
    "%ED%B2%80",                                    # an encoded surrogate
])
def test_what_cannot_be_decoded_is_kept_as_written(value):
    assert _decode(value) == value


def test_an_undecodable_escape_does_not_stop_the_rest_of_the_value_decoding():
    # The overlong "../" of a classic traversal: the %C0%AE pairs are not
    # UTF-8 and stay readable as what was sent, the %2F beside them decodes.
    assert _decode("%C0%AE%C0%AE%2Fetc%2Fpasswd") == "%C0%AE%C0%AE/etc/passwd"
    assert _decode("%E2%28%A1") == "%E2(%A1"
    assert _decode("%zz%41") == "%zzA"


def test_empty_is_none_like_every_other_op():
    assert _decode("") is None
    assert _decode(None) is None


def test_registry_lists_it_as_its_own_family_and_never_suggests_it():
    listed = {o["id"]: o for o in timeparse.list_ops()}
    op = listed["url_decode"]
    assert op["family"] == "decode" and op["value_type"] == "text"
    assert op["multi_input"] is False
    assert timeparse.validate_params("url_decode", {}) == {"plus_as_space": True}
    assert timeparse.validate_params("url_decode", {"plus_as_space": False}) == {"plus_as_space": False}
    assert all(r["op_id"] != "url_decode" for r in timeparse.detect(["a%20b", "c%2Fd"]))


# ---------------------------------------------------------------- the store

def test_backfill_decodes_and_counts_nothing_as_a_failure(store, tmp_path):
    sid = store.ingest_csv(_write(tmp_path / "u.csv", [
        ["/search?q=a+b%26c", ""], ["caf%E9", ""], ["plain", ""], ["", ""], ["file.php%00.jpg", ""],
    ]))["id"]
    res = store.add_derived_column(sid, "Uri (decoded)", "Uri", "url_decode", {})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert _values(store, sid, "Uri (decoded)") == [
        "/search?q=a b&c", "caf%E9", "plain", None, "file.php\x00.jpg"]
    d = next(d for d in store.list_derived_columns(sid) if d["name"] == "Uri (decoded)")
    assert d["status"] == "ready" and d["parse_failures"] == 0
    col = next(c for c in store._source_lite(sid)["columns"] if c["name"] == "Uri (decoded)")
    assert col["type"] == "text" and col["derived"]


def test_preview_shows_what_each_value_becomes(store, tmp_path):
    sid = store.ingest_csv(_write(tmp_path / "u.csv", [["a%20b", ""], ["c+d", ""]]))["id"]
    res = store.preview_derived(sid, "Uri", "url_decode", {"plus_as_space": False})
    assert [(p["input"], p["output"]) for p in res["preview"]] == [("a%20b", "a b"), ("c+d", "c+d")]
    assert res["failures"] == 0


def test_rederive_changes_the_plus_rule_in_place(store, tmp_path):
    sid = store.ingest_csv(_write(tmp_path / "u.csv", [["/a+b%20c", ""]]))["id"]
    res = store.add_derived_column(sid, "Path", "Uri", "url_decode", {})
    store.wait_for_ingest_job(res["job_id"], timeout=30)
    assert _values(store, sid, "Path") == ["/a b c"]
    d = next(d for d in store.list_derived_columns(sid) if d["name"] == "Path")
    store.rederive_column(d["id"], {"plus_as_space": False})
    store.wait_for_ingest_job(timeout=30)
    assert _values(store, sid, "Path") == ["/a+b c"]


def test_merge_parity(store, tmp_path):
    a = store.ingest_csv(_write(tmp_path / "a.csv", [["x%2Fy", ""]]))["id"]
    b = store.ingest_csv(_write(tmp_path / "b.csv", [["p+q", ""]]))["id"]
    merge = store.create_merge("m", [a, b])["id"]
    res = store.add_derived_column(merge, "Decoded", "Uri", "url_decode", {})
    for jid in res["job_ids"]:
        store.wait_for_ingest_job(jid, timeout=30)
    assert _values(store, a, "Decoded") == ["x/y"]
    assert _values(store, b, "Decoded") == ["p q"]
    # ... and the merge reads each row through its own member's column
    cols = [c["name"] for c in store.get_source(merge)["columns"]]
    v = store.build_view(merge, {"source_id": merge, "filters": [],
                                 "sort": [{"column": "Decoded", "dir": "asc"}]})
    rows = store.fetch_rows(v["view_id"], 0, 10)["rows"]
    assert [r["cells"][cols.index("Decoded")] for r in rows] == ["p q", "x/y"]
