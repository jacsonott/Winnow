# plugins/

Drop a plugin here and restart the server — that's the whole install.
A plugin is either a single `something.py` file or a folder with an
`__init__.py`. Delete it to uninstall. Nothing in this folder is ever
committed except this README.

Winnow imports each plugin at startup and calls its `register(api)`
function; the full authoring contract (metadata, `register_ingest_format`,
the `parse` streaming contract, per-format options) is documented at the
top of [`plugin_api.py`](../plugin_api.py). A ready-made example — a raw
NTFS `$MFT` / USN-journal (`$J`) parser — lives in
[`examples/plugins/mft_usn/`](../examples/plugins/mft_usn/); copy that
folder in here to enable it:

```bash
cp -r examples/plugins/mft_usn plugins/
python server.py
```

A plugin that fails to load never takes the server down — it's listed with
its error in **Plugins…** (under the ≡ menu) and in the startup output.

Extra directories can be added with `--plugins-dir DIR` (repeatable) or
the `WINNOW_PLUGINS_DIR` environment variable.

**Security:** a plugin is arbitrary Python running with the same
privileges as Winnow itself — the same trust model as a Notepad++ plugin.
Winnow never downloads plugins; putting a file in this folder is the
consent step. Only install plugins you have read or trust.
