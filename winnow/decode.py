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
filtered or grouped on. The second is base64, which is how PowerShell's
-EncodedCommand, a JWT's parts and a good deal of malware configuration
hide text from a search.

Both keep a value they cannot decode exactly as written rather than
emptying it: the column reads as the source made readable where it could
be, and a value with nothing to decode is still the value.
"""

from __future__ import annotations

import base64
import binascii
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


# ------------------------------------------------------------------ base64

# Either alphabet — standard (+/) or URL-safe (-_), which JWTs and a lot of
# web tooling use — and up to two '=' of padding, or none.
_B64 = re.compile(r"[A-Za-z0-9+/_-]+={0,2}")
_B64_URLSAFE = str.maketrans("-_", "+/")
# What decoded bytes must not hold to count as text: C0 controls other
# than tab and the line breaks, DEL and C1. With strict decoding, this is
# what keeps binary, and most ordinary values that happen to be valid
# base64, from being "decoded" into noise: "AAAA" is three NULs, and a
# number or a hash rarely decodes to UTF-8. Not all of them — "WS01" is
# "Y-5", which nothing can tell from text — so the op is for a column
# that holds base64, not for finding it.
_NOT_TEXT = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _b64_bytes(s: str) -> bytes | None:
    """The bytes a value encodes, or None when it isn't base64. Line breaks
    are dropped first (MIME wraps base64 at 76 characters); any other
    character outside the alphabet, padding anywhere but the end, or a
    length no base64 can have, is not base64."""
    s = s.strip().replace("\r", "").replace("\n", "")
    if not _B64.fullmatch(s):
        return None
    body = s.rstrip("=")
    if len(body) % 4 == 1:
        return None
    try:
        return base64.b64decode(body.translate(_B64_URLSAFE) + "=" * (-len(body) % 4), validate=True)
    except (binascii.Error, ValueError):
        return None


def _looks_utf16le(raw: bytes) -> bool:
    """UTF-16LE text that is mostly Latin has a zero in the high byte of
    nearly every unit — PowerShell's -EncodedCommand is the case that
    matters. Half the units is the bar, so a command line with some
    non-Latin text in it still reads as UTF-16."""
    if len(raw) < 2 or len(raw) % 2:
        return False
    high = raw[1::2]
    return high.count(0) * 2 >= len(high)


def _bytes_to_text(raw: bytes, encoding: str) -> str | None:
    if encoding == "auto":
        if raw.startswith(b"\xef\xbb\xbf"):
            raw, encoding = raw[3:], "utf-8"
        elif raw.startswith(b"\xff\xfe"):
            raw, encoding = raw[2:], "utf-16le"
        else:
            encoding = "utf-16le" if _looks_utf16le(raw) else "utf-8"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        return None
    if not text or _NOT_TEXT.search(text):
        return None
    return text


def _base64_decode(value, params: dict, state: dict) -> str | None:
    """The text a base64 value encodes, or the value as written when it
    isn't base64 or what it encodes isn't text. Not a parse failure in
    either case, the same rule url_decode follows."""
    if value is None:
        return None
    s = value if isinstance(value, str) else str(value)
    if not s:
        return None
    raw = _b64_bytes(s)
    if raw is None:
        return s
    text = _bytes_to_text(raw, params.get("encoding") or "auto")
    return s if text is None else text


register_op({
    "id": "base64_decode",
    "label": "Base64 decode",
    "description": "Turn base64 (standard or URL-safe, with or without = padding) back into the text it "
                   "encodes — PowerShell's -EncodedCommand, a token, an encoded header. A value that isn't "
                   "base64, or that encodes something other than text, is kept as written.",
    "params": [{
        "name": "encoding", "label": "Text encoding", "type": "select",
        "options": ["auto", "utf-8", "utf-16le"], "default": "auto",
        "help": "auto reads UTF-8, or UTF-16LE when the bytes look like it (PowerShell's "
                "-EncodedCommand is UTF-16LE). Pick one to force it.",
    }],
    "parse": _base64_decode,
    "value_type": "text",
    "derived_kind": "text",
    "family": "decode",
    "hidden_from_detect": True,
})
