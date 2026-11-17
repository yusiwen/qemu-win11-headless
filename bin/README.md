# win11/bin — every script for this VM

All of them are stdlib Python or POSIX shell: no packages, no dependencies. Each
file carries a docstring explaining the mechanism it exploits. This document is the
guided tour — **what problem each script solves, how it works inside, and the trap
it was written to escape**.

> 中文版：[README-zh_cn.md](README-zh_cn.md)

## The three channels

```
                ┌────────────── host (nuc12) ────────────────┐
 your Mac ──ssh─┤                                            │
                │  qmp.sock ◄── QMP ──┐                      │
                │                     ├── QEMU ── guest Windows
                │  qga.sock ◄── QGA ──┘                      │
                │  :5901    ◄── RFB (VNC) ──┘                │
                └────────────────────────────────────────────┘
```

| Channel | Talks to | Can do | Cannot do |
|---|---|---|---|
| **QMP** (`qmp.sock`) | host ↔ QEMU itself | power, status, screendump, change the VNC password, device tree | **cannot** run anything inside the guest |
| **QGA** (`qga.sock`) | host ↔ qemu-ga in the guest | run commands, read network config, graceful shutdown | needs guest tools installed |
| **RFB/VNC** (`:5901`) | client ↔ QEMU's framebuffer | watch the screen, move the mouse | there is no such thing as "the client sets the resolution" |

**The misconception that costs the most time**: that QMP forwards guest commands
(`qmp.py … guest-ping` answers `CommandNotFound`). That forwarding layer is
**libvirt's**, not QEMU's. Anything inside the guest goes through `qga.py`.

## Python scripts

### `qmp.py` — the QEMU control plane
```bash
./qmp.py /data/vms/win11/qmp.sock query-status
./qmp.py /data/vms/win11/qmp.sock change-vnc-password '{"password":"xxx"}'
```
JSON-lines over a unix socket. On connect you first receive a `{"QMP":{…}}` greeting,
then you must send `{"execute":"qmp_capabilities"}` before anything else works.
Replies can be interleaved with asynchronous **events** (`{"event":…}`), so the reader
has to skip until it sees `return` or `error` — that is what the `while True` is for.

### `qga.py` — the guest-agent client
```bash
./qga.py /data/vms/win11/qga.sock guest-ping
./qga.py /data/vms/win11/qga.sock guest-info | head -c 300
./qga.py /data/vms/win11/qga.sock guest-exec '{"path":"C:\\Windows\\System32\\cmd.exe","arg":["/c","ver"],"capture-output":true}'
```
Two traps, both handled in the code:

1. **qemu-ga never greets you.** A client that simply `readline()`s waits until it
   times out. Send `{"execute":"guest-sync","arguments":{"id":N}}` first, wait for
   `{"return": N}`, and only then send the real command.
2. **`guest-shutdown` disconnects before it replies.** The guest starts shutting down,
   the socket closes, and the response never arrives. The code treats "EOF after
   sending shutdown" as success — otherwise systemd would record the stop as failed.
   This script *is* the `ExecStop` of `win11.service`.

### `screen.py` — a screenshot with no display attached
```bash
./screen.py                      # -> /data/vms/win11/logs/screen.png
./screen.py --out /tmp/x.png
```
Two steps: QMP `screendump` makes QEMU write the framebuffer as **PPM (P6)**, then the
script hand-writes a PNG (signature + IHDR + IDAT + IEND, each chunk being
`length|type|data|CRC32`, with a filter byte of 0 in front of every scanline).

Why it is worth reading: PPM viewers are rare, PNG opens in Preview — and this is the
**only** diagnostic channel before the guest has a network or an agent. It confirmed
three things here: that OVMF saw the CD, that "press any key" timed out into PXE, and
whether the desktop had actually come up.

### `iso-verify.py` — look inside an ISO without mounting, root, or extra packages
```bash
./iso-verify.py /data/vms/iso/virtio.iso 'viostor/w11/amd64' 'guest-tools'
```
ISO9660 is three facts: volume descriptors start at sector 16 (bytes 1-5 are `CD001`);
the root directory record sits at offset 156 of the PVD, with the extent LBA as a
little-endian u32 (+2) and the length as u32 (+10); a directory is a run of records
`len(1) attr(1) lba(4) size(4) time(7) flags(1) … namelen(1) name`, where `flags & 2`
means directory.

Why it matters: **prove `viostor/w11/amd64` is on the media before you install**,
because a virtio-blk disk does not appear in the setup disk list and loading that
driver is the only way forward.

### `measure-qemu-cpu.py` — precise QEMU CPU accounting (better than `top`)
```bash
./measure-qemu-cpu.py 12         # sample for 12 s
```
Reads fields 14/15 of `/proc/<pid>/task/*/stat` (utime + stime, in `SC_CLK_TCK`
ticks), takes the delta at both ends and divides by elapsed time. `top`'s instantaneous
value lies in virtualisation: the first reading here was 185 %, while precise sampling
showed a 100 % steady state — the 185 % was Windows Update.

**What the numbers bought**: the main thread (VNC compression included) sits at
~30 %, the five vCPU threads at 8–16 % each, and the guest reports 18 % (≈1.08 cores)
— they agree, so there is no hidden overhead; it is all the guest working.

### `guest-load.py` — ask the guest what it is busy with
```bash
./guest-load.py
```
Runs PowerShell over QGA `guest-exec`: CPU load, top processes, Windows Update and
Defender service state, pending updates. The technique is **base64 +
`-EncodedCommand`**: complex PowerShell shoved onto a command line gets eaten by
quoting and by the layers of shell in between (three wasted rounds here). Encode as
UTF-16LE base64, pass it as one argument, and it works first time.

### `guest-ps.py` — the generic hammer: arbitrary PowerShell in the guest
```bash
./guest-ps.py script.ps1              # run and print
./guest-ps.py - < script.ps1          # from stdin
./guest-ps.py --detach long-job.ps1   # start, print the pid, return
```
Everything above is a purpose-built wrapper around this pattern; this one takes
whatever script you have. `--detach` matters: deleting `C:\Windows.old` takes ~15
minutes, so start it, write progress to a log inside the guest, poll from outside.
It decodes output as UTF-8 and falls back to GBK, because Windows PowerShell on a pipe
uses the OEM code page and turns Chinese into mojibake.

### `check-update-state.py` — the post-Windows-Update physical
```bash
./check-update-state.py
```
One call answers "what did the update break": version and build, boot time and uptime,
all three pending-reboot flags, `fDenyTSConnections` (RDP switch),
`DevicePasswordLessBuildVersion` (Hello passwordless), `HiberbootEnabled` (Fast
Startup), time zone, UTC, C: free space, plus the last 12 hours of
1074/6005/6006/6008/41 events.

Why it is a script: feature updates turn deliberately-disabled switches back on
(`HiberbootEnabled` went 0 → 1 here), and whether RDP works — and whether a Microsoft
account password can log in at all — depends on two registry values not being rewritten.
"it connects now" is not verification; each value has to be read.

### `check-display.py` / `enum-display-modes.py` — adapter and resolution
```bash
./check-display.py               # adapter name, current resolution, driver version
./enum-display-modes.py          # should list modes; measured count=0 (see below)
```
`check-display.py` goes through `Win32_VideoController` and shows
"Microsoft Basic Display Adapter / 1280 / 800" — the root cause of the window being
stuck at one size.

`enum-display-modes.py` P/Invokes `EnumDisplaySettings` to list every mode and returns
**0 here**, because qemu-ga runs in Windows **session 0** (the service session), which
has no interactive desktop. That is a useful negative result: enumerating the guest's
resolutions from the host is not possible; it has to happen inside an RDP or
interactive session.

### `rdp-probe.py` — protocol-level RDP, instead of lying to yourself with `nc`
```bash
./rdp-probe.py                  # 127.0.0.1:13389
```
**`nc -vz 127.0.0.1 13389` is a false positive here**: QEMU's SLIRP completes the TCP
handshake itself and only then tries the guest, so a guest with nothing on 3389 still
looks open. This script sends the real first RDP packet (TPKT + X.224 Connection
Request, with the mstshash cookie and an NLA negotiation request); only a genuine
Terminal Services stack answers with X.224 **Connection Confirm (0xD0)** and reports
that it negotiated `PROTOCOL_HYBRID` (CredSSP/NLA).

### `enable-rdp.py` — idempotent remote desktop enablement
```bash
./enable-rdp.py
```
Four things in one shot: `fDenyTSConnections=0`, `TermService` set to automatic and
started, and the firewall rule group `@FirewallAPI.dll,-28752` enabled — that is the
**language-independent group id**; its display name is localised, so looking it up by
name fails on a Chinese system. "I turned it on in the GUI and it still isn't on" is
the most common shape of this problem.

### `check-rdp.py` — RDP admission physical
```bash
./check-rdp.py
```
Checks the switch, the listener, the NLA requirement, logged-on users, account source
(local / MicrosoftAccount), the Remote Desktop Users group, the Administrators group and
the firewall direction. When "the password is right but logon fails", look at the
account source first — a Microsoft account needs the MS password, a PIN never works.

### `check-rdp-logon.py` — read the error code instead of guessing
```bash
./check-rdp-logon.py
```
Reads 4625 (failed logon) from the guest's security log with **Status/SubStatus**:

| SubStatus | Meaning | What to fix |
|---|---|---|
| `0xC000006A` | wrong password | the password |
| `0xC0000064` | no such user | username format / spelling |
| `0xC0000234` | account locked | wait, or check 4740 |
| `0xC0000072` | account disabled | enable it |
| `0xC000015B` | logon type not granted | `SeRemoteInteractiveLogonRight` |

It also prints the members of `SeRemoteInteractiveLogonRight` (SIDs resolved to names),
the account's enabled / PrincipalSource / PasswordLastSet, and RDP-Tcp's
securitylayer/usrauth. **This separates "not permitted", "no such account" and "wrong
password" in one shot** — far cheaper than retrying passwords.

### `verify-rdp-works.py` — confirm RDP actually works
```bash
./verify-rdp-works.py
```
Three pieces of evidence: `DevicePasswordLessBuildVersion` (2 = Hello-only, 0 = password
allowed), `Security 4624 LogonType=10` (RemoteInteractive, a real RDP session), and the
session table from `query user`. **Do not accept `LogonType=11`** (CachedInteractive)
as RDP success — that is a console logon from cache.

### `fix-guest-time.py` — time zone versus absolute time
```bash
./fix-guest-time.py [--tz "China Standard Time"]
```
Two steps: `Set-TimeZone` plus QGA `guest-set-time` (epoch nanoseconds, **zone
independent**, no NTP required from the guest).

Why it is needed: `-rtc base=localtime` only puts the host's wall clock into the RTC;
Windows still interprets it in **its own** zone. A fresh install landed on
`FLE Standard Time (UTC+2)`, so the displayed clock looked right while **UTC was six
hours ahead** — and certificate validation and Microsoft-account verification run on
UTC. That is what made OOBE report "PIN setup failed" on this machine.

### `check-shutdown-events.py` — was the shutdown actually clean
```bash
./check-shutdown-events.py
```
Three events: **1074** (who initiated), **6006** (event log service stopped = the
shutdown completed), **41** (Kernel-Power: the previous stop was not clean). Verdict:
1074 + 6006 and no new 41 = clean. This is the evidence that revealed
"`systemctl stop` is really a power cut" in an earlier version of this setup.

## Shell scripts

| Script | What it does |
|---|---|
| `start-vm.sh` | starts swtpm → starts QEMU (`-daemonize`) → sets the VNC password over QMP. Why each flag is there: `../README-zh_cn.md` §4 |
| `boot-from-cd.sh` | boots the installer and **presses space for you**; Windows' "Press any key to boot from CD" waits ~5 s and headless always loses that race |

## Guest-side PowerShell

| Script | What it does |
|---|---|
| `cleanup-windows-old.ps1` | removes `C:\Windows.old` after feature upgrades, then TRIMs the volume so the host image actually shrinks. `rd /s /q` as SYSTEM leaves ~40 % of the files behind (TrustedInstaller ACLs), so the takeown+icacls fallback is mandatory. Run it with `guest-ps.py --detach` |
| `enable-rdp.py` etc. | see above — these also run *in* the guest, driven from the host |

## guacamole/bin/ — the browser-gateway side

| Script | What it does |
|---|---|
| `../guacamole/bin/guac-setup.py` | idempotent bootstrap: rotate the admin password off the factory `guacadmin`, then create/update the `win11-rdp` connection from `.env` |
| `../guacamole/bin/guac-probe.py` | speaks the Guacamole protocol to guacd directly (`select` → `args` → `connect` → `ready`) and separates "path broken" from "credentials wrong". `--hold N` keeps listening after `ready` and judges liveness by whether frames flow |

Why a protocol probe is worth its own script: HTTP 200 and a running container prove
nothing about the RDP leg, and `ready` is not authentication success either — guacd's
RDP driver connects on its own thread, so bad credentials only show up *after* `ready`
as a closed connection. Same philosophy as `rdp-probe.py`: never trust the port, trust
the protocol. Details: `../guacamole/README.md` §5.

## The method behind these experiments (reusable)

1. **Measure first, then change.** `measure-qemu-cpu.py` → change one variable (say
   `kvm halt_poll_ns`) → sample twice more. The A/B here (0 vs 200000) gave 103.6 % vs
   103.3 % — **no difference**, so it went back to the default. A tuning knob that
   shows no gain does not stay in.
2. **Never trust a single sample.** The same configuration measured 103 % and then
   76 %; the difference was the guest running Windows Update. Conclusions need
   "repeat under the same guest state".
3. **Look for disproof.** To prove a shutdown was soft, do not check whether
   `systemctl stop` printed an error — read the guest's event log. To prove the CD
   boots, do not check that a port is listening — take a screenshot.
