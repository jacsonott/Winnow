"""A .tsv is read as tab-separated, because that is what the name says.

Content sniffing alone got this wrong on real exports. `csv.Sniffer`
scores each candidate by how consistently it appears per line and, when
two of them tie, breaks the tie with CPython's own `preferred` list —
which starts with the comma. One comma on every line is enough to tie,
and a column named `Last Modified (UTC, local)` puts one on the header
as well as the body. The file then imports with the header split down the
middle of a column name and every row read down the wrong columns.

So the extension is consulted first, and believed when the bytes agree
with it. A mislabelled file still falls through to the sniffer: the name
is evidence, not an instruction.
"""

from __future__ import annotations

import pytest

from winnow.store import EXTENSION_DELIMITERS, Store

# The header carries a comma, so every line does, so the comma is exactly
# as consistent as the tab. This is the shape that was arriving as CSV.
TIED = (
    "Last Modified (UTC, local)\tPath\tSize\n"
    "2026-01-01, 00:00:01\tC:\\Windows\\a.txt\t10\n"
    "2026-01-01, 00:00:02\tC:\\Windows\\b.txt\t20\n"
    "2026-01-01, 00:00:03\tC:\\Windows\\c.txt\t30\n"
)


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8", newline="")
    return str(p)


# ------------------------------------------------------- the sniff itself

def test_the_tie_really_does_go_to_the_comma_without_a_name():
    """The bug this fixes, pinned as the reason the fix exists. If CPython
    ever changes its tie-break this fails, and the extension rule becomes
    belt and braces rather than the thing holding the file together."""
    assert Store._sniff(TIED) == ","


def test_the_extension_settles_it():
    assert Store._sniff(TIED, "MFTECmd_$MFT_Output.tsv") == "\t"


@pytest.mark.parametrize("ext,ch", sorted(EXTENSION_DELIMITERS.items()))
def test_every_named_extension_is_honoured(ext, ch):
    text = "".join(f"a{ch}b{ch}c\n" for _ in range(4))
    assert Store._sniff(text, "export" + ext) == ch


def test_a_name_that_disagrees_with_the_bytes_is_not_believed():
    """The name is evidence, not an instruction — a comma-separated file
    somebody saved as .tsv is still read as commas rather than as one
    very wide column."""
    assert Store._sniff("a,b,c\n1,2,3\n4,5,6\n", "mislabelled.tsv") == ","


def test_an_extension_that_names_nothing_is_sniffed():
    """.txt and extensionless dumps say nothing about their delimiter, so
    they go through the sniffer exactly as before."""
    tabbed = "a\tb\tc\n1\t2\t3\n4\t5\t6\n"
    assert Store._sniff(tabbed, "log.txt") == "\t"
    assert Store._sniff(tabbed, "no_extension_at_all") == "\t"
    assert Store._sniff(tabbed) == "\t"


def test_a_stray_delimiter_on_one_line_does_not_qualify():
    """One tab inside one field of a CSV must not make the file a TSV."""
    assert Store._sniff("a,b,c\n1,2,3\nx\ty,2,3\n", "notes.csv") == ","
    assert Store._looks_delimited_by("a,b\n1,2\nx\ty\n", "\t") is False


def test_ragged_rows_still_count_as_delimited():
    """Ragged rows are padded rather than refused (docs/notes/ingest.md),
    so a file is not disqualified by one short line."""
    rows = ["a\tb\tc"] + ["1\t2\t3"] * 18 + ["4\t5"]
    assert Store._looks_delimited_by("\n".join(rows), "\t") is True


def test_a_line_without_it_at_all_disqualifies():
    rows = ["a\tb\tc"] + ["1\t2\t3"] * 8 + ["a line with no tab in it"]
    assert Store._looks_delimited_by("\n".join(rows), "\t") is False


# ------------------------------------------------------------ end to end

def test_a_tsv_imports_with_its_own_columns(store, tmp_path):
    path = _write(tmp_path, "mft.tsv", TIED)
    rec = store.ingest_csv(path, name="mft.tsv")
    assert [c["name"] for c in rec["columns"]] == ["Last Modified (UTC, local)", "Path", "Size"]
    assert rec["row_count"] == 3
    view = store.build_view(rec["id"], {"source_id": rec["id"], "filters": [], "sort": []})
    rows = store.fetch_rows(view["view_id"], 0, 10)["rows"]
    assert rows[0]["cells"] == ["2026-01-01, 00:00:01", "C:\\Windows\\a.txt", "10"]


def test_without_the_name_the_same_bytes_came_in_wrong(store, tmp_path):
    """The before picture, as the file an analyst actually had: saved
    without the extension, the comma wins and the header is cut in half."""
    path = _write(tmp_path, "mft_no_ext", TIED)
    rec = store.ingest_csv(path, name="mft_no_ext")
    assert [c["name"] for c in rec["columns"]][0] == "Last Modified (UTC"


def test_the_preview_and_the_import_agree(store, tmp_path):
    """The analyst's complaint was about the preview. If these two ever
    disagree the preview is a picture of a table the import will not
    build."""
    path = _write(tmp_path, "mft.tsv", TIED)
    prev = store.preview_csv_text(TIED, name="mft.tsv")
    rec = store.ingest_csv(path, name="mft.tsv")
    assert prev["delimiter"] == "\t"
    assert prev["columns"] == [c["name"] for c in rec["columns"]]


def test_an_explicit_delimiter_still_wins(store, tmp_path):
    """Setting it by hand in the preview overrides both the name and the
    bytes — that path is how a genuinely odd file gets imported at all."""
    path = _write(tmp_path, "mft.tsv", TIED)
    rec = store.ingest_csv(path, name="mft.tsv", delimiter=",")
    assert [c["name"] for c in rec["columns"]][0] == "Last Modified (UTC"
    assert store.preview_csv_text(TIED, delimiter=",", name="mft.tsv")["delimiter"] == ","


def test_a_display_name_without_an_extension_does_not_lose_the_sniff(store, tmp_path):
    """`name` is the caller's to override and is often a nickname; the
    extension comes off the PATH, so a renamed import still reads right."""
    path = _write(tmp_path, "mft.tsv", TIED)
    rec = store.ingest_csv(path, name="MFT for WKSTN-14")
    assert [c["name"] for c in rec["columns"]] == ["Last Modified (UTC, local)", "Path", "Size"]


# ---------------------------------------------------------- the routes

def test_the_path_preview_route_reads_the_extension(client, tmp_path):
    path = _write(tmp_path, "mft.tsv", TIED)
    r = client.post("/api/ingest/preview/path", json={"path": path})
    assert r.status_code == 200
    assert r.json()["delimiter"] == "\t"


def test_the_upload_preview_route_reads_the_filename(client):
    r = client.post("/api/ingest/preview",
                    files={"file": ("mft.tsv", TIED.encode(), "text/tab-separated-values")},
                    data={"has_header": "true", "kind": "csv"})
    assert r.status_code == 200
    body = r.json()
    assert body["delimiter"] == "\t"
    assert body["columns"] == ["Last Modified (UTC, local)", "Path", "Size"]
