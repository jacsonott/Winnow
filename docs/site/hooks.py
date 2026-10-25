"""MkDocs hooks for the Pages site.

"Edit this page" opens the page's source. scripts/build_wiki.py --site
writes each page's repository path into its front matter as edit_url,
because the site's names and the repository's differ (Home.md is
published as index.md, and writing-plugins.md comes from docs/ rather
than docs/wiki/), so MkDocs' single edit_uri can't spell them all.

The front page's card icons are filled in from Material's own icon set
as inline SVG. build_wiki.py writes an empty span naming each one. The
pymdownx.emoji extension would do the same from `:material-name:`, but it
also turns every other :word: on every page into an emoji fetched from a
CDN, and the wiki writes times as hh:mm:ss.

A recording on a page (each feature page's GIF) with a still of the same
name beside it is shown as that still to a reader who asked for less
motion, as the front page's banner is. Markdown has no way to say so, and
the pages are written to read on GitHub too, so it is said here.
"""

import posixpath
import re
from pathlib import Path

_ICON = re.compile(r'(<span class="twemoji[^"]*" data-icon="([\w/-]+)">)(</span>)')
_GIF = re.compile(r'<img [^>]*?src="([^":]+)\.gif"[^>]*>')


def _icons_dir():
    import material  # only ever imported under MkDocs, where it's the theme

    return Path(material.__file__).parent / "templates" / ".icons"


def on_page_markdown(markdown, page, **kwargs):
    page.edit_url = page.meta.get("edit_url", page.edit_url)
    return markdown


def _stills(html, page, docs_dir):
    # The src is relative to the page's address, and docs_dir holds each
    # file at the address it is served from.
    here = page.url if page.url.endswith("/") or not page.url else posixpath.dirname(page.url)

    def swap(m):
        still = m.group(1) + ".png"
        if not (docs_dir / posixpath.normpath(posixpath.join(here, still))).is_file():
            return m.group(0)
        return (f'<picture class="wn-recording"><source media="(prefers-reduced-motion: reduce)"'
                f' srcset="{still}">{m.group(0)}</picture>')

    return _GIF.sub(swap, html)


def on_page_content(html, page, config=None, **kwargs):
    if config is not None and '.gif"' in html:
        html = _stills(html, page, Path(config["docs_dir"]))
    if 'data-icon="' not in html:
        return html
    icons = _icons_dir()
    return _ICON.sub(
        lambda m: m.group(1) + (icons / f"{m.group(2)}.svg").read_text(encoding="utf-8") + m.group(3),
        html)
