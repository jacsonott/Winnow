# mft-usn — raw NTFS $MFT / USN journal ingest for Winnow

Ingests the two core NTFS artifacts directly from their raw form — no
MFTECmd/EZTools (and no .NET runtime) in the loop:

- **NTFS $MFT (raw)** — one row per FILE record: full path reconstructed
  from parent references (sequence-checked, so a deleted file whose parent
  entry was reused shows `<orphan>` instead of a made-up location),
  `$STANDARD_INFORMATION` and `$FILE_NAME` timestamps side by side with an
  `SI<FN Created` timestomp hint, in-use and deleted records alike (an
  option restricts to either). Matches `$MFT`, `*.mft`.
- **NTFS USN journal ($J, raw)** — USN_RECORD v2/v3 entries: timestamp,
  filename, decoded reason flags (`FILE_CREATE|CLOSE`), and MFT
  entry/sequence numbers. The sparse leading zero run is skipped at C
  speed; torn records are carved past, not fatal. Matches `$J`,
  `*$UsnJrnl*`, `*.usn`.

Because both formats emit MFT entry + sequence numbers, the two tables
join in Winnow's SQL pane:

```sql
-- what did the journal see happen to files that are now deleted?
SELECT u.Timestamp, u.Reason, m.FullPath
FROM src_2 u JOIN src_1 m
  ON m.EntryNumber = u.EntryNumber AND m.SequenceNumber = u.SequenceNumber
WHERE m.InUse = 'false' ORDER BY u.Timestamp;
```

## Install

**Settings → Plugins → "Install a plugin folder…"** and pick this folder
(takes effect immediately, no restart), or copy it in by hand:

```bash
cp -r examples/plugins/mft_usn plugins/
```

Then import a raw `$MFT`/`$J` by drag-and-drop, through **Import files…**,
a folder import, or the per-format picker in Settings → Plugins.

## As a reference for writing your own plugin

This folder demonstrates every part of the authoring contract documented
in [`plugin_api.py`](../../../plugin_api.py): a package-style plugin with
`PLUGIN` metadata and relative imports, two `register_ingest_format`
calls, extension *and* bare-filename matching (a `$MFT` has no extension),
a per-format `choice` option, streaming row generators, and explicit
`column_types` so timestamp columns don't depend on sample inference.
