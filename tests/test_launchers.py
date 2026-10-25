"""The Windows double-click launchers, simulated.

Neither can run here, and both failures were Windows behaviour rather than
anything in the scripts' own logic, so each test models the one piece of
Windows that bit — cmd.exe's /c quote rule, the Microsoft Store's python
alias stub — exactly as documented, and runs the shipped file through it.
They assert the symptom: which command actually ends up running.
"""

from __future__ import annotations

import re

from winnow import paths

LAUNCH = paths.INSTALL_ROOT / "launch"

SPECIAL = set('&<>()@^|')


# ------------------------------------------------------------ winnow.vbs

def _vbs_run_command(install: str) -> str:
    """The string winnow.vbs hands WScript.Shell.Run, for an install at
    `install`. Evaluates the one expression shape the file uses: string
    literals ("" is a quote inside one) joined with &, plus the
    fso.BuildPath(here, ...) call."""
    src = (LAUNCH / "winnow.vbs").read_text(encoding="utf-8")
    line = next(l for l in src.splitlines() if l.strip().startswith("sh.Run "))
    expr = line.strip()[len("sh.Run "):].rsplit(",", 2)[0]
    here = install + r"\launch"
    out, i = [], 0
    while i < len(expr):
        c = expr[i]
        if c == '"':
            j, buf = i + 1, []
            while True:
                if expr[j] == '"' and expr[j + 1:j + 2] == '"':
                    buf.append('"')
                    j += 2
                elif expr[j] == '"':
                    break
                else:
                    buf.append(expr[j])
                    j += 1
            out.append("".join(buf))
            i = j + 1
        elif expr.startswith('fso.BuildPath(here, "winnow.bat")', i):
            out.append(here + r"\winnow.bat")
            i += len('fso.BuildPath(here, "winnow.bat")')
        elif c in " &":
            i += 1
        else:
            raise AssertionError(f"unexpected token in winnow.vbs at {expr[i:]!r}")
    return "".join(out)


def _cmd_c(cmdline: str) -> str:
    """What cmd.exe runs for `cmd /c <rest>`, per `cmd /?`: the quotes are
    kept only if there are exactly two, nothing between them is one of
    &<>()@^|, and there is whitespace between them; otherwise the first
    character, if a quote, and the LAST quote on the line are removed."""
    assert cmdline.lower().startswith("cmd /c ")
    rest = cmdline[len("cmd /c "):]
    quotes = [m.start() for m in re.finditer('"', rest)]
    if len(quotes) == 2:
        inner = rest[quotes[0] + 1:quotes[1]]
        if not (set(inner) & SPECIAL) and re.search(r"\s", inner):
            return rest
    if rest.startswith('"'):
        last = rest.rfind('"')
        rest = rest[1:last] + rest[last + 1:]
    return rest


def _program(line: str) -> str:
    """The program cmd starts for a command line: a quoted first token is
    taken whole, an unquoted one ends at whitespace or a special char."""
    if line.startswith('"'):
        return line[1:line.index('"', 1)]
    return re.split(r'[\s&<>()@^|]', line, 1)[0]


def test_the_vbs_launcher_runs_the_bat_from_an_install_path_cmd_finds_special():
    """An install unzipped twice is "winnow-0.3.0 (1)". cmd /c sees the
    parentheses, falls back to stripping the outer quotes, and ran
    C:\\Users\\a\\Downloads\\winnow-0.3.0 — nothing started, no window."""
    for install in (r"C:\Users\a\Downloads\winnow-0.3.0 (1)",
                    r"C:\Users\a\R&D tools\winnow",
                    r"C:\Program Files\winnow"):
        ran = _program(_cmd_c(_vbs_run_command(install)))
        assert ran == install + r"\launch\winnow.bat", (install, ran)


# ------------------------------------------------------------ winnow.bat

def _run_bat(programs: dict) -> list[str]:
    """Run winnow.bat's interpreter lines against a fake machine.

    `programs` maps a program name on PATH to the exit code it returns for
    any invocation. Each `<probe> && ( <cmd> & goto :eof )` line runs its
    probe; on exit 0 it runs <cmd> and the script ends whatever <cmd>
    returns (goto :eof follows `&`, not `&&`). Returns what was run."""
    ran: list[str] = []

    def run(command: str) -> int:
        command = re.sub(r"\s*(?:>nul|2>nul)", "", command).strip()
        prog, _, args = command.partition(" ")
        ran.append(command)
        if prog == "where":
            return 0 if args.strip() in programs else 1
        return programs.get(prog, 9009)   # cmd's own "not recognized" is 9009 too

    lines = (LAUNCH / "winnow.bat").read_text(encoding="utf-8").splitlines()
    for line in lines:
        m = re.fullmatch(r"(.+?) && \( (.+?) & goto :eof \)", line.strip())
        if m and run(m.group(1)) == 0:
            run(m.group(2).replace(" %*", ""))
            return ran
    return ran


def _servers(ran: list[str]) -> list[str]:
    return [c for c in ran if c.endswith("server.py")]


def test_the_bat_launcher_gets_past_the_microsoft_store_python_stub():
    """WindowsApps\\python.exe is on PATH on a stock Windows 10/11 even with
    no Python from the Store; run, it says Python was not found and exits
    9009. `where python` finds it, so the script ran it and stopped."""
    stub_plus_launcher = {"python": 9009, "py": 0}
    assert _servers(_run_bat(stub_plus_launcher)) == ["py -3 server.py"]


def test_the_bat_launcher_still_prefers_python_on_path():
    """A real `python` first, as before — an activated venv or a PATH
    Python is where the analyst installed the requirements."""
    assert _servers(_run_bat({"python": 0, "py": 0})) == ["python server.py"]


def test_the_bat_launcher_with_only_the_stub_tries_no_server():
    assert _servers(_run_bat({"python": 9009})) == []
