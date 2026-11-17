#!/usr/bin/env python3
"""Minimal QMP client (stdlib only).

    qmp.py SOCKET COMMAND ['{"json":"args"}']
    qmp.py /data/vms/win11/qmp.sock query-status
    qmp.py /data/vms/win11/qmp.sock guest-shutdown          # defaults to powerdown
    qmp.py /data/vms/win11/qmp.sock guest-ping              # requires qemu-ga
    qmp.py /data/vms/win11/qmp.sock screendump '{"filename":"/data/vms/win11/logs/screen.ppm"}'

QEMU must be started with -qmp unix:<socket>,server=on,wait=off.
"""
import json
import socket
import sys


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    sock_path, command = sys.argv[1], sys.argv[2]
    args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else None
    # guest-shutdown without explicit mode would be a no-op; powerdown is what we want
    if command == "guest-shutdown" and args is None:
        args = {"mode": "powerdown"}

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(30)
    s.connect(sock_path)
    f = s.makefile("rw")
    f.readline()  # greeting: {"QMP": {...}}

    for msg in ({"execute": "qmp_capabilities"},
                {"execute": command, **(({"arguments": args}) if args else {})}):
        f.write(json.dumps(msg) + "\n")
        f.flush()
        while True:
            line = f.readline()
            if not line:
                sys.exit("qmp: connection closed by QEMU")
            reply = json.loads(line)
            if "return" in reply or "error" in reply:  # skip async events
                print(json.dumps(reply, ensure_ascii=False))
                if "error" in reply:
                    sys.exit(1)
                break


if __name__ == "__main__":
    main()
