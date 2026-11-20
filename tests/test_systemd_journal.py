"""The systemd_journal example plugin: Linux journal files and journalctl's
export and JSON output.

The fixtures in tests/data/journal/ were written by systemd itself
(systemd-journal-remote 255, from the made-up entries in
make_fixtures.py), and journalctl printed the export and JSON files from
the first of them. So the load-bearing tests hold the plugin's reading of
a journal FILE to what journalctl says the same file holds, field for
field, in every shape systemd writes: compact with zstd (systemd 252+),
regular with LZ4 and with XZ. Damage is made by cutting and overwriting
bytes of those real files, the way a crash or a bad copy would.

The pure-Python zstd decoder is checked against zstd-*.zst, written by
the zstd command-line tool, on every Python — on 3.14 the plugin uses the
standard library's decoder, so the pure one is tested on its own there.
"""

from __future__ import annotations

import importlib.util
import lzma
import random
import struct
import sys
from pathlib import Path

import pytest

from winnow import log as wlog
from winnow.plugin_api import PluginRegistry, static_meta

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples" / "plugins"
PLUGIN_DIR = EXAMPLES / "systemd_journal"
DATA = Path(__file__).resolve().parent / "data" / "journal"
FORMAT = "systemd-journal.journal"

JOURNALS = ["compact-zstd.journal", "regular-lz4.journal", "regular-xz.journal"]
OUTPUTS = ["journalctl-export.txt", "journalctl-json.txt", "journalctl-json-pretty.txt",
           "journalctl-json-seq.txt", "journalctl-json-sse.txt"]
# What differs between two files holding the same entries: each file has its
# own sequence-number ID, which the cursor carries, and its own hash key.
PER_FILE = ("Offset", "__CURSOR", "__SEQNUM_ID")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def plugin():
    """The plugin as a package, the way the host imports it (its modules
    import each other relatively)."""
    spec = importlib.util.spec_from_file_location(
        "systemd_journal_under_test", PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    yield mod
    for key in [k for k in sys.modules if k == spec.name or k.startswith(spec.name + ".")]:
        del sys.modules[key]


@pytest.fixture(scope="module")
def fixtures() -> dict:
    """name -> make_fixtures.py, for the source of truth it holds."""
    return {"make": _load("journal_make_fixtures", DATA / "make_fixtures.py")}


@pytest.fixture
def unpacked(tmp_path) -> Path:
    for name in JOURNALS + OUTPUTS:
        (tmp_path / name).write_bytes(lzma.decompress((DATA / f"{name}.xz").read_bytes()))
    return tmp_path


def _table(plugin, path):
    out = plugin.parse(str(path), {})
    cols = out["columns"]
    return out, [dict(zip(cols, row)) for row in out["rows"]]


def _without(rows, keys):
    return [{k: v for k, v in r.items() if k not in keys} for r in rows]


# ------------------------------------------------------------ registration

def test_the_plugin_loads_switched_off_and_describes_itself():
    reg = PluginRegistry()
    reg.load([EXAMPLES])
    rec = next(p for p in reg.describe() if p["name"] == "systemd-journal")
    assert rec["error"] is None, rec["error"]
    meta = static_meta(PLUGIN_DIR / "__init__.py")
    assert meta["name"] == "systemd-journal" and meta["declares"] == ["dashboard", "ingest_format"]
    fmt = reg.get_format(FORMAT)
    for name in ("system.journal", "user-1000.journal",
                 "system@1e3c4a2b0f8d4c6e9a7b5d3f1c2e4a6b-0000000000000001-00062d1e7c0b2a40.journal",
                 "system@00062d1e7c0b2a40-9b1d2c3e4f5a6b7c.journal~", "host.export", "SYSTEM.JOURNAL"):
        assert fmt.matches(name), name
    for name in ("journal.json", "journal.txt", "journal", "system.journal.gz"):
        assert not fmt.matches(name), name


# ------------------------------------------------ the files, read as journalctl does

@pytest.mark.parametrize("name", JOURNALS)
def test_a_journal_file_reads_as_journalctl_prints_it(plugin, unpacked, name):
    """Every shape systemd writes, held to journalctl's own export of the
    same entries: the same columns, and every value in every row."""
    out, rows = _table(plugin, unpacked / name)
    _, want = _table(plugin, unpacked / "journalctl-export.txt")
    assert out["columns"][-1] == "Offset"
    assert [c for c in out["columns"] if c != "Offset"] == list(want[0])
    assert len(rows) == 20
    assert _without(rows, PER_FILE) == _without(want, PER_FILE)
    if name == "compact-zstd.journal":  # the file journalctl printed
        assert _without(rows, ("Offset",)) == want


@pytest.mark.parametrize("name", OUTPUTS)
def test_every_lossless_journalctl_output_reads_the_same(plugin, unpacked, name):
    """export, json, json-pretty, json-seq and json-sse of one file: one table."""
    out, rows = _table(plugin, unpacked / name)
    _, want = _table(plugin, unpacked / "compact-zstd.journal")
    assert out["columns"] == [c for c in want[0] if c != "Offset"]
    assert rows == _without(want, ("Offset",))


def test_the_lead_columns_read_the_way_journalctl_shows_an_entry(plugin, unpacked, fixtures):
    out, rows = _table(plugin, unpacked / "compact-zstd.journal")
    assert out["columns"][:6] == ["Timestamp (UTC)", "Hostname", "Identifier", "PID", "Priority", "Message"]
    assert out["column_types"][:6] == ["datetime", "text", "text", "number", "text", "text"]
    # MESSAGE, _HOSTNAME and PRIORITY live in the lead columns, not twice.
    assert not {"MESSAGE", "_HOSTNAME", "PRIORITY"} & set(out["columns"])
    by_msg = {r["Message"]: r for r in rows}

    first = rows[0]
    assert (first["Timestamp (UTC)"], first["Hostname"], first["Identifier"], first["Priority"]) == (
        "2025-11-17 09:00:00.000000", "web01", "kernel", "notice")
    # _SOURCE_REALTIME_TIMESTAMP, when there is one, is the time shown (as
    # journalctl does); the reception time stays in __REALTIME_TIMESTAMP.
    failed = rows[3]
    assert failed["Message"].startswith("Failed password for invalid user admin from 203.0.113.7")
    assert failed["Timestamp (UTC)"] == "2025-11-17 09:00:59.998500"
    assert failed["__REALTIME_TIMESTAMP"] == "1763370060000000"
    assert (failed["Identifier"], failed["PID"], failed["_SYSTEMD_UNIT"]) == ("sshd", "2210", "ssh.service")
    # No SYSLOG_IDENTIFIER: _COMM. No _PID: SYSLOG_PID.
    assert (by_msg["disk usage at 91%"]["Identifier"], by_msg["disk usage at 91%"]["PID"]) == ("backup.sh", "3500")
    cron = by_msg["(root) CMD (/usr/local/bin/backup.sh)"]
    assert (cron["Identifier"], cron["PID"], cron["_PID"]) == ("cron", "3601", "")
    # A priority that isn't one is kept as logged.
    assert by_msg["priority out of range"]["Priority"] == "9"
    assert by_msg["three tags"]["TAG"] == "first\nsecond\nthird"
    # Bytes that aren't text are kept, visibly; text stays text.
    assert "payload \\xff\\xfe not utf-8\\x00 after nul" in by_msg
    assert "\x1b[1;31mred\x1b[0m alert" in by_msg
    assert by_msg['traceback:\n  File "agent.py", line 9\nRuntimeError: lost the C2']["Priority"] == "crit"
    # Two boots, in the order they happened.
    boots = list(dict.fromkeys(r["_BOOT_ID"] for r in rows))
    assert boots == [fixtures["make"].BOOT_A, fixtures["make"].BOOT_B]
    assert [r["__SEQNUM"] for r in rows] == [str(n) for n in range(1, 21)]
    assert rows[-1]["__CURSOR"].startswith(f"s={rows[-1]['__SEQNUM_ID']};i=14;b={fixtures['make'].BOOT_B};")


@pytest.mark.parametrize("name", JOURNALS)
def test_compressed_fields_are_read_whatever_compressed_them(plugin, unpacked, fixtures, name):
    """The command line (one DATA object shared by three entries) and the
    long sudo line are over journald's 512-byte threshold, so they are the
    zstd, LZ4 or XZ objects in each file."""
    data = (unpacked / name).read_bytes()
    flags = {"compact-zstd.journal": 4, "regular-lz4.journal": 2, "regular-xz.journal": 1}[name]
    assert _data_object_flags(data).count(flags) == 2
    _, rows = _table(plugin, unpacked / name)
    java = [r for r in rows if r["Identifier"] == "java"]
    assert [r["_CMDLINE"] for r in java] == [fixtures["make"].CMDLINE] * 3
    sudo = next(r for r in rows if r["Identifier"] == "sudo")
    assert sudo["Message"].endswith("echo step15 >> /var/tmp/.cache/log'")


def test_zstd_fields_read_without_the_standard_library_decoder(plugin, unpacked, monkeypatch):
    """Python 3.10–3.13 have no zstd; the pure decoder reads the file the
    same, and on 3.14 this is the only test that makes it."""
    from importlib import import_module

    dec = import_module(plugin.__name__ + ".decompress")
    monkeypatch.setattr(dec, "_std_zstd", None)
    _, rows = _table(plugin, unpacked / "compact-zstd.journal")
    _, want = _table(plugin, unpacked / "journalctl-export.txt")
    assert _without(rows, ("Offset",)) == want


def _data_object_flags(data: bytes) -> list[int]:
    pos = struct.unpack_from("<Q", data, 88)[0]
    flags = []
    while pos + 16 <= len(data):
        otype, flag = data[pos], data[pos + 1]
        (size,) = struct.unpack_from("<Q", data, pos + 8)
        if not otype:
            break
        if otype == 1:
            flags.append(flag)
        pos += (size + 7) & ~7
    return flags


# ------------------------------------------------------------------- damage

def _entries(plugin, path: Path) -> list[int]:
    from importlib import import_module

    jf = import_module(plugin.__name__ + ".journalfile").JournalFile(str(path))
    try:
        return jf.scan()[0]
    finally:
        jf.close()


@pytest.fixture
def logged(monkeypatch):
    out = []
    monkeypatch.setattr(wlog, "record", lambda level, msg: out.append((level, msg)))
    return out


def test_a_copy_cut_short_reads_every_entry_before_the_cut(plugin, unpacked, logged):
    """journalctl refuses such a file outright."""
    src = unpacked / "compact-zstd.journal"
    data = src.read_bytes()
    _, full = _table(plugin, src)
    cut = _entries(plugin, src)[12] + 40   # partway into the 13th entry
    (unpacked / "cut.journal").write_bytes(data[:cut])
    out, rows = _table(plugin, unpacked / "cut.journal")
    # Columns are the fields the entries that are there carry (no TAG: the
    # entry that had one is past the cut).
    assert set(out["columns"]) < set(full[0]) and "TAG" not in out["columns"]
    assert rows == [{c: r[c] for c in out["columns"]} for r in full[:12]]
    ((level, msg),) = logged
    assert level == "warn" and "header counts 20 entries and 12 could be read" in msg
    assert "the copy is cut short there" in msg


def test_a_damaged_object_is_carved_past(plugin, unpacked, logged):
    """Overwrite the header of the object after entry 6 (a crash's torn
    write): the walk carves forward to the next object, and every entry
    after it is still read."""
    src = unpacked / "regular-lz4.journal"
    data = bytearray(src.read_bytes())
    _, full = _table(plugin, src)
    entry = _entries(plugin, src)[6]
    nxt = entry + ((struct.unpack_from("<Q", data, entry + 8)[0] + 7) & ~7)
    data[nxt:nxt + 16] = b"\xff" * 16
    (unpacked / "hole.journal").write_bytes(bytes(data))
    _, rows = _table(plugin, unpacked / "hole.journal")
    assert len(rows) == 20
    assert rows[:7] == full[:7] and rows[-1] == full[-1]
    ((level, msg),) = logged
    assert level == "warn" and "damaged bytes from offset" in msg and "after them were read" in msg


def test_an_entry_whose_items_are_damaged_keeps_the_rest(plugin, unpacked, logged):
    src = unpacked / "compact-zstd.journal"
    data = bytearray(src.read_bytes())
    entry = _entries(plugin, src)[3]
    data[entry + 64:entry + 72] = b"\x07" * 8          # two compact item offsets
    (unpacked / "items.journal").write_bytes(bytes(data))
    _, rows = _table(plugin, unpacked / "items.journal")
    _, full = _table(plugin, src)
    assert len(rows) == 20 and rows[3] != full[3]
    assert rows[3]["__SEQNUM"] == "4" and rows[3]["_BOOT_ID"] == full[3]["_BOOT_ID"]
    ((level, msg),) = logged
    assert level == "warn" and "2 field(s) pointed at damaged data" in msg


def test_a_file_journald_still_had_open_says_so(plugin, unpacked, logged):
    data = bytearray((unpacked / "compact-zstd.journal").read_bytes())
    data[16] = 1   # STATE_ONLINE
    (unpacked / "online.journal").write_bytes(bytes(data))
    _, rows = _table(plugin, unpacked / "online.journal")
    assert len(rows) == 20
    assert logged == [("info", "online.journal (journal file): it was still open for writing (online) "
                               "when it was collected; every entry its header counts was read.")]


def test_entries_written_after_the_header_was_last_updated_are_read(plugin, unpacked, logged):
    """A crash leaves the header's counts behind the objects; the walk
    doesn't use them."""
    data = bytearray((unpacked / "regular-xz.journal").read_bytes())
    data[16] = 1
    struct.pack_into("<Q", data, 152, 15)     # n_entries
    (unpacked / "stale.journal").write_bytes(bytes(data))
    _, rows = _table(plugin, unpacked / "stale.journal")
    assert len(rows) == 20
    ((level, msg),) = logged
    assert level == "info" and "5 entries written after its header was last updated were read too" in msg


def test_format_flags_from_a_newer_systemd_are_reported(plugin, unpacked, logged):
    data = bytearray((unpacked / "regular-lz4.journal").read_bytes())
    struct.pack_into("<I", data, 12, struct.unpack_from("<I", data, 12)[0] | 1 << 9)
    (unpacked / "future.journal").write_bytes(bytes(data))
    _, rows = _table(plugin, unpacked / "future.journal")
    assert len(rows) == 20
    assert "format flags this reader doesn't know (0x200)" in logged[0][1]


def test_a_huge_field_is_cut_and_says_so(plugin, tmp_path, logged, monkeypatch):
    from importlib import import_module

    common = import_module(plugin.__name__ + ".common")
    monkeypatch.setattr(common, "FIELD_MAX", 16)
    p = tmp_path / "big.export"
    p.write_bytes(b"__REALTIME_TIMESTAMP=1763370000000000\n_BOOT_ID=" + b"a" * 32
                  + b"\nMESSAGE=" + b"x" * 40 + b"\n\n")
    _, rows = _table(plugin, p)
    assert rows[0]["Message"] == "x" * 16 + "… [over 16 bytes; only the first 16 were imported]"
    assert "were cut to their first 1 MiB" in logged[0][1]


# -------------------------------------------------- export and JSON edge cases

def test_export_binary_fields_and_crlf(plugin, tmp_path):
    p = tmp_path / "x.export"
    body = (b"__REALTIME_TIMESTAMP=1763370000000000\r\n_BOOT_ID=" + b"b" * 32 + b"\r\n"
            b"MESSAGE\r\n")
    # A binary field's length and bytes are not lines: no CRLF inside them.
    p.write_bytes(b"__REALTIME_TIMESTAMP=1763370000000000\n_BOOT_ID=" + b"b" * 32 + b"\nMESSAGE\n"
                  + struct.pack("<Q", 5) + b"a\nb\x00c" + b"\nSYSLOG_IDENTIFIER=x\n\n")
    _, rows = _table(plugin, p)
    assert rows[0]["Message"] == "a\nb\\x00c" and rows[0]["Identifier"] == "x"
    p.write_bytes(body.replace(b"MESSAGE\r\n", b"MESSAGE=hi\r\nPRIORITY=4\r\n\r\n"))
    _, rows = _table(plugin, p)
    assert (rows[0]["Message"], rows[0]["Priority"]) == ("hi", "warning")


def test_json_byte_arrays_repeats_and_omitted_values(plugin, tmp_path, monkeypatch):
    p = tmp_path / "j.json"
    p.write_text('{"__REALTIME_TIMESTAMP":"1763370000000000","_BOOT_ID":"' + "c" * 32 + '",'
                 '"MESSAGE":[104,105,255],"TAG":["one",[116,119,111]],"BIG":null}\n'
                 'not json at all\n')
    from importlib import import_module

    exported = import_module(plugin.__name__ + ".exported")
    out, rows = _table(plugin, p)
    assert rows[0]["Message"] == "hi\\xff"
    assert rows[0]["TAG"] == "one\ntwo"
    assert rows[0]["BIG"] == exported.JSON_OMITTED


def test_what_isnt_journal_data_is_refused_with_a_reason(plugin, tmp_path):
    short = tmp_path / "journal.txt"
    short.write_text("Nov 17 09:00:00 web01 sshd[2210]: Accepted publickey for alice\n")
    with pytest.raises(ValueError, match="text output"):
        plugin.parse(str(short), {})
    empty = tmp_path / "empty.journal"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="is empty"):
        plugin.parse(str(empty), {})
    fake = tmp_path / "fake.journal"
    fake.write_bytes(b"LPKSHHRH" + b"\0" * 80)
    with pytest.raises(ValueError, match="too short"):
        plugin.parse(str(fake), {})


# --------------------------------------------------------------- in a case

@pytest.fixture
def journal_client(client, monkeypatch):
    import server

    reg = PluginRegistry()
    reg.load([EXAMPLES])
    monkeypatch.setattr(server, "PLUGINS", reg)
    return client


def test_importing_journals_into_a_case(journal_client, store, unpacked):
    """Through the plugin import route, as the import window does it: the
    table's types, the Timeline summary, and the overview board over two
    journal tables (a host's journal is a directory of files)."""
    recs = []
    for name in ("compact-zstd.journal", "journalctl-json.txt"):
        r = journal_client.post("/api/ingest/plugin/path", json={
            "path": str(unpacked / name), "format_id": FORMAT, "options": {}, "build_fts": False})
        assert r.status_code == 200, r.text
        recs.append(r.json())
    rec = recs[0]
    assert rec["row_count"] == 20 and rec["name"] == "compact-zstd.journal"
    types = {c["name"]: c["type"] for c in rec["columns"]}
    assert (types["Timestamp (UTC)"], types["PID"], types["_UID"], types["_CMDLINE"]) == (
        "datetime", "number", "number", "text")
    match = store._timeline_summary_match(store.get_source(rec["id"]))
    assert match is not None and match[0] == "systemd journal"
    # The board filters on Identifier in most of its queries (sshd, sudo,
    # kernel), so the header set has the import index it up front.
    assert store._shape_index_columns(store.get_source(rec["id"])) == ["Identifier"]
    indexed = [r[0] for r in store.db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ?",
        (rec["table_name"],)) if r[0]]
    assert any('"Identifier"' in sql for sql in indexed), indexed

    reg = PluginRegistry()
    reg.load([EXAMPLES])
    board = reg.get_dashboard("systemd_journal", "overview")
    got = {}
    for w in board["widgets"]:
        res = store.dashboard_widget_preview(w["source"], w.get("query") or {}, cells=w.get("cells"))
        got[w["title"]] = res["rows"]
    assert got["Journal entries"] == [[40]]
    assert dict(got["Signals"]) == {"SSH logins accepted": 4, "Failed SSH logins": 6,
                                    "sudo commands": 2, "Errors or worse": 4, "Kernel messages": 6}
    make = _load("journal_make_fixtures_case", DATA / "make_fixtures.py")
    assert got["Boots, oldest first"] == [[make.BOOT_A, 36], [make.BOOT_B, 4]]
    assert dict(got["Priority"]) == {"info": 26, "notice": 6, "warning": 2, "err": 2, "crit": 2, "9": 2}
    assert got["Activity over time"] == [["2025-11-17 09", 36], ["2025-11-17 10", 4]]


def test_every_drill_on_the_board_opens_what_its_number_counted(journal_client, store, unpacked):
    for name in ("regular-lz4.journal", "journalctl-export.txt"):
        r = journal_client.post("/api/ingest/plugin/path", json={
            "path": str(unpacked / name), "format_id": FORMAT, "options": {}, "build_fts": False})
        assert r.status_code == 200, r.text
    sids = [s["id"] for s in store.list_sources() if not s.get("is_merge")]

    def opened(drill, value=None):
        kids = [{"type": "cond", **c} for c in drill.get("where", [])]
        if drill.get("tree"):
            kids.append(drill["tree"])
        if value is not None and drill.get("column"):
            kids.append({"type": "cond", "column": drill["column"], "op": "equals", "value": value})
        tree = {"type": "group", "op": "AND", "children": kids}
        return sum(len(store.run_sql(store.spec_sql(sid, {"source_id": sid, "filter_tree": tree}),
                                     limit=1000)["rows"]) for sid in sids)

    reg = PluginRegistry()
    reg.load([EXAMPLES])
    board = reg.get_dashboard("systemd_journal", "overview")
    for w in board["widgets"]:
        if w["source"] == "cells":
            for cell in w["cells"]:
                (n,) = store.dashboard_widget_preview("sql", cell["query"])["rows"][0]
                assert opened(cell["drill"]) == n, cell["label"]
        elif w["render"] == "stat":
            (n,) = store.dashboard_widget_preview("sql", w["query"])["rows"][0]
            assert opened(w["drill"]) == n, w["title"]
        elif w["render"] in ("bar", "list"):
            for label, count in store.dashboard_widget_preview("sql", w["query"])["rows"]:
                assert opened(w["drill"], value=str(label)) == count, (w["title"], label)


# ---------------------------------------------------------------- decoders

@pytest.fixture(scope="module")
def zstd_pure():
    return _load("systemd_journal_zstd_under_test", PLUGIN_DIR / "zstd.py")


def test_the_pure_zstd_decoder_matches_the_reference_encoder(zstd_pure, fixtures):
    make = fixtures["make"]
    inputs = make.zstd_inputs()
    for name, (src, _args) in make.ZSTD_VECTORS.items():
        frame = (DATA / f"zstd-{name}.zst").read_bytes()
        assert zstd_pure.decompress(frame) == inputs[src], name
    # Two frames back to back are one stream, as the format says.
    two = (DATA / "zstd-short.zst").read_bytes() + (DATA / "zstd-run.zst").read_bytes()
    assert zstd_pure.decompress(two) == inputs["short"] + inputs["run"]


@pytest.mark.skipif(sys.version_info < (3, 14), reason="compression.zstd is Python 3.14+")
def test_the_pure_decoder_agrees_with_the_standard_library(zstd_pure, fixtures):
    from compression import zstd

    for name in fixtures["make"].ZSTD_VECTORS:
        frame = (DATA / f"zstd-{name}.zst").read_bytes()
        assert zstd_pure.decompress(frame) == zstd.decompress(frame), name


def test_damaged_zstd_is_an_error_never_a_crash(zstd_pure, fixtures):
    """Flip bytes all over real frames: every result is the right bytes or
    a ZstdError (a ValueError), never IndexError or a hang."""
    rnd = random.Random(7)
    inputs = fixtures["make"].zstd_inputs()
    for name in ("short", "quad", "alt", "skewed"):
        frame = (DATA / f"zstd-{name}.zst").read_bytes()
        src = inputs[fixtures["make"].ZSTD_VECTORS[name][0]]
        for _ in range(60):
            bad = bytearray(frame)
            for _ in range(rnd.randint(1, 3)):
                bad[rnd.randrange(4, len(bad))] ^= 1 << rnd.randrange(8)
            try:
                zstd_pure.decompress(bytes(bad))
            except zstd_pure.ZstdError:
                pass
        for cut in (5, 9, len(frame) // 2):
            try:
                assert zstd_pure.decompress(frame[:cut]) != src
            except zstd_pure.ZstdError:
                pass


def test_lz4_blocks_as_systemd_stores_them(plugin):
    from importlib import import_module

    dec = import_module(plugin.__name__ + ".decompress")
    # "ab", then a match 2 back for 6 (overlapping): "abababab"; then the
    # final literals-only sequence "!".
    block = bytes([0x22]) + b"ab" + struct.pack("<H", 2) + bytes([0x10]) + b"!"
    payload = struct.pack("<Q", 9) + block
    assert dec.lz4(payload) == b"abababab!"
    with pytest.raises(dec.DecompressError, match="not the 12"):
        dec.lz4(struct.pack("<Q", 12) + block)
    with pytest.raises(dec.DecompressError):
        dec.lz4(struct.pack("<Q", 9) + bytes([0x22]) + b"ab" + struct.pack("<H", 9))
