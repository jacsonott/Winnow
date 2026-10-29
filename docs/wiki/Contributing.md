# Contributing

Winnow is a FastAPI and SQLite backend with a plain JavaScript frontend:
no framework, no bundler, no build step. That shape is deliberate. It has
to install and run on an airgapped analysis box, so nothing it needs at
runtime may come from a CDN, npm, a web font or any other network source.
A change that adds a runtime network dependency is the wrong change; the
one exception is a plugin that is explicitly opt-in, like
`claude_assistant`.

This page is for people changing Winnow itself. If you want to extend it
without touching the core, a plugin is usually the better route: see the
[plugin guide](../writing-plugins.md).

## Reporting a bug

Open an issue at <https://github.com/jacsonott/Winnow/issues>. The things
that make one quick to act on are in
[Troubleshooting](Troubleshooting.md#reporting-a-bug): the version, what
you did, what you expected, and what happened instead. Never attach real
evidence or a real case file to a public issue; a few synthetic rows that
show the problem are enough.

## Branches

**Open every pull request against `develop`.** It is where all work lands
and it carries the full history.

`main` holds releases only, one snapshot commit per release, and is the
repository's default branch because it is what a visitor should land on.
That means GitHub's "New pull request" button and `gh pr create` both
default to `main`, which is the wrong target, so name the base every time:

```bash
gh pr create --base develop
```

`main` and `develop` share no history, so a feature branch merged into
`main` can't be untangled short of a release. Releases are cut by the
maintainer with `scripts/release.py`; contributors never need to touch
`main`.

## Setting up

```bash
git clone https://github.com/jacsonott/Winnow
cd winnow
git checkout develop
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
playwright install chromium                     # once, for the browser tests
python server.py
```

`requirements-dev.txt` adds pytest, httpx2, esprima and Playwright on top
of the runtime requirements. They are development-only and must never
become something the app needs to run.

`python scripts/make_fixture.py --rows 1200000 --out sample.csv` writes a
synthetic 1.2-million-row event log for trying things at realistic size.
Generate fixtures rather than committing them; `*.csv`, case files and
`workspace/` are all gitignored.

## Where things are

| path | what it is |
| --- | --- |
| `server.py` | HTTP routes and the command-line entry point. Kept thin; the logic is in `winnow/`. |
| `update.py` | The command-line updater. One of only two `.py` files in the root, because they're the only ones an analyst runs. |
| `winnow/store.py` | All SQLite: ingest, view building, tags, sessions, export. |
| `winnow/` | Everything else the app is made of: timestamp and JSON/XML parsing for derived columns, archive, Excel, Plaso, IIS log and key=value log readers, the plugin host, the updater, file associations. |
| `winnow/defaults/` | The shipped header sets, triage filters and profiles, as JSON. |
| `static/` | The frontend. `index.html`, `style.css`, and one ES module per subsystem in `static/js/` (`main.js` is the entry). |
| `examples/plugins/` | The bundled plugins, which double as the reference for writing new ones. |
| `docs/notes/` | Per-subsystem working notes. |
| `tests/` | The pytest suite, including `tests/ui/` (browser tests). |
| `bench/` | The performance suite. |

## Read the notes before you change a subsystem

[`docs/notes/`](../notes/README.md) holds one file per part of the app
(store, ingest, grid, UI, server, derived columns, plugins, frontend
modules). Each entry records something that looks simplifiable but isn't,
and why: usually a measurement or a failure that actually happened. Read
the note for the part you are about to touch first, and add a new entry
there when you find a new trap.

A few rules hold everywhere and are the ones most worth knowing up front:

- **Evidence is never modified.** Each import becomes a read-only source
  table; tags, notes, layouts and derived columns are all sidecars keyed
  to it.
- **Never page a filtered or sorted view with `LIMIT/OFFSET`.** Views are
  materialised once and paged by position, which is what keeps scrolling
  at a page 150,000 rows deep down to a millisecond.
- **Column names are user data.** Quote them through `store.q()`; never
  format a raw header into SQL.
- **Only the visible rows are ever in the DOM**, and the grid uses one
  delegated listener rather than one per row.
- **Every tag write goes through `Store._apply_tag_change`**, so undo
  replays exactly the rows a change touched.
- **Anything that works on a table works on a merged table too.** The
  merge path is part of a feature, not a follow-up.

## Tests

```bash
pytest                      # everything
pytest -m "not ui"          # skip the browser tests
pytest tests/ui             # only the browser tests
```

The suite has three layers. The backend tests cover the store, the server
and the parsers. `tests/test_static_syntax.py` parses every frontend
module and checks that each one imports what it uses, since with no build
step a stray brace or a missing import otherwise ships as a blank page.
`tests/ui/` drives a real server with a real Chromium through Playwright
and catches the layout and state bugs nothing else can see. The browser
fixtures skip, rather than fail, when Playwright or Chromium is missing.

CI runs all three on every pull request into `develop` or `main`, split
into parallel shards. A UI test should assert the symptom a user would
see, not the shape of the fix, and must be deterministic: a flaky required
check teaches everyone to re-run until green.

## Performance

`bench/` is a separate, stdlib-only performance suite.

```bash
python3 -m bench --vs-ref HEAD --only-changed   # did my change cost anything?
python3 -m bench --size quick                   # about two minutes
```

It reports a regression only when a change clears several thresholds at
once, so two runs of identical code report nothing.
`bench/README.md`, on develop, has the details.

## Documentation travels with the change

- A change a user would notice updates the relevant [wiki page](Home.md),
  and the [README](../../README.md) if it changes the front-page story.
  Edit the pages in `docs/wiki/`, not in the GitHub wiki: a merge to
  `develop` republishes the wiki from them and overwrites it. A new page
  goes in the list under **Pages** on [Home](Home.md), which is also the
  wiki's sidebar, and `tests/test_build_wiki.py` fails if one is missing or
  a link doesn't resolve.
- The same pages are the project's GitHub Pages website, rebuilt from
  `main` at each release, with Home's page list as its navigation. A pull
  request that changes them also builds the site, and that check fails on
  a link to a heading that doesn't exist. To preview it,
  `pip install -r docs/site/requirements.txt`, then
  `python scripts/build_wiki.py --site /tmp/winnow-site` and
  `mkdocs serve -f /tmp/winnow-site/mkdocs.yml`.
- Anything a plugin author can call or receive is documented in the same
  commit, both in the `winnow/plugin_api.py` module docstring and in
  [`docs/writing-plugins.md`](../writing-plugins.md).
  `tests/test_plugin_docs_current.py` fails the build if a public
  `register_*` hook is missing from either.
- `python3 scripts/screenshots.py` regenerates the README's and the
  manual's screenshots (the launch animation's still among them) and the
  website's animated banner and feature pictures from a seeded demo case,
  so a changed image means the UI moved. The animation also needs Pillow
  (`pip install pillow`).
