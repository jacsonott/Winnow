"""Zstandard decompression in pure Python (RFC 8878), for journal fields.

systemd compresses a journal DATA object once its payload passes a
threshold (512 bytes by default), and since systemd 246 the algorithm is
zstd wherever systemd was built with it — every current distribution. So
the long values are the compressed ones: a Java service's _CMDLINE, a
sudo COMMAND, an audit record, a stack trace. Python reads zstd natively
only from 3.14 (`compression.zstd`); Winnow supports 3.10 and installs
nothing on an airgapped box, so this decoder is what reads those fields
everywhere else. `decompress` in decompress.py prefers the standard
library when it is there.

Decoding only, of what systemd writes: one or more frames, no
dictionary, the content checksum skipped rather than verified (the
journal has its own hashes, and a mismatch would be no reason to hide
the text). Speed is a few MB/s, which is plenty for fields that are
mostly under a kilobyte, and `limit` stops early for the rare huge one.

Every structure is checked as it is read; anything inconsistent raises
ZstdError (a ValueError), never IndexError or a wrong answer.
"""

from __future__ import annotations

__all__ = ["ZstdError", "decompress"]


class ZstdError(ValueError):
    pass


_FRAME_MAGIC = 0xFD2FB528
_MAX_BLOCK = 128 * 1024

# Literals_Length and Match_Length codes: baseline and extra bits
# (RFC 8878 §3.1.1.3.2.1.1).
_LL_BASE = list(range(16)) + [16, 18, 20, 22, 24, 28, 32, 40, 48, 64, 128, 256, 512,
                              1024, 2048, 4096, 8192, 16384, 32768, 65536]
_LL_BITS = [0] * 16 + [1, 1, 1, 1, 2, 2, 3, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
_ML_BASE = list(range(3, 35)) + [35, 37, 39, 41, 43, 47, 51, 59, 67, 83, 99, 131, 259, 515,
                                 1027, 2051, 4099, 8195, 16387, 32771, 65539]
_ML_BITS = [0] * 32 + [1, 1, 1, 1, 2, 2, 3, 3, 4, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]

# The predefined distributions (§3.1.1.3.2.2) and their accuracy logs.
_LL_DEFAULT = ([4, 3, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2,
                2, 3, 2, 1, 1, 1, 1, 1, -1, -1, -1, -1], 6)
_ML_DEFAULT = ([1, 4, 3, 2, 2, 2, 2, 2, 2] + [1] * 37 + [-1] * 7, 6)
_OF_DEFAULT = ([1, 1, 1, 1, 1, 1, 2, 2, 2] + [1] * 15 + [-1] * 5, 5)

# name: (predefined, largest accuracy log, largest symbol)
_SEQ_KINDS = {
    "ll": (_LL_DEFAULT, 9, 35),
    "of": (_OF_DEFAULT, 8, 31),
    "ml": (_ML_DEFAULT, 9, 52),
}

_DEFAULT_TABLES: dict = {}
_MASK = [(1 << n) - 1 for n in range(33)]


def decompress(data: bytes, limit: int | None = None) -> bytes:
    """Every frame in `data`, concatenated. With `limit`, decoding stops at
    the first block boundary at or past that many bytes (the result may be
    a little longer; the caller trims)."""
    data = bytes(data)
    out = bytearray()
    pos, n = 0, len(data)
    if not n:
        raise ZstdError("empty input")
    while pos < n:
        if n - pos < 4:
            raise ZstdError("truncated frame header")
        magic = int.from_bytes(data[pos:pos + 4], "little")
        if 0x184D2A50 <= magic <= 0x184D2A5F:  # skippable frame
            if n - pos < 8:
                raise ZstdError("truncated skippable frame")
            pos += 8 + int.from_bytes(data[pos + 4:pos + 8], "little")
            continue
        if magic != _FRAME_MAGIC:
            raise ZstdError("not a zstd frame")
        pos = _frame(data, pos + 4, out, limit)
        if limit is not None and len(out) >= limit:
            break
    return bytes(out)


class _State:
    """What one block hands the next within a frame: the repeat offsets,
    the Huffman table for treeless literals and the FSE tables for
    Repeat_Mode."""
    __slots__ = ("rep", "huf", "tables")

    def __init__(self):
        self.rep = [1, 4, 8]
        self.huf = None
        self.tables = {}


def _frame(data: bytes, pos: int, out: bytearray, limit: int | None) -> int:
    n = len(data)
    if pos >= n:
        raise ZstdError("truncated frame header")
    fhd = data[pos]
    pos += 1
    if fhd & 0x08:
        raise ZstdError("reserved bit set in the frame header")
    single_segment = fhd & 0x20
    has_checksum = fhd & 0x04
    if not single_segment:
        pos += 1  # Window_Descriptor: the whole output is kept, so unused
    did_size = (0, 1, 2, 4)[fhd & 3]
    if did_size:
        if pos + did_size > n:
            raise ZstdError("truncated frame header")
        if int.from_bytes(data[pos:pos + did_size], "little"):
            raise ZstdError("frame needs a dictionary")
        pos += did_size
    pos += ((1 if single_segment else 0), 2, 4, 8)[fhd >> 6]
    if pos > n:
        raise ZstdError("truncated frame header")
    st = _State()
    frame_start = len(out)
    while True:
        if pos + 3 > n:
            raise ZstdError("truncated block header")
        bh = data[pos] | data[pos + 1] << 8 | data[pos + 2] << 16
        pos += 3
        last, btype, bsize = bh & 1, (bh >> 1) & 3, bh >> 3
        if btype == 0:  # raw
            if pos + bsize > n:
                raise ZstdError("truncated raw block")
            out += data[pos:pos + bsize]
            pos += bsize
        elif btype == 1:  # RLE: one byte, bsize times
            if pos >= n:
                raise ZstdError("truncated RLE block")
            out += data[pos:pos + 1] * bsize
            pos += 1
        elif btype == 2:
            if bsize > _MAX_BLOCK or pos + bsize > n:
                raise ZstdError("bad compressed block size")
            _block(data, pos, pos + bsize, out, st, frame_start)
            pos += bsize
        else:
            raise ZstdError("reserved block type")
        if last:
            break
        if limit is not None and len(out) >= limit:
            return n
    if has_checksum:
        pos += 4
    return pos


# ------------------------------------------------------------- bitstreams

class _BackBits:
    """The backward bitstream FSE and Huffman streams are written as: read
    from the last byte toward the first, starting just below the highest
    set bit of the last byte (the end marker). Held reversed, so a read is
    one 8-byte big-endian slice and a shift."""
    __slots__ = ("buf", "pos", "end")

    def __init__(self, data: bytes):
        if not data or not data[-1]:
            raise ZstdError("bitstream without its end marker")
        self.buf = data[::-1] + b"\0" * 16
        self.end = len(data) * 8
        self.pos = 9 - data[-1].bit_length()  # the padding zeros and the marker

    def read(self, nbits: int) -> int:
        if not nbits:
            return 0
        p = self.pos
        chunk = self.buf[p >> 3:(p >> 3) + 8]
        if len(chunk) < 8:
            raise ZstdError("read past the end of a bitstream")
        self.pos = p + nbits
        return (int.from_bytes(chunk, "big") >> (64 - (p & 7) - nbits)) & ((1 << nbits) - 1)

    def peek(self, nbits: int) -> int:
        p = self.pos
        chunk = self.buf[p >> 3:(p >> 3) + 8]
        if len(chunk) < 8:
            raise ZstdError("read past the end of a bitstream")
        return (int.from_bytes(chunk, "big") >> (64 - (p & 7) - nbits)) & ((1 << nbits) - 1)


def _read_ncount(data: bytes, pos: int, end: int, max_log: int, max_symbol: int):
    """An FSE table description (§4.1.1): normalized counts, read forward
    little-endian. Returns (counts, accuracy_log, position after it)."""
    stop = min(end, pos + 512)
    avail = (stop - pos) * 8
    if avail <= 0:
        raise ZstdError("truncated FSE table description")
    v = int.from_bytes(data[pos:stop], "little")
    acc = (v & 15) + 5
    if acc > max_log:
        raise ZstdError("FSE accuracy log too large")
    bp = 4
    remaining = (1 << acc) + 1
    threshold = 1 << acc
    nbits = acc + 1
    counts: list[int] = []
    prev_zero = False
    while remaining > 1:
        if prev_zero:
            while True:
                r = (v >> bp) & 3
                bp += 2
                counts.extend([0] * r)
                if r != 3:
                    break
        top = (2 * threshold - 1) - remaining
        low = (v >> bp) & (threshold - 1)
        if low < top:
            value = low
            bp += nbits - 1
        else:
            value = (v >> bp) & (2 * threshold - 1)
            if value >= threshold:
                value -= top
            bp += nbits
        count = value - 1  # -1: "less than one"
        remaining -= 1 if count < 0 else count
        counts.append(count)
        prev_zero = count == 0
        while remaining < threshold:
            nbits -= 1
            threshold >>= 1
        if bp > avail or len(counts) > max_symbol + 1:
            raise ZstdError("corrupt FSE table description")
    if remaining != 1:
        raise ZstdError("corrupt FSE table description")
    return counts, acc, pos + (bp + 7) // 8


def _fse_table(counts: list[int], acc: int):
    """The decoding table for a distribution (§4.1.1): per state, its
    symbol, how many bits the next state takes, and the baseline they
    add to."""
    size = 1 << acc
    symbols = [0] * size
    high = size - 1
    nxt = [0] * len(counts)
    for s, c in enumerate(counts):
        if c == -1:
            symbols[high] = s
            high -= 1
            nxt[s] = 1
        else:
            nxt[s] = c
    step = (size >> 1) + (size >> 3) + 3
    mask = size - 1
    p = 0
    for s, c in enumerate(counts):
        for _ in range(c):
            symbols[p] = s
            p = (p + step) & mask
            while p > high:
                p = (p + step) & mask
    if p:
        raise ZstdError("corrupt FSE distribution")
    nbits = [0] * size
    base = [0] * size
    for u in range(size):
        s = symbols[u]
        x = nxt[s]
        nxt[s] = x + 1
        nb = acc - (x.bit_length() - 1)
        nbits[u] = nb
        base[u] = (x << nb) - size
    return symbols, nbits, base, acc


# --------------------------------------------------------------- literals

def _literals(data: bytes, pos: int, end: int, st: _State):
    if pos >= end:
        raise ZstdError("truncated literals section")
    b0 = data[pos]
    ltype, fmt = b0 & 3, (b0 >> 2) & 3
    if ltype < 2:  # raw or RLE
        if fmt in (0, 2):
            size, pos = b0 >> 3, pos + 1
        elif fmt == 1:
            if pos + 2 > end:
                raise ZstdError("truncated literals header")
            size, pos = (b0 >> 4) | data[pos + 1] << 4, pos + 2
        else:
            if pos + 3 > end:
                raise ZstdError("truncated literals header")
            size, pos = (b0 >> 4) | data[pos + 1] << 4 | data[pos + 2] << 12, pos + 3
        if ltype == 0:
            if pos + size > end:
                raise ZstdError("truncated raw literals")
            return data[pos:pos + size], pos + size
        if pos >= end:
            raise ZstdError("truncated RLE literals")
        return data[pos:pos + 1] * size, pos + 1
    hsize, bits = ((3, 10), (3, 10), (4, 14), (5, 18))[fmt]
    if pos + hsize > end:
        raise ZstdError("truncated literals header")
    h = int.from_bytes(data[pos:pos + hsize], "little")
    mask = (1 << bits) - 1
    regen = (h >> 4) & mask
    csize = (h >> (4 + bits)) & mask
    pos += hsize
    cend = pos + csize
    if cend > end:
        raise ZstdError("truncated compressed literals")
    if ltype == 2:
        st.huf, pos = _huffman_table(data, pos, cend)
    elif st.huf is None:
        raise ZstdError("treeless literals with no table before them")
    table = st.huf
    if fmt == 0:
        return _huf_stream(data[pos:cend], regen, table), cend
    if pos + 6 > cend:
        raise ZstdError("truncated literals jump table")
    s1 = data[pos] | data[pos + 1] << 8
    s2 = data[pos + 2] | data[pos + 3] << 8
    s3 = data[pos + 4] | data[pos + 5] << 8
    pos += 6
    if pos + s1 + s2 + s3 > cend:
        raise ZstdError("corrupt literals jump table")
    seg = (regen + 3) // 4
    if 3 * seg > regen:
        raise ZstdError("corrupt literals size")
    a, b, c = pos + s1, pos + s1 + s2, pos + s1 + s2 + s3
    out = bytearray()
    out += _huf_stream(data[pos:a], seg, table)
    out += _huf_stream(data[a:b], seg, table)
    out += _huf_stream(data[b:c], seg, table)
    out += _huf_stream(data[c:cend], regen - 3 * seg, table)
    return bytes(out), cend


def _huffman_table(data: bytes, pos: int, end: int):
    if pos >= end:
        raise ZstdError("truncated Huffman table")
    hb = data[pos]
    pos += 1
    if hb < 128:
        if pos + hb > end:
            raise ZstdError("truncated Huffman weights")
        weights = _fse_weights(data[pos:pos + hb])
        pos += hb
    else:
        nw = hb - 127
        nbytes = (nw + 1) // 2
        if pos + nbytes > end:
            raise ZstdError("truncated Huffman weights")
        weights = []
        for byte in data[pos:pos + nbytes]:
            weights.append(byte >> 4)
            weights.append(byte & 15)
        weights = weights[:nw]
        pos += nbytes
    if len(weights) > 255 or any(w > 11 for w in weights):
        raise ZstdError("corrupt Huffman weights")
    total = sum(1 << (w - 1) for w in weights if w)
    if not total:
        raise ZstdError("corrupt Huffman weights")
    max_bits = total.bit_length()
    rest = (1 << max_bits) - total
    if max_bits > 11 or rest & (rest - 1):
        raise ZstdError("corrupt Huffman weights")
    weights.append(rest.bit_length())
    size = 1 << max_bits
    syms = bytearray(size)
    nbs = bytearray(size)
    at = 0
    for w in range(1, max_bits + 1):
        length = 1 << (w - 1)
        nb = max_bits + 1 - w
        for s, sw in enumerate(weights):
            if sw == w:
                syms[at:at + length] = bytes((s,)) * length
                nbs[at:at + length] = bytes((nb,)) * length
                at += length
    return (syms, nbs, max_bits), pos


def _fse_weights(src: bytes) -> list[int]:
    """Huffman weights compressed with FSE: two interleaved states over one
    backward stream, until a read runs past its start (§4.2.1.2)."""
    counts, acc, p = _read_ncount(src, 0, len(src), 6, 255)
    symbols, nbits, base, _ = _fse_table(counts, acc)
    br = _BackBits(src[p:])
    s1 = br.read(acc)
    s2 = br.read(acc)
    out: list[int] = []
    while True:
        out.append(symbols[s1])
        s1 = base[s1] + br.read(nbits[s1])
        if br.pos > br.end:
            out.append(symbols[s2])
            break
        out.append(symbols[s2])
        s2 = base[s2] + br.read(nbits[s2])
        if br.pos > br.end:
            out.append(symbols[s1])
            break
        if len(out) > 255:
            raise ZstdError("corrupt Huffman weights")
    return out


def _huf_stream(stream: bytes, count: int, table) -> bytes:
    """`count` literals from one Huffman stream. Read 128 bits at a time
    and as many symbols as they hold decoded from each: a function call
    and a slice per symbol was most of the decoder's time."""
    syms, nbs, max_bits = table
    br = _BackBits(stream)
    buf, pos = br.buf, br.pos
    limit = len(buf) - 16
    mask = (1 << max_bits) - 1
    out = bytearray(count)
    i = 0
    while i < count:
        q = pos >> 3
        if q > limit:
            raise ZstdError("Huffman stream overrun")
        v = int.from_bytes(buf[q:q + 16], "big")
        r = 128 - (pos & 7)
        start = r
        stop = min(count, i + (r - 8) // max_bits)
        while i < stop:
            idx = (v >> (r - max_bits)) & mask
            out[i] = syms[idx]
            r -= nbs[idx]
            i += 1
        pos += start - r
    if pos != br.end:
        raise ZstdError("Huffman stream did not end where it should")
    return bytes(out)


# -------------------------------------------------------------- sequences

def _seq_table(data: bytes, pos: int, end: int, mode: int, st: _State, kind: str):
    default, max_log, max_symbol = _SEQ_KINDS[kind]
    if mode == 0:
        table = _DEFAULT_TABLES.get(kind)
        if table is None:
            table = _DEFAULT_TABLES[kind] = _fse_table(*default)
    elif mode == 1:
        if pos >= end:
            raise ZstdError("truncated RLE sequence table")
        if data[pos] > max_symbol:
            raise ZstdError("RLE symbol out of range")
        table = ([data[pos]], [0], [0], 0)
        pos += 1
    elif mode == 2:
        counts, acc, pos = _read_ncount(data, pos, end, max_log, max_symbol)
        table = _fse_table(counts, acc)
    else:
        table = st.tables.get(kind)
        if table is None:
            raise ZstdError("repeat mode with no table before it")
    st.tables[kind] = table
    return table, pos


def _block(data: bytes, pos: int, end: int, out: bytearray, st: _State, frame_start: int) -> None:
    lit, pos = _literals(data, pos, end, st)
    if pos >= end:
        raise ZstdError("truncated sequences section")
    b0 = data[pos]
    if b0 == 0:
        out += lit
        return
    if b0 < 128:
        nseq, pos = b0, pos + 1
    elif b0 < 255:
        if pos + 2 > end:
            raise ZstdError("truncated sequences header")
        nseq, pos = ((b0 - 128) << 8) + data[pos + 1], pos + 2
    else:
        if pos + 3 > end:
            raise ZstdError("truncated sequences header")
        nseq, pos = data[pos + 1] + (data[pos + 2] << 8) + 0x7F00, pos + 3
    if pos >= end:
        raise ZstdError("truncated sequences header")
    modes = data[pos]
    pos += 1
    if modes & 3:
        raise ZstdError("reserved bits set in the sequences header")
    (ll_sym, ll_nb, ll_base, ll_acc), pos = _seq_table(data, pos, end, modes >> 6, st, "ll")
    (of_sym, of_nb, of_base, of_acc), pos = _seq_table(data, pos, end, (modes >> 4) & 3, st, "of")
    (ml_sym, ml_nb, ml_base, ml_acc), pos = _seq_table(data, pos, end, (modes >> 2) & 3, st, "ml")
    br = _BackBits(data[pos:end])
    read = br.read
    ll_s, of_s, ml_s = read(ll_acc), read(of_acc), read(ml_acc)
    # The rest is read inline, 128 bits per sequence: the most one takes is
    # 31 offset bits, 16 + 16 length bits and 9 + 9 + 8 state bits.
    buf, bpos = br.buf, br.pos
    limit = len(buf) - 16
    rep = st.rep
    lp = 0
    nlit = len(lit)
    last = nseq - 1
    for i in range(nseq):
        q = bpos >> 3
        if q > limit:
            raise ZstdError("read past the end of the sequences bitstream")
        v = int.from_bytes(buf[q:q + 16], "big")
        r = start = 128 - (bpos & 7)
        of_code, ll_code, ml_code = of_sym[of_s], ll_sym[ll_s], ml_sym[ml_s]
        if of_code > 31:
            raise ZstdError("offset code out of range")
        r -= of_code
        ofv = (1 << of_code) + ((v >> r) & _MASK[of_code])
        nb = _ML_BITS[ml_code]
        r -= nb
        ml = _ML_BASE[ml_code] + ((v >> r) & _MASK[nb])
        nb = _LL_BITS[ll_code]
        r -= nb
        ll = _LL_BASE[ll_code] + ((v >> r) & _MASK[nb])
        if i != last:
            nb = ll_nb[ll_s]
            r -= nb
            ll_s = ll_base[ll_s] + ((v >> r) & _MASK[nb])
            nb = ml_nb[ml_s]
            r -= nb
            ml_s = ml_base[ml_s] + ((v >> r) & _MASK[nb])
            nb = of_nb[of_s]
            r -= nb
            of_s = of_base[of_s] + ((v >> r) & _MASK[nb])
        bpos += start - r
        if ofv > 3:
            off = ofv - 3
            rep[2], rep[1], rep[0] = rep[1], rep[0], off
        else:
            idx = ofv - 1 + (ll == 0)
            if idx == 0:
                off = rep[0]
            else:
                off = rep[0] - 1 if idx == 3 else rep[idx]
                off = off or 1
                if idx != 1:
                    rep[2] = rep[1]
                rep[1], rep[0] = rep[0], off
        if ll:
            if lp + ll > nlit:
                raise ZstdError("sequence reads past its literals")
            out += lit[lp:lp + ll]
            lp += ll
        start = len(out) - off
        if start < frame_start:
            raise ZstdError("match offset before the start of the frame")
        if off >= ml:
            out += out[start:start + ml]
        else:
            out += (out[start:] * (ml // off + 1))[:ml]
    if bpos != br.end:
        raise ZstdError("sequence bitstream did not end where it should")
    out += lit[lp:]
