# Launching Winnow without the terminal

These are double-click launchers for people who'd rather not open a terminal
and type `python server.py`. Each one just finds Python and starts Winnow
from the install root (the folder above this one) — no build step, nothing
installed, so they're airgap-safe. Winnow still needs Python 3 and its
runtime deps (`pip install -r requirements.txt`), same as running it by hand.

| OS | File | Notes |
| --- | --- | --- |
| Linux | `winnow.sh` | Double-click (mark executable / "allow launching" if your file manager asks). Or make a desktop entry — see below. |
| macOS | `winnow.command` | Double-click; Finder runs it in Terminal. First run may need Right-click → Open (Gatekeeper). |
| Windows | `winnow.bat` | Double-click; opens a small console window that stays while Winnow runs. |
| Windows | `winnow.vbs` | Double-click for the same thing with **no** console window. |

You can copy any of these somewhere handier (Desktop, taskbar, Dock) — they
locate the install by their own path, so they keep working when moved as long
as the install itself doesn't move.

## A Linux desktop entry (optional)

For an app-menu / dock icon, create `~/.local/share/applications/winnow.desktop`
with the absolute path to your install:

```ini
[Desktop Entry]
Type=Application
Name=Winnow
Exec=/full/path/to/winnow/launch/winnow.sh
Icon=/full/path/to/winnow/static/icons/winnow-icon-256.png
Terminal=false
Categories=Utility;
```

(This is separate from Winnow's file associations, which open specific file
types in Winnow — see Settings → File associations.)
