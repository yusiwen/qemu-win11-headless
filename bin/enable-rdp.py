#!/usr/bin/env python3
"""Enable Remote Desktop inside the guest over QGA (idempotent).

    ./enable-rdp.py

Why a script: the GUI switch is easy to think you flipped but not actually flip
(fDenyTSConnections stays 1, and `nc -vz` through the QEMU hostfwd still succeeds
because SLIRP answers the TCP handshake on its own — a false positive).

Sets:
  * HKLM\\...\\Terminal Server\\fDenyTSConnections = 0     (the master switch)
  * TermService startup = Automatic, and started
  * firewall group @FirewallAPI.dll,-28752 enabled        (language-neutral group
    id, so it works on a Chinese-localized Windows where the display name is
    "远程桌面" instead of "Remote Desktop")
  * NLA left as-is (required by default; the Windows App client supports it)
"""
import base64
import json
import socket
import sys
import time

SOCK = "/data/vms/win11/qga.sock"

PS = r"""
$ErrorActionPreference = 'Continue'
Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value 0
Set-Service TermService -StartupType Automatic
if ((Get-Service TermService).Status -ne 'Running') { Start-Service TermService }
Enable-NetFirewallRule -Group '@FirewallAPI.dll,-28752' -ErrorAction SilentlyContinue
'fDenyTSConnections=' + (Get-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server').fDenyTSConnections
'TermService=' + (Get-Service TermService).Status
'listening=' + ((Get-NetTCPConnection -LocalPort 3389 -State Listen -ErrorAction SilentlyContinue | Measure-Object).Count)
'fw_rules=' + ((Get-NetFirewallRule -Group '@FirewallAPI.dll,-28752' -ErrorAction SilentlyContinue | Where-Object Enabled -eq 'True' | Measure-Object).Count)
"""


def main():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(60)
    s.connect(SOCK)
    f = s.makefile("rw")

    def call(cmd, args=None):
        f.write(json.dumps({"execute": "guest-sync", "arguments": {"id": 31}}) + "\n")
        f.flush()
        f.readline()
        m = {"execute": cmd}
        if args:
            m["arguments"] = args
        f.write(json.dumps(m) + "\n")
        f.flush()
        while True:
            d = json.loads(f.readline())
            if "return" in d or "error" in d:
                return d

    enc = base64.b64encode(PS.encode("utf-16-le")).decode()
    r = call("guest-exec", {
        "path": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "arg": ["-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
        "capture-output": True,
    })
    if "error" in r:
        sys.exit("guest-exec failed: %s" % r["error"])
    pid = r["return"]["pid"]
    for _ in range(120):
        time.sleep(0.5)
        st = call("guest-exec-status", {"pid": pid})["return"]
        if st.get("exited"):
            out = base64.b64decode(st.get("out-data") or "").decode("utf-8", "replace")
            err = base64.b64decode(st.get("err-data") or "").decode("utf-8", "replace")
            print(out.strip())
            if err.strip():
                print("STDERR:", err.strip()[:400])
            return
    sys.exit("timeout waiting for powershell")


if __name__ == "__main__":
    main()
