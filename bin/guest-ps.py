#!/usr/bin/env python3
"""Run a PowerShell script inside the guest over QGA, and print what it said.

The generic hammer for "there is no console, but I need to do something inside
Windows". Everything else in this directory is a purpose-built wrapper around
this pattern; this one takes arbitrary PowerShell.

    guest-ps.py script.ps1                 # wait and print output
    guest-ps.py - < script.ps1             # same, from stdin
    guest-ps.py --detach long-job.ps1      # start and return the pid at once

Two QGA rules that cost real debugging time:
  * qemu-ga sends no greeting, so guest-sync must precede every command.
  * QMP does not proxy guest-* commands; talk to qga.sock directly.

Encoding: Windows PowerShell defaults to the OEM code page on a pipe, which
turns Chinese into mojibake. Scripts should set OutputEncoding themselves; this
helper decodes as UTF-8 and falls back to gbk.
"""

import argparse
import base64
import json
import socket
import sys
import time

PS_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def connect(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(30)
    s.connect(path)
    return s, s.makefile("rw")


def call(f, cmd, args=None):
    f.write(json.dumps({"execute": "guest-sync", "arguments": {"id": 42}}) + "\n")
    f.flush()
    f.readline()
    msg = {"execute": cmd}
    if args:
        msg["arguments"] = args
    f.write(json.dumps(msg) + "\n")
    f.flush()
    while True:
        d = json.loads(f.readline())
        if "return" in d or "error" in d:
            return d


def decode(b64):
    raw = base64.b64decode(b64 or b"")
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("script", help="PowerShell file, or - for stdin")
    ap.add_argument("--sock", default="/data/vms/win11/qga.sock")
    ap.add_argument("--timeout", type=int, default=300, help="seconds to wait")
    ap.add_argument("--detach", action="store_true",
                    help="start the script and return immediately (prints pid)")
    args = ap.parse_args()

    text = sys.stdin.read() if args.script == "-" else open(args.script, encoding="utf-8").read()
    enc = base64.b64encode(text.encode("utf-16-le")).decode()

    s, f = connect(args.sock)
    try:
        r = call(f, "guest-exec", {
            "path": PS_EXE,
            "arg": ["-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
            "capture-output": True,
        })
        if "error" in r:
            print("guest-exec 失败:", r["error"], file=sys.stderr)
            return 1
        pid = r["return"]["pid"]
        if args.detach:
            print(pid)
            return 0

        deadline = time.time() + args.timeout
        while time.time() < deadline:
            time.sleep(0.5)
            st = call(f, "guest-exec-status", {"pid": pid})["return"]
            if st.get("exited"):
                out = decode(st.get("out-data"))
                err = decode(st.get("err-data"))
                if out.strip():
                    print(out.rstrip())
                if err.strip():
                    print("--- stderr ---")
                    print(err.rstrip())
                print("退出码: %s" % st.get("exitcode", "?"))
                return 0 if st.get("exitcode", 0) == 0 else 2
        print("超时 %ds，脚本可能还在客机里跑" % args.timeout, file=sys.stderr)
        return 3
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
