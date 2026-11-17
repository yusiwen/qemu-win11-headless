#!/bin/sh
# Boot win11 from the installer CD, answering the "Press any key to boot from CD
# or DVD" prompt. Windows' boot code waits only ~5 s for a keypress; with nobody
# at the console OVMF falls through to PXE. So: restart the VM and hammer space
# over QMP for 20 seconds. Run with sudo (it restarts the service).
set -eu
DIR=/data/vms/win11
QMP="python3 $DIR/bin/qmp.py $DIR/qmp.sock"
KEY='{"keys":[{"type":"qcode","data":"spc"}],"hold-time":80}'

systemctl stop win11 2>/dev/null || true
sleep 1
systemctl start win11
sleep 2

i=0
while [ "$i" -lt 40 ]; do
  i=$((i + 1))
  $QMP send-key "$KEY" >/dev/null 2>&1 || true
  sleep 0.5
done

echo "keystrokes sent - check the console: ssh -N -L 5901:127.0.0.1:5901 yusiwen@192.168.2.48"
