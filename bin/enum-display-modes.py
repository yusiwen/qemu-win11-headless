#!/usr/bin/env python3
import base64
import json
import socket
import time

SOCK = "/data/vms/win11/qga.sock"

PS = r"""
$src = @'
using System;
using System.Runtime.InteropServices;
public class D {
  [DllImport("user32.dll")]
  public static extern bool EnumDisplaySettings(string dev, int mode, ref DEVMODE dm);
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Ansi)]
  public struct DEVMODE {
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string dmDeviceName;
    public short dmSpecVersion; public short dmDriverVersion; public short dmSize; public short dmDriverExtra;
    public int dmFields; public int dmPositionX; public int dmPositionY;
    public int dmDisplayOrientation; public int dmDisplayFixedOutput;
    public short dmColor; public short dmDuplex; public short dmYResolution; public short dmTTOption; public short dmCollate;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string dmFormName;
    public short dmLogPixels; public int dmBitsPerPel; public int dmPelsWidth; public int dmPelsHeight;
    public int dmDisplayFlags; public int dmDisplayFrequency;
    public int dmICMMethod; public int dmICMIntent; public int dmMediaType; public int dmDitherType;
    public int dmReserved1; public int dmReserved2; public int dmPanningWidth; public int dmPanningHeight;
  }
}
'@
Add-Type -TypeDefinition $src
$dm = New-Object D+DEVMODE
$dm.dmSize = [System.Runtime.InteropServices.Marshal]::SizeOf($dm)
$modes = @()
$i = 0
while ([D]::EnumDisplaySettings($null, $i, [ref]$dm)) {
  $modes += ('{0}x{1}' -f $dm.dmPelsWidth, $dm.dmPelsHeight)
  $i++
  if ($i -gt 500) { break }
}
"count=$i"
$modes | Sort-Object -Unique
"""

enc = base64.b64encode(PS.encode("utf-16-le")).decode()

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(60)
s.connect(SOCK)
f = s.makefile("rw")


def call(cmd, args=None):
    f.write(json.dumps({"execute": "guest-sync", "arguments": {"id": 9}}) + "\n")
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


r = call("guest-exec", {
    "path": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    "arg": ["-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
    "capture-output": True,
})
if "error" in r:
    print("ERR", r)
else:
    pid = r["return"]["pid"]
    for _ in range(80):
        time.sleep(0.5)
        st = call("guest-exec-status", {"pid": pid})["return"]
        if st.get("exited"):
            out = base64.b64decode(st.get("out-data") or "").decode("utf-8", "replace")
            err = base64.b64decode(st.get("err-data") or "").decode("utf-8", "replace")
            print("exit", st.get("exitcode"))
            print(out.strip() or "(no stdout)")
            if err.strip():
                print("STDERR:", err.strip()[:500])
            break
    else:
        print("timeout")
