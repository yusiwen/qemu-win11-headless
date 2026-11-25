# Headless Windows 11 on QEMU/KVM

A field-tested runbook for running a Windows 11 guest on a **display-less** Linux
host, driven entirely over SSH — no physical console, no libvirt, no Proxmox. Two
ways in: native RDP, and a browser via Apache Guacamole.

Everything here was built, broken and fixed on one real machine; the numbers in this
document are measurements, not estimates. The exhaustive Chinese write-up — every
failed attempt included — is in **[README-zh_cn.md](README-zh_cn.md)**.

```
host  nuc12wski5 · Ubuntu 24.04.5 · i5-1240P (4P+8E/16T) · 62 GiB RAM
guest Windows 11 26H2 (build 26300.9974) · 6 vCPU · 12 GiB · UEFI + Secure Boot + TPM 2.0
disk  120 GiB qcow2, 32 GiB allocated, sparse, TRIM wired through to the host
```

## Contents

| Path | What it is |
|---|---|
| `README-zh_cn.md` | The complete manual: every flag explained, 12 troubleshooting sections, full change log |
| `README.md` | This file — the short version |
| `bin/` | 20 helper scripts (QMP/QGA clients, protocol probes, guest forensics) + [`bin/README.md`](bin/README.md) |
| `win11.service` | systemd unit (Type=forking, QGA graceful stop) |
| `vm.conf` | Boot order only (`BOOT=c`), sourced by the start script |
| `guacamole/` | Browser gateway: compose + properties + bootstrap, see [`guacamole/README.md`](guacamole/README.md) |
| `TROUBLESHOOTING.md` | Sessions and keystrokes: three faults that are not where they look · [中文](TROUBLESHOOTING-zh_cn.md) |

## Architecture

```
                    ┌─────────────────────── host: nuc12wski5 ───────────────────────┐
  Windows App ──RDP──▶ 0.0.0.0:13389 (QEMU hostfwd) ─┐                              │
                    │                                ├─▶ SLIRP ─▶ guest 10.0.2.15:3389│
  browser ──HTTP──▶  0.0.0.0:8081 guacamole ─▶ guacd ┘                             │
                    │   (127.0.0.1:4822 guacd)                                       │
  browser ──HTTPS─▶ VPS nginx (Authelia) ──wireguard──▶ the 8081 above               │
  ssh tunnel ─────▶  127.0.0.1:5901 QEMU VNC ───────▶ framebuffer (rescue only)     │
                    └────────────────────────────────────────────────────────────────┘
```

| Port | Bind | What | Why exposed that way |
|---|---|---|---|
| 13389 | `0.0.0.0` | RDP → guest 3389 | Direct LAN access; NLA + TLS underneath |
| 8081 | `0.0.0.0` | Guacamole webapp, plain HTTP | LAN + the front proxy. TLS is terminated on the VPS nginx, so this hop stays plain — the browser's secure context (clipboard) comes from that certificate |
| 5901 | `127.0.0.1` | QEMU VNC console | Rescue only; RFB is unencrypted, so SSH tunnel or nothing |
| 4822 | `127.0.0.1` | guacd | Internal hop; published on loopback only for the probe script |

## Quick start

```bash
# 1. host packages
sudo apt install -y qemu-system-x86 qemu-utils ovmf swtpm
sudo usermod -aG kvm "$USER"

# 2. storage and firmware state
D=/data/vms/win11
mkdir -p $D/{iso,ovmf,tpm,logs}
qemu-img create -f qcow2 -o preallocation=metadata,lazy_refcounts=on $D/win11.qcow2 120G
cp /usr/share/OVMF/OVMF_VARS_4M.ms.fd $D/ovmf/WIN11_VARS.fd

# 3. swtpm is AppArmor-confined; allow the datastore
sudo tee /etc/apparmor.d/local/usr.bin.swtpm >/dev/null <<'EOF'
owner /data/vms/win11/tpm/ rw,
owner /data/vms/win11/tpm/** rwk,
owner /data/vms/win11/logs/swtpm.log rwk,
EOF
sudo apparmor_parser -r /etc/apparmor.d/usr.bin.swtpm

# 4. install Windows over VNC (headless: hammer space so the CD boots)
ssh -N -L 5901:127.0.0.1:5901 user@host   # then open vnc://127.0.0.1:5901
sudo systemctl start win11
./bin/boot-from-cd.sh                     # answers the "press any key" prompt over QMP

# 5. once Windows is in, make remote access real
./bin/enable-rdp.py        # idempotent: fDenyTSConnections=0, TermService, firewall group
./bin/rdp-probe.py         # protocol-level check, not `nc`
./bin/verify-rdp-works.py  # guest-side 4624 LogonType=10

# 6. optional: browser access
cd guacamole && ./bootstrap.sh
```

## What has been verified on this deployment

| Item | Measured |
|---|---|
| Boot chain | OVMF + Secure Boot + swtpm TPM 2.0; Windows 11 installs with no bypass |
| Guest tools | qemu-ga 110.0.2 over virtio-serial; `guest-exec` drives the guest from the host |
| Clean shutdown | `systemctl stop` → guest events 1074 + 6006, **no** Kernel-Power 41 |
| RDP | `X.224 Connection Confirm`, `PROTOCOL_HYBRID` (CredSSP/NLA), 4624 LogonType=10 |
| Guest clock | within 1 s of the host's UTC after `Set-TimeZone` + QGA `guest-set-time` |
| Idle CPU | ~100 % of one core for the whole guest (≈6 % of the host's 16 threads) |
| Disk | 32 GiB allocated of 120 GiB virtual; guest TRIM reaches the host (`discard=unmap`) |
| Snapshots | one internal snapshot `post-26h2`; creating one costs only metadata |
| Two feature upgrades | 23H2 → 25H2 → 26H2 in one day, no reinstall, settings survived |

## Design decisions

| Decision | Why |
|---|---|
| **Plain QEMU + one systemd unit** | One VM. libvirt adds a daemon, a storage-pool model, XML and a permission layer to solve problems this setup does not have. Revisit at 3+ VMs, or when you need live snapshots/clones. |
| **UEFI + Secure Boot + TPM 2.0** (`OVMF_CODE_4M.secboot.fd`, `OVMF_VARS_4M.ms.fd`, `swtpm`) | Windows 11 refuses otherwise; the MS-key VARS file means no manual key enrolment. |
| **`e1000e`, not `virtio-net`** | In-box driver: the guest has network before any driver install, so OOBE and RDP work immediately. 1 Gbps is plenty for RDP. |
| **`virtio-blk`, not IDE/NVMe** | Fastest; the only cost is loading `viostor` from the virtio-win CD during setup. |
| **NAT + `hostfwd`, never a bridge** | The host's single NIC is a k0s/docker node managed by cloud-init netplan. A bad bridge on a headless box has no recovery path. |
| **VNC stays on loopback** | RFB is unencrypted; it exists for BIOS, blue screens and the installer, not daily use. |
| **Guacamole, not KasmVNC** | KasmVNC is a VNC *server* (its own X server) and cannot attach to someone else's VNC/RDP endpoint. Kasm *Workspaces* can — and its web-native RDP path *is* guacd. For one VM, use the engine directly. |
| **No GPU passthrough** | The iGPU has no GVT-g/SR-IOV, and RDP encoding happens inside the guest anyway. Zero payoff. |
| **RDP daily, VNC for rescue** | RFB cannot resize the guest's resolution from the client; RDP with `resize-method=display-update` can. |
| **TLS at the front proxy, not on this host** | The Clipboard API only exists in a secure context, and that context is decided by the origin the browser talks to. With nginx (Authelia + Let's Encrypt) in front, plain HTTP on the LAN hop costs nothing; a Caddy overlay remains available for direct browser access on the LAN, where clipboard would otherwise die silently. |

## Pitfalls

The reason this repo is worth publishing: each of these cost real time, and several
produce symptoms that point at the wrong layer.

**Hypervisor / boot**

- `swtpm` under `/data` dies with *"Could not open UnixIO socket: Permission denied"* — exit 1, empty stderr. Ubuntu's AppArmor profile only allows libvirt paths; add a `local/` override.
- *"Press any key to boot from CD"* waits ~5 s, then falls through to PXE. Headless installs have to hammer space over QMP (`bin/boot-from-cd.sh`).
- **Never put a comment line inside the backslash-continued QEMU command.** The continuation swallows it, `#` comments out the rest, and every later argument is silently dropped — the symptom is QEMU trying to open a GUI: `gtk initialization failed`, because `-vnc`/`-qmp` never arrived. Validate with `sh -n` and a real restart.
- `hostfwd=tcp:127.0.0.1:PORT` is loopback-only. Bind `0.0.0.0` for direct LAN access.

**Windows guest**

- `-rtc base=localtime` does **not** set the guest's time zone. A fresh install came up on UTC+2 with UTC six hours off, which makes OOBE report "PIN setup failed". Fix with `Set-TimeZone` **and** QGA `guest-set-time`.
- A persistent `0xC000006A` on a Microsoft account is Windows Hello passwordless, not a typo: `DevicePasswordLessBuildVersion = 2` leaves no usable password verifier, so NLA rejects everything. Set it to `0`, sign in with the password once, then RDP works. Use the bare email — `MicrosoftAccount\<email>` resolves to *no such user* (`0xC0000064`).
- `systemctl stop` becomes a hard power cut (Kernel-Power 41) unless the QGA channel exists **and** `ExecStop` waits for the QEMU pid — systemd SIGTERMs the moment `ExecStop` returns.
- Feature updates re-enable Fast Startup (`HiberbootEnabled` 0 → 1). It leaves the guest half-hibernated, so host-side shutdown and image snapshots lie about the disk.

**Disks**

- Deleting files inside the guest does **not** shrink the qcow2. It needs `discard=unmap` in the QEMU command **and** a TRIM from the guest (`Optimize-Volume -ReTrim`).
- Internal snapshots pin clusters: with two stale snapshots the image stayed at 55 GiB while the guest held 40 GB. Deleting the snapshots took it to 32 GiB in seconds.
- `C:\Windows.old` after two feature upgrades: 37.6 GB, 390 k files. `rd /s /q` as SYSTEM still leaves ~168 k files owned by TrustedInstaller; `takeown` + `icacls` + `Remove-Item` is mandatory, not optional.

**Remote access / Guacamole**

- `nc -vz 127.0.0.1:13389` is a **false positive** through a SLIRP `hostfwd`: QEMU completes the TCP handshake before it ever consults the guest. Probe the protocol instead.
- A VNC server offering only *security type None* is unusable from macOS Screen Sharing. `-vnc ...,password=on` plus a QMP `change-vnc-password` fixes it.
- QMP does **not** proxy `guest-*` commands; talk to the QGA socket. qemu-ga sends no greeting, so a client must send `guest-sync` first or it times out.
- Guacamole: `initdb.sql` created under `umask 077` is unreadable by the postgres image (uid 70) — containers all "up", HTTP 200, and zero tables. `psql: Permission denied` is the only clue.
- guacd's `ready` arrives **before** authentication completes. Treating it as success reports a false pass; keep reading for `sync`/`img` frames.
- In the Guacamole protocol it is the **`connect`** instruction that carries connection parameters (in the order `args` listed them), not `select`. `error` is `(message, status)`.
- `api-session-timeout` defaults to 60 minutes of inactivity, which logs the browser out mid-task. Set it in `guacamole.properties`.

Full explanations for each of these, with command output: `README-zh_cn.md` §7.

Session-level faults have their own document — the session dropping minutes after the
browser loses focus, a password prompt on every reconnect, and `Ctrl+C` arriving as
`Ctrl+Shift+C`: **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)**.

## Operations cheat sheet

```bash
sudo systemctl start|stop|status win11      # stop = graceful shutdown via QGA
python3 bin/qmp.py /data/vms/win11/qmp.sock query-status
python3 bin/qga.py /data/vms/win11/qga.sock guest-ping
python3 bin/screen.py --out /tmp/screen.png # QMP screendump; works with no viewer attached
bin/check-update-state.py                   # build, uptime, pending reboots, RDP switches
bin/guest-ps.py - < some.ps1                # run arbitrary PowerShell inside the guest

# snapshots (VM stopped)
qemu-img snapshot -c post-26h2 /data/vms/win11/win11.qcow2
qemu-img snapshot -l -U        /data/vms/win11/win11.qcow2

# backup: image, TPM state and UEFI vars are one unit of state
rsync -a --sparse /data/vms/win11/{win11.qcow2,tpm,ovmf} backup:/
```

Snapshots are **not** backups: they live inside the same qcow2, so a bad disk takes
both. Back up the files.

### What happens on host poweroff

The guest is **shut down**, never hibernated. `win11.service` carries
`Conflicts=shutdown.target` / `Before=shutdown.target`, so a host poweroff stops it
and runs `ExecStop`, which sends `guest-shutdown` over QGA (not ACPI) and then waits
up to 170 s for the QEMU pid to disappear.

Measured on a real host poweroff: `logind: The system will power off now!` at
01:28:56 → unit `Deactivated` at 01:28:59 (**3 s**); the guest logged `1074` power-off
by `wininit.exe` on behalf of `NT AUTHORITY\SYSTEM` plus `6006` — and **no `41`**, no
`42`.

Two deliberate choices make this work:

- **QGA, not the ACPI power button.** Modern Windows maps `system_powerdown` to
  *sleep* — and this guest has S1/S2/S3/S0ix all unavailable, so an ACPI power event
  would hang or blank rather than shut down.
- **Fast Startup stays off** (`HiberbootEnabled=0`); otherwise "shutdown" is really
  half-hibernate and the disk state lies.

The one degradation: if Windows does not finish within 170 s (installing an update,
an app blocking shutdown) **or if QGA is dead**, systemd SIGTERMs and then SIGKILLs
QEMU after the 3-minute stop timeout — that is a power cut (guest logs `41` next
boot), still not hibernation.

## Repository conventions

Tracked: documents, scripts, the systemd unit, boot config, and the whole Guacamole
definition including `initdb.sql` (its header records the image tag it came from, and
`bootstrap.sh` warns rather than silently reusing a stale schema).

Not tracked, by design: the qcow2 image, TPM state, the UEFI variable store,
passwords (`vnc.pass`, `guacamole/.env`), runtime sockets and pids, and the postgres
data directory. **No password appears in any tracked file** — the docs point at the
gitignored file instead of printing the value.

## Documentation

Every document is English with a `-zh_cn` Chinese counterpart.

- **[bin/README.md](bin/README.md)** — what each script does and the experiments behind it · [中文](bin/README-zh_cn.md)
- **[guacamole/README.md](guacamole/README.md)** — the browser gateway in depth · [中文](guacamole/README-zh_cn.md)
- **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** — sessions and keystrokes: the three faults that look like gateway bugs · [中文](TROUBLESHOOTING-zh_cn.md)
- **[README-zh_cn.md](README-zh_cn.md)** — the full Chinese manual: every QEMU flag, the install walkthrough, 12 troubleshooting sections, complete change log

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, ship it; just keep the copyright notice.
