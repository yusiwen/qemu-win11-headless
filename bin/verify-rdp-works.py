#!/usr/bin/env python3
"""Verify the passwordless-Windows-Hello setting and recent successful RDP logons.

    ./verify-rdp-works.py

Why: Windows 11 turns on "For improved security, only allow Windows Hello sign-in
for Microsoft accounts on this device" (DevicePasswordLessBuildVersion = 2) by
default. In that state the local SAM has no usable password verifier for the
Microsoft-account-backed user, so NLA/CredSSP answers 0xC000006A (bad password)
no matter what you type — and the console lock screen doesn't even offer the
password tile. Setting it to 0 and signing in once with the password populates
the cache, after which RDP works.

Checks:
  * registry DevicePasswordLessBuildVersion (2 = passwordless, 0 = password allowed)
  * Security 4624 with LogonType 10 = RemoteInteractive = a real RDP session
  * TerminalServices-LocalSessionManager 1149 = RDP authentication succeeded
"""
import base64
import json
import socket
import sys
import time

SOCK = "/data/vms/win11/qga.sock"

PS = r"""
'--- passwordless setting (2 = Hello-only, 0 = password logon allowed)'
$k = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device'
if (Test-Path $k) { 'DevicePasswordLessBuildVersion = ' + (Get-ItemProperty $k).DevicePasswordLessBuildVersion } else { 'key missing (defaults to allowed)' }

'--- successful RDP logons (4624 LogonType 10, last 120 min)'
try {
  Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4624; StartTime=(Get-Date).AddMinutes(-120)} -ErrorAction Stop |
    ForEach-Object {
      $x = [xml]$_.ToXml(); $d = @{}
      if ($x.Event.EventData.Data) { $x.Event.EventData.Data | ForEach-Object { $d[$_.Name] = $_.'#text' } }
      if ($d['LogonType'] -eq '10') {
        '{0} user={1} src={2} auth={3}' -f $_.TimeCreated.ToString('HH:mm:ss'), $d['TargetUserName'], $d['IpAddress'], $d['AuthenticationPackageName']
      }
    }
} catch { '  (no 4624 events readable: ' + $_.Exception.Message.Split([char]10)[0] + ')' }

'--- RDP auth success events (1149)'
try {
  Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-TerminalServices-LocalSessionManager/Operational'; Id=1149; StartTime=(Get-Date).AddMinutes(-120)} -ErrorAction Stop |
    Select-Object -First 5 | ForEach-Object {
      $x = [xml]$_.ToXml(); $d = @{}
      if ($x.Event.EventData.Data) { $x.Event.EventData.Data | ForEach-Object { $d[$_.Name] = $_.'#text' } }
      '{0} {1}' -f $_.TimeCreated.ToString('HH:mm:ss'), $d['User']
    }
} catch { '  (none)' }

'--- current sessions (query user)'
(query user) 2>&1
"""


def main():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(60)
    s.connect(SOCK)
    f = s.makefile("rw")

    def call(cmd, args=None):
        f.write(json.dumps({"execute": "guest-sync", "arguments": {"id": 61}}) + "\n")
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
    for _ in range(180):
        time.sleep(0.5)
        st = call("guest-exec-status", {"pid": pid})["return"]
        if st.get("exited"):
            print(base64.b64decode(st.get("out-data") or "").decode("utf-8", "replace").strip())
            return
    sys.exit("timeout")


if __name__ == "__main__":
    main()
