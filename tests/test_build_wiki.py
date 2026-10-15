"""scripts/build_wiki.py: docs/wiki/ as the GitHub wiki publishes it.

The publish itself (.github/workflows/wiki.yml) only runs after a merge to
develop, so a page that would publish with a dead link, or not be
reachable from the sidebar, has to be caught here or it is caught by a
reader. The first half builds the real docs/wiki/ and checks what would be
published; the second pins the rewriting rules on small synthetic pages.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("build_wiki", ROOT / "scripts" / "build_wiki.py")
build_wiki = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_wiki)

REPO = "jacsonott/Winnow"
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
CODE = re.compile(r"(`+)[\s\S]+?\1")


def _targets(text):
    """Link targets outside code spans and fenced blocks."""
    text = re.sub(r"^(```|~~~)[\s\S]*?^\1", "", text, flags=re.M)
    return LINK.findall(CODE.sub("", text))


@pytest.fixture(scope="module")
def published(tmp_path_factory):
    out = tmp_path_factory.mktemp("wiki")
    build_wiki.build(out, repo=REPO, ref="develop")
    return {p.name: p.read_text(encoding="utf-8") for p in out.iterdir()}


def test_every_page_is_published_with_a_sidebar_and_footer(published):
    pages = {p.name for p in (ROOT / "docs" / "wiki").glob("*.md")}
    assert set(published) == pages | {"_Sidebar.md", "_Footer.md"}


def test_every_link_resolves_where_it_lands(published):
    names = {n[:-3] for n in published}
    prefixes = (f"https://github.com/{REPO}/blob/develop/",
                f"https://github.com/{REPO}/tree/develop/",
                f"https://raw.githubusercontent.com/{REPO}/develop/")
    for filename, text in published.items():
        for target in _targets(text):
            if target.startswith("#") or target.startswith(("winnow:", "mailto:")):
                continue
            path = target.split("#")[0]
            if path.startswith(prefixes):
                rel = next(path[len(p):] for p in prefixes if path.startswith(p))
                assert (ROOT / rel).exists(), f"{filename}: {target}"
            elif re.match(r"https?://", path):
                continue
            else:
                # A wiki page link: bare page name, no .md, no directories.
                assert path in names, f"{filename}: {target} is not a wiki page"


def test_the_sidebar_reaches_every_page(published):
    sidebar = set(_targets(published["_Sidebar.md"]))
    for filename in published:
        if not filename.startswith("_"):
            assert filename[:-3] in sidebar, filename


def test_pages_do_not_repeat_the_title_the_wiki_prints(published):
    for filename, text in published.items():
        assert not text.startswith("# "), filename


# --- the rewriting rules, on synthetic pages --------------------------------

def _tree(tmp_path, pages, extra=()):
    wiki = tmp_path / "docs" / "wiki"
    wiki.mkdir(parents=True)
    for name, text in pages.items():
        (wiki / name).write_text(text, encoding="utf-8")
    for rel in extra:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    return tmp_path


HOME = "# Home title\n\nIntro.\n\n## Pages\n\n**Group**\n\n- [Other](Other.md): about it,\n  wrapped.\n\n## After\n"


def _build(root, tmp_path):
    out = tmp_path / "out"
    build_wiki.build(out, root=root, repo=REPO, ref="dev")
    return {p.name: p.read_text(encoding="utf-8") for p in out.iterdir()}


def test_links_are_rewritten_for_the_wiki(tmp_path):
    root = _tree(tmp_path / "src", {
        "Home.md": HOME,
        "Other.md": (
            "# Other\n\n"
            "See [Home](Home.md#pages), [a wrapped\nlink](Home.md), "
            "[notes](../notes/README.md), [the notes folder](../notes/), "
            "[top](#other) and [out](https://example.com).\n\n"
            "![shot](../shots/a.png)\n\n"
            "`[code](Missing.md)` stays, and so does a fence:\n\n"
            "```\n[fenced](Missing.md)\n```\n"
        ),
    }, extra=["docs/notes/README.md", "docs/shots/a.png"])
    other = _build(root, tmp_path)["Other.md"]
    assert other.startswith("See [Home](Home#pages)")
    assert "[a wrapped\nlink](Home)" in other
    assert f"[notes](https://github.com/{REPO}/blob/dev/docs/notes/README.md)" in other
    assert f"[the notes folder](https://github.com/{REPO}/tree/dev/docs/notes)" in other
    assert "[top](#other)" in other and "[out](https://example.com)" in other
    assert f"![shot](https://raw.githubusercontent.com/{REPO}/dev/docs/shots/a.png)" in other
    assert "`[code](Missing.md)`" in other and "[fenced](Missing.md)" in other


def test_the_sidebar_is_homes_page_list(tmp_path):
    root = _tree(tmp_path / "src", {"Home.md": HOME, "Other.md": "# Other\n"})
    built = _build(root, tmp_path)
    assert built["_Sidebar.md"] == "[Home](Home)\n\n**Group**\n\n- [Other](Other)\n"
    assert built["Home.md"].startswith("Intro.")
    assert "docs/wiki" in built["_Footer.md"] and "`dev`" in built["_Footer.md"]


@pytest.mark.parametrize("page, complaint", [
    ("[gone](Gone.md)", "Gone.md is not a page"),
    ("[gone](../nowhere.md)", "does not exist"),
    ("[up](../../../x.md)", "outside the repository"),
])
def test_a_broken_link_fails_the_build_and_writes_nothing(tmp_path, page, complaint):
    root = _tree(tmp_path / "src", {"Home.md": HOME, "Other.md": f"# Other\n\n{page}\n"})
    with pytest.raises(build_wiki.BuildError, match=re.escape(complaint)):
        _build(root, tmp_path)
    assert not (tmp_path / "out").exists()


def test_a_page_home_does_not_list_fails_the_build(tmp_path):
    root = _tree(tmp_path / "src", {"Home.md": HOME, "Other.md": "# Other\n",
                                    "Orphan.md": "# Orphan\n"})
    with pytest.raises(build_wiki.BuildError, match="does not list Orphan.md"):
        _build(root, tmp_path)
