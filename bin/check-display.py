#!/usr/bin/env python3
import base64
import json
import socket
import time

SOCK = "/data/vms/win11/qga.sock"
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(30)
s.connect(SOCK)
f = s.makefile("rw")


def call(cmd, args=None):
    f.write(json.dumps({"execute": "guest-sync", "arguments": {"id": 7}}) + "\n")
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


def ps(script, label):
    r = call("guest-exec", {
        "path": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "arg": ["-NoProfile", "-NonInteractive", "-Command", script],
        "capture-output": True,
    })
    if "error" in r:
        print(label, "ERR", r)
        return
    pid = r["return"]["pid"]
    for _ in range(60):
        time.sleep(0.5)
        st = call("guest-exec-status", {"pid": pid})["return"]
        if st.get("exited"):
            out = base64.b64decode(st.get("out-data") or "").decode("utf-8", "replace")
            err = base64.b64decode(st.get("err-data") or "").decode("utf-8", "replace")
            print("=== %s (exit %s)" % (label, st.get("exitcode")))
            print(out.strip() or "(no stdout)")
            if err.strip():
                print("STDERR:", err.strip()[:400])
            return
    print(label, "timeout")


ps("Get-CimInstance Win32_VideoController | ForEach-Object { "
   "'{0} | {1} | {2} | {3}' -f $_.Name, $_.CurrentHorizontalResolution, "
   "$_.CurrentVerticalResolution, $_.DriverVersion }", "GPU / 当前分辨率")

ps("Get-CimInstance Win32_VideoController | ForEach-Object { "
   "($_.VideoModeDescription); $_.CurrentRefreshRate }", "显示模式")

ps("Add-Type -AssemblyName System.Windows.Forms; "
   "[System.Windows.Forms.Screen]::AllScreens | ForEach-Object { "
   "'{0} {1}x{2} primary={3}' -f $_.DeviceName, $_.Bounds.Width, $_.Bounds.Height, $_.Primary }", "桌面尺寸")
