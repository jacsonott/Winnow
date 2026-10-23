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


# --- the Pages site (build_wiki.py --site) ----------------------------------
#
# MkDocs itself only runs in .github/workflows/pages.yml (it isn't a
# development dependency). What can be checked without it is checked here:
# that what it is handed is complete and every link in it lands.

SITE_REF = "main"


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    out = tmp_path_factory.mktemp("site")
    build_wiki.build_site(out, repo=REPO, ref=SITE_REF, edit_ref="develop")
    return out


def _site_pages(site):
    return {p.name: p.read_text(encoding="utf-8") for p in (site / "docs").glob("*.md")}


def _nav_targets(config):
    return re.findall(r'^\s+- "[^"]+": "([^"]+)"$', config, flags=re.M)


def test_the_site_carries_every_page_and_the_plugin_guide(site):
    wiki = {p.name for p in (ROOT / "docs" / "wiki").glob("*.md")}
    expected = (wiki - {"Home.md"}) | {"index.md", "writing-plugins.md"}
    assert set(_site_pages(site)) == expected


def test_the_site_nav_is_homes_page_list_and_reaches_every_page(site):
    config = (site / "mkdocs.yml").read_text(encoding="utf-8")
    nav = _nav_targets(config)
    assert nav[0] == "index.md"
    assert sorted(nav) == sorted(_site_pages(site)), "each page once, none missing"
    for group in re.findall(r"^\*\*(.+)\*\*$",
                            (ROOT / "docs/wiki/Home.md").read_text(encoding="utf-8"), re.M):
        assert f'  - "{group}":' in config


def test_every_site_link_resolves_where_it_lands(site):
    docs = site / "docs"
    prefixes = (f"https://github.com/{REPO}/blob/{SITE_REF}/",
                f"https://github.com/{REPO}/tree/{SITE_REF}/")
    for filename, text in _site_pages(site).items():
        for target in _targets(text):
            path = target.split("#")[0]
            if not path or target.startswith(("winnow:", "mailto:")):
                continue
            if path.startswith(prefixes):
                rel = next(path[len(p):] for p in prefixes if path.startswith(p))
                assert (ROOT / rel).exists(), f"{filename}: {target}"
            elif re.match(r"https?://", path):
                assert "raw.githubusercontent.com" not in path, \
                    f"{filename}: {target} should be copied into the site"
            else:
                # A page or an image, beside the page in docs/.
                assert (docs / path).is_file(), f"{filename}: {target}"


def test_site_pages_open_their_source_on_develop_for_editing(site):
    for filename, text in _site_pages(site).items():
        m = re.match(r'---\nedit_url: "([^"]+)"\n', text)
        assert m, filename
        prefix = f"https://github.com/{REPO}/edit/develop/"
        assert m.group(1).startswith(prefix), filename
        assert (ROOT / m.group(1)[len(prefix):]).is_file(), filename
    assert _site_pages(site)["index.md"].startswith(
        f'---\nedit_url: "https://github.com/{REPO}/edit/develop/docs/wiki/Home.md"')


def test_the_theme_files_docs_site_names_are_staged(site):
    theme = (ROOT / "docs" / "site" / "mkdocs.yml").read_text(encoding="utf-8")
    named = re.findall(r"(assets/[\w./-]+)", theme)
    assert named
    for rel in named:
        assert (site / "docs" / rel).is_file(), rel


def _anchors(markdown):
    """The ids the site gives a page's headings: GitHub's, as mkdocs.yml
    asks pymdownx.slugs for (lower case, punctuation dropped, spaces to
    hyphens)."""
    out = set()
    for heading in re.findall(r"^#{1,6} (.+)$", markdown, re.M):
        text = re.sub(r"[^\w\s-]", "", heading.replace("`", "")).strip().lower()
        out.add(re.sub(r"\s", "-", text))
    return out


def test_every_link_on_the_front_page_lands(site):
    """MkDocs checks the links in pages, not in templates: the banner's,
    and each feature's picture and its section of the manual."""
    home = (ROOT / "docs" / "site" / "overrides" / "home.html").read_text(encoding="utf-8")
    named = re.findall(r"\{\{ '([^']+)' \| url \}\}", home)
    assert {"Installation/", "screenshots/triage.gif", "screenshots/grid.png",
            "screenshots/feature-pivot.png", "Plugins/"} <= set(named)
    docs = site / "docs"
    for rel in named:
        if rel.endswith("/"):
            assert (docs / f"{rel[:-1]}.md").is_file(), rel
        else:
            assert (docs / rel).is_file(), rel
    pages = _site_pages(site)
    for anchor in re.findall(r'href="#([\w-]+)"', home):
        assert anchor in _anchors(pages["index.md"]), anchor
    deep = re.findall(r"\{\{ '([^']+)/' \| url \}\}#([\w-]+)", home)
    assert ("Plugins", "first_last") in deep
    for page, anchor in deep:
        assert anchor in _anchors(pages[f"{page}.md"]), f"{page}/#{anchor}"


def test_the_front_page_is_homes_text_under_the_banner_with_cards(site):
    index = _site_pages(site)["index.md"]
    front, _, body = index.partition("\n---\n\n")
    assert "template: home.html" in front
    assert "hide:\n  - navigation\n  - toc" in front
    # The banner is the title, so Home.md's own goes; the rest stays.
    home = (ROOT / "docs/wiki/Home.md").read_text(encoding="utf-8")
    assert not body.lstrip().startswith("# ")
    assert body.lstrip().startswith(home.split("\n\n")[1].split("\n")[0])
    for heading in re.findall(r"^## .+$", home, re.M):
        assert heading in body
    # One card per group in Home's page list, each with an icon.
    cards = body[body.index('<div class="grid cards" markdown>'):body.index("</div>")]
    groups = re.findall(r"^\*\*(.+)\*\*$", home, re.M)
    assert re.findall(r'^-   <span class="twemoji lg middle" data-icon="[\w/-]+"></span> \*\*(.+)\*\*$',
                      cards, re.M) == groups
    for icon in re.findall(r'data-icon="([\w/-]+)"', cards):
        assert icon in build_wiki.SITE_GROUP_ICONS.values()


def test_the_front_page_hook_inlines_each_icon(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("site_hooks", ROOT / "docs" / "site" / "hooks.py")
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)
    (tmp_path / "material").mkdir()
    (tmp_path / "material" / "star.svg").write_text("<svg>*</svg>", encoding="utf-8")
    monkeypatch.setattr(hooks, "_icons_dir", lambda: tmp_path)
    html = '<li><span class="twemoji lg middle" data-icon="material/star"></span> <strong>G</strong></li>'
    assert hooks.on_page_content(html, page=None) == (
        '<li><span class="twemoji lg middle" data-icon="material/star"><svg>*</svg></span>'
        ' <strong>G</strong></li>')
    # A page with no icon is left alone, without looking for the theme.
    monkeypatch.setattr(hooks, "_icons_dir", lambda: 1 / 0)
    assert hooks.on_page_content("<p>hh:mm:ss</p>", page=None) == "<p>hh:mm:ss</p>"


def test_the_hook_sets_each_pages_edit_link_from_its_front_matter():
    spec = importlib.util.spec_from_file_location("site_hooks", ROOT / "docs" / "site" / "hooks.py")
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)

    class Page:
        def __init__(self, meta):
            self.meta, self.edit_url = meta, None

    page = Page({"edit_url": "https://example.com/edit/x.md"})
    assert hooks.on_page_markdown("text", page) == "text"
    assert page.edit_url == "https://example.com/edit/x.md"
    untouched = Page({})
    hooks.on_page_markdown("text", untouched)
    assert untouched.edit_url is None


def _build_site(root, tmp_path):
    out = tmp_path / "site-out"
    build_wiki.build_site(out, root=root, repo=REPO, ref="rel", edit_ref="dev")
    return out


def test_links_are_rewritten_for_the_site(tmp_path):
    root = _tree(tmp_path / "src", {
        "Home.md": HOME,
        "Other.md": (
            "# Other\n\n"
            "See [Home](Home.md#pages), [a wrapped\nlink](Home.md), "
            "[notes](../notes/README.md) and [top](#other).\n\n"
            "![shot](../shots/a.png)\n\n"
            "`[code](Missing.md)` stays.\n"
        ),
    }, extra=["docs/notes/README.md", "docs/shots/a.png"])
    out = _build_site(root, tmp_path)
    other = (out / "docs" / "Other.md").read_text(encoding="utf-8")
    assert other.startswith('---\nedit_url: "https://github.com/'
                            f'{REPO}/edit/dev/docs/wiki/Other.md"\n---\n\n# Other\n')
    assert "See [Home](index.md#pages)" in other
    assert "[a wrapped\nlink](index.md)" in other
    assert f"[notes](https://github.com/{REPO}/blob/rel/docs/notes/README.md)" in other
    assert "[top](#other)" in other and "`[code](Missing.md)`" in other
    assert "![shot](shots/a.png)" in other
    assert (out / "docs" / "shots" / "a.png").is_file()


def test_homes_page_list_becomes_cards_on_the_site(tmp_path):
    home = HOME.replace("## After", "**Odd one**\n\n- [Third](Third.md): more.\n\n## After")
    root = _tree(tmp_path / "src", {"Home.md": home, "Other.md": "# Other\n",
                                    "Third.md": "# Third\n"})
    index = (_build_site(root, tmp_path) / "docs" / "index.md").read_text(encoding="utf-8")
    body = index.split("\n---\n\n", 1)[1]
    assert body.startswith("Intro.\n\n## Pages\n")
    assert "# Home title" not in body
    icon = build_wiki.SITE_GROUP_ICONS[None]
    assert (
        '<div class="grid cards" markdown>\n'
        f'\n-   <span class="twemoji lg middle" data-icon="{icon}"></span> **Group**\n'
        "\n    ---\n\n"
        "    - [Other](Other.md): about it,\n"
        "      wrapped.\n"
    ) in body
    assert f'data-icon="{icon}"></span> **Odd one**' in body
    assert "    - [Third](Third.md): more.\n" in body
    assert body.endswith("</div>\n\n## After\n")


def test_the_site_nav_follows_homes_groups(tmp_path):
    home = HOME.replace("## After", "**Second**\n\n- [Third](Third.md): more.\n\n## After")
    root = _tree(tmp_path / "src", {"Home.md": home, "Other.md": "# Other\n",
                                    "Third.md": "# Third\n"})
    config = (_build_site(root, tmp_path) / "mkdocs.yml").read_text(encoding="utf-8")
    assert config.endswith(
        'nav:\n'
        '  - "Home": "index.md"\n'
        '  - "Group":\n'
        '      - "Other": "Other.md"\n'
        '  - "Second":\n'
        '      - "Third": "Third.md"\n')
    assert f"tree/dev/docs/wiki" in config and "<code>rel</code>" in config


def test_a_page_home_does_not_list_fails_the_site_too(tmp_path):
    root = _tree(tmp_path / "src", {"Home.md": HOME, "Other.md": "# Other\n",
                                    "Orphan.md": "# Orphan\n"})
    with pytest.raises(build_wiki.BuildError, match="does not list Orphan.md"):
        _build_site(root, tmp_path)
    assert not (tmp_path / "site-out").exists()


def test_table_pipes_lose_githubs_escape_only_inside_table_code(tmp_path):
    page = (
        "# Other\n\n"
        "| Type | Notes |\n| --- | --- |\n| `a\\|b` | plain a\\|b |\n\n"
        "Prose `x\\|y` keeps it.\n\n"
        "```\n| `fenced\\|kept` |\n```\n"
    )
    root = _tree(tmp_path / "src", {"Home.md": HOME, "Other.md": page})
    other = (_build_site(root, tmp_path) / "docs" / "Other.md").read_text(encoding="utf-8")
    assert "| `a|b` | plain a\\|b |" in other
    assert "Prose `x\\|y` keeps it." in other
    assert "| `fenced\\|kept` |" in other


def test_site_pages_are_not_left_with_escaped_table_pipes(site):
    for filename, text in _site_pages(site).items():
        for line in text.splitlines():
            if line.lstrip().startswith("|"):
                spans = [m.group(0) for m in CODE.finditer(line)]
                assert not any("\\|" in span for span in spans), f"{filename}: {line}"
