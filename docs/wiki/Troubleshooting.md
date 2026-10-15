# Troubleshooting

This page collects the problems people actually run into with Winnow, what
each one looks like, and what to do about it. Most of them come down to
Winnow being a local server and a browser window that talk to each other:
when something goes wrong, the message usually says which side it came
from. If yours isn't here, [Reporting a bug](#reporting-a-bug) at the end
says what to send.

## Starting Winnow

### The server stops with ModuleNotFoundError

A traceback ending in `ModuleNotFoundError: No module named 'openpyxl'`, or
naming another of Winnow's packages, means a runtime package is missing.
Winnow loads all of them at startup, Excel support included, so it stops
before it starts listening. Install them with the same Python you run
Winnow with:

```bash
python -m pip install -r requirements.txt
```

See [Installation](Installation.md) for the requirements.

### "error while attempting to bind on address"

The port is already in use, almost always by another Winnow you started
earlier and forgot about. Winnow prints its usual `Winnow on
http://127.0.0.1:8777` line, then this error, and exits. It does not move
to another port by itself.

Either use the Winnow that is already running (open
<http://127.0.0.1:8777>), shut that one down, or start this one on another
port with `python server.py --port 8778`. Opening a file through a file
association is the exception: it either hands the file to a Winnow that is
already running or starts on a free port.

### "is not a Winnow case file"

You pointed `--case`, or **Open existing case file…**, at a SQLite file
that isn't a Winnow case, such as a browser's `History` database. Winnow
refuses to open it as a case because doing so would write its own tables
into the file. If it's evidence, import it into a case instead (**Case ▾ →
Import…**, see [Cases and importing](Cases-and-importing.md)). To make a
new case, use **+ New case** (or `--case` with a path that doesn't exist
yet).

### "No case file at …"

**Open existing case file…** only opens a case that is already there. Check
the path, and that the drive holding it is connected; to start a new case,
use **+ New case**.

### No app window, or a browser tab instead

Winnow opens itself in a chromeless app window using a Chromium-family
browser: Edge, Chrome, Brave or Chromium (Edge is tried first on Windows).
If none is installed, it opens your default browser in an ordinary tab
instead; Firefox is only ever used that way. Nothing is wrong in either
case.

If no window appears at all, open the address Winnow printed
(<http://127.0.0.1:8777> by default) yourself. `--browser-tab` always uses
an ordinary tab, and `--no-browser` opens nothing. A server started with
`--no-browser` that no browser ever connects to shuts itself down after 15
minutes.

### Double-clicking a file does nothing (Windows)

When Winnow is started through a file association, it runs without a
console window by default, so an error at startup has nowhere to show.
Untick **Settings → Appearance → Start Winnow hidden when a file is
opened** to give association launches their console back. Or start Winnow
from a terminal with `python server.py`, which prints everything, and open
the file from inside the app.

## Case files

### "This case is already open in another Winnow"

A case file is meant to be open in one Winnow at a time. Two servers on
one case don't see each other's tags, notes or imports until they reload,
and a long write in one (an import, or compacting) starts failing the
other. The message names who has it open, on which machine, and when that
Winnow last checked in.

- In the app, the dialog offers **Open anyway** and **Don't open**.
- On the command line, Winnow refuses and exits. `--force` opens it anyway.

If the Winnow it names isn't running any more (it crashed or was killed),
the claim expires on its own: about two and a half minutes after that
Winnow last checked in, the case reads as free again. Use **Open anyway**
or `--force` only when you know the other Winnow is gone, or that nobody
will write to the case from it.

### The case file is on a network share

Keep case files on a local disk. SQLite's locking doesn't work over SMB or
NFS, so two people opening one case from a share can corrupt it. Winnow
doesn't detect a share for you. Copy the case to your own disk to work on
it, and to collaborate, give each analyst their own case and exchange
session files ([Sessions and export](Sessions-and-export.md)).

### Compact fails: not enough free disk space

**Case ▾ → Compact case file…** rewrites the whole file, so it needs about
the case's size plus 256 MB free in the case's folder. Winnow checks first
and refuses with the numbers ("needs about … MB free and … MB is
available") rather than running out of space part-way. Free some space and
try again.

## While you work

### Winnow shut down by itself

Winnow stops its own server when nobody is using it, so a forgotten one
doesn't hold a case open all weekend:

| Situation | Shuts down after |
| --- | --- |
| The last window was closed | 2 minutes |
| Windows went quiet without closing (laptop asleep, browser suspended the tab) | 30 minutes |
| No window ever connected | 15 minutes |

It waits while an import is running or queued, a table is being saved, a
watchlist scan or a search is running in the background, or a download is
still in progress. The terminal shows a line such as `the last window
closed 120s ago — shutting down (disable with --no-idle-shutdown)`.

A window left open after that shows **Lost the connection to Winnow's
server**. Start Winnow again and click **Try again**, which reloads the
page. Everything committed is in the case file.

To keep the server running with no window open, start it with
`--no-idle-shutdown`. The double-click launchers pass no options, so run
`python server.py --no-idle-shutdown` from a terminal in the Winnow folder.

### The grid reloads, or a view "expired"

Each filtered and sorted view is built into a scratch file that lives only
as long as the server. When Winnow restarts, or a newer view replaces the
one a request was using, Winnow rebuilds it for you: you may see the grid
reload, or a brief "That search's view expired — running it again". Your
tags, notes and filters are not affected.

"The case this request targeted has been closed or switched — reload the
page" is different. A server has one case open at a time, and every window
using it shares that case. If another window opened a different case,
reload this one to see what's open now.

### A regular expression is refused

A pattern that can backtrack catastrophically, such as `(a+)+$` or
`(\w+\.?)+`, is refused with a message starting **Not a usable regular
expression**. One crafted cell could freeze Winnow on such a pattern, and
nothing can interrupt it. The message says how to rewrite it: end each
repeated part at a character it can't match (`(\w+\.)+` instead of
`(\w+\.?)+`), use a character class such as `[ab]` instead of `(a|b)`, or
search with Contains. The same check applies to regex filters, regex
search, `REGEXP` in the SQL pane and regex derived columns. See
[Filtering and searching](Filtering-and-searching.md).

### Searching is slow

Contains and Advanced search use a substring index that builds in the
background after a table is imported, or the first time you search it.
Until it's ready, search still gives the right answers, just by scanning
every row, and the hint in the search box reads `substring · index
building`. **Case ▾ → Tables…** shows each table's index as **⏳ Building…**
or **✓ Ready**. Regex search always scans.

If an index never builds and **Tables…** keeps saying **Not started**, the
Python running Winnow has a SQLite older than 3.45, which can't use the
index. Search still works at scanning speed. Check the version with:

```bash
python -c "import sqlite3; print(sqlite3.sqlite_version)"
```

### Imports are slow, or rows look wrong

Winnow parses two imports at a time; the rest wait as **queued** in the
jobs panel at the bottom right, where each can be cancelled. A cancelled
import removes its partial table. An import that fails part-way keeps the
rows before the failure, and the message says how many.

The message when an import finishes flags the usual causes of odd results:
rows padded or trimmed to fit, a row "with very long multi-line fields —
check for a stray quote if the row count looks low", or a file that was
"not UTF-8 — read as Windows-1252". [Cases and
importing](Cases-and-importing.md) explains each.

### Scrolling is slow over Remote Desktop

Turn on **Settings → Remote session → Remote session mode**. It scrolls the
grid a whole row at a time and stops animations and hover effects, so the
remote display has far less to send. It's saved for the machine.

### Keyboard shortcuts are back to the defaults

Custom shortcuts are stored in the browser, for the address Winnow was
opened at. A different port (a file-association launch can start on one of
its own), a different browser, or `--browser-profile` starts from the
defaults. Appearance settings are also kept by the server, so the theme
follows you; shortcuts don't. See
[Settings and shortcuts](Settings-and-shortcuts.md).

## Using Winnow from another machine

Winnow listens only on `127.0.0.1` unless told otherwise. To reach it from
another computer, start it with `--host 0.0.0.0` (or the address of one
network card). Winnow prints a warning when you do: it has no login and no
access control, so anyone who can reach that address can open, import
into and query any case the server can reach. Only do this on a trusted,
isolated network.

From the other machine, use the server's IP address,
`http://192.0.2.10:8777` for example. A host name is refused with
**Unrecognised Host header. Reach Winnow at the address it printed on
startup, or pass --allow-host if you front it with a name.** Add each name
you use with `--allow-host NAME` (repeatable).

Some things work only from the machine Winnow runs on, and are refused
from anywhere else: picking files in the file browser, creating folders,
quick-look cases, installing plugins, file associations, and
**Settings → Environment**. Paths you type are paths on the Winnow
machine, not yours. Files you drag onto an open case upload normally.

## Sessions and exports

### An Excel export has more than one sheet per table

A worksheet holds 1,048,575 rows of data plus its header. A bigger table
continues on sheets named `Name (2)`, `Name (3)` and so on, and every row is
exported. **Export all tables (.xlsx)** asks before it starts when this
will happen. The CSV exports have no row limit. Details, along with how
long cells and sheet names are handled, are in [Sessions and
export](Sessions-and-export.md).

### A session load skipped a table

After **Load**, a message names each table the session couldn't place:
the evidence isn't in your case, it was imported with different options,
or several tables could be it. Import the same file the same way, then
load again. [Sessions and export](Sessions-and-export.md) lists every
case.

### A session didn't restore tags on a table made in Winnow

Tables saved from the SQL pane with **Save as table…**, or written by a
plugin tab such as first_last, are matched by an identity Winnow gives them
when they are made. A session saved by an older version of Winnow didn't
record it, so loading that session can't tell which table it meant. Those
tables keep the tags and notes they have, and the message after the load
names them. Save a new session to record them. A session from another
analyst's case never applies to a table you made yourself, even one with
the same name; only a copy of their table matches.

## Plugins

### A plugin failed to load

A broken plugin doesn't stop Winnow or the other plugins. It shows up in
three places:

- **Settings → Plugins** adds "· N failed to load" to its summary line.
- **Manage plugins…** marks the plugin **error**, gives its status as
  "failed to load", and shows the error under **This plugin did not load**.
- If it failed at startup, **Case ▾ → Log…** has a `Plugin FAILED` entry,
  and the **Case** button shows a dot until you open the log.

A plugin can also fail because it needs a newer plugin API than this
Winnow provides; the error says so. Switch it off, fix or remove it, or
update Winnow. See [Plugins](Plugins.md).

## An update broke something

Roll back from a terminal in the Winnow folder:

```bash
python update.py --rollback
```

This restores the program files from before the last update, keeping your
cases, saved filters, plugins and settings, the same as an update does. It
needs no running server, so it works even when the update stopped the app
from starting. Restart Winnow afterwards. Rollback is only on the command
line; **Settings → Updates** can install an update but not undo one.
Winnow keeps backups of the last three versions it replaced, in
`.winnow-backup/` in the Winnow folder. See
[Installation](Installation.md) for updating.

## Leftover files after a crash

While a case is open, Winnow keeps its views in a scratch file named
`winnow-views-…` in `/dev/shm` on Linux, or the system temp folder
elsewhere. A clean shutdown deletes it. After a crash or a forced kill it
stays behind, and the next Winnow to start deletes it, printing `Cleaned
up N orphaned temp file(s) from previous runs`. A file belonging to a
Winnow that is still running is never touched.

Two other kinds of temp file can be left by a crash and are not cleaned up
automatically: `winnow-export-….xlsx` (an **Export all tables** in
progress) and `winnow-upload-…` folders (a file being uploaded for
import), both in the system temp folder. They are safe to delete when no
Winnow is running.

A crash can also leave `<case>.db-winnow-wal` and `-shm` files and a
`.winnow-lock` file beside the case. Leave them: the next open uses or
replaces them.

## Where the log is

**Case ▾ → Log…** shows what happened since the server started, newest
first: imports and exports as they start and finish, and server-side
warnings and errors. **Errors** and **All** switch between just the errors
and everything, and the filter box searches the messages. When an error
arrives that you haven't seen, the **Case** button shows a dot.

The log is kept in memory, the most recent 2,000 entries, and is gone when
the server stops. Winnow does not write a log file. Everything in it is
also printed to the terminal Winnow was started from (on standard error),
so to keep a copy, start Winnow from a terminal and redirect it:

```bash
python server.py 2> winnow-log.txt
```

## Reporting a bug

Open an issue at <https://github.com/jacsonott/Winnow/issues> and include:

- the Winnow version, from **Settings → Updates** (a develop build shows
  its branch and commit there too) or from `winnow/version.py`;
- your operating system, `python --version`, and the browser;
- what you did, what you expected, and what happened instead;
- the relevant lines from **Case ▾ → Log…** (switch to **All**) or from the
  terminal, including any traceback.

Never attach real evidence or a real case file to a public issue. A few
synthetic rows that show the problem are enough.
