# plugins/

Winnow's installed plugins live here — one `something.py` file or one
folder with an `__init__.py` per plugin. Nothing in this folder is ever
committed except this README.

Manage plugins from **Settings → Plugins → Manage plugins…** (Settings is
the `?` panel in the app). The manager lists every plugin in this folder
on the left, and the whole of one on the right: what it is, what it adds,
which folder it came from, which plugin API version it wants against what
this build provides, and which profiles turn it on. **A plugin that is
switched off still describes itself** — its name, version and description
are read from its source without importing it, so you can decide before
you run anything.

Scope is two controls. **Everywhere** is the machine-wide default; **In
&lt;case&gt;** — offered only while a case is on screen — is an override
stored in the case file, so it travels with the case, and can Follow the
machine setting or override it either way. A disabled plugin's code is
never imported. **Install a plugin…** explains which of the two shapes you
have — a single `.py` file, or a folder with an `__init__.py` — and offers
a picker for each, copying what you pick from anywhere on disk into here.
Scope changes and installs take effect immediately — no server restart.
Copying a plugin into this folder by hand still works exactly the same;
the manager picks it up on next open.

Seven ready-made examples ship in [`examples/plugins/`](../examples/plugins/):
`mft_usn` (raw NTFS `$MFT`/`$J` parsing — an ingest-format plugin),
`lateral_movement` (a pinned graph tab — a custom-UI plugin),
`top_values` (a toolbar panel that follows the grid — the most common
values of a column),
`first_last`, `pivot`, `esxi_logs`, and `claude_assistant` (a Claude chat
tab — an external-integration plugin; needs network + API key).

They are already loaded: `examples/plugins/` is a bundled plugin
directory, so all seven are listed in the manager with no install
step, switched **off** by default. Turn one on there rather than copying
it here — a copy in this folder shadows the bundled one and you end up
maintaining two.

Writing one? Start with
**[docs/writing-plugins.md](../docs/writing-plugins.md)** — quickstart,
the seven extension points (ingest formats, tabs, API routes, row actions,
toolbar panels, page panels, dashboards), testing, and troubleshooting. The contract is also
spelled out at the top of
[`winnow/plugin_api.py`](../winnow/plugin_api.py). A plugin that fails to load never
takes the server down — it's listed with its error in the manager
and in the startup output.

Extra directories can be added with `--plugins-dir DIR` (repeatable) or
the `WINNOW_PLUGINS_DIR` environment variable; installs from the UI
always land in this folder.

**Security:** a plugin is arbitrary Python running with the same
privileges as Winnow itself — the same trust model as a Notepad++ plugin.
Winnow never downloads plugins; installing one (from the UI or by hand)
is the consent step. Only install plugins you have read or trust.
