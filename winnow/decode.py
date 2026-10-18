"""Derived-column operations that decode a value written in an encoding.

Registered into timeparse.OPERATIONS through register_op, the same seam
structparse.py, combine.py and enrich.py use — a derived column needs the
same materialisation, sidecar and lifecycle whatever produced it. Family
"decode", its own type in the Add-derived-column modal. Stdlib only;
imports timeparse and nothing else from the app. store.py imports this
module for the registration side effect.

The first of these is URL decoding — IIS and proxy logs, browser history
and web shells all write the request as it went over the wire, and
`cmd%3Dwhoami%26x%3D%2Fetc%2Fpasswd` has to be read before it can be
filtered or grouped on.
"""

from __future__ import annotations

import re

from .timeparse import register_op

# A run of consecutive escapes is decoded together: a multi-byte UTF-8
# character is spelled as several of them (%C3%A9 is é), so escapes
# decoded one at a time could only ever produce Latin-1.
_ESCAPE_RUN = re.compile(r"(?:%[0-9A-Fa-f]{2})+")
# What surrogateescape writes for a byte that is not part of valid UTF-8.
_UNDECODED_BYTE = re.compile("[\udc80-\udcff]")


def _utf8_len(cp: int) -> int:
    return 1 if cp < 0x80 else 2 if cp < 0x800 else 3 if cp < 0x10000 else 4


def _decode_run(m: re.Match) -> str:
    """One run of %XX escapes as UTF-8 text, with every byte that is not
    part of a valid UTF-8 sequence kept as the escape it was written as.

    Kept, not replaced with U+FFFD: `caf%E9` (Latin-1) and the overlong
    `%C0%AE%C0%AE%2F` of a directory traversal are exactly the values an
    analyst is decoding a column to find, and a replacement character
    would turn both into the same unreadable mark. Kept per escape rather
    than failing the whole value, so the `%2F` beside them still decodes.
    The text is the cell's own spelling (case of the hex included), so a
    value with nothing decodable in it comes out exactly as it went in."""
    text = m.group()
    out = bytes.fromhex(text.replace("%", "")).decode("utf-8", "surrogateescape")
    if not _UNDECODED_BYTE.search(out):
        return out
    parts = []
    pos = 0     # bytes consumed, so text[3*pos:] is the next escape
    for ch in out:
        cp = ord(ch)
        if 0xDC80 <= cp <= 0xDCFF:
            parts.append(text[3 * pos:3 * pos + 3])
            pos += 1
        else:
            parts.append(ch)
            pos += _utf8_len(cp)
    return "".join(parts)


def _url_decode(value, params: dict, state: dict) -> str | None:
    """Percent-decoding, form style by default: `+` is a space, then every
    %XX run is decoded as UTF-8. The `+` goes first so an encoded plus
    (%2B) survives as a plus. A value with no escapes, or with escapes
    that are not valid (`100%`, `%zz`), is kept as written rather than
    counted as a failure — it is still the value, just not an encoded one."""
    if value is None:
        return None
    s = value if isinstance(value, str) else str(value)
    if not s:
        return None
    if params.get("plus_as_space", True):
        s = s.replace("+", " ")
    if "%" not in s:
        return s
    return _ESCAPE_RUN.sub(_decode_run, s)


register_op({
    "id": "url_decode",
    "label": "URL decode",
    "description": "Turn percent-encoding (%20, %2F, %C3%A9) back into the text it stands for — "
                   "a request path, a query string, a form post. An escape that isn't valid UTF-8 "
                   "is kept as written, so nothing in the value is lost.",
    "params": [{
        "name": "plus_as_space", "label": "Treat + as a space", "type": "bool", "default": True,
        "help": "On for query strings and form posts, which send a space as +. "
                "Off for a URL path, where + is a plus. An encoded plus (%2B) is a plus either way.",
    }],
    "parse": _url_decode,
    "value_type": "text",
    "derived_kind": "text",
    "family": "decode",
    "hidden_from_detect": True,
})
