#!/usr/bin/env python3
"""Diagnose a failed RDP logon from inside the guest, over QGA.

    ./check-rdp-logon.py

Reads what Windows actually recorded, instead of guessing:

  * Security 4625 - failed logon, with Status/SubStatus telling you WHY
      0xC000006A password wrong        0xC0000064 no such user
      0xC0000234 account locked        0xC0000072 account disabled
      0xC000015B logon type not granted (missing SeRemoteInteractiveLogonRight)
      0xC000006E logon restriction / time restriction
  * Security 4740 - account locked out
  * Security 4624 - successful logon, logon type 10 = RemoteInteractive
  * TerminalServices-LocalSessionManager 1149 - RDP auth succeeded
  * SeRemoteInteractiveLogonRight - who is allowed to log on via RDP
  * account state (enabled, password last set, local vs MicrosoftAccount)
"""
import base64
import json
import socket
import sys
import time

SOCK = "/data/vms/win11/qga.sock"

PS = r"""
function Ev($log, $ids, $mins) {
  try {
    Get-WinEvent -FilterHashtable @{LogName=$log; Id=$ids; StartTime=(Get-Date).AddMinutes(-$mins)} -ErrorAction Stop |
      ForEach-Object {
        $x = [xml]$_.ToXml(); $d = @{}
        if ($x.Event.EventData.Data) { $x.Event.EventData.Data | ForEach-Object { $d[$_.Name] = $_.'#text' } }
        '{0} id={1} user={2} status={3} substatus={4} logontype={5}' -f `
          $_.TimeCreated.ToString('HH:mm:ss'), $_.Id, $d['TargetUserName'], $d['Status'], $d['SubStatus'], $d['LogonType']
      }
  } catch { "  (no events: $($_.Exception.Message.Split([char]10)[0]))" }
}

'--- 4625 failed logons (last 60 min)'
Ev 'Security' 4625 60
'--- 4740 lockouts (last 60 min)'
Ev 'Security' 4740 60
'--- 4624 successful logons type 10 (last 60 min)'
Ev 'Security' 4624 60
'--- RDP session auth 1149'
Ev 'Microsoft-Windows-TerminalServices-LocalSessionManager/Operational' 1149 60
'--- who may log on via RDP (SeRemoteInteractiveLogonRight)'
$inf = Join-Path $env:TEMP 'hermes_sec.inf'
secedit /export /cfg $inf /quiet | Out-Null
(Select-String -Path $inf -Pattern 'SeRemoteInteractiveLogonRight').Line
Get-Content $inf | Where-Object { $_ -match '^SeRemoteInteractiveLogonRight' } | ForEach-Object {
  ($_ -split '=')[1].Split(',') | ForEach-Object {
    $s = $_.Trim()
    if ($s) { try { $s + ' = ' + (New-Object System.Security.Principal.SecurityIdentifier($s)).Translate([System.Security.Principal.NTAccount]).Value } catch { $s } }
  }
}
'--- account state'
$u = Get-LocalUser -Name 'yusiw' -ErrorAction SilentlyContinue
if ($u) { '{0} enabled={1} source={2} pwdlastset={3} sid={4}' -f $u.Name, $u.Enabled, $u.PrincipalSource, $u.PasswordLastSet, $u.SID.Value }
'--- RDP protocol settings'
$k = 'HKLM:\System\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp'
'{0} securitylayer={1} usrauth={2} minencrypt={3}' -f 'RDP-Tcp', (Get-ItemProperty $k).SecurityLayer, (Get-ItemProperty $k).UserAuthentication, (Get-ItemProperty $k).MinEncryptionLevel
"""


def main():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(60)
    s.connect(SOCK)
    f = s.makefile("rw")

    def call(cmd, args=None):
        f.write(json.dumps({"execute": "guest-sync", "arguments": {"id": 41}}) + "\n")
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
    for _ in range(240):
        time.sleep(0.5)
        st = call("guest-exec-status", {"pid": pid})["return"]
        if st.get("exited"):
            out = base64.b64decode(st.get("out-data") or "").decode("utf-8", "replace")
            err = base64.b64decode(st.get("err-data") or "").decode("utf-8", "replace")
            print(out)
            if err.strip():
                print("STDERR:", err.strip()[:600])
            return
    sys.exit("timeout")


if __name__ == "__main__":
    main()
