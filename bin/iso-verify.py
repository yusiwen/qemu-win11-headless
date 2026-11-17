#!/usr/bin/env python3
"""Walk an ISO9660 image and report what is inside it - no mount, no root, no isoinfo.

    ./iso-verify.py /data/vms/iso/virtio.iso 'viostor/w11/amd64' 'guest-tools'
    ./iso-verify.py /data/vms/iso/win11.iso        # just count files

Why this exists: for driver media (virtio-win) size and `file` are not proof.
Before a 30-minute install you want to know that `viostor/w11/amd64` really is on
the ISO, because that is the directory the Windows setup "Load driver" dialog
needs when the virtio-blk disk list comes up empty.

How it works (ISO9660 in 3 facts):
  * The volume descriptor set starts at sector 16 (offset 16*2048). The Primary
    Volume Descriptor is the one with bytes 1..5 == "CD001" and byte 0 == 1.
  * The root directory record lives at offset 156 of the PVD: extent LBA is a
    little-endian u32 at +2, data length a u32 at +10.
  * A directory is a flat list of records: length(1) ext_attr_len(1) extent_lba(4)
    data_len(4) datetime(7) flags(1) ... name_len(1) name(name_len) pad.
    flags & 2 == directory. Name bytes 0x00 and 0x01 are "." and "..".
Names are the plain ISO 9660 (8.3-ish) ones; long names live in the Joliet tree,
which this deliberately ignores.
"""
import struct
import sys

SECTOR = 2048


def read_at(f, lba, size):
    f.seek(lba * SECTOR)
    return f.read(size)


def walk(f, lba, size, prefix, depth, out):
    data = read_at(f, lba, size)
    i = 0
    while i < len(data):
        length = data[i]
        if length == 0:                       # rest of this sector is padding
            i = (i // SECTOR + 1) * SECTOR
            continue
        rec = data[i:i + length]
        extent = struct.unpack("<I", rec[2:6])[0]
        data_len = struct.unpack("<I", rec[10:14])[0]
        flags = rec[25]
        name_len = rec[32]
        name = rec[33:33 + name_len]
        i += length
        if name in (b"\x00", b"\x01"):
            continue
        try:
            nm = name.decode("ascii")
        except UnicodeDecodeError:
            nm = repr(name)
        path = prefix + "/" + nm
        if flags & 2:
            if depth < 4:
                walk(f, extent, data_len, path, depth + 1, out)
        else:
            out.append((path, data_len))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    image = sys.argv[1]
    needles = [n.lower() for n in sys.argv[2:]]

    f = open(image, "rb")
    pvd = read_at(f, 16, SECTOR)
    if pvd[1:6] != b"CD001":
        sys.exit("not an ISO9660 image (no CD001 at sector 16)")
    root = pvd[156:190]
    root_lba = struct.unpack("<I", root[2:6])[0]
    root_len = struct.unpack("<I", root[10:14])[0]

    files = []
    walk(f, root_lba, root_len, "", 0, files)

    print("%s: %d files, %.2f GiB of directory entries"
          % (image, len(files), sum(s for _, s in files) / 2**30))
    for needle in needles:
        hits = [(p, s) for p, s in files if needle in p.lower()]
        print("\n[%s] %d hit(s)" % (needle, len(hits)))
        for p, s in hits[:20]:
            print("  %10d  %s" % (s, p))
        if len(hits) > 20:
            print("  ... %d more" % (len(hits) - 20))


if __name__ == "__main__":
    main()
