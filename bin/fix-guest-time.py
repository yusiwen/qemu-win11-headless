#!/usr/bin/env python3
"""Fix the guest time zone / clock over QGA.

    ./fix-guest-time.py [--tz "China Standard Time"]

Why: with `-rtc base=localtime` QEMU hands Windows the host's wall clock, but
Windows interprets it with **its own** time zone. A guest left on the default
(FLE Standard Time / UTC+2 was what OVMF+setup produced here) then has a UTC
clock that is hours off even though the displayed time looks right. Skewed UTC
breaks certificate validation and Microsoft-account features, and it silently
explains "PIN setup failed" during OOBE.
"""
import base64
import json
import socket
import sys
import time

SOCK = "/data/vms/win11/qga.sock"
TZ = "China Standard Time"
if "--tz" in sys.argv:
    TZ = sys.argv[sys.argv.index("--tz") + 1]

PS = """
$ErrorActionPreference = 'Continue'
try { Set-TimeZone -Id '%s'; 'set-timezone: ok' } catch { 'set-timezone: ' + $_.Exception.Message }
try { w32tm /resync /force | Out-Null; 'resync: ok' } catch { 'resync: ' + $_.Exception.Message }
'local = ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')
'utc   = ' + (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ss')
'tz    = ' + (Get-TimeZone).Id
""" % TZ

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(60)
s.connect(SOCK)
f = s.makefile("rw")


def call(cmd, args=None):
    f.write(json.dumps({"execute": "guest-sync", "arguments": {"id": 51}}) + "\n")
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


# 2) absolute clock: QGA guest-set-time takes nanoseconds since the epoch and sets
#    the guest's UTC clock directly, which is time-zone independent and does not
#    depend on the guest having a reachable NTP server.
ns = int(time.time() * 1e9)
settime = call("guest-set-time", {"time": ns})
print("guest-set-time:", json.dumps(settime.get("return", settime.get("error"))))

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
        print("exit=%s" % st.get("exitcode"))
        print(base64.b64decode(st.get("out-data") or "").decode("utf-8", "replace").strip())
        err = base64.b64decode(st.get("err-data") or "").decode("utf-8", "replace").strip()
        if err:
            print("STDERR:", err[:500])
        break
else:
    sys.exit("timeout")
