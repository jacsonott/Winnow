"""The three compressions a journal DATA object can carry.

journald compresses a field's payload once it passes a size threshold
(512 bytes by default), with whichever algorithm it was built for: XZ
(the oldest), LZ4 (systemd 221 to 245's default) or zstd (246 onward).
The object's flags say which. All three are read with no third-party
package: XZ through the standard library's lzma, LZ4 with the block
decoder below (systemd stores an LZ4 *block*, not a frame, behind an
8-byte length), zstd through `compression.zstd` on Python 3.14 and the
pure-Python decoder in zstd.py before that.
"""

from __future__ import annotations

from . import zstd as _pure_zstd

try:  # Python 3.14+
    from compression import zstd as _std_zstd
except ImportError:  # pragma: no cover - depends on the interpreter
    _std_zstd = None

try:
    import lzma as _lzma
except ImportError:  # pragma: no cover - a Python built without liblzma
    _lzma = None

COMPRESSED_XZ = 1 << 0
COMPRESSED_LZ4 = 1 << 1
COMPRESSED_ZSTD = 1 << 2
COMPRESSION_MASK = COMPRESSED_XZ | COMPRESSED_LZ4 | COMPRESSED_ZSTD

NAMES = {COMPRESSED_XZ: "XZ", COMPRESSED_LZ4: "LZ4", COMPRESSED_ZSTD: "zstd"}


class DecompressError(ValueError):
    pass


def decompress(flag: int, payload: bytes, limit: int | None = None) -> bytes:
    """`payload` as journald wrote it under object flag `flag`. `limit`
    bounds the work for a huge field: the result is at least that long
    when the field is, and may run past it."""
    try:
        if flag == COMPRESSED_ZSTD:
            return zstd(payload, limit)
        if flag == COMPRESSED_LZ4:
            return lz4(payload, limit)
        if flag == COMPRESSED_XZ:
            return xz(payload, limit)
    except DecompressError:
        raise
    except Exception as e:  # lzma.LZMAError, zstd.ZstdError, a stdlib ZstdError
        raise DecompressError(f"{NAMES.get(flag, 'compressed')} data is damaged ({e})") from None
    raise DecompressError(f"unknown compression flags 0x{flag:x}")


def zstd(payload: bytes, limit: int | None = None) -> bytes:
    if _std_zstd is not None:
        if limit is None:
            return _std_zstd.decompress(payload)
        d = _std_zstd.ZstdDecompressor()
        return d.decompress(payload, max_length=limit)
    return _pure_zstd.decompress(payload, limit)


def xz(payload: bytes, limit: int | None = None) -> bytes:
    if _lzma is None:
        raise DecompressError("this Python was built without lzma, so XZ fields can't be read")
    d = _lzma.LZMADecompressor(format=_lzma.FORMAT_XZ)
    return d.decompress(payload, max_length=-1 if limit is None else limit)


def lz4(payload: bytes, limit: int | None = None) -> bytes:
    """systemd's LZ4 layout: the uncompressed size as a little-endian
    64-bit number, then one raw LZ4 block."""
    if len(payload) < 8:
        raise DecompressError("LZ4 data is shorter than its length prefix")
    size = int.from_bytes(payload[:8], "little")
    out = lz4_block(payload, 8, size if limit is None else min(size, limit))
    if (limit is None or size <= limit) and len(out) != size:
        raise DecompressError(f"LZ4 data decoded to {len(out)} bytes, not the {size} it declares")
    return out


def lz4_block(src: bytes, pos: int = 0, stop_at: int | None = None) -> bytes:
    """One LZ4 block (the format LZ4_decompress_safe reads): sequences of a
    token, literals, a 16-bit back offset and a match, the last sequence
    literals only."""
    out = bytearray()
    n = len(src)
    try:
        while pos < n:
            token = src[pos]
            pos += 1
            ll = token >> 4
            if ll == 15:
                while True:
                    b = src[pos]
                    pos += 1
                    ll += b
                    if b != 255:
                        break
            if ll:
                if pos + ll > n:
                    raise DecompressError("LZ4 literals run past the end of the data")
                out += src[pos:pos + ll]
                pos += ll
            if pos >= n:
                break
            off = src[pos] | src[pos + 1] << 8
            pos += 2
            if not off or off > len(out):
                raise DecompressError("LZ4 match points before the start of the data")
            ml = token & 15
            if ml == 15:
                while True:
                    b = src[pos]
                    pos += 1
                    ml += b
                    if b != 255:
                        break
            ml += 4
            start = len(out) - off
            if off >= ml:
                out += out[start:start + ml]
            else:
                out += (out[start:] * (ml // off + 1))[:ml]
            if stop_at is not None and len(out) >= stop_at:
                break
    except IndexError:
        raise DecompressError("LZ4 data ends in the middle of a sequence") from None
    return bytes(out)
