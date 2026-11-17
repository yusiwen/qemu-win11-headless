#!/usr/bin/env python3
"""Report what a Windows guest is doing right after a Windows Update reboot.

Answers the questions you actually have when "RDP dropped after an update":
  - is the guest up, and how long ago did it boot
  - which build is it on now (did 25H2 land?)
  - is another reboot still pending
  - is anyone logged in, and via which logon type
  - what did the System log record around the reboot (1074/6005/6006/41/6008)

Talks to qemu-ga over the QGA socket. Note the two QGA quirks: the agent sends no
greeting (a guest-sync must go first), and QMP does not proxy guest-* commands,
so this goes straight at qga.sock.
"""

import base64
import json
import socket
import sys
import time

SOCK = sys.argv[1] if len(sys.argv) > 1 else "/data/vms/win11/qga.sock"

PS = r"""
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$os = Get-CimInstance Win32_OperatingSystem
[pscustomobject]@{
  product   = $cv.ProductName
  edition   = $cv.EditionID
  version   = $cv.DisplayVersion
  build     = "$($cv.CurrentBuild).$($cv.UBR)"
  boot      = $os.LastBootUpTime.ToString('yyyy-MM-dd HH:mm:ss')
  uptime_h  = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 2)
  pending_cbs   = Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending'
  pending_wu    = Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'
  pending_rename = [bool](Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations -ErrorAction SilentlyContinue)
} | ConvertTo-Json -Compress
"""

PS_RDP = r"""
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ts  = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections
$pl  = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device' -Name DevicePasswordLessBuildVersion -ErrorAction SilentlyContinue
$pw  = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' -Name HiberbootEnabled -ErrorAction SilentlyContinue
[pscustomobject]@{
  rdp_deny          = $ts.fDenyTSConnections
  passwordless      = $pl.DevicePasswordLessBuildVersion
  fast_startup      = $pw.HiberbootEnabled
  timezone          = (Get-TimeZone).Id
  utc               = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ss')
  c_free_gb         = [math]::Round((Get-PSDrive C).Free/1GB, 1)
} | ConvertTo-Json -Compress
"""

PS_SESS = r"""
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$s = (query user 2>&1) -join ' | '
"{0}" -f $s
"""

PS_EVT = r"""
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Get-WinEvent -FilterHashtable @{LogName='System'; Id=1074,6005,6006,6008,41; StartTime=(Get-Date).AddHours(-12)} -MaxEvents 12 -ErrorAction SilentlyContinue |
  Sort-Object TimeCreated -Descending |
  ForEach-Object { "{0} [{1}] {2}" -f $_.TimeCreated.ToString('MM-dd HH:mm:ss'), $_.Id, ($_.Message -split "`n")[0] }
"""


def connect(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(30)
    s.connect(path)
    return s, s.makefile("rw")


def call(f, cmd, args=None):
    f.write(json.dumps({"execute": "guest-sync", "arguments": {"id": 7}}) + "\n")
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


def run_ps(f, text, timeout=60):
    enc = base64.b64encode(text.encode("utf-16-le")).decode()
    r = call(f, "guest-exec", {
        "path": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "arg": ["-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
        "capture-output": True,
    })
    if "error" in r:
        return "ERR: %s" % r["error"]
    pid = r["return"]["pid"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.4)
        st = call(f, "guest-exec-status", {"pid": pid})["return"]
        if st.get("exited"):
            out = base64.b64decode(st.get("out-data") or b"").decode("utf-8", "replace")
            err = base64.b64decode(st.get("err-data") or b"").decode("utf-8", "replace")
            return (out + err).strip()
    return "TIMEOUT"


def main():
    s, f = connect(SOCK)
    try:
        info = run_ps(f, PS)
        print("=== 客机状态 ===")
        try:
            d = json.loads(info)
            for k, v in d.items():
                print("  %-15s %s" % (k, v))
            if d.get("pending_cbs") or d.get("pending_wu") or d.get("pending_rename"):
                print("  >>> 还有挂起的重启（更新没走完）")
            else:
                print("  >>> 没有挂起的重启")
        except Exception:
            print(info)

        print("\n=== 远程访问相关（feature update 常把这些改回去）===")
        rdp = run_ps(f, PS_RDP)
        try:
            d2 = json.loads(rdp)
            for k, v in d2.items():
                print("  %-15s %s" % (k, v))
            if str(d2.get("rdp_deny")) == "1":
                print("  >>> RDP 被关了（fDenyTSConnections=1），要重新开")
            if str(d2.get("passwordless")) == "2":
                print("  >>> Hello 无密码模式又开了，微软账户密码登录会再次失败")
        except Exception:
            print(rdp)

        print("\n=== 已登录会话 ===")
        print("  " + (run_ps(f, PS_SESS) or "(无)"))

        print("\n=== 最近 12 小时的关键事件 ===")
        print(run_ps(f, PS_EVT) or "(无)")
    finally:
        s.close()


if __name__ == "__main__":
    main()
