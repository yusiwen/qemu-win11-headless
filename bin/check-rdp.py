import base64
import json
import socket
import time

SOCK = "/data/vms/win11/qga.sock"

CHECKS = [
    ("RDP 是否启用(0=启用)", "(Get-ItemProperty 'HKLM:\\System\\CurrentControlSet\\Control\\Terminal Server').fDenyTSConnections"),
    ("3389 是否在监听", "(Get-NetTCPConnection -LocalPort 3389 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1).LocalAddress"),
    ("NLA 是否要求", "(Get-ItemProperty 'HKLM:\\System\\CurrentControlSet\\Control\\Terminal Server\\WinStations\\RDP-Tcp').UserAuthentication"),
    ("当前登录用户", "(Get-CimInstance Win32_ComputerSystem).UserName"),
    ("本机账户类型", "Get-LocalUser | Where-Object Enabled | ForEach-Object { '{0} | {1}' -f $_.Name, $_.PrincipalSource }"),
    ("远程桌面用户组", "(Get-LocalGroupMember -Group 'Remote Desktop Users' -ErrorAction SilentlyContinue | ForEach-Object { $_.Name }) -join ', '"),
    ("管理员组成员", "(Get-LocalGroupMember -Group 'Administrators' | ForEach-Object { $_.Name }) -join ', '"),
    ("防火墙 3389 规则", "Get-NetFirewallRule -DisplayGroup '远程桌面' -ErrorAction SilentlyContinue | Where-Object Enabled -eq True | Select-Object -First 2 -ExpandProperty Direction"),
]

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(60)
s.connect(SOCK)
f = s.makefile("rw")


def call(cmd, args=None):
    f.write(json.dumps({"execute": "guest-sync", "arguments": {"id": 21}}) + "\n")
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


for label, script in CHECKS:
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    r = call("guest-exec", {
        "path": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "arg": ["-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
        "capture-output": True,
    })
    if "error" in r:
        print("%-22s ERR %s" % (label, r["error"].get("desc")))
        continue
    pid = r["return"]["pid"]
    done = False
    for _ in range(60):
        time.sleep(0.4)
        st = call("guest-exec-status", {"pid": pid})["return"]
        if st.get("exited"):
            out = base64.b64decode(st.get("out-data") or "").decode("utf-8", "replace").strip()
            print("%-22s %s" % (label, out.replace("\r\n", " ; ") or "(空)"))
            done = True
            break
    if not done:
        print("%-22s timeout" % label)
