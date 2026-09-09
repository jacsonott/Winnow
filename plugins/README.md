# plugins/

Winnow's installed plugins live here — one `something.py` file or one
folder with an `__init__.py` per plugin. Nothing in this folder is ever
committed except this README.

Manage plugins from **Settings → Plugins** (the `?` panel in the app):
every plugin in this folder is listed there with a checkbox to toggle it
on or off (a disabled plugin's code is never imported), and the two
install buttons copy a `.py` file or a plugin folder picked from anywhere
on disk into here. Toggles and installs take effect immediately — no
server restart. Copying a plugin into this folder by hand still works
exactly the same; the panel picks it up on next open.

Seven ready-made examples ship in [`examples/plugins/`](../examples/plugins/):
`mft_usn` (raw NTFS `$MFT`/`$J` parsing — an ingest-format plugin),
`lateral_movement` (a pinned graph tab — a custom-UI plugin),
`table_histogram` (a toolbar panel you drag on to set the timeframe),
`first_last`, `pivot`, `esxi_logs`, and `claude_assistant` (a Claude chat
tab — an external-integration plugin; needs network + API key).

They are already loaded: `examples/plugins/` is a bundled plugin
directory, so all seven are listed in Settings → Plugins with no install
step, switched **off** by default. Turn one on there rather than copying
it here — a copy in this folder shadows the bundled one and you end up
maintaining two.

Writing one? Start with
**[docs/writing-plugins.md](../docs/writing-plugins.md)** — quickstart,
the five extension points (ingest formats, tabs, API routes, row actions,
toolbar panels), testing, and troubleshooting. The contract is also
spelled out at the top of
[`winnow/plugin_api.py`](../winnow/plugin_api.py). A plugin that fails to load never
takes the server down — it's listed with its error in Settings → Plugins
and in the startup output.

Extra directories can be added with `--plugins-dir DIR` (repeatable) or
the `WINNOW_PLUGINS_DIR` environment variable; installs from the UI
always land in this folder.

**Security:** a plugin is arbitrary Python running with the same
privileges as Winnow itself — the same trust model as a Notepad++ plugin.
Winnow never downloads plugins; installing one (from the UI or by hand)
is the consent step. Only install plugins you have read or trust.
