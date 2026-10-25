"""Turn docs/wiki/ into the pages of the repository's GitHub wiki, or into
the source of its GitHub Pages site.

docs/wiki/ is the source. It is reviewed with the code, ships in the
release zip, and reads correctly when browsed in the repository. The
GitHub wiki is a published copy of it, rebuilt by
.github/workflows/wiki.yml on every push to develop that changes it, so an
edit made in the wiki itself is overwritten by the next publish.

The two places need different links. In the repository a page links to
`Installation.md` and to `../writing-plugins.md`; in the wiki a page is
just `Installation`, and nothing outside docs/wiki is in the wiki at all.
So each page is copied with

- links to another wiki page stripped of `.md` (anchors kept),
- links to any other repository file, and images, made absolute against
  the branch being published,
- its leading `# Title` dropped, because the wiki prints the page name
  as the title already,

and two pages are generated: _Sidebar.md (Home.md's page list, which is
why a page Home.md doesn't list fails the build: the wiki would have no
way to it) and _Footer.md (where to edit).

A link to a file that doesn't exist fails the build too, rather than
publishing a dead link. Links inside code spans and fenced blocks are
left alone.

With --site, OUT_DIR gets what MkDocs needs to build the Pages site
instead. The site is the manual for the released version, so
.github/workflows/pages.yml builds it from main, at each release, with
--ref main; "Edit this page" still opens the page on develop
(--edit-ref), where changes to it land. OUT_DIR gets the same pages under
docs/, with docs/writing-plugins.md as a page of its own, the images they
show copied in beside them, and a generated mkdocs.yml whose nav is
Home.md's page list, so Home.md stays the one place a page is listed.
Page links keep their `.md` (MkDocs resolves them), titles stay, and
links to the rest of the repository go to GitHub as in the wiki. Home.md
becomes index.md, the front page, which is the one page that changes
shape: a banner (docs/site/overrides/home.html) stands in for its title,
and its page list is shown as a card per group. The site's look, which
doesn't change from build to build, is docs/site/mkdocs.yml, which the
generated file INHERITs.

The site also has pages the wiki doesn't: one per feature the front page
lists (docs/site/features/, in SITE_FEATURES' order), each a short
recording and a few lines with a link into the manual. They are
features/ on the site and the first group in its nav, and their pictures
are docs/site/media/, published as media/. Neither folder is in the wiki,
or in the release zip (.gitattributes).

    python scripts/build_wiki.py OUT_DIR [--repo OWNER/NAME] [--ref BRANCH]
    python scripts/build_wiki.py --site OUT_DIR --ref main [--edit-ref BRANCH]
    mkdocs build --strict -f OUT_DIR/mkdocs.yml     # the site, in OUT_DIR/site

Stdlib only; MkDocs is needed for the last step and nothing else.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "docs/wiki"
SITE_CONFIG = "docs/site"
DEFAULT_REPO = "jacsonott/Winnow"
DEFAULT_REF = "develop"

# Pages the site carries besides docs/wiki's own, by repository path, and
# the name each has there. A link to one of these from a wiki page stays a
# page link on the site; in the wiki it is a link to GitHub, as before.
SITE_EXTRA_PAGES = {"docs/writing-plugins.md": "writing-plugins.md"}

# Files the theme uses rather than a page, copied in at build time rather
# than kept twice: the app's brand mark as logo and favicon (named in
# docs/site/mkdocs.yml), and the README's picture of the grid, which the
# front page's banner (docs/site/overrides/home.html) shows a reader who
# asked for less motion. The site's own pictures, docs/site/media/, are
# copied in whole.
SITE_ASSETS = {"static/icons/winnow-mark.svg": "assets/winnow-mark.svg",
               "static/icons/winnow-icon.svg": "assets/favicon.svg",
               "docs/screenshots/grid.png": "screenshots/grid.png"}

# The feature pages, in the order the front page lists them and the nav
# shows them: docs/site/features/<name>.md, published as
# features/<name>.md. Each shows docs/site/media/feature-<name>.gif, and
# the front page shows feature-<name>.png; scripts/screenshots.py makes
# both.
SITE_FEATURES = ("saved-filters", "timeframe", "histogram", "derived-columns",
                 "sql-pane", "first-last", "pivot")
SITE_FEATURES_DIR = f"{SITE_CONFIG}/features"

# The icon on each of the front page's section cards, by the group's name
# in Home.md: a name in Material's icon set, which docs/site/hooks.py
# inlines. A group not named here gets the last one.
SITE_GROUP_ICONS = {
    "Getting set up": "material/rocket-launch-outline",
    "Working a case": "material/filter-variant",
    "Going further": "material/chart-box-outline",
    "Reference": "material/book-open-variant",
    None: "material/file-document-outline",
}

# A code span (left alone) or a link/image. Only the target is rewritten.
_TOKEN = re.compile(r"(`+)(?:[\s\S]+?)\1|(!?)\[([^\]]*)\]\(([^)\s]+)\)")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


class BuildError(Exception):
    pass


def _rewrite_target(target, *, image, pages, root, repo, ref, where, errors,
                    base=SOURCE, images=None, at=""):
    """One link target as the output spells it.

    `pages` maps the repository path of each page the output carries to
    where it is in the output. `images` is None to link images from
    GitHub; otherwise each image is added to it (repository path -> path
    in the output) and linked where it will be copied to. `at` is the
    folder in the output of the page the link is on: page and image links
    are written relative to it.
    """
    if target.startswith("#") or _SCHEME.match(target):
        return target
    path, hash_, anchor = target.partition("#")
    resolved = posixpath.normpath(posixpath.join(base, path))
    if resolved.startswith("../") or resolved == "..":
        errors.append(f"{where}: {target} points outside the repository")
        return target
    if resolved in pages:
        return _from(at, pages[resolved]) + hash_ + anchor
    if posixpath.dirname(resolved) == SOURCE and resolved.endswith(".md"):
        errors.append(f"{where}: {target} is not a page in {SOURCE}/")
        return target
    on_disk = root / resolved
    if not on_disk.exists():
        errors.append(f"{where}: {target} names {resolved}, which does not exist")
        return target
    if image and images is not None:
        # A picture under docs/site/ (the site's own) or docs/ keeps its
        # path below that folder.
        top = next((d for d in (SITE_CONFIG, "docs") if resolved.startswith(d + "/")), None)
        dest = posixpath.relpath(resolved, top) if top else resolved
        images[resolved] = dest
        return _from(at, dest)
    if image:
        return f"https://raw.githubusercontent.com/{repo}/{ref}/{resolved}"
    kind = "tree" if on_disk.is_dir() else "blob"
    return f"https://github.com/{repo}/{kind}/{ref}/{resolved}{hash_}{anchor}"


def _from(at, path):
    """`path`, a path in the output, as a link from a page in folder `at`."""
    return posixpath.relpath(path, at) if at else path


def _relink(lines, *, where, errors, **target_kw):
    """The lines with every link target outside code rewritten."""

    def sub(match):
        if match.group(1):
            return match.group(0)
        bang, label, target = match.group(2), match.group(3), match.group(4)
        new = _rewrite_target(target, image=bool(bang), where=where,
                              errors=errors, **target_kw)
        return f"{bang}[{label}]({new})"

    # Rewritten a paragraph at a time, not a line at a time: link text
    # wraps ("[Sessions and\nexport](Sessions-and-export.md)"), and so can
    # a code span. Fenced blocks are copied as they are.
    out, para, fence = [], [], None

    def flush():
        if para:
            out.extend(_TOKEN.sub(sub, "\n".join(para)).split("\n"))
            para.clear()

    for line in lines:
        stripped = line.lstrip()
        if fence:
            if stripped.startswith(fence):
                fence = None
            out.append(line)
            continue
        m = re.match(r"(`{3,}|~{3,})", stripped)
        if m:
            flush()
            fence = m.group(1)
            out.append(line)
        elif not stripped:
            flush()
            out.append(line)
        else:
            para.append(line)
    flush()
    return "\n".join(out) + "\n"


def _drop_title(lines):
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].startswith("# "):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    return lines


def _wiki_pages(page_names):
    return {f"{SOURCE}/{name}.md": name for name in page_names}


def convert(text, *, name, page_names, root, repo, ref, errors):
    """One page's markdown as the wiki should have it."""
    # The wiki already titles the page from its name.
    lines = _drop_title(text.splitlines())
    return _relink(lines, pages=_wiki_pages(page_names), root=root, repo=repo,
                   ref=ref, where=f"{name}.md", errors=errors)


def _page_list(home_text, errors):
    """Home.md's "## Pages" list: [(group or None, [(label, target)])],
    targets as Home.md writes them. None, and an error, if it has none."""
    lines = home_text.splitlines()
    try:
        start = lines.index("## Pages") + 1
    except ValueError:
        errors.append("Home.md: no '## Pages' section to build the sidebar from")
        return None
    groups = [(None, [])]
    for line in lines[start:]:
        if line.startswith("## "):
            break
        if line.startswith("**"):
            groups.append((line.strip(), []))
        elif line.startswith("- ["):
            m = _TOKEN.search(line)
            if m and not m.group(1):
                groups[-1][1].append((m.group(3), m.group(4)))
    return [g for g in groups if g[0] or g[1]]


def _check_listed(listed, page_names, errors):
    for missing in sorted(page_names - listed - {"Home"}):
        errors.append(f"Home.md: '## Pages' does not list {missing}.md, "
                      "so the wiki's sidebar would have no way to it")


def sidebar(home_text, *, page_names, root, repo, ref, errors):
    """Home.md's "## Pages" list, without the descriptions."""
    groups = _page_list(home_text, errors)
    if groups is None:
        return ""
    out = ["[Home](Home)", ""]
    listed = set()
    for group, items in groups:
        if group:
            if out[-1]:
                out.append("")
            out.extend([group, ""])
        for label, target in items:
            target = _rewrite_target(
                target, image=False, pages=_wiki_pages(page_names), root=root,
                repo=repo, ref=ref, where="Home.md", errors=errors)
            out.append(f"- [{label}]({target})")
            if "/" not in target:
                listed.add(target.partition("#")[0])
    _check_listed(listed, page_names, errors)
    return "\n".join(out).rstrip() + "\n"


def footer(repo, ref):
    return (
        f"This wiki is published from [`{SOURCE}/`]"
        f"(https://github.com/{repo}/tree/{ref}/{SOURCE}) on the `{ref}` "
        "branch and rebuilt every time it changes there, so edit the pages "
        "in the repository: an edit made here is overwritten by the next "
        "publish.\n"
    )


# Where the Pages site is served. The project's own is on a custom domain,
# which is set in the repository's Settings → Pages: a site deployed by
# Actions ignores a CNAME file, so that setting is what serves it there, and
# this is only the address the pages say they are at (site_url: canonical
# links, the sitemap, the 404 page's assets). A fork's build gets the
# address GitHub gives any project's Pages site.
SITE_DOMAINS = {DEFAULT_REPO: "winnow.jacson.tech"}


def site_url(repo):
    if repo in SITE_DOMAINS:
        return f"https://{SITE_DOMAINS[repo]}/"
    owner, name = repo.split("/")
    return f"https://{owner.lower()}.github.io/{name}/"


def _read_pages(root):
    src = root / SOURCE
    pages = {p.stem: p.read_text(encoding="utf-8") for p in sorted(src.glob("*.md"))}
    if "Home" not in pages:
        raise BuildError(f"{SOURCE}/Home.md is missing; the wiki has no front page")
    return pages


def build(out_dir, *, root=ROOT, repo=DEFAULT_REPO, ref=DEFAULT_REF):
    """Write the wiki's pages into out_dir. Raises BuildError listing every
    broken link and unlisted page, having written nothing."""
    pages = _read_pages(root)
    names = set(pages)
    errors = []
    built = {
        f"{name}.md": convert(text, name=name, page_names=names, root=root,
                              repo=repo, ref=ref, errors=errors)
        for name, text in pages.items()
    }
    built["_Sidebar.md"] = sidebar(pages["Home"], page_names=names, root=root,
                                   repo=repo, ref=ref, errors=errors)
    built["_Footer.md"] = footer(repo, ref)
    if errors:
        raise BuildError("\n".join(errors))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for filename, text in built.items():
        (out_dir / filename).write_text(text, encoding="utf-8")
    return sorted(built)


# --- the Pages site ----------------------------------------------------------

_CODE_SPAN = re.compile(r"(`+)[\s\S]+?\1")


def _table_pipes(text):
    """GitHub needs a pipe in a table's code span escaped (`a\\|b`), or it
    splits the cell, and prints it without the backslash. Python-Markdown
    already keeps a code span's pipes in their cell, and prints the
    backslash, so the site's copy drops it."""
    out, fence = [], None
    for line in text.split("\n"):
        stripped = line.lstrip()
        if fence:
            if stripped.startswith(fence):
                fence = None
        elif m := re.match(r"(`{3,}|~{3,})", stripped):
            fence = m.group(1)
        elif stripped.startswith("|"):
            line = _CODE_SPAN.sub(lambda c: c.group(0).replace("\\|", "|"), line)
        out.append(line)
    return "\n".join(out)


def _front_page(text):
    """Home.md as the site's front page. Its title goes, because the
    banner in docs/site/overrides/home.html is the title there, and its
    "## Pages" list becomes a grid of cards, one per group, each listing
    its pages with their descriptions. The rest is Home.md as written."""
    lines = _drop_title(text.splitlines())
    try:
        start = lines.index("## Pages") + 1
    except ValueError:
        return "\n".join(lines)
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")),
               len(lines))
    def card(name):
        icon = SITE_GROUP_ICONS.get(name, SITE_GROUP_ICONS[None])
        title = f" **{name}**" if name else ""
        return ["", f'-   <span class="twemoji lg middle" data-icon="{icon}"></span>{title}',
                "", "    ---"]

    out, in_card = ['', '<div class="grid cards" markdown>', ''], False
    for line in lines[start:end]:
        if line.startswith("**"):
            out += card(line.strip().strip("*"))
            in_card = True
        elif line.strip():
            if not in_card:
                out += card(None)
                in_card = True
            out.append("    " + line)
        else:
            out.append("")
    out += ["", "</div>", ""]
    block = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).split("\n")
    return "\n".join(lines[:start] + block + lines[end:])


def _q(text):
    """A YAML double-quoted scalar. JSON's escapes are a subset of YAML's."""
    return json.dumps(text)


def _site_config(nav, *, root, repo, ref, edit_ref):
    """The mkdocs.yml MkDocs reads: per-build settings and the nav, on top
    of docs/site/mkdocs.yml."""
    config_dir = (root / SITE_CONFIG).resolve()
    lines = [
        "# Generated by scripts/build_wiki.py --site. Edit docs/wiki/Home.md",
        "# for the nav and docs/site/mkdocs.yml for everything else.",
        f"INHERIT: {_q(str(config_dir / 'mkdocs.yml'))}",
        "docs_dir: docs",
        "site_dir: site",
        f"site_url: {_q(site_url(repo))}",
        f"repo_url: {_q(f'https://github.com/{repo}')}",
        f"repo_name: {_q(repo)}",
        # Each page names its own source (edit_url, read by hooks.py):
        # Home.md is published as index.md, so no one edit_uri fits all.
        'edit_uri: ""',
        "hooks:",
        f"  - {_q(str(config_dir / 'hooks.py'))}",
        "theme:",
        f"  custom_dir: {_q(str(config_dir / 'overrides'))}",
        "copyright: " + _q(
            f"Built from <code>{SOURCE}/</code> on <code>{ref}</code>. "
            f'Edit the pages on <a href="https://github.com/{repo}/tree/{edit_ref}/{SOURCE}">'
            f"<code>{edit_ref}</code></a>."),
        "nav:",
    ]
    for group, items in nav:
        if group is None:
            for label, target in items:
                lines.append(f"  - {_q(label)}: {_q(target)}")
            continue
        lines.append(f"  - {_q(group)}:")
        for label, target in items:
            lines.append(f"      - {_q(label)}: {_q(target)}")
    return "\n".join(lines) + "\n"


def build_site(out_dir, *, root=ROOT, repo=DEFAULT_REPO, ref=DEFAULT_REF,
               edit_ref=DEFAULT_REF):
    """Write MkDocs' input for the Pages site into out_dir: mkdocs.yml and
    docs/. Links into the repository point at `ref`, "Edit this page" at
    `edit_ref`. Raises BuildError as build() does, having written nothing."""
    pages = _read_pages(root)
    names = set(pages)
    sources, site_pages = {}, {}
    for name, text in pages.items():
        sources[f"{SOURCE}/{name}.md"] = text
        site_pages[f"{SOURCE}/{name}.md"] = "index.md" if name == "Home" else f"{name}.md"
    for path, dest in SITE_EXTRA_PAGES.items():
        on_disk = root / path
        if on_disk.exists():
            sources[path] = on_disk.read_text(encoding="utf-8")
            site_pages[path] = dest

    errors, images = [], {}
    features = []
    for name in SITE_FEATURES:
        path = f"{SITE_FEATURES_DIR}/{name}.md"
        if (root / path).exists():
            sources[path] = (root / path).read_text(encoding="utf-8")
            site_pages[path] = f"features/{name}.md"
            features.append(path)
    if (root / SITE_FEATURES_DIR).is_dir():
        for unlisted in sorted((root / SITE_FEATURES_DIR).glob("*.md")):
            if unlisted.stem not in SITE_FEATURES:
                errors.append(f"{SITE_FEATURES_DIR}/{unlisted.name}: not in SITE_FEATURES, "
                              "so the site's nav would have no way to it")

    built = {}
    for path, text in sources.items():
        meta = [f"edit_url: {_q(f'https://github.com/{repo}/edit/{edit_ref}/{path}')}"]
        if site_pages[path] == "index.md":
            text = _front_page(text)
            # The banner, and room for the cards: the cards are the nav
            # here, and Home has three headings to list.
            meta += ["template: home.html", "hide:", "  - navigation", "  - toc"]
        elif path in features:
            # A recording and a few lines, under no heading but the title:
            # the column the contents list would take goes to the picture.
            meta += ["hide:", "  - toc"]
        body = _relink(text.splitlines(), pages=site_pages, root=root, repo=repo,
                       ref=ref, where=posixpath.basename(path), errors=errors,
                       base=posixpath.dirname(path), images=images,
                       at=posixpath.dirname(site_pages[path]))
        built[site_pages[path]] = ("---\n" + "\n".join(meta) + "\n---\n\n"
                                   + _table_pipes(body))

    nav, listed = [(None, [("Home", "index.md")])], set()
    if features:
        entries = []
        for path in features:
            title = re.match(r"# (.+)", sources[path])
            if not title:
                errors.append(f"{path}: starts with no '# Title' to name it by in the nav")
            entries.append((title.group(1) if title else path, site_pages[path]))
        nav.append(("Features", entries))
    for group, items in _page_list(pages["Home"], errors) or []:
        entries = []
        for label, target in items:
            target = _rewrite_target(
                target, image=False, pages=site_pages, root=root, repo=repo,
                ref=ref, where="Home.md", errors=errors)
            entries.append((label, target))
            listed.add(target.partition("#")[0].removesuffix(".md"))
        nav.append((group.strip("*") if group else None, entries))
    _check_listed(listed, names, errors)
    if errors:
        raise BuildError("\n".join(errors))

    out_dir = Path(out_dir)
    docs = out_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (out_dir / "mkdocs.yml").write_text(
        _site_config(nav, root=root, repo=repo, ref=ref, edit_ref=edit_ref),
        encoding="utf-8")
    for filename, text in built.items():
        (docs / filename).parent.mkdir(parents=True, exist_ok=True)
        (docs / filename).write_text(text, encoding="utf-8")
    for src, dest in images.items():
        (docs / dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / src, docs / dest)
    # The theme's own files go to docs/assets/, where docs/site/mkdocs.yml
    # points, and the site's pictures to docs/media/, where its pages and
    # front page link.
    for folder in ("assets", "media"):
        if (root / SITE_CONFIG / folder).is_dir():
            shutil.copytree(root / SITE_CONFIG / folder, docs / folder, dirs_exist_ok=True)
    for src, dest in SITE_ASSETS.items():
        if (root / src).exists():
            (docs / dest).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / src, docs / dest)
    return sorted(built)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out_dir", help="directory to write the wiki's pages into")
    ap.add_argument("--site", action="store_true",
                    help="write the Pages site's MkDocs input instead of wiki pages")
    ap.add_argument("--repo", default=DEFAULT_REPO,
                    help=f"OWNER/NAME for absolute links (default {DEFAULT_REPO})")
    ap.add_argument("--ref", default=DEFAULT_REF,
                    help=f"branch repository links point at (default {DEFAULT_REF})")
    ap.add_argument("--edit-ref", default=DEFAULT_REF,
                    help="with --site: branch \"Edit this page\" opens "
                         f"(default {DEFAULT_REF}, where pull requests go)")
    args = ap.parse_args(argv)
    try:
        if args.site:
            written = build_site(args.out_dir, repo=args.repo, ref=args.ref,
                                 edit_ref=args.edit_ref)
        else:
            written = build(args.out_dir, repo=args.repo, ref=args.ref)
    except BuildError as e:
        print(f"build_wiki: not built:\n{e}", file=sys.stderr)
        return 1
    print(f"build_wiki: {len(written)} pages written to {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
