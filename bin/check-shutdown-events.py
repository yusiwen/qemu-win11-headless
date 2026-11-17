#!/usr/bin/env python3
import base64
import json
import socket
import sys
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


def run(path, args, label):
    r = call("guest-exec", {"path": path, "arg": args, "capture-output": True})
    if "error" in r:
        print(label, "ERR", r)
        return
    pid = r["return"]["pid"]
    for _ in range(40):
        time.sleep(0.4)
        st = call("guest-exec-status", {"pid": pid})["return"]
        if st.get("exited"):
            out = base64.b64decode(st.get("out-data") or "").decode("utf-8", "replace")
            err = base64.b64decode(st.get("err-data") or "").decode("utf-8", "replace")
            code = st.get("exitcode")
            print("--- %s exit=%s" % (label, code))
            print(out.strip()[:900] or "(no stdout)")
            if err.strip():
                print("STDERR:", err.strip()[:300])
            return
    print(label, "did not finish")


W = r"C:\Windows\System32\wevtutil.exe"
run(W, ["qe", "System", "/q:*[System[EventID=6006]]", "/c:3", "/rd:true", "/f:text"], "6006 clean-shutdown")
run(W, ["qe", "System", "/q:*[System[EventID=1074]]", "/c:3", "/rd:true", "/f:text"], "1074 initiated-by")
run(W, ["qe", "System", "/q:*[System[EventID=41]]", "/c:3", "/rd:true", "/f:text"], "41 hard-power-loss")
