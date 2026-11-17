#!/usr/bin/env python3
import base64
import json
import socket
import time

SOCK = "/data/vms/win11/qga.sock"
PROCS = [
    ("客机 CPU 负载", "(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average"),
    ("CPU 占用最高的进程", "Get-Process | Sort-Object CPU -Descending | Select-Object -First 6 Name, CPU, WS | Format-Table -AutoSize | Out-String -Width 120"),
    ("Windows Update 服务", "Get-Service wuauserv, BITS, WinDefend | ForEach-Object { '{0} = {1}' -f $_.Name, $_.Status }"),
    ("待装更新数", "$s=(New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher().Search('IsInstalled=0'); 'pending updates: ' + $s.Updates.Count"),
]

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(60)
s.connect(SOCK)
f = s.makefile("rw")


def call(cmd, args=None):
    f.write(json.dumps({"execute": "guest-sync", "arguments": {"id": 11}}) + "\n")
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


for label, script in PROCS:
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    r = call("guest-exec", {
        "path": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "arg": ["-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
        "capture-output": True,
    })
    if "error" in r:
        print(label, "ERR", r)
        continue
    pid = r["return"]["pid"]
    for _ in range(120):
        time.sleep(0.5)
        st = call("guest-exec-status", {"pid": pid})["return"]
        if st.get("exited"):
            out = base64.b64decode(st.get("out-data") or "").decode("utf-8", "replace")
            print("=== %s" % label)
            print(out.strip() or "(empty)")
            break
    else:
        print(label, "timeout")
