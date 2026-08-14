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

A ready-made example — a raw NTFS `$MFT` / USN-journal (`$J`) parser —
ships in [`examples/plugins/mft_usn/`](../examples/plugins/mft_usn/):
install it from Settings → Plugins → "Install a plugin folder…", or

```bash
cp -r examples/plugins/mft_usn plugins/
```

The authoring contract (metadata, `register_ingest_format`, the `parse`
streaming contract, per-format options) is documented at the top of
[`plugin_api.py`](../plugin_api.py). A plugin that fails to load never
takes the server down — it's listed with its error in Settings → Plugins
and in the startup output.

Extra directories can be added with `--plugins-dir DIR` (repeatable) or
the `WINNOW_PLUGINS_DIR` environment variable; installs from the UI
always land in this folder.

**Security:** a plugin is arbitrary Python running with the same
privileges as Winnow itself — the same trust model as a Notepad++ plugin.
Winnow never downloads plugins; installing one (from the UI or by hand)
is the consent step. Only install plugins you have read or trust.
