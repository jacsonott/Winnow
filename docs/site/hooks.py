"""MkDocs hook for the Pages site: "Edit this page" opens the page's source.

scripts/build_wiki.py --site writes each page's repository path into its
front matter as edit_url, because the site's names and the repository's
differ (Home.md is published as index.md, and writing-plugins.md comes
from docs/ rather than docs/wiki/), so MkDocs' single edit_uri can't
spell them all.
"""


def on_page_markdown(markdown, page, **kwargs):
    page.edit_url = page.meta.get("edit_url", page.edit_url)
    return markdown
