#!/bin/sh
# Windows 11 guest under plain QEMU/KVM: headless, UEFI + Secure Boot + TPM 2.0.
#   BOOT=d start-vm.sh   # first boots: installer CD first
#   BOOT=c start-vm.sh   # afterwards: disk first
# Normally started through systemd (win11.service), which supplies BOOT from
# /data/vms/win11/vm.conf. Every flag below is load-bearing - see README.md.
set -eu

VM=win11
DIR=/data/vms/$VM
ISO=/data/vms/iso
BOOT=${BOOT:-c}

# TPM 2.0 emulator must be up before QEMU opens the chardev socket.
# Start it only if it is not already alive (a stale socket file is not enough).
PIDF=$DIR/tpm/swtpm.pid
if [ ! -f "$PIDF" ] || ! kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null; then
  rm -f "$DIR/tpm/swtpm.sock"
  swtpm socket --tpm2 --tpmstate dir="$DIR/tpm" \
    --ctrl type=unixio,path="$DIR/tpm/swtpm.sock" \
    --pid file="$PIDF" --log file="$DIR/logs/swtpm.log" --daemon
  i=0
  while [ ! -S "$DIR/tpm/swtpm.sock" ] && [ "$i" -lt 50 ]; do
    i=$((i + 1)); sleep 0.1
  done
  [ -S "$DIR/tpm/swtpm.sock" ] || { echo "swtpm socket never appeared" >&2; exit 1; }
fi

# RDP forward listens on 0.0.0.0 so the Windows App can reach the guest directly
# at 192.168.2.48:13389, with or without an ssh tunnel. Switch it back to
# 127.0.0.1 if you want RDP reachable only through the tunnel. VNC stays on
# loopback on purpose: RFB is unencrypted, whereas RDP has NLA/TLS.
#
# WARNING: never put a comment line inside the backslash-continued command below.
# The continuation swallows it, the # comments out the rest, and every argument
# after it is silently dropped - the symptom is QEMU trying to open a GTK window
# ("gtk initialization failed") because -vnc/-qmp never made it onto the line.
qemu-system-x86_64 \
  -name $VM,process=$VM -daemonize -pidfile "$DIR/qemu.pid" \
  -machine q35,accel=kvm,smm=on,hpet=off \
  -global driver=cfi.pflash01,property=secure,value=on \
  -cpu host,migratable=off,hv_relaxed,hv_vapic,hv_spinlocks=0x1fff,hv_time \
  -smp 6,sockets=1,cores=6,threads=1 -m 12G \
  -rtc base=localtime,clock=host,driftfix=slew \
  -drive if=pflash,format=raw,unit=0,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.secboot.fd \
  -drive if=pflash,format=raw,unit=1,file="$DIR/ovmf/WIN11_VARS.fd" \
  -chardev socket,id=chrtpm,path="$DIR/tpm/swtpm.sock" \
  -tpmdev emulator,id=tpm0,chardev=chrtpm -device tpm-crb,tpmdev=tpm0 \
  -drive if=none,id=disk0,file="$DIR/win11.qcow2",cache=none,aio=io_uring,discard=unmap,detect-zeroes=unmap \
  -device virtio-blk-pci,drive=disk0,serial=WIN11DSK \
  -drive if=none,id=cd0,media=cdrom,readonly=on,file="$ISO/win11.iso" \
  -device ide-cd,bus=ide.0,drive=cd0 \
  -drive if=none,id=cd1,media=cdrom,readonly=on,file="$ISO/virtio.iso" \
  -device ide-cd,bus=ide.1,drive=cd1 \
  -chardev socket,id=qga0,path="$DIR/qga.sock",server=on,wait=off \
  -device virtio-serial-pci \
  -device virtserialport,chardev=qga0,name=org.qemu.guest_agent.0 \
  -boot order=$BOOT,menu=on \
  -netdev user,id=net0,hostfwd=tcp:0.0.0.0:13389-:3389 \
  -device e1000e,netdev=net0 \
  -device qemu-xhci -device usb-tablet \
  -vnc 127.0.0.1:1,password=on \
  -qmp unix:"$DIR/qmp.sock",server=on,wait=off

# VNC has no command-line password option: `password=on` only makes the server
# *require* auth; the secret must be set over QMP. Without it the server
# advertises security type None (1), which macOS Screen Sharing refuses to
# connect to ("the software on the remote computer appears to be incompatible").
i=0
while [ ! -S "$DIR/qmp.sock" ] && [ "$i" -lt 100 ]; do
  i=$((i + 1)); sleep 0.1
done
if [ -s "$DIR/vnc.pass" ]; then
  python3 "$DIR/bin/qmp.py" "$DIR/qmp.sock" change-vnc-password \
    "{\"password\":\"$(cat "$DIR/vnc.pass")\"}" >/dev/null \
    || echo "warning: could not set the VNC password" >&2
fi

# Console: ssh -N -L 5901:127.0.0.1:5901 yusiwen@192.168.2.48 ; open vnc://127.0.0.1:5901
# RDP:     direct  192.168.2.48:13389   (or via tunnel: ssh -L 13389:127.0.0.1:13389)
# For a real LAN address instead of NAT, keep -vnc/-qmp on loopback and replace the
# -netdev/-device pair with macvtap (never bridge the host's only NIC: k0s node).
