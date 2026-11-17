#!/usr/bin/env python3
"""Grab the guest's current screen as a PNG, with no viewer and no X server.

    ./screen.py                                  # -> logs/screen.png
    ./screen.py --sock /data/vms/win11/qmp.sock --out /tmp/x.png

Two steps, both worth reading if you care how this works:

1. QMP `screendump` asks QEMU to write its VGA framebuffer to a file in **PPM**
   (P6, binary RGB). No client, no display device, no X. This is the only
   diagnostic channel you have before the guest has a network or a guest agent.

2. PPM -> PNG by hand (stdlib zlib + struct only): PNG is just signature +
   IHDR + IDAT + IEND chunks, each `len | type | data | CRC32(type+data)`.
   Every scanline gets a leading filter byte (0 = none). Why bother converting?
   Because PNG opens in Preview on a Mac and PPM does not.

Typical use: prove the firmware reached the installer / the desktop came up,
instead of trusting "the service is active".
"""
import argparse
import json
import os
import socket
import struct
import sys
import zlib


def qmp_call(sock_path, command, args=None, timeout=20):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(sock_path)
    f = s.makefile("rw")
    f.readline()  # QMP greeting {"QMP": {...}}
    replies = []
    for msg in ({"execute": "qmp_capabilities"},
                {"execute": command, **(({"arguments": args}) if args else {})}):
        f.write(json.dumps(msg) + "\n")
        f.flush()
        while True:
            line = f.readline()
            if not line:
                sys.exit("qmp: connection closed")
            reply = json.loads(line)
            if "return" in reply or "error" in reply:   # skip async events
                replies.append(reply)
                if "error" in reply:
                    sys.exit("qmp error: %s" % reply["error"])
                break
    s.close()
    return replies[-1].get("return")


def ppm_to_png(ppm_path, png_path):
    d = open(ppm_path, "rb").read()
    if d[:2] != b"P6":
        sys.exit("not a binary PPM (P6): %r" % d[:2])
    # header: P6 <width> <height> <maxval>, whitespace separated, comments omitted
    i, tokens = 2, []
    while len(tokens) < 3:
        while d[i:i + 1].isspace():
            i += 1
        j = i
        while not d[j:j + 1].isspace():
            j += 1
        tokens.append(d[i:j])
        i = j
    i += 1
    w, h, maxval = int(tokens[0]), int(tokens[1]), int(tokens[2])
    assert maxval == 255, "only 8-bit PPM supported, got maxval=%d" % maxval
    px = d[i:i + w * h * 3]
    if len(px) != w * h * 3:
        sys.exit("truncated framebuffer: %d bytes for %dx%d" % (len(px), w, h))

    raw = b"".join(b"\x00" + px[y * w * 3:(y + 1) * w * 3] for y in range(h))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    open(png_path, "wb").write(png)
    return w, h, len(png)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sock", default="/data/vms/win11/qmp.sock")
    ap.add_argument("--out", default="/data/vms/win11/logs/screen.png")
    a = ap.parse_args()

    ppm = os.path.splitext(a.out)[0] + ".ppm"
    qmp_call(a.sock, "screendump", {"filename": ppm})
    w, h, size = ppm_to_png(ppm, a.out)
    print("%s  %dx%d  %d bytes" % (a.out, w, h, size))


if __name__ == "__main__":
    main()
