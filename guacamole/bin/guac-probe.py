#!/usr/bin/env python3
"""Drive guacd directly over TCP and report what the RDP handshake actually did.

Why this exists: a web gateway can look perfectly healthy - container up, HTTP
200, login page rendered - while guacd still cannot reach or authenticate
against the guest. This speaks the Guacamole protocol to guacd itself, so the
RDP leg is proven without a browser and without guessing.

    host -> guacd:4822 --RDP--> host.docker.internal:13389 -> guest :3389

Handshake, per the protocol manual (Order matters, connect is last):

    C: select, rdp                        load the RDP driver
    S: args, VERSION_1_1_0, <names...>    version + the parameters it accepts
    C: size / audio / video / image / timezone
    C: connect, <VERSION>, <one value per name, in args order>
    S: ready, <connection id>             the remote session is up
    S: error, <message>, <status>         it failed, and why

Outcomes are classified so a failure points at the right layer:
    ready                  -> guacd holds an authenticated RDP session
    CLIENT_AUTH_FAILED     -> network fine, the guest rejected the credentials
    UPSTREAM_UNAVAILABLE   -> guacd cannot open the RDP socket at all
                             (hostfwd missing, wrong host, container network)

Usage:
    ./bin/guac-probe.py                      # config read from ../.env
    ./bin/guac-probe.py --user a@b.com --verbose
"""

import argparse
import os
import socket
import sys
import time

# guacamole/client.h status codes worth naming
STATUS = {
    0: "SUCCESS",
    256: "SERVER_ERROR",
    512: "SERVER_BUSY",
    513: "UPSTREAM_TIMEOUT",
    514: "UPSTREAM_ERROR",
    515: "RESOURCE_NOT_FOUND",
    516: "RESOURCE_CONFLICT",
    517: "RESOURCE_CLOSED",
    518: "UPSTREAM_NOT_FOUND",
    519: "UPSTREAM_UNAVAILABLE",
    520: "SESSION_CONFLICT",
    521: "SESSION_TIMEOUT",
    522: "SESSION_CLOSED",
    523: "CLIENT_BAD_REQUEST",
    524: "CLIENT_UNAUTHORIZED",
    525: "CLIENT_FORBIDDEN",
    526: "CLIENT_TIMEOUT",
    527: "CLIENT_OVERRUN",
    528: "CLIENT_BAD_TYPE",
    529: "CLIENT_TOO_MANY",
    769: "CLIENT_AUTH_FAILED",
    771: "CLIENT_AUTH_FORBIDDEN",
    772: "CLIENT_AUTH_REQUIRED",
    773: "CLIENT_AUTH_TIMEOUT",
    774: "CLIENT_AUTH_TOO_MANY",
}

VERDICT = {
    "ready": "认证通过的 RDP 会话已建立",
    "CLIENT_AUTH_FAILED": "链路通，客机拒绝了凭据（密码/账户问题，或 passwordless 回归）",
    "CLIENT_AUTH_FORBIDDEN": "链路通，账户没有远程登录权限",
    "UPSTREAM_UNAVAILABLE": "guacd 连不到 RDP 端点（hostfwd / 容器网络 / 主机名）",
    "UPSTREAM_TIMEOUT": "guacd 连到了但超时（客机没响应，RDP 服务可能没起）",
    "CLIENT_BAD_REQUEST": "参数被驱动拒绝（查一下参数名和取值）",
}


def encode(*fields):
    """One Guacamole instruction: comma-separated <len>.<data>, ending in ';'."""
    parts = []
    for f in fields:
        raw = f if isinstance(f, bytes) else str(f).encode()
        parts.append(str(len(raw)).encode() + b"." + raw)
    return b",".join(parts) + b";"


class Reader:
    """Incremental parser for the Guacamole wire format."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise EOFError("guacd 关闭了连接")
        self.buf += chunk

    def _take(self, n):
        while len(self.buf) < n:
            self._fill()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def instruction(self):
        fields = []
        while True:
            dot = self.buf.find(b".")
            while dot < 0:
                self._fill()
                dot = self.buf.find(b".")
            length = int(self.buf[:dot])
            self.buf = self.buf[dot + 1:]
            fields.append(self._take(length))
            sep = self._take(1)
            if sep == b";":
                return fields
            if sep != b",":
                raise ValueError("bad separator %r" % sep)


def read_env(path):
    env = {}
    if not os.path.exists(path):
        return env
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    env = read_env(os.path.join(here, "..", ".env"))

    ap = argparse.ArgumentParser()
    ap.add_argument("--guacd", default="127.0.0.1")
    ap.add_argument("--guacd-port", type=int, default=4822)
    ap.add_argument("--host", default=env.get("RDP_HOST", "host.docker.internal"))
    ap.add_argument("--port", default=env.get("RDP_PORT", "13389"))
    ap.add_argument("--user", default=env.get("RDP_USER", ""))
    ap.add_argument("--password", default="", help="留空则只测到认证阶段")
    ap.add_argument("--verbose", action="store_true", help="逐条打印协议指令")
    ap.add_argument("--hold", type=float, default=6.0,
                    help="ready 之后继续监听多少秒，确认画面真的在流")
    args = ap.parse_args()

    values = {
        "hostname": args.host,
        "port": str(args.port),
        "username": args.user,
        "password": args.password,
        "domain": "",
        "security": "nla",
        "ignore-cert": "true",
        "resize-method": "display-update",
        "color-depth": "32",
        "enable-drive": "false",
        "disable-audio": "true",
        "read-only": "",
    }

    sock = socket.create_connection((args.guacd, args.guacd_port), timeout=10)
    sock.settimeout(30)
    rd = Reader(sock)
    t0 = time.time()

    sock.sendall(encode("select", "rdp"))
    print("-> select rdp")
    print("   target %s:%s user=%s password=%s"
          % (args.host, args.port, args.user or "(空)",
             "已提供" if args.password else "(空，测到认证阶段)"))

    while True:
        fields = rd.instruction()
        name = fields[0].decode("utf-8", "replace")
        if args.verbose:
            print("<-", [f.decode("utf-8", "replace")[:60] for f in fields[:5]])

        if name == "args":
            names = [f.decode("utf-8", "replace") for f in fields[1:]]
            version = names[0] if names else "VERSION_1_1_0"
            print("<- args %s, %d 个参数" % (version, len(names) - 1))
            # capabilities, then the mandatory final connect instruction
            sock.sendall(encode("size", 0, 1920, 1080))
            sock.sendall(encode("audio", "audio/L16"))
            sock.sendall(encode("video"))
            sock.sendall(encode("image", "image/png", "image/jpeg"))
            sock.sendall(encode("timezone", "Asia/Shanghai"))
            # one value per parameter, in exactly the order the server listed them
            supplied = [values.get(n, "") for n in names[1:]]
            sock.sendall(encode("connect", version, *supplied))
            print("-> connect %s (%d 个值)" % (version, len(supplied)))
        elif name == "ready":
            conn = fields[1].decode("utf-8", "replace") if len(fields) > 1 else "?"
            dt = time.time() - t0
            print("<- ready id=%s (%.2fs)" % (conn, dt))
            # ready only means guacd accepted the parameters; the RDP driver
            # connects on its own thread, so a bad credential arrives AFTER
            # ready as an error. Keep listening and count what actually flows.
            seen = {}
            deadline = time.time() + args.hold
            while time.time() < deadline:
                sock.settimeout(max(0.2, deadline - time.time()))
                try:
                    f2 = rd.instruction()
                except socket.timeout:
                    break
                except EOFError:
                    print("<- 连接在 %.2fs 后被关闭" % (time.time() - t0))
                    print("\n结论: guacd 接受了参数但 RDP 会话没建起来（看 guacd 日志）")
                    return 1
                op = f2[0].decode("utf-8", "replace")
                seen[op] = seen.get(op, 0) + 1
                if op == "error":
                    msg = f2[1].decode("utf-8", "replace") if len(f2) > 1 else ""
                    status = int(f2[2]) if len(f2) > 2 else 0
                    label = STATUS.get(status, "STATUS_%d" % status)
                    print("<- error %d (%s): %s" % (status, label, msg))
                    print("\n结论: %s" % VERDICT.get(label, "未分类失败"))
                    return 2 if label == "CLIENT_AUTH_FAILED" else 1
            frames = sum(v for k, v in seen.items() if k in ("img", "blob", "png", "jpeg", "webp"))
            syncs = seen.get("sync", 0)
            print("<- %.1fs 内收到: %s" % (args.hold,
                  ", ".join("%s×%d" % (k, v) for k, v in sorted(seen.items())) or "什么都没有"))
            if syncs and frames:
                print("\n结论: 会话活着且在推画面（sync %d 次, 帧 %d 个）" % (syncs, frames))
                sock.close()
                return 0
            print("\n结论: 会话已经建立但没有画面流动，需要人工看一眼")
            sock.close()
            return 3
        elif name == "error":
            # error carries (message, status) - message first
            msg = fields[1].decode("utf-8", "replace") if len(fields) > 1 else ""
            status = int(fields[2]) if len(fields) > 2 else 0
            label = STATUS.get(status, "STATUS_%d" % status)
            print("<- error %d (%s): %s" % (status, label, msg))
            print("\n结论: %s" % VERDICT.get(label, "未分类失败，看上面的 message"))
            sock.close()
            return 2 if label == "CLIENT_AUTH_FAILED" else 1
        elif name == "require":
            want = [f.decode("utf-8", "replace") for f in fields[1:]]
            print("<- require %s（服务端要求补参数）" % want)
        elif name in ("name", "log"):
            text = " ".join(f.decode("utf-8", "replace") for f in fields[1:])
            print("<- %s %s" % (name, text[:140]))
        else:
            if args.verbose:
                print("   (忽略 %s)" % name)


if __name__ == "__main__":
    sys.exit(main())
