# systemd-journal — Linux journal ingest for Winnow

Reads a Linux host's systemd journal with no `journalctl` in the loop (and
no Linux: it runs wherever Winnow does, airgapped included). One import
format, **systemd journal**, recognizes three kinds of file by what is in
them:

- **Journal files**: `system.journal`, `user-1000.journal`, the archived
  `system@….journal`, and the `system@….journal~` journald leaves when it
  finds a file it can't trust. Both layouts (regular, and the compact one
  systemd 252 and later writes) and every compression a field can carry
  (XZ, LZ4, zstd). Damaged files read too: entries are found by walking
  the file's objects rather than its indexes, so a copy cut short or a
  crash's `.journal~` gives every entry still on disk, and the case log
  (**Case ▾ → Log…**) says what was skipped.
- **`journalctl -o export`**: the lossless stream `systemd-journal-remote`
  reads.
- **`journalctl -o json`**, and `json-pretty`, `json-seq`, `json-sse`.

All three land in the same columns: `Timestamp (UTC)`, `Hostname`,
`Identifier`, `PID`, `Priority` and `Message` as journalctl prints an
entry, then a column per field the entries carry (`_BOOT_ID`,
`_SYSTEMD_UNIT`, `_CMDLINE`, …), then the entry's own metadata
(`__REALTIME_TIMESTAMP`, `__CURSOR`, …) and, for a journal file, the
entry's byte `Offset`. So the journal files collected from a host and an
export a responder ran on it line up column for column.

journalctl's text output (`short`, `short-iso`, `verbose` …) is not read:
it has already dropped fields, and the default one has no year.

## Collecting

Best is the files themselves, from `/var/log/journal/<machine-id>/` (or
`/run/log/journal/` on a host that keeps the journal in memory only).
Copy the whole folder: a host's journal is many files, one per rotation
and one per user.

When only a live shell is available:

```sh
journalctl -o export > "$(hostname)-journal.export"
# or, for one boot or a time window:
journalctl -o export -b -1 > previous-boot.export
journalctl -o export --since '2025-11-17 00:00' --until '2025-11-18 00:00' > day.export
```

`-o json` works too, but leaves out values over 4096 bytes unless run
with `--all`; `-o export` always keeps them, and keeps bytes that aren't
text exactly.

## Install

It ships with Winnow, switched off: turn it on in **Settings → Plugins →
Manage plugins…** (no restart). To run a modified copy instead, copy this
folder into `plugins/` (or use **Install a plugin… → Pick a plugin
folder…** there); a copy in `plugins/` replaces the bundled one.

Then drag the journal files or their folder onto Winnow, or use a folder
or archive import: `.journal`, `.journal~` and `.export` files come to
this plugin. Winnow's own imports claim `.json` and `.txt`, so for
journalctl output saved under either name, use the **Import files…**
button beside **systemd journal** in the plugins manager ("What it
adds"); the format reads any file by its content.

It also offers a board, **Linux journal overview**, under **Dashboards ▸
Library**: entries, SSH logins accepted and failed, sudo commands, errors
or worse, kernel messages, boots, priorities, top identifiers and
activity over time, across every journal table in the case.

```sql
-- every command sudo ran, by whom, in time order
SELECT "Timestamp (UTC)", Hostname, Message
FROM src_1
WHERE Identifier = 'sudo' AND Message LIKE '%COMMAND=%'
ORDER BY "Timestamp (UTC)";
```

## As a reference for writing your own plugin

A package plugin with one `register_ingest_format` call that reads a
binary format by its content rather than its name (`parse` sniffs the
first bytes and routes to `journalfile.py` or `exported.py`), two passes
so the columns are known before the first row, explicit `column_types`,
problems reported to the case log with `winnow.log` instead of refusing
the file, and a `register_dashboard` board written against a header set
(`winnow/defaults/headers.json`) with `{{all:header_set:…}}`, so it spans
every journal table in a case. Everything is standard library: zstd is
decoded by `compression.zstd` on Python 3.14 and by `zstd.py`, a
pure-Python decoder, before that.
