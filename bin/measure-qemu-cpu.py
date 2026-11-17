#!/usr/bin/env python3
import os
import sys
import time

PID_FILE = "/data/vms/win11/qemu.pid"
DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
pid = int(open(PID_FILE).read().strip())
HZ = os.sysconf("SC_CLK_TCK")


def snap(p):
    out = {}
    for t in os.listdir("/proc/%d/task" % p):
        try:
            parts = open("/proc/%d/task/%s/stat" % (p, t)).read().split()
            out[t] = int(parts[13]) + int(parts[14])
        except OSError:
            pass
    return out


a = snap(pid)
t0 = time.time()
time.sleep(DUR)
b = snap(pid)
dt = time.time() - t0
total = (sum(b.values()) - sum(a.values())) / HZ / dt * 100
poll = open("/sys/module/kvm/parameters/halt_poll_ns").read().strip()
guest = "?"

rows = sorted(((b[t] - a.get(t, 0)) / HZ / dt * 100, t) for t in b)[::-1]
print("halt_poll_ns=%-8s QEMU 合计 %5.1f%%  (窗口 %.0fs, 宿主 %d 线程)"
      % (poll, total, dt, os.cpu_count()))
for pct, t in rows[:6]:
    print("    thread %-8s %5.1f%%" % (t, pct))
