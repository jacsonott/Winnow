"""Hostile Markdown in case Notes renders as text, never as markup.

The preview is `innerHTML = renderMarkdown(...)`, and its escaper used to
leave quotes alone while a link's URL went straight into `href="..."`.
`[x](https://host"onmouseover="...)` therefore broke out of the attribute
and ran script when the link was hovered. Notes are stored in the case
file and travel with it — to the colleague doing QC, who opens the case
and reads them — and pasting a URL out of evidence into a note as a link
is routine. Script in the page can call every local API.

Asserted as the symptom: nothing in the rendered preview carries an event
handler or is an element the renderer never makes, no hostile payload
sets its flag even when its link is hovered, and the ordinary forms still
render as themselves.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

# Each payload, if it ever runs, sets window.__xss to its own name.
HOSTILE = "\n".join([
    # The reported one: a double quote ends href and starts a handler.
    '[dq](https://example.test/"onmouseover="window.__xss=\'href-dq\'"x=")',
    # Same with single quotes, and with the handler glued on.
    "[sq](https://example.test/'onmouseover='window.__xss=`href-sq`')",
    '[glued](https://example.test/"/onmouseover="window.__xss=\'glued\'")',
    # Emphasis inside the URL used to be rewritten into the href too.
    '[em](https://example.test/*a"onmouseover="window.__xss=\'em\'"*)',
    # Not a scheme the renderer links, so it must stay text.
    "[js](javascript:window.__xss='js')",
    # The in-app form only takes kind/digits.
    '[w](winnow:table/1"onmouseover="window.__xss=\'winnow\'")',
    # Markup in every inline and block context.
    "[<img src=x onerror=window.__xss='label'>](https://example.test/ok)",
    "`<img src=x onerror=window.__xss='code'>`",
    "# <svg onload=window.__xss='heading'>",
    "> <img src=x onerror=window.__xss='quote'>",
    "- <img src=x onerror=window.__xss='list'>",
    "| <img src=x onerror=window.__xss='th'> | b |",
    "|---|---|",
    "| <img src=x onerror=window.__xss='td'> | [t](https://example.test/\"onmouseover=\"window.__xss='td-link') |",
    "```",
    "<script>window.__xss='fence'</script>",
    "```",
    "**<iframe src=javascript:window.__xss='strong'></iframe>**",
])


def _preview(page, text):
    page.locator("#tabNotes").click()
    page.wait_for_selector("#notesview:not([hidden])")
    page.locator("#notesEditor").fill(text)
    page.locator("#btnNotesPreview").click()
    page.wait_for_selector("#notesPreviewBody > *")


def _clear(page):
    page.locator("#btnNotesEdit").click()
    page.locator("#notesEditor").fill("")
    page.wait_for_function(
        "() => document.getElementById('notesSaved').textContent === 'Saved'", timeout=6000)


def test_hostile_markdown_in_the_preview_runs_nothing(page):
    page.evaluate("() => { delete window.__xss; }")
    _preview(page, HOSTILE)
    try:
        # Hover every link: the reported payload only fires on mouseover.
        links = page.locator("#notesPreviewBody a")
        for i in range(links.count()):
            links.nth(i).hover()
        page.wait_for_timeout(300)   # let any onerror/onload that was going to fire, fire
        found = page.evaluate("""() => {
            const body = document.getElementById('notesPreviewBody');
            const handlers = [], foreign = [], hrefs = [];
            const made = new Set(['H1','H2','H3','H4','P','UL','LI','BLOCKQUOTE','PRE',
                                  'CODE','STRONG','EM','A','TABLE','THEAD','TBODY','TR','TH','TD']);
            for (const n of body.querySelectorAll('*')) {
                if (!made.has(n.tagName)) foreign.push(n.tagName);
                for (const a of n.attributes) if (/^on/i.test(a.name)) handlers.push(n.tagName + ' ' + a.name);
                if (n.tagName === 'A') hrefs.push(n.getAttribute('href'));
            }
            return { xss: window.__xss ?? null, handlers, foreign, hrefs };
        }""")
        assert found["xss"] is None, f"payload {found['xss']!r} ran"
        assert found["handlers"] == []
        assert found["foreign"] == []
        # Every link is either the in-app "#" or an http(s) URL — the
        # javascript: one and the doctored winnow: one are text.
        assert all(h == "#" or h.startswith(("http://", "https://")) for h in found["hrefs"]), found["hrefs"]
        assert "javascript:" in page.locator("#notesPreviewBody").inner_text()
    finally:
        _clear(page)


def test_the_ordinary_forms_still_render(page):
    """The fix must not cost the Markdown that analysts actually write: a
    URL with a query string keeps its `&`, quotes in prose stay quotes,
    a code span keeps its asterisks literal, and the in-app link is still
    an in-app link."""
    sid = page.evaluate("() => __winnow.S.sources.find((s) => !s.is_merge).id")
    _preview(page, "\n".join([
        "See [the report](https://example.test/r?a=1&b=it's) and "
        f"[the logons](winnow:table/{sid}).",
        'He said "run it" and it\'s **done**, *quickly*.',
        "Run `rundll32 a*b*c` then [`cmd`](https://example.test/cmd).",
    ]))
    try:
        ext = page.locator("#notesPreviewBody a[target=_blank]").first
        assert ext.get_attribute("href") == "https://example.test/r?a=1&b=it's"
        assert ext.inner_text() == "the report"
        assert page.locator("#notesPreviewBody a.notes-link").get_attribute("data-winnow") == f"table/{sid}"
        text = page.locator("#notesPreviewBody").inner_text()
        assert 'He said "run it" and it\'s done, quickly.' in text
        assert page.locator("#notesPreviewBody strong").first.inner_text() == "done"
        assert page.locator("#notesPreviewBody em").first.inner_text() == "quickly"
        assert page.locator("#notesPreviewBody code").first.inner_text() == "rundll32 a*b*c"
        assert page.locator("#notesPreviewBody a code").inner_text() == "cmd"
    finally:
        _clear(page)
