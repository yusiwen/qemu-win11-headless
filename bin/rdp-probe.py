#!/usr/bin/env python3
"""Prove the guest's RDP service actually answers - not just that the port forward exists.

    ./rdp-probe.py                      # 127.0.0.1:13389 (QEMU hostfwd -> guest 3389)
    ./rdp-probe.py 192.168.2.48 13389

Why: `nc -vz 127.0.0.1 13389` is a FALSE POSITIVE here. QEMU's SLIRP hostfwd accepts
the TCP handshake itself and only then tries to reach the guest, so a closed 3389
inside the guest still looks "open" from the host. This script speaks the first
RDP packet instead, which only the real terminal server can answer.

Packet: TPKT(03 00 len) + X.224 Connection Request (0xE0) with a mstshash cookie
and an RDP Negotiation Request asking for SSL|HYBRID. A live server replies with
TPKT + X.224 Connection Confirm (0xD0). Anything else (refused / EOF / timeouts)
means the service is not there.
"""
import socket
import sys


def build_cr(cookie=b"Cookie: mstshash=probe\r\n"):
    neg = bytes([0x01, 0x00, 0x08, 0x00, 0x03, 0x00, 0x00, 0x00])  # SSL|HYBRID
    body = bytes([0xE0, 0x00, 0x00, 0x12, 0x34, 0x00]) + cookie + neg
    x224 = bytes([len(body)]) + body
    tpkt_len = 4 + len(x224)
    return bytes([0x03, 0x00, tpkt_len >> 8, tpkt_len & 0xFF]) + x224


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 13389
    try:
        s = socket.create_connection((host, port), 8)
    except OSError as e:
        sys.exit("%s:%d 连接失败: %s（隧道没起？）" % (host, port, e))
    s.settimeout(8)
    s.sendall(build_cr())
    try:
        data = s.recv(64)
    except socket.timeout:
        sys.exit("%s:%d 连上了但没有应答 —— 端口转发活着，客机里的 RDP 没在听"
                 % (host, port))
    finally:
        s.close()

    if len(data) >= 6 and data[0] == 0x03 and data[5] == 0xD0:
        print("%s:%d  RDP 服务在应答（X.224 Connection Confirm）" % (host, port))
        if len(data) >= 19 and data[11] == 0x02:      # RDP_NEG_RSP
            chosen = data[15]
            names = {0: "PROTOCOL_RDP（标准安全层）",
                     1: "PROTOCOL_SSL（TLS）",
                     2: "PROTOCOL_HYBRID（CredSSP/NLA）",
                     8: "PROTOCOL_HYBRID_EX"}
            print("  协商结果: %s" % names.get(chosen, str(chosen)))
        return 0
    if not data:
        sys.exit("连接被对端关闭 —— 客机拒绝（RDP 未启用/防火墙拦）")
    sys.exit("非 RDP 应答: %r" % data[:16])


if __name__ == "__main__":
    sys.exit(main())
