#!/usr/bin/env python3
"""Regenerate the README's screenshots against the app as it is today.

    python3 scripts/screenshots.py                  # -> docs/screenshots/*.png
    python3 scripts/screenshots.py --keep           # leave the demo case behind
    python3 scripts/screenshots.py --only grid      # one shot, while iterating
    python3 scripts/screenshots.py --only triage    # the Pages site's banner GIF

Why this exists: the four images the README shows went 766 commits without
being regenerated, because regenerating them meant hand-building a case,
hand-driving a browser and remembering which filter had been applied. None
of that was written down. Everything here is seeded, so two runs of the same
commit produce the same rows in the same order, and a diff in a screenshot
means the UI moved rather than the fixture did.

The demo case is EvtxECmd-shaped on purpose: the header row is the exact
column list winnow/defaults/headers.json calls "Event logs (EvtxECmd)", so
the shipped triage filters bind to it and the saved-filter strip populates.
A fixture with invented column names shows an empty strip and quietly
misrepresents the product.

Needs the dev extras (playwright) — it is a maintenance tool, not part of
the app. `pip install -r requirements-dev.txt && playwright install chromium`.
The GIF also needs Pillow (`pip install pillow`), which nothing else in the
repository uses, so it is asked for only when the GIF is.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"

# The EvtxECmd header set, verbatim from winnow/defaults/headers.json. If that
# file's column list changes, this must change with it or the saved filters
# stop binding and the grid shot loses the strip that makes it legible.
EVTX_COLUMNS = [
    "RecordNumber", "EventRecordId", "TimeCreated", "EventId", "Level",
    "Provider", "Channel", "ProcessId", "ThreadId", "Computer", "ChunkNumber",
    "UserId", "MapDescription", "UserName", "RemoteHost", "PayloadData1",
    "PayloadData2", "PayloadData3", "PayloadData4", "PayloadData5",
    "PayloadData6", "ExecutableInfo", "HiddenRecord", "SourceFile",
    "Keywords", "ExtraDataOffset", "Payload",
]

HOSTS = ["WKSTN-014", "WKSTN-002", "SRV-DC01", "SRV-FS02"]
USERS = ["ACME\\jsmith", "ACME\\mreyes", "ACME\\svc_backup", "NT AUTHORITY\\SYSTEM"]
# EventId -> (MapDescription, Provider, Channel). Only ids the shipped filters
# actually look for, so the strip's chips select real rows.
EVENTS = {
    4624: ("Successful logon", "Microsoft-Windows-Security-Auditing", "Security"),
    4625: ("Failed logon", "Microsoft-Windows-Security-Auditing", "Security"),
    4672: ("Special privileges assigned to new logon", "Microsoft-Windows-Security-Auditing", "Security"),
    4688: ("Process created", "Microsoft-Windows-Security-Auditing", "Security"),
    4104: ("Remote Powershell session", "Microsoft-Windows-PowerShell", "Microsoft-Windows-PowerShell/Operational"),
    7045: ("Service installed", "Service Control Manager", "System"),
    1102: ("Event log cleared", "Microsoft-Windows-Eventlog", "Security"),
}


def evtx_rows(n: int, seed: int) -> list[list]:
    rng = random.Random(seed)
    t0 = dt.datetime(2026, 3, 14, 0, 0, 0)
    rows = []
    for i in range(n):
        eid = rng.choices(list(EVENTS), weights=[34, 18, 22, 14, 6, 4, 2])[0]
        desc, provider, channel = EVENTS[eid]
        host, user = rng.choice(HOSTS), rng.choice(USERS)
        t = t0 + dt.timedelta(seconds=i * 37 + rng.randint(0, 30))
        rows.append([
            i + 1, 100000 + i, t.strftime("%Y-%m-%d %H:%M:%S"), eid,
            "Information" if eid != 4625 else "Warning", provider, channel,
            rng.choice([4, 640, 1044, 3120]), rng.randint(100, 9999), host,
            i // 200, "" if eid == 4688 else f"S-1-5-21-{rng.randint(1000, 9999)}",
            desc, user, f"10.4.{rng.randint(0, 5)}.{rng.randint(1, 254)}",
            f"Target: {user}", f"LogonType: {rng.choice([2, 3, 10])}", host,
            "", "", "", "", "false", f"{channel}.evtx", "Audit Success", 0,
            f'{{"EventData":{{"SubjectUserName":"{user.split(chr(92))[-1]}"}}}}',
        ])
    return rows


def write_csv(path: Path, header: list[str], rows: list[list]) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


def build_case(case: Path, work: Path) -> dict:
    """Ingest the demo tables and tag a few rows, straight through Store.

    Deliberately not through the HTTP API: the case has to exist before the
    server starts so the shots open on a populated app rather than the home
    screen, and set_tags is the same write path the grid's hotkeys take
    (_apply_tag_change), so the rail and the ribbon counts are real.
    """
    sys.path.insert(0, str(ROOT))
    from winnow.store import Store

    evtx = write_csv(work / "Security.csv", EVTX_COLUMNS, evtx_rows(2400, seed=7))
    # A second table so the Timeline has more than one source to interleave,
    # which is the whole point of that page.
    ps = write_csv(work / "PowerShell.csv", EVTX_COLUMNS, evtx_rows(600, seed=11))

    st = Store(str(case), default_tags=None)
    ids = {}
    for path, label in ((evtx, "Security.csv"), (ps, "PowerShell.csv")):
        ids[label] = st.ingest_csv(str(path), build_fts=False)["id"]

    tags = {t["name"]: t["id"] for t in st.list_tags()}
    # Tag a scatter rather than a block: the rail exists to show WHERE
    # findings cluster in a filtered view, and a contiguous run tells that
    # story wrongly.
    rng = random.Random(3)
    st.set_tags(ids["Security.csv"], sorted(rng.sample(range(1, 2401), 26)), tags["TA"], True)
    st.set_tags(ids["Security.csv"], sorted(rng.sample(range(1, 2401), 41)), tags["Suspicious"], True)
    st.set_tags(ids["Security.csv"], sorted(rng.sample(range(1, 2401), 18)), tags["Benign"], True)
    st.set_tags(ids["PowerShell.csv"], sorted(rng.sample(range(1, 601), 12)), tags["TA"], True)
    st.close()
    return ids


def serve(case: Path, port: int):
    # A workspace of its own beside the demo case, not the developer's: the
    # pivot still switches a plugin on, and in the real workspace it stayed
    # on, so every later run drew its tab into the other pictures.
    env = {**os.environ, "WINNOW_WORKSPACE_DIR": str(case.parent / "workspace")}
    p = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py"), "--case", str(case),
         "--port", str(port), "--no-browser", "--no-fts", "--no-idle-shutdown"],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(240):
        try:
            urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{port}/api/sources",
                                       headers={"X-Timeline-Lite-Client": "1"}), timeout=5).read()
            return p
        except Exception:
            time.sleep(0.25)
    p.terminate()
    raise SystemExit("server did not come up")


# Same seed tests/ui/conftest.py uses. The splash covers the viewport for
# several seconds and the first-run remote prompt puts an overlay over
# everything; both would appear in — or block — every shot.
FIRST_RUN = (
    "localStorage.setItem('winnow.remotePrompt', 'seen');"
    "localStorage.setItem('winnow.appearance',"
    " JSON.stringify({ splash: false, pagesMenu: false, filterUi: 'row' }));"
    "localStorage.setItem('winnow.sidebar', JSON.stringify({ collapsed: false }))"
)


# EvtxECmd emits 27 columns and an analyst hides the bookkeeping ones on
# arrival. Doing the same here is not dressing the shot up: leaving them in
# pushes MapDescription, UserName and RemoteHost — the columns the filters
# are about — off the right edge, so the picture would show the tool doing
# its job with the answer cropped out.
BOOKKEEPING_COLUMNS = (
    "RecordNumber", "EventRecordId", "ProcessId", "ThreadId", "ChunkNumber",
    "UserId", "Level", "ExtraDataOffset", "HiddenRecord", "Keywords",
    "ExecutableInfo", "Payload",
)


def shoot_grid(pg, win):
    """The front page: a real triage view — one of the shipped EVTX filters
    applied, tags on the rail and in the ribbon, the histogram open. The one
    image that has to say what the tool is in a single glance."""
    win.open_table("Security.csv")
    win.hide_columns(*BOOKKEEPING_COLUMNS)
    win.apply_shipped_filter("Logons")
    win.histogram(True)
    win.quiet()
    return None                                 # full window


def shoot_value_picker(pg, win):
    """The Excel-style header dropdown, which is where most filtering starts.

    Same column hiding as the grid shot, for a second reason: the picker
    anchors to its column header, so with 27 columns MapDescription sits
    against the right edge and the panel opens half off-screen.
    """
    win.open_table("Security.csv")
    win.hide_columns(*BOOKKEEPING_COLUMNS)
    pg.evaluate("() => __winnow.openValuePickerForColumn('MapDescription')")
    pg.wait_for_selector(".value-picker", state="attached")
    win.quiet(400)
    # Frame the whole panel with room around it, clamped to the viewport, and
    # fail rather than ship a picture of a clipped dropdown.
    box = pg.evaluate("""() => {
      const p = document.querySelector('.value-picker').getBoundingClientRect();
      const pad = 80;
      const x = Math.max(0, p.left - pad);
      const y = Math.max(0, p.top - 150);
      return { x, y,
               width: Math.min(window.innerWidth - x, p.width + pad * 2),
               height: Math.min(window.innerHeight - y, p.height + 190),
               full: { right: p.right, bottom: p.bottom, w: window.innerWidth } };
    }""")
    full = box.pop("full")
    if full["right"] > box["x"] + box["width"] + 1 or full["right"] > full["w"]:
        raise SystemExit("the value picker does not fit the viewport — widen --width")
    return box


def shoot_timeline(pg, win):
    """Every tagged row in the case, both tables, one stream."""
    win.open_table("Security.csv")
    pg.evaluate("() => __winnow.showTimelineTab()")
    pg.wait_for_function("() => { const t = document.getElementById('timelineview');"
                         " return t && !t.hidden; }", timeout=30_000)
    win.quiet(1400)
    return None


def shoot_pivot(pg, win):
    """A plugin tab, to show the extension point is real rather than claimed.

    The pivot is built by writing the tab's own saved state and letting the
    plugin restore it, rather than by scripting drags into the Rows and
    Columns wells. Drag-and-drop is the most brittle thing a screenshot
    script can depend on, and tabState is a documented part of the plugin
    contract — so this exercises a real path instead of pantomiming one.
    """
    win.enable_plugin("pivot")
    win.open_table("Security.csv")
    tab_id = pg.evaluate("""() => {
      const t = (__winnow.S.pluginTabs || []).find((x) => /pivot/i.test(x.id));
      return t ? t.id : null;
    }""")
    if not tab_id:
        raise SystemExit("the pivot example did not register a tab — is it still bundled?")
    pg.evaluate("""async ([key, sid]) => {
      await __winnow.post('/api/plugin_state', { key, payload: {
        v: 1, active: 0,
        pivots: [{ name: 'Pivot 1', source: { id: sid, name: 'Security.csv' },
                   rows: ['Computer'], cols: ['MapDescription'],
                   values: [{ column: 'RecordNumber', agg: 'count' }],
                   filters: [], subtotals: true, grandTotals: true }],
      } });
    }""", [f"tab:{tab_id}", win.ids["Security.csv"]])
    pg.reload()
    pg.wait_for_function("() => window.__winnow && __winnow.S.sources.length > 0", timeout=60_000)
    win.open_table("Security.csv")
    pg.evaluate("async (id) => { await __winnow.showPage('plugin:' + id); }", tab_id)
    pg.wait_for_function(
        "() => document.querySelectorAll('.pluginview table td, .pluginview .pv-cell').length > 4",
        timeout=45_000)
    # The restore banner is a true thing the plugin does, and incidental to
    # what this picture is about — it is here only because seeding the tab
    # state is how the pivot got built. Same reasoning as clearing a toast.
    pg.evaluate("""() => {
      for (const n of document.querySelectorAll('.pluginview *')) {
        if (/Restored the pivots/i.test(n.textContent || '') && n.children.length < 4) { n.remove(); break; }
      }
    }""")
    win.quiet(700)
    # Crop to the table and the wells; a pivot of four hosts leaves most of
    # a 1000px viewport empty and the whitespace says nothing.
    return pg.evaluate("""() => {
      // Down to whichever ends lower, the cross-tab or the field wells —
      // the wells are half the point of the picture, so a crop that keeps
      // the table and loses Values shows a result with no question.
      // [data-zone] is the plugin's own hook on the field wells — put there
      // to be addressable from tests and the console, which is exactly this.
      const ends = [...document.querySelectorAll('.pluginview table, .pluginview [data-zone]')]
        .map((n) => n.getBoundingClientRect().bottom);
      const bottom = ends.length ? Math.max(...ends) : 760;
      return { x: 0, y: 0, width: window.innerWidth,
               height: Math.min(window.innerHeight, Math.max(600, bottom + 36)) };
    }""")


SHOTS = {
    "grid": shoot_grid,
    "value-picker": shoot_value_picker,
    "timeline": shoot_timeline,
    "pivot": shoot_pivot,
}


def record_triage(pg, win, reel):
    """The Pages site's banner: the loop the tool is built around, once.

    Filter a table down with a shipped filter, narrow it by typing in the
    filter row, tag a few rows, and read them back on the Timeline. Real
    clicks and keys against the real app, so a step that stops working
    fails here instead of being drawn.
    """
    win.open_table("Security.csv")
    win.hide_columns(*BOOKKEEPING_COLUMNS)
    win.histogram(True)
    reel.rest(1100, 520)
    reel.frame(1600)

    reel.click("#btnFilters", hold=700)
    reel.click(".menu .menu-item:has-text('EVTX — Logons')", hold=1500, settle=win.quiet)

    reel.click('.fcell input[data-col="UserName"]', hold=250)
    for ch in "mreyes":
        pg.keyboard.type(ch)
        # One frame per key, each taken once the rebuild it set off has
        # landed: the grid narrows as the name is typed, the same at every
        # run rather than wherever a debounce happened to be.
        pg.wait_for_timeout(260)
        win.quiet(60)
        reel.frame(170)
    pg.keyboard.press("Enter")
    win.quiet(200)
    reel.frame(1300)

    reel.click('#body .row[data-pos="1"] .cell', hold=350)
    reel.click('#body .row[data-pos="4"] .cell', hold=600, modifiers=["Shift"])
    # The selection's own toolbar rather than the `1` key: a key press is
    # invisible in a recording, a click on "TA" is not.
    reel.click(".tag-toolbar .tag-chip:has-text('TA')", hold=300, settle=lambda: pg.wait_for_function(
        "() => [1, 2, 3, 4].every((i) => document.querySelector("
        "`#body .row[data-pos=\"${i}\"] .gutter-mid .stripe`))", timeout=30_000))
    # Put the selection away so what is left is the tag: a stripe on each row.
    pg.keyboard.press("Escape")
    win.quiet(300)
    reel.frame(1700)

    reel.click("#tabTimeline", hold=2600, settle=lambda: (
        pg.wait_for_function("() => { const t = document.getElementById('timelineview');"
                             " return t && !t.hidden; }", timeout=30_000),
        win.quiet(1200)))

    # The stills are taken after this, from the same case: take the tag
    # back off so their counts are the seeded ones.
    pg.evaluate("() => __winnow.post('/api/row_tags/undo', {})")


GIFS = {
    "triage": record_triage,
}


# A pointer drawn into the page, since a screenshot has none. Every move is
# also a real mouse move, so hover states show where the pointer is.
POINTER = """() => {
  if (document.getElementById('reel-pointer')) return;
  const p = document.createElement('div');
  p.id = 'reel-pointer';
  p.style.cssText = 'position:fixed;left:0;top:0;z-index:2147483647;'
    + 'pointer-events:none;width:26px;height:26px;will-change:transform';
  p.innerHTML = '<svg viewBox="0 0 26 26" width="26" height="26">'
    + '<path d="M3 2 L3 21 L8 16.5 L11.5 24 L15 22.5 L11.5 15.2 L18 15.2 Z" '
    + 'fill="#fff" stroke="#111" stroke-width="1.6" stroke-linejoin="round"/></svg>';
  const ring = document.createElement('div');
  ring.id = 'reel-ring';
  ring.style.cssText = 'position:fixed;left:0;top:0;z-index:2147483646;'
    + 'pointer-events:none;border:2px solid #e0a94a;border-radius:50%;opacity:0';
  document.body.append(ring, p);
}"""


class Reel:
    """Frames for a GIF, each one a screenshot held for as long as the
    script says. Not a screen recording: the timing is chosen rather than
    measured, so a slow runner makes the same GIF as a fast one."""

    def __init__(self, pg):
        self.pg, self.frames, self.at = pg, [], (0.0, 0.0)

    def frame(self, ms: int):
        self.frames.append((self.pg.screenshot(), ms))

    def _point(self, x: float, y: float):
        self.pg.evaluate("""([x, y]) => {
          document.getElementById('reel-pointer').style.transform = `translate(${x - 3}px, ${y - 2}px)`;
        }""", [x, y])
        self.at = (x, y)

    def rest(self, x: float, y: float):
        self.pg.evaluate(POINTER)
        self.pg.mouse.move(x, y)
        self._point(x, y)

    def glide(self, x: float, y: float, steps: int = 9, ms: int = 34):
        x0, y0 = self.at
        for i in range(1, steps + 1):
            t = i / steps
            t = t * t * (3 - 2 * t)             # ease in and out
            px, py = x0 + (x - x0) * t, y0 + (y - y0) * t
            self.pg.mouse.move(px, py)
            self._point(px, py)
            self.frame(ms)

    def click(self, selector: str, *, hold: int, settle=None, modifiers=None):
        box = self.pg.locator(selector).first.bounding_box()
        if box is None:
            raise SystemExit(f"nothing to click for {selector!r} — did the UI move?")
        x, y = box["x"] + min(box["width"] / 2, 60), box["y"] + box["height"] / 2
        self.glide(x, y)
        for size, alpha in ((14, .9), (30, .45)):
            self.pg.evaluate("""([x, y, d, a]) => {
              const r = document.getElementById('reel-ring');
              r.style.width = r.style.height = d + 'px';
              r.style.transform = `translate(${x - d / 2}px, ${y - d / 2}px)`;
              r.style.opacity = a;
            }""", [x, y, size, alpha])
            self.frame(60)
        self.pg.evaluate("() => { document.getElementById('reel-ring').style.opacity = 0; }")
        for key in modifiers or ():
            self.pg.keyboard.down(key)
        self.pg.mouse.click(x, y)
        for key in modifiers or ():
            self.pg.keyboard.up(key)
        if settle:
            settle()
        else:
            self.pg.wait_for_timeout(250)
        self.frame(hold)


def save_gif(frames, dest: Path):
    """Frames to one looping GIF, every frame on one shared palette.

    A palette per frame shifts the colours of everything that did not
    change, and then nothing is unchanged — each frame is stored whole
    instead of as the patch that moved. One palette, no dithering, and
    Pillow stores only the changed rectangle of each frame.
    """
    try:
        from PIL import Image
    except ImportError:
        raise SystemExit("the GIF needs Pillow: pip install pillow")
    import io

    images = [Image.open(io.BytesIO(png)).convert("RGB") for png, _ms in frames]
    # The palette comes from every frame the GIF holds on, at full size: a
    # tag's stripe is three pixels wide, and anything scaled down far
    # enough to make a cheap sample averages it into the gold around it.
    held = [im for im, (_png, ms) in zip(images, frames) if ms >= 250] or images
    sample = Image.new("RGB", (images[0].width, images[0].height * len(held)))
    for i, im in enumerate(held):
        sample.paste(im, (0, im.height * i))
    palette = sample.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
    quantized = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in images]
    quantized[0].save(dest, save_all=True, append_images=quantized[1:],
                      duration=[ms for _png, ms in frames], loop=0, optimize=False,
                      disposal=1)


class Win:
    """Thin driver over the page, in the app's own vocabulary.

    Deliberately calls the app's exported functions through __winnow rather
    than clicking chrome wherever there is a choice. A CSS path is a bet on
    markup that moves; `openSource(id)` is the same call the tab strip makes
    and survives a re-skin. The screenshots rotted once already because
    reproducing them meant remembering a click path nobody wrote down.
    """

    def __init__(self, pg, ids):
        self.pg, self.ids = pg, ids

    def quiet(self, ms: int = 800):
        """Let the view land, then clear any toast.

        A toast is transient chrome that happens to be on screen when the
        shutter opens — "Filters cleared", or an aborted-request notice from
        a rebuild the script superseded. It says nothing about the product
        and dates the image the moment the wording changes.
        """
        self.pg.wait_for_function(
            "() => !__winnow.rebuildInFlight || !__winnow.rebuildInFlight()", timeout=60_000)
        self.pg.wait_for_timeout(ms)
        self.pg.evaluate("""() => {
          for (const t of document.querySelectorAll('.toast')) { t.hidden = true; t.textContent = ''; }
        }""")

    def open_table(self, name: str):
        self.pg.evaluate("async (id) => { await __winnow.openSource(id); }", self.ids[name])
        self.pg.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count > 0",
                                  timeout=60_000)
        self.quiet()

    def apply_shipped_filter(self, needle: str):
        """Apply one of the filters winnow/defaults/filters.json ships, by a
        fragment of its name. Fails loudly rather than shooting an unfiltered
        grid and calling it triage: if the demo fixture's header row ever
        stops matching the EvtxECmd set, nothing binds and the shot would
        quietly become a picture of a plain table."""
        n = self.pg.evaluate("""(needle) => {
          const list = __winnow.filtersForCurrentSource();
          const hit = list.find((f) => f.name.toLowerCase().includes(needle.toLowerCase()));
          if (hit) __winnow.applyPreset(hit);
          return list.length;
        }""", needle)
        if not n:
            raise SystemExit(
                "no shipped filter bound to the demo table — the fixture's header row "
                "no longer matches the EvtxECmd set in winnow/defaults/headers.json")
        self.pg.wait_for_function("() => __winnow.S.view && __winnow.S.view.row_count > 0",
                                  timeout=60_000)
        self.quiet()

    def reset(self):
        """Back to a neutral app between shots.

        The shots share one page — booting the app per shot would triple the
        runtime — so anything one of them opens is still open for the next.
        The value picker leaking into the Timeline shot is exactly what this
        prevents, and it is the sort of thing nobody notices until the image
        is on the front page.
        """
        self.pg.keyboard.press("Escape")
        self.pg.evaluate("""() => {
          for (const p of document.querySelectorAll('.value-picker, .menu, .anchored-panel')) p.remove();
          for (const t of document.querySelectorAll('.toast')) { t.hidden = true; t.textContent = ''; }
          __winnow.S.layout = {};
          if (__winnow.histogramOpen && __winnow.histogramOpen()) __winnow.toggleHistogram(false);
        }""")
        self.pg.evaluate("() => __winnow.clearAllFilters()")
        self.pg.wait_for_timeout(500)

    def hide_columns(self, *names: str):
        self.pg.evaluate("""(names) => {
          for (const n of names) {
            if (!__winnow.S.layout[n]) __winnow.S.layout[n] = {};
            __winnow.S.layout[n].hidden = true;
          }
          __winnow.renderHead(); __winnow.render();
        }""", list(names))
        self.quiet(300)

    def histogram(self, on: bool = True):
        self.pg.evaluate("(v) => __winnow.toggleHistogram(v)", on)
        self.pg.wait_for_selector("#histogramPanel", state="attached")
        self.quiet()

    def enable_plugin(self, fs_name: str):
        """Turn a bundled example on, then reload — the registry is rebuilt
        server-side and the tab strip is built at boot."""
        self.pg.evaluate("""async (n) => {
          await __winnow.post('/api/plugins/toggle', { fs_name: n, enabled: true });
        }""", fs_name)
        self.pg.reload()
        self.pg.wait_for_function("() => window.__winnow && __winnow.S.sources.length > 0",
                                  timeout=60_000)
        self.quiet()


def record_gif(browser, name: str, a, ids):
    """One of GIFS, recorded at the GIF's own size and at 1x, so text is
    drawn at the size it is shown rather than scaled down to it — scaling
    blurs it, and the in-between colours the blur makes cost the GIF a
    third of its size again."""
    w, h = (int(n) for n in a.gif_size.split("x"))
    ctx = browser.new_context(viewport={"width": w, "height": h})
    ctx.add_init_script(FIRST_RUN)
    pg = ctx.new_page()
    pg.goto(f"http://127.0.0.1:{a.port}/")
    pg.wait_for_function("() => window.__winnow && __winnow.S.sources.length > 0",
                         timeout=60_000)
    reel = Reel(pg)
    GIFS[name](pg, Win(pg, ids), reel)
    dest = OUT / f"{name}.gif"
    save_gif(reel.frames, dest)
    print(f"  {dest.relative_to(ROOT)}  ({len(reel.frames)} frames, "
          f"{dest.stat().st_size // 1024} KB)")
    ctx.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", choices=sorted({*SHOTS, *GIFS}), default=None)
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--keep", action="store_true", help="leave the demo case on disk")
    ap.add_argument("--width", type=int, default=1680)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--gif-size", default="1200x740",
                    help="the GIF's size in pixels, which is the window it is recorded in")
    a = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("needs playwright: pip install -r requirements-dev.txt && playwright install chromium",
              file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix="winnow-shots-"))
    case = work / "demo.db-winnow"
    OUT.mkdir(parents=True, exist_ok=True)
    ids = build_case(case, work)
    proc = serve(case, a.port)
    wanted = a.only or [*sorted(GIFS), *sorted(SHOTS)]
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            # The GIFs first, each in a window of its own: the pivot still
            # turns a plugin on for the whole server, which would put its
            # tab in the recording, and the recording takes its tag back
            # off before the stills are taken.
            for name in wanted:
                if name in GIFS:
                    record_gif(b, name, a, ids)
            ctx = b.new_context(viewport={"width": a.width, "height": a.height},
                                device_scale_factor=2)
            ctx.add_init_script(FIRST_RUN)
            pg = ctx.new_page()
            pg.goto(f"http://127.0.0.1:{a.port}/")
            pg.wait_for_function("() => window.__winnow && __winnow.S.sources.length > 0",
                                 timeout=60_000)
            win = Win(pg, ids)
            for name in wanted:
                if name not in SHOTS:
                    continue
                win.reset()
                clip = SHOTS[name](pg, win)
                dest = OUT / f"{name}.png"
                pg.screenshot(path=str(dest), clip=clip)
                print(f"  {dest.relative_to(ROOT)}  ({dest.stat().st_size // 1024} KB)")
            b.close()
    finally:
        proc.terminate()
        if a.keep:
            print(f"demo case kept at {case}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
