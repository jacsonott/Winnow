"""No native browser dialogs — anywhere.

window.alert()/confirm()/prompt() cannot be themed, look like a different
program next to the app, and block the page. Winnow has a replacement for
each (ui.js alertDialog/confirmDialog/promptDialog, all on the plugin
context) and the guide tells plugin authors to use them; this keeps the
app and the bundled examples honest about it. Comments are stripped
first — the one place the natives are named in code is ui.js's comment
explaining what replaces them.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN = [ROOT / "static" / "js", ROOT / "examples" / "plugins"]

NATIVE = re.compile(r"(?<![\w.$])(alert|confirm|prompt)\s*\(|window\.(alert|confirm|prompt)\s*\(")
COMMENT_LINE = re.compile(r"^\s*(//|/\*|\*)")


def _code_lines(text):
    """Lines with block and line comments removed (roughly — enough for a
    scan whose false positives would be a comment naming the natives)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    for line in text.splitlines():
        if COMMENT_LINE.match(line):
            continue
        yield line.split("//", 1)[0]


def _js_files():
    for base in SCAN:
        yield from sorted(base.rglob("*.js"))


def test_the_scan_sees_files():
    files = list(_js_files())
    assert len(files) > 20


def test_no_native_dialogs():
    hits = []
    for path in _js_files():
        for n, line in enumerate(_code_lines(path.read_text(encoding="utf-8")), 1):
            if NATIVE.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not hits, "native browser dialog — use alertDialog/confirmDialog/promptDialog:\n" + "\n".join(hits)


def test_the_pattern_catches_what_it_should():
    assert NATIVE.search("alert('x')")
    assert NATIVE.search("window.confirm(msg)")
    assert NATIVE.search("if (!confirm (msg)) return;")
    assert not NATIVE.search("winnow.alertDialog('x')")
    assert not NATIVE.search("await confirmDialog(msg)")
    assert not NATIVE.search("promptDialog('Name:')")
