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
"""

import re
from pathlib import Path

_ICON = re.compile(r'(<span class="twemoji[^"]*" data-icon="([\w/-]+)">)(</span>)')


def _icons_dir():
    import material  # only ever imported under MkDocs, where it's the theme

    return Path(material.__file__).parent / "templates" / ".icons"


def on_page_markdown(markdown, page, **kwargs):
    page.edit_url = page.meta.get("edit_url", page.edit_url)
    return markdown


def on_page_content(html, page, **kwargs):
    if 'data-icon="' not in html:
        return html
    icons = _icons_dir()
    return _ICON.sub(
        lambda m: m.group(1) + (icons / f"{m.group(2)}.svg").read_text(encoding="utf-8") + m.group(3),
        html)
