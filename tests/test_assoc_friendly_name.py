"""The Windows Open With entry is named "Winnow" with Winnow's icon — not
"Python" with the interpreter's, which is what Explorer shows for a ProgId
whose open verb carries no FriendlyAppName and whose command starts with
python.exe. Registry layout only; whether Explorer honours it is a check
on a Windows box."""

from __future__ import annotations

from fakes import FakeReg

from winnow import assoc


def _cat(*exts):
    cat = [t for t in assoc.supported_types() if t["ext"] in exts]
    assert [t["ext"] for t in cat] == list(exts)
    return cat, assoc.supported_types()


def _win(**kw):
    return assoc.WindowsAssoc(reg=FakeReg(), **kw)


def test_register_names_and_icons_the_open_verb():
    win = _win()
    picked, cat = _cat(".txt")
    win.register(picked, cat)
    keys = win.reg.keys
    base = "Software\\Classes\\Winnow.File"
    assert keys[base]["FriendlyAppName"] == "Winnow"
    assert keys[f"{base}\\shell\\open"]["FriendlyAppName"] == "Winnow"
    assert keys[f"{base}\\shell\\open"]["Icon"].endswith("winnow.ico,0")
    assert keys[f"{base}\\shell\\open"]["Icon"] == keys[f"{base}\\DefaultIcon"][None]
    # The command itself is unchanged: still python + server.py --assoc.
    assert keys[f"{base}\\shell\\open\\command"][None].endswith('"%1"')


def test_the_background_toggle_keeps_the_name():
    """refresh_command rewrites the verb for pythonw.exe; the name and icon
    must survive that rewrite, or the toggle would bring "Python" back."""
    win = _win()
    picked, cat = _cat(".csv")
    win.register(picked, cat)
    win.background = True
    assert win.refresh_command() is True
    verb = win.reg.keys["Software\\Classes\\Winnow.File\\shell\\open"]
    assert verb["FriendlyAppName"] == "Winnow" and verb["Icon"].endswith("winnow.ico,0")


def test_unregistering_the_last_type_removes_the_verb_too():
    win = _win()
    picked, cat = _cat(".csv")
    win.register(picked, cat)
    win.unregister(picked, cat)
    assert not any(k.startswith("Software\\Classes\\Winnow.File") for k in win.reg.keys)
