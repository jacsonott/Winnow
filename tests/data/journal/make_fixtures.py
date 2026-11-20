"""Remake the systemd journal fixtures tests/test_systemd_journal.py reads.

    python tests/data/journal/make_fixtures.py

Needs a Linux box with systemd-journal-remote and journalctl (Debian and
Ubuntu: `apt install systemd-journal-remote`). The fixtures in the repo
were made with systemd 255.

Writes the synthetic entries below as a `journalctl -o export` stream, has
systemd-journal-remote turn it into journal FILES in three shapes (the
compact zstd layout systemd 252+ writes, and the regular layout with LZ4
and with XZ, what older hosts left behind), and has journalctl print the
first one back as export, json, json-pretty, json-seq and json-sse. Every
file is trimmed to its last object (a journal is mostly preallocated
zeros) and stored xz-compressed.

It also writes zstd-*.zst: inputs from `zstd_inputs()` (which the test
rebuilds byte for byte) compressed by the zstd command-line tool at the
settings that make it use each part of the format, so the pure-Python
decoder is checked against the reference encoder on every Python, not
only on 3.14, where the standard library decodes zstd itself. The tests hold the plugin's reading of
each journal file to journalctl's own output of it, so what systemd
itself says these files hold is what the test checks against, with no
systemd needed to run it.

Nothing here came from a real host: the names, addresses and IDs are
made up.
"""

from __future__ import annotations

import lzma
import os
import random
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
JOURNAL_REMOTE = "/lib/systemd/systemd-journal-remote"

BOOT_A = "6c0a2f6f5e3e4b1a9d1c2b3a4f5e6d7c"
BOOT_B = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"
MACHINE = "2b7c8d9e0f1a4b3c8d7e6f5a4b3c2d1e"
T0 = 1_763_370_000_000_000  # 2025-11-17 09:00:00 UTC, in microseconds

CMDLINE = ("/usr/lib/jvm/java-17/bin/java -Xms512m -Xmx4g -Dspring.profiles.active=prod -cp "
           + ":".join(f"/opt/billing/lib/dependency-{n:02d}-1.4.2.jar" for n in range(24))
           + " com.example.billing.Main --port 8443")


def entries():
    """(boot, seconds after T0, fields) — fields as (name, bytes) pairs, so a
    field can repeat and a value can be any bytes."""
    def e(boot, sec, **kw):
        out = [("_HOSTNAME", b"web01"), ("_MACHINE_ID", MACHINE.encode())]
        for k, v in kw.items():
            for one in v if isinstance(v, list) else [v]:
                out.append((k, one if isinstance(one, bytes) else str(one).encode()))
        return boot, sec, out

    yield e(BOOT_A, 0, _TRANSPORT="kernel", SYSLOG_IDENTIFIER="kernel", PRIORITY=5, SYSLOG_FACILITY=0,
            MESSAGE="Linux version 6.8.0-49-generic (buildd@lcy02-amd64-103)")
    yield e(BOOT_A, 2, _TRANSPORT="kernel", SYSLOG_IDENTIFIER="kernel", PRIORITY=6,
            _KERNEL_DEVICE="+usb:1-1", _KERNEL_SUBSYSTEM="usb",
            MESSAGE="usb 1-1: new high-speed USB device number 2 using xhci_hcd")
    yield e(BOOT_A, 9, _TRANSPORT="journal", SYSLOG_IDENTIFIER="systemd", _PID=1, _UID=0, _GID=0,
            _COMM="systemd", _EXE="/usr/lib/systemd/systemd", _SYSTEMD_UNIT="init.scope",
            UNIT="ssh.service", PRIORITY=6, MESSAGE_ID="39f53479d3a045ac8e11786248231fbf",
            MESSAGE="Started ssh.service - OpenBSD Secure Shell server.")
    for n, ip in enumerate(["203.0.113.7", "203.0.113.7", "198.51.100.23"]):
        yield e(BOOT_A, 60 + n, _TRANSPORT="syslog", SYSLOG_IDENTIFIER="sshd", SYSLOG_FACILITY=10,
                _PID=2210 + n, SYSLOG_PID=2210 + n, _UID=0, _COMM="sshd", _EXE="/usr/sbin/sshd",
                _SYSTEMD_UNIT="ssh.service", PRIORITY=6,
                _SOURCE_REALTIME_TIMESTAMP=T0 + (60 + n) * 1_000_000 - 1500,
                MESSAGE=(f"Failed password for invalid user admin from {ip} port {50100 + n} ssh2"
                         if n < 2 else f"Accepted publickey for alice from {ip} port 50902 ssh2: "
                         "ED25519 SHA256:QWxpY2UncyBrZXkgZm9yIHRlc3Rz"))
    yield e(BOOT_A, 64, _TRANSPORT="syslog", SYSLOG_IDENTIFIER="sshd", _PID=2213, _COMM="sshd",
            PRIORITY=6, MESSAGE="Invalid user oracle from 203.0.113.7 port 50133")
    # A command line long enough that journald compresses it, logged by
    # one process three times: one DATA object, three entries.
    for n in range(3):
        yield e(BOOT_A, 120 + n, _TRANSPORT="stdout", _SYSTEMD_UNIT="billing.service",
                SYSLOG_IDENTIFIER="java", _PID=3100, _UID=998, _GID=998, _COMM="java",
                _EXE="/usr/lib/jvm/java-17/bin/java", _CMDLINE=CMDLINE, _STREAM_ID="a1" * 16,
                PRIORITY=6, MESSAGE=f"Handled invoice batch {n + 1} of 3 in {40 + n} ms")
    yield e(BOOT_A, 200, _TRANSPORT="syslog", SYSLOG_IDENTIFIER="sudo", _PID=3301, _UID=1000,
            _AUDIT_LOGINUID=1000, _AUDIT_SESSION=3, _COMM="sudo", PRIORITY=5, SYSLOG_FACILITY=10,
            MESSAGE=("alice : TTY=pts/0 ; PWD=/home/alice ; USER=root ; COMMAND=/usr/bin/bash -c "
                     + "'curl -s https://updates.example.net/agent.sh | sh; "
                     + "; ".join(f"echo step{n:02d} >> /var/tmp/.cache/log" for n in range(16)) + "'"))
    # No SYSLOG_IDENTIFIER: journalctl falls back to _COMM. No _PID: to
    # SYSLOG_PID.
    yield e(BOOT_A, 240, _TRANSPORT="stdout", _COMM="backup.sh", _PID=3500, PRIORITY=4,
            MESSAGE="disk usage at 91%")
    yield e(BOOT_A, 241, _TRANSPORT="syslog", SYSLOG_IDENTIFIER="cron", SYSLOG_PID=3601, PRIORITY=6,
            MESSAGE="(root) CMD (/usr/local/bin/backup.sh)")
    # Bytes that aren't text: invalid UTF-8 and a NUL; a newline; an ANSI
    # colour escape; a field that appears three times; a priority that
    # isn't one.
    yield e(BOOT_A, 300, SYSLOG_IDENTIFIER="agent", PRIORITY=3,
            MESSAGE=b"payload \xff\xfe not utf-8\x00 after nul")
    yield e(BOOT_A, 301, SYSLOG_IDENTIFIER="agent", PRIORITY=2,
            MESSAGE="traceback:\n  File \"agent.py\", line 9\nRuntimeError: lost the C2")
    yield e(BOOT_A, 302, SYSLOG_IDENTIFIER="agent", PRIORITY=6, MESSAGE="\x1b[1;31mred\x1b[0m alert")
    yield e(BOOT_A, 303, SYSLOG_IDENTIFIER="agent", PRIORITY=6, MESSAGE="three tags",
            TAG=["first", "second", "third"])
    yield e(BOOT_A, 304, SYSLOG_IDENTIFIER="agent", PRIORITY=9, MESSAGE="priority out of range")
    # The second boot.
    yield e(BOOT_B, 3600, _TRANSPORT="kernel", SYSLOG_IDENTIFIER="kernel", PRIORITY=5,
            MESSAGE="Linux version 6.8.0-49-generic (buildd@lcy02-amd64-103)")
    yield e(BOOT_B, 3700, _TRANSPORT="syslog", SYSLOG_IDENTIFIER="sshd", _PID=901, _COMM="sshd",
            PRIORITY=6, MESSAGE="Accepted password for root from 198.51.100.23 port 61022 ssh2")


def export_stream() -> bytes:
    out = bytearray()
    monotonic = {BOOT_A: 1_200_000, BOOT_B: 1_100_000}
    for boot, sec, fields in entries():
        out += f"__REALTIME_TIMESTAMP={T0 + sec * 1_000_000}\n".encode()
        out += f"__MONOTONIC_TIMESTAMP={monotonic[boot] + sec * 1000}\n".encode()
        out += f"_BOOT_ID={boot}\n".encode()
        for name, value in fields:
            if b"\n" in value or not all(32 <= c < 127 for c in value):
                out += name.encode() + b"\n" + struct.pack("<Q", len(value)) + value + b"\n"
            else:
                out += name.encode() + b"=" + value + b"\n"
        out += b"\n"
    return bytes(out)


def trimmed(path: str) -> bytes:
    data = open(path, "rb").read()
    tail = struct.unpack_from("<Q", data, 136)[0]
    size = struct.unpack_from("<Q", data, tail + 8)[0]
    return data[:tail + ((size + 7) & ~7)]


def write_xz(name: str, data: bytes) -> None:
    with open(os.path.join(HERE, name + ".xz"), "wb") as f:
        f.write(lzma.compress(data, preset=9 | lzma.PRESET_EXTREME))
    print(f"{name}.xz  ({len(data):,} bytes)")


SHAPES = {
    # name: (compact, compression, keyed hash)
    "compact-zstd.journal": ("1", "ZSTD", "1"),
    "regular-lz4.journal": ("0", "LZ4", "0"),
    "regular-xz.journal": ("0", "XZ", "1"),
}


WORDS = ("sshd systemd kernel sudo cron Accepted Failed password publickey for root alice from "
         "port ssh2 session opened closed user uid= gid= COMMAND=/usr/bin/bash -c "
         "/opt/billing/lib/dependency.jar Started Stopped Reached target network.target "
         "usb new high-speed device number using xhci_hcd 203.0.113.7 198.51.100.23").split()


def zstd_inputs() -> dict:
    """name -> bytes. Deterministic: random.Random(seed) gives the same
    stream on every Python 3."""
    rnd = random.Random(20261117)
    words = " ".join(rnd.choice(WORDS) + ("\n" if rnd.random() < 0.08 else "") for _ in range(26000))
    return {
        "words": words.encode()[:135_000],  # past 128 KiB: more than one block
        "short": b"alice : TTY=pts/0 ; PWD=/home/alice ; USER=root ; COMMAND=/usr/bin/bash -c "
                 + b"'echo step >> /var/tmp/.cache/log; ' " * 14,
        # Over 16 KiB of literals in one block: the 18-bit literals header.
        "skewed": bytes(rnd.choice(b"aaaaaaaabbbbccdeEF\x00\xff") for _ in range(40000)),
        # Four symbols: Huffman weights written directly, not FSE-compressed.
        "quad": bytes(rnd.choice(b"\x00\x01\x02\x03") for _ in range(8000)),
        # Every match the same length: an RLE match-length table.
        "alt": b"".join((b"x" if i % 2 else b"y") + b"-common-tail-" for i in range(5000)),
        "noise": bytes(rnd.getrandbits(8) for _ in range(3000)),
        "run": b"A" * 4000,
    }


ZSTD_VECTORS = {
    # name: (input, zstd arguments)
    "words-1": ("words", ["-1"]),
    "words-3": ("words", ["-3"]),
    "words-19": ("words", ["-19"]),
    "short": ("short", ["-3"]),
    "skewed": ("skewed", ["-19"]),
    "quad": ("quad", ["-3"]),
    "alt": ("alt", ["-3"]),
    "noise": ("noise", ["-3"]),
    "run": ("run", ["-3"]),
}


def main() -> int:
    inputs = zstd_inputs()
    for name, (src, args) in ZSTD_VECTORS.items():
        out = subprocess.run(["zstd", "-q", "-c", *args], input=inputs[src], check=True,
                             capture_output=True).stdout
        with open(os.path.join(HERE, f"zstd-{name}.zst"), "wb") as f:
            f.write(out)
        print(f"zstd-{name}.zst  ({len(out):,} bytes)")
    stream = export_stream()
    with tempfile.TemporaryDirectory() as tmp:
        for name, (compact, compress, keyed) in SHAPES.items():
            path = os.path.join(tmp, name)
            env = dict(os.environ, SYSTEMD_JOURNAL_COMPACT=compact, SYSTEMD_JOURNAL_COMPRESS=compress,
                       SYSTEMD_JOURNAL_KEYED_HASH=keyed)
            subprocess.run([JOURNAL_REMOTE, "-o", path, "--split-mode=none", "-"], input=stream,
                           env=env, check=True, capture_output=True)
            write_xz(name, trimmed(path))
        first = os.path.join(tmp, next(iter(SHAPES)))
        for mode in ("export", "json", "json-pretty", "json-seq", "json-sse"):
            out = subprocess.run(["journalctl", f"--file={first}", "-o", mode, "--all"],
                                 check=True, capture_output=True).stdout
            write_xz(f"journalctl-{mode}.txt", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
