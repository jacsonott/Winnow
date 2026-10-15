"""Turn docs/wiki/ into the pages of the repository's GitHub wiki.

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

    python scripts/build_wiki.py OUT_DIR [--repo OWNER/NAME] [--ref BRANCH]

Stdlib only.
"""

from __future__ import annotations

import argparse
import posixpath
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "docs/wiki"
DEFAULT_REPO = "jacsonott/Winnow"
DEFAULT_REF = "develop"

# A code span (left alone) or a link/image. Only the target is rewritten.
_TOKEN = re.compile(r"(`+)(?:[\s\S]+?)\1|(!?)\[([^\]]*)\]\(([^)\s]+)\)")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


class BuildError(Exception):
    pass


def _rewrite_target(target, *, image, page_names, root, repo, ref, where, errors):
    if target.startswith("#") or _SCHEME.match(target):
        return target
    path, hash_, anchor = target.partition("#")
    resolved = posixpath.normpath(posixpath.join(SOURCE, path))
    if resolved.startswith("../") or resolved == "..":
        errors.append(f"{where}: {target} points outside the repository")
        return target
    if posixpath.dirname(resolved) == SOURCE and resolved.endswith(".md"):
        name = posixpath.basename(resolved)[:-3]
        if name not in page_names:
            errors.append(f"{where}: {target} is not a page in {SOURCE}/")
            return target
        return name + hash_ + anchor
    on_disk = root / resolved
    if not on_disk.exists():
        errors.append(f"{where}: {target} names {resolved}, which does not exist")
        return target
    if image:
        return f"https://raw.githubusercontent.com/{repo}/{ref}/{resolved}"
    kind = "tree" if on_disk.is_dir() else "blob"
    return f"https://github.com/{repo}/{kind}/{ref}/{resolved}{hash_}{anchor}"


def convert(text, *, name, page_names, root, repo, ref, errors):
    """One page's markdown as the wiki should have it."""
    lines = text.splitlines()
    # The wiki already titles the page from its name.
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].startswith("# "):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)

    def sub(match):
        if match.group(1):
            return match.group(0)
        bang, label, target = match.group(2), match.group(3), match.group(4)
        new = _rewrite_target(
            target, image=bool(bang), page_names=page_names, root=root,
            repo=repo, ref=ref, where=f"{name}.md", errors=errors)
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


def sidebar(home_text, *, page_names, root, repo, ref, errors):
    """Home.md's "## Pages" list, without the descriptions."""
    lines = home_text.splitlines()
    try:
        start = lines.index("## Pages") + 1
    except ValueError:
        errors.append("Home.md: no '## Pages' section to build the sidebar from")
        return ""
    out = ["[Home](Home)", ""]
    listed = set()
    for line in lines[start:]:
        if line.startswith("## "):
            break
        if line.startswith("**"):
            if out[-1]:
                out.append("")
            out.extend([line.strip(), ""])
        elif line.startswith("- ["):
            m = _TOKEN.search(line)
            if not m or m.group(1):
                continue
            target = _rewrite_target(
                m.group(4), image=False, page_names=page_names, root=root,
                repo=repo, ref=ref, where="Home.md", errors=errors)
            out.append(f"- [{m.group(3)}]({target})")
            if "/" not in target:
                listed.add(target.partition("#")[0])
    for missing in sorted(page_names - listed - {"Home"}):
        errors.append(f"Home.md: '## Pages' does not list {missing}.md, "
                      "so the wiki's sidebar would have no way to it")
    return "\n".join(out).rstrip() + "\n"


def footer(repo, ref):
    return (
        f"This wiki is published from [`{SOURCE}/`]"
        f"(https://github.com/{repo}/tree/{ref}/{SOURCE}) on the `{ref}` "
        "branch and rebuilt every time it changes there, so edit the pages "
        "in the repository: an edit made here is overwritten by the next "
        "publish.\n"
    )


def build(out_dir, *, root=ROOT, repo=DEFAULT_REPO, ref=DEFAULT_REF):
    """Write the wiki's pages into out_dir. Raises BuildError listing every
    broken link and unlisted page, having written nothing."""
    src = root / SOURCE
    pages = {p.stem: p.read_text(encoding="utf-8") for p in sorted(src.glob("*.md"))}
    if "Home" not in pages:
        raise BuildError(f"{SOURCE}/Home.md is missing; the wiki has no front page")
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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out_dir", help="directory to write the wiki's pages into")
    ap.add_argument("--repo", default=DEFAULT_REPO,
                    help=f"OWNER/NAME for absolute links (default {DEFAULT_REPO})")
    ap.add_argument("--ref", default=DEFAULT_REF,
                    help=f"branch repository links point at (default {DEFAULT_REF})")
    args = ap.parse_args(argv)
    try:
        written = build(args.out_dir, repo=args.repo, ref=args.ref)
    except BuildError as e:
        print(f"build_wiki: not built:\n{e}", file=sys.stderr)
        return 1
    print(f"build_wiki: {len(written)} pages written to {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
