# Installation

Winnow is a Python program you run on your own machine: one server process,
a browser window pointed at it, and no network needed once it is installed.
This page covers getting it, what it needs, starting it, every command-line
option, where it keeps its files, and how to update, roll back and remove it.

## Requirements

- **Python 3.10 or newer.** Python 3.9 and older stop at startup.
- **Four packages and their dependencies**, from `requirements.txt`:
  FastAPI, uvicorn (with its `[standard]` extras), python-multipart and
  openpyxl. Install from the file rather than by hand: openpyxl is imported
  at startup, and without it the server stops before it binds.
- **A browser.** A Chromium-family browser (Edge, Chrome, Brave, Chromium)
  gets you an app window; any other browser works as an ordinary tab.
- **Windows, macOS or Linux.** All three have double-click launchers. File
  associations are Windows and Linux only.

Nothing is fetched at runtime: no CDN, no web fonts, no update check unless
you press the button. That is what makes it usable on an airgapped analysis
box.

## Getting Winnow

### From a release

Releases are published on GitHub at
<https://github.com/jacsonott/Winnow/releases>, tagged `v0.1.0` through
`v0.3.0` so far. Download **Source code (zip)** from the latest one and unzip
it wherever you want Winnow to live. That folder is the install: Winnow keeps
its settings, plugins and (by default) your cases inside it, which is why
updating is done in place rather than by replacing the folder (see
[Updating](#updating)).

### From git

```bash
git clone https://github.com/jacsonott/Winnow.git
```

The default branch, `main`, holds released versions only. Development
happens on `develop`; check it out if you want unreleased code.
Update a clone with `git pull` like any other repository. A clone reports
the version written in `winnow/version.py`, and on `develop` that is still
the last release's number, so give the commit as well when you state which
build you used.

## Installing the Python packages

A virtual environment keeps Winnow's packages away from the system Python,
and on recent Linux distributions pip refuses to install into the system
Python at all ("externally managed environment"). From the Winnow folder:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Name it `.venv` or `venv` and keep it inside the Winnow folder: updates never
touch either name.

The double-click launchers do not look for a virtual environment. They run
the first `python3` or `python` on your PATH (on Windows, `python` and then
`py -3`). If the packages live only in a venv, start Winnow with that
environment's interpreter instead: `.venv/bin/python server.py`, or
`.venv\Scripts\python server.py` on Windows.

### On an airgapped machine

Winnow itself needs nothing from the network; only the packages have to be
carried in. One way, using pip's own tools, is to collect the wheels on a
connected machine with the same operating system, CPU architecture and
Python version as the target (some of the dependencies are compiled, so the
wheels are platform-specific):

```bash
python -m pip download -r requirements.txt -d wheels
```

Carry the `wheels` folder over with the Winnow zip, then on the target:

```bash
python -m pip install --no-index --find-links wheels -r requirements.txt
```

## First launch

From the Winnow folder:

```bash
python server.py        # python3 on most Linux and macOS installs
```

The terminal prints `Winnow on http://127.0.0.1:8777  (home screen — no case
open)` and a window opens on the home screen. Winnow plays a short launch
animation (any key or click skips it; it is skipped when your system asks for
reduced motion) and asks two questions, once each:

- whether you are working over RDP or another remote desktop. **Enable
  remote mode** switches the grid to whole-row scrolling and stops
  animations; you can change it later in **Settings → Appearance**.
- where case files should live. **Use the default** puts them in a `cases`
  folder; **Choose a folder…** lets you pick one.

Start Winnow from its own folder, as the launchers do. The default `cases`
folder chosen at first run is resolved against the folder you started in,
and so is a case created by `--open` without `--case`.

From the home screen you create or open a case; see
[Cases and importing](Cases-and-importing.md).

### App window or browser tab

Winnow looks for a Chromium-family browser and opens itself in that browser's
app mode: a window with no address bar or tabs. It waits
for the server to answer (up to 15 seconds) before opening anything. The
first browser found wins:

| OS | Looked for, in order |
| --- | --- |
| Windows | Microsoft Edge, Google Chrome, Brave, Chromium (in Program Files, Program Files (x86), and for Chrome and Chromium also your local AppData) |
| macOS | Google Chrome, Microsoft Edge, Brave Browser, Chromium (in `/Applications`, then `~/Applications`) |
| Linux | the first of `google-chrome`, `google-chrome-stable`, `chromium`, `chromium-browser`, `microsoft-edge`, `microsoft-edge-stable`, `brave-browser` on your PATH |

If none is found, or the browser fails to start, Winnow opens an ordinary tab
in your default browser instead. Firefox has no app mode, so it is only ever
used that way.

The app window runs in your normal browser profile, so your extensions run in
it too, and Winnow's per-browser preferences (keyboard bindings, among
others) are stored in that profile. `--browser-tab` opens an ordinary tab
instead, and `--no-browser` opens nothing, leaving you to browse to the
printed address. `--browser-profile DIR` gives the app window a profile of
its own in `DIR`: your extensions stay out and Winnow gets a separate taskbar
entry, but keyboard bindings and the other per-browser preferences start from
their defaults there. It has no effect with `--browser-tab`.

Closing the window is enough to stop Winnow: the server exits on its own
shortly afterwards (see [Idle shutdown](#idle-shutdown)).

## Double-click launchers

`launch/` holds launchers for people who would rather not open a terminal.
Each finds Python and starts `server.py` from the folder above it, so they
need Python and the packages installed exactly as above. Full details are in
[launch/README.md](../../launch/README.md).

| OS | File | Notes |
| --- | --- | --- |
| Linux | `winnow.sh` | Mark it executable, or allow launching, if your file manager asks. |
| macOS | `winnow.command` | Finder runs it in Terminal. The first run may need right-click → Open (Gatekeeper). |
| Windows | `winnow.bat` | Opens a small console window that stays while Winnow runs. |
| Windows | `winnow.vbs` | The same, with no console window. |

Leave the launchers where they are: each one works out the install folder
from its own location, so a copy on the Desktop finds nothing. Make a
shortcut, alias or desktop entry that points back at the original instead.
The shell and batch launchers pass any arguments you give them on to
`server.py`.

On Linux, if you make a desktop entry as `launch/README.md` describes and
also use [file associations](Settings-and-shortcuts.md#file-associations),
give your entry a name other than `winnow.desktop`. Winnow writes its own
`~/.local/share/applications/winnow.desktop` when you register a file type,
and deletes it when you unregister the last one.

## Command-line options

`python server.py --help` lists these too.

| Option | Default | What it does |
| --- | --- | --- |
| `--case FILE` | none (home screen) | Open this case file, creating it if it does not exist. The case is added to the home screen's list. |
| `--open FILE …` | | Import these delimited text files (CSV, TSV and the like) into the case at startup. Without `--case`, the case is `case.db-winnow` in the current folder. |
| `--no-fts` | off | Skip the full-text index for the `--open` imports. Import is faster; search falls back to scanning. |
| `--port PORT` | `8777` | Port to listen on. |
| `--host HOST` | `127.0.0.1` | Address to bind. See [Reaching Winnow from another machine](#reaching-winnow-from-another-machine). |
| `--allow-host NAME` | | Accept this name in the Host header as well. Repeatable. |
| `--force` | off | Open `--case` even if another Winnow has it open. |
| `--no-idle-shutdown` | off | Keep running with no window connected, for driving Winnow from scripts. |
| `--no-browser` | off | Open no window. |
| `--browser-tab` | off | Open an ordinary browser tab instead of an app window. |
| `--browser-profile DIR` | | Run the app window in its own browser profile. |
| `--plugins-dir DIR` | | Load plugins from this folder as well. Repeatable. `plugins/` and `$WINNOW_PLUGINS_DIR` are always scanned. See [Plugins](Plugins.md). |
| `--assoc FILE …` | | What file associations run on a double-click. You do not normally type it. |

To jump straight into a case with data in it:

```bash
python server.py --case acme.db-winnow --open EvtxECmd_Output.csv
```

Each `--open` file prints its row count and import speed as it goes, plus a
note if rows had the wrong number of columns or the file was not UTF-8. For
workbooks, SQLite databases, JSON and the other formats, import from the UI.

`--case` refuses a file that exists but is not a Winnow case, rather than
writing Winnow's tables into it. To work with a SQLite database as evidence,
open a case and import the database.

### One case, one Winnow

A case file should be open in one Winnow at a time. Each open case leaves a
`<case>.winnow-lock` marker beside it, and a second Winnow that tries to open
the same case refuses on the command line, naming who has it:

```
ERROR: /cases/acme.db-winnow is already open in another Winnow.
  analyst@lab-01 (pid 4650), open since 2026-10-13T05:36:46, last seen 21s ago
  ...
  Pass --force to open it anyway.
```

From the home screen you get the same information and a choice of
**Open anyway** or **Don’t open**. If the other Winnow was killed rather than
shut down, its marker can still read as live for up to two and a half
minutes; after that the case opens normally. Keep case files off network
shares: SQLite's locking does not work over SMB or NFS. Two analysts on one
investigation work in separate case files and exchange session files (see
[Sessions and export](Sessions-and-export.md)).

Several Winnows can run at once, each on its own port and its own case. If
the port is already taken, the second one stops at startup with an "address
already in use" error; pick another with `--port`.

### Reaching Winnow from another machine

By default Winnow listens on `127.0.0.1` only. `--host 0.0.0.0` (or a
specific address) makes it reachable from the network, and it prints a
warning when you do: Winnow has no login and no access control, so anyone who
can reach the address can open, import into or query any case the server can
reach. Only do this on a trusted, isolated network.

Winnow also checks the Host header of every request. It answers to
`localhost`, `127.0.0.1`, `::1`, any IP address, the `--host` value, and any
name given with `--allow-host`. Anything else, including the machine's own
hostname, gets `421 Unrecognised Host header`. So a colleague can browse to
`http://10.0.0.5:8777`, but to use `http://lab-01:8777` start Winnow with
`--allow-host lab-01`. The same applies to a reverse proxy's name.

Some things only work from the machine Winnow runs on: file associations,
the **Environment** settings, creating folders, the import dialog's
**Add from this machine…** picker, quick-look cases and installing plugins.

If you drive Winnow's API from a script, every request that changes
something (anything but GET) must carry the header
`X-Timeline-Lite-Client: 1`, or it is refused with 403. That rule is what
stops a web page in another tab from acting on your case.

### Idle shutdown

A Winnow with no windows left shuts itself down, so a forgotten server does
not hold a case open all weekend. Every Winnow window keeps a connection open
to the server, and the server watches the count:

| Situation | Winnow exits after |
| --- | --- |
| The last window was closed | about 2 minutes |
| Windows stopped answering without closing (laptop asleep, browser suspended the tab, VM paused) | 30 minutes |
| No window ever connected (for example `--no-browser` and nobody browsed to it) | 15 minutes |

It never exits while a window is connected, while a request is still running
(a long export keeps downloading after its tab closes), or while background
work is going on: an import, a **Save as table** copy, a watchlist scan, or a
search still building. A reload is covered by the grace period. When it
does exit it prints the reason in the terminal, and any window still open
shows "Lost the connection to Winnow’s server" with a **Try again** button.

`--no-idle-shutdown` turns it off.

## Stopping Winnow

Any of these stops the server cleanly. Everything already committed (tags,
notes, finished imports) is in the case file.

- **⏻ Shut down** on the home screen.
- **Settings → Shut down → ⏻ Shut down Winnow**.
- **Case ▾ → Shut down Winnow…** inside a case.
- `Ctrl+C` in the terminal it was started from.
- Closing every window, then waiting (see above).

The buttons ask first, and warn if an import or other work is still running.

## Where things live on disk

Inside the Winnow folder:

| Path | What it holds |
| --- | --- |
| `workspace/` | Cross-case state as readable JSON: the home screen's case list, saved filters, the default tag template, column layouts and header nicknames, timeline templates, import profiles, which plugins are on, profiles, the dashboard library, app settings, the cases folder setting, plugin data, and the list of running Winnows. Deleting it loses your case list and these preferences, never analysis. |
| `cases/` | The default home for case files, unless you chose another folder at first run. |
| `cases/quicklook/` | Temporary cases created when you double-click a file through a file association. One you never saved and never did any work in is deleted after seven days. |
| `plugins/` | Plugins you installed. The bundled examples live in `examples/plugins/`. |
| `sessions/` | A folder older versions created beside a case file for saved sessions. Current versions keep sessions inside the case file; updates leave any `sessions/` folder alone. |
| `.winnow-backup/`, `.winnow-install.json` | The updater's backups of previous versions and its record of what it installed. |

A case is one file, `name.db-winnow`, and it holds everything you did in it:
tags, notes, saved sessions, layouts, derived columns, dashboards. While it
is open you will also see `name.db-winnow-wal`, `name.db-winnow-shm` and
`name.db-winnow.winnow-lock` beside it. Older cases ending in `.db` still
open. The evidence files you import are read, never modified.

Outside the Winnow folder:

| What | Where |
| --- | --- |
| Keyboard bindings, striped rows, the autofit limit, the filter-row choice, panel sizes | The browser's storage for Winnow's address, per browser profile. |
| Skin, theme, accent, row density, launch animation, **Pages as a dropdown**, **Open new tables when an import finishes** | The browser's storage, plus a machine copy in `workspace/` that a new browser, profile or port starts from. |
| Environment variables set in **Settings → Environment** | Windows: your user environment (`HKCU\Environment`). Elsewhere: `~/.config/winnow/env`. |
| File associations | Windows: `HKCU\Software\Classes`. Linux: `~/.local/share/applications/winnow.desktop`, `~/.config/mimeapps.list`, and for plugin and case types `~/.local/share/mime/packages/winnow.xml` and icons under `~/.local/share/icons/hicolor/`. |
| Scratch file for the open case's views | `/dev/shm` on Linux, otherwise the system temp folder, named `winnow-views-*`. Deleted on exit; leftovers from a crash are cleaned up at the next start. |

## Updating

An update replaces the files Winnow ships and nothing else, after backing up
the ones it replaces. Winnow never checks for updates on its own; every
check is something you clicked or typed.

### From Settings

**Settings → Updates** shows the installed version. **Check for updates**
asks GitHub for the latest release. If there is a newer one it shows the
release notes and an **Install update** button, which asks for confirmation,
downloads and installs it. Restart Winnow afterwards: the running server
keeps running the old code until it restarts. Settings has no rollback
button; use `update.py --rollback` for that.

### From the command line

`update.py` does the same from a terminal and needs no running server, so it
still works when a bad update has broken the UI. Shut Winnow down first.

```bash
python update.py --check       # what's available; changes nothing
python update.py               # check, show the plan, ask, apply
python update.py --dry-run     # show the plan and stop
python update.py --rollback    # undo the last update
```

| Option | What it does |
| --- | --- |
| `--check` | Report the installed and latest versions, and the release notes if newer. |
| `--dry-run` | Show the plan, then stop: how many files would be changed, added and removed (the first few named), and which folders are preserved. |
| `--from ZIP` | Apply a release archive already on this machine instead of downloading one. |
| `--download-only` | Download the latest release archive and stop, to carry to another machine. |
| `--dest DIR` | Where downloads go. Default: the current folder. |
| `--dev` | Sync to the tip of the `develop` branch instead of the latest release. |
| `--branch NAME` | With `--dev`, follow this branch instead of `develop`. |
| `--rollback` | Restore the version from before the last update. |
| `-y`, `--yes` | Don't ask for confirmation. |

`--from` cannot be combined with `--check`, `--download-only` or `--dev`. A
downloaded archive is saved as `winnow-<version>.zip` in the current folder
(or `--dest`) and left there.

The first update of an install made from a zip cannot tell which old files
the new version dropped, so it removes none; later updates can. After an
update, run `pip install -r requirements.txt` again before restarting, in
case the new version needs a package the old one did not.

### Updating an airgapped machine

On a machine with network:

```bash
python update.py --download-only --dest /media/usb
```

Then on the analysis box:

```bash
python update.py --from /media/usb/winnow-<version>.zip
```

A **Source code (zip)** downloaded from the GitHub release page works with
`--from` as well. The archive is checked for damage before anything is
written, and an archive that does not contain Winnow, or contains a path
that would write outside the install, is refused.

### What an update keeps

These are never read, written or deleted by an update:

- `workspace/`, `plugins/` (apart from its README), `sessions/` and `cases/`;
- `.venv/` and `venv/`;
- any case or evidence file by its extension (`.db-winnow`, `.db`, `.csv`,
  `.tsv` and their SQLite companions), wherever it sits in the folder;
- the updater's own backups.

Case files stored outside the Winnow folder are not involved at all.

### Rolling back

```bash
python update.py --rollback
```

This restores the files from the most recent backup in `.winnow-backup/`
and removes the files that update added. The three most recent backups are
kept. Only program files are rolled back; case files never are. Restart
Winnow afterwards.

### Development builds

`python update.py --dev` syncs the install to the tip of `develop`, with the
same backup, protected folders and rollback as a release update. It is
unreleased code, meant for beta testers. **Settings → Updates** then shows
the build as `Winnow 0.3.0 — develop build <commit>`, because the version
number on `develop` is still the last release's. `--dev --check` shows the
commit it would sync to, and `--dev --download-only` saves it as
`winnow-develop-<commit>.zip`. To go back to a release, use `--rollback`, or
apply the release zip with `--from`.

## Uninstalling

Winnow installs nothing outside its folder except the items in the second
table under [Where things live on disk](#where-things-live-on-disk). To
remove it:

1. If you registered file associations, open **Settings → File
   associations** and untick every type first. That removes the registry
   entries (Windows) or the desktop entry, `mimeapps.list` lines, MIME
   package and icons (Linux). Deleting the folder first leaves the Open With
   entries pointing at a program that is no longer there. If you made Winnow
   a default through Windows' own **Open with → Choose another app → Always**
   dialog, Windows keeps that choice; pick another app for those types the
   same way.
2. If you stored variables in **Settings → Environment**, remove them there,
   or delete `~/.config/winnow/env` (on Windows they are ordinary user
   environment variables).
3. Shut Winnow down.
4. Move any case files you want to keep out of `cases/`, then delete the
   Winnow folder. Case files you kept in another folder are not affected;
   delete them separately if you mean to.
5. Optionally, remove a Linux desktop entry or Windows shortcut you made for
   a launcher, a `--browser-profile` folder if you used one, and the
   browser's stored data for Winnow's address (`http://127.0.0.1:8777`).
