#!/usr/bin/env python3
"""Minimal QEMU guest-agent (QGA) client over the virtio-serial chardev socket.

    qga.py SOCKET COMMAND ['{"json":"args"}']
    qga.py /data/vms/win11/qga.sock guest-ping
    qga.py /data/vms/win11/qga.sock guest-shutdown        # defaults to powerdown
    qga.py /data/vms/win11/qga.sock guest-info
    qga.py /data/vms/win11/qga.sock guest-network-get-interfaces

QEMU side (see bin/start-vm.sh):
    -chardev socket,id=qga0,path=<sock>,server=on,wait=off
    -device virtio-serial-pci
    -device virtserialport,chardev=qga0,name=org.qemu.guest_agent.0

Two things bite here:
  * QMP does NOT proxy guest-* commands (QEMU 8.2 as shipped on Debian/Ubuntu);
    `qmp.py ... guest-ping` answers CommandNotFound. Talk to the chardev socket.
  * qemu-ga stays silent until the client sends a `guest-sync` preamble, so the
    greeting is never the first thing you read. Always sync first.
"""
import json
import os
import socket
import sys
import time


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    sock_path, command = sys.argv[1], sys.argv[2]
    args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else None
    if command == "guest-shutdown" and args is None:
        args = {"mode": "powerdown"}  # without a mode the agent does nothing

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(60)
    s.connect(sock_path)
    f = s.makefile("rw")

    sync_id = int(time.time() * 1000) % 2**31
    f.write(json.dumps({"execute": "guest-sync", "arguments": {"id": sync_id}}) + "\n")
    f.flush()
    while True:
        line = f.readline()
        if not line:
            sys.exit("qga: no response to guest-sync (agent not running in guest?)")
        reply = json.loads(line)
        if reply.get("return") == sync_id or "error" in reply:
            if "error" in reply:
                sys.exit(f"qga: sync failed: {reply['error']}")
            break

    msg = {"execute": command, **(({"arguments": args}) if args else {})}
    f.write(json.dumps(msg) + "\n")
    f.flush()
    while True:
        line = f.readline()
        if not line:
            # guest-shutdown makes the agent go down before it answers, closing the
            # socket; that is success, not failure (otherwise systemd logs the stop
            # as failed and the unit looks broken).
            if command in ("guest-shutdown", "guest-suspend-disk", "guest-suspend-ram"):
                print(json.dumps({"return": {"note": "agent went down before replying"}}))
                return
            sys.exit("qga: connection closed by agent")
        reply = json.loads(line)
        if "return" in reply or "error" in reply:
            print(json.dumps(reply, ensure_ascii=False))
            if "error" in reply:
                sys.exit(1)
            break


if __name__ == "__main__":
    main()
