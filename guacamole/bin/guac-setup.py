#!/usr/bin/env python3
"""Idempotent Guacamole bootstrap for the win11 VM.

Two jobs, both safe to re-run:
  1. Move the web login off the factory default (guacadmin/guacadmin) to the
     password in .env.
  2. Define (or update) the RDP connection pointing at the QEMU hostfwd.

The Windows password is intentionally left empty in the connection: Guacamole
then prompts for it in the browser on every connect, so nothing stores it and
nothing has to be re-done when the account password rotates.

Run it on the host, after `docker compose up -d`:
    ./bin/guac-setup.py
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ADMIN = "guacadmin"
DEFAULT_PASSWORD = "guacadmin"

CONNECTION_NAME = "win11-rdp"


def read_env(path):
    env = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


class Guac:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.token = None
        self.ds = None

    def _call(self, path, method="GET", body=None, form=None, token=True):
        url = self.base + path
        data = None
        headers = {}
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token and self.token:
            sep = "&" if "?" in url else "?"
            url += sep + "token=" + urllib.parse.quote(self.token)
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            return e.code, raw

    def login(self, user, password):
        status, body = self._call("/tokens", method="POST",
                                  form={"username": user, "password": password},
                                  token=False)
        if status == 200 and isinstance(body, dict):
            self.token = body["authToken"]
            self.ds = body["dataSource"]
            return True
        return False

    def change_password(self, user, old, new):
        return self._call("/session/data/%s/users/%s/password" % (self.ds, user),
                          method="PUT",
                          body={"oldPassword": old, "newPassword": new})

    def connections(self):
        status, body = self._call("/session/data/%s/connections" % self.ds)
        if status != 200:
            raise SystemExit("列连接失败: %s %s" % (status, body))
        return body

    def put_connection(self, cid, payload):
        return self._call("/session/data/%s/connections/%s" % (self.ds, cid),
                          method="PUT", body=payload)

    def post_connection(self, payload):
        return self._call("/session/data/%s/connections" % self.ds,
                          method="POST", body=payload)


def connection_payload(env):
    return {
        "parentIdentifier": "ROOT",
        "name": CONNECTION_NAME,
        "protocol": "rdp",
        "parameters": {
            # --- where the guest lives -------------------------------------
            "hostname": env.get("RDP_HOST", "host.docker.internal"),
            "port": env.get("RDP_PORT", "13389"),
            # --- who logs in; password blank => prompt in the browser ------
            "username": env.get("RDP_USER", ""),
            "password": "",
            # --- NLA: the guest only offers CredSSP, and its cert is
            #     self-signed by the guest, hence ignore-cert ----------------
            "security": "nla",
            "ignore-cert": "true",
            # --- display: the reason RDP beats VNC here --------------------
            "resize-method": "display-update",
            "color-depth": "32",
            "enable-wallpaper": "true",
            "enable-font-smoothing": "true",
            "enable-desktop-composition": "true",
            # --- clipboard works by default; drive = file transfer --------
            "enable-drive": "true",
            "drive-name": "win11",
            "create-drive-path": "true",
        },
        "attributes": {
            "max-connections": "",
            "max-connections-per-user": "",
            "weight": "",
            "failover-only": "",
            "guacd-hostname": "",
            "guacd-port": "",
            "guacd-encryption": "",
        },
    }


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    env = read_env(os.path.join(here, "..", ".env"))
    port = env.get("WEB_PORT", "8081")
    base = "http://127.0.0.1:%s/guacamole/api" % port
    admin = env.get("ADMIN_PASSWORD")

    if not admin:
        raise SystemExit(".env 里缺 ADMIN_PASSWORD")

    guac = Guac(base)

    # 1. login: prefer the already-rotated password, fall back to the factory one
    rotated = guac.login(DEFAULT_ADMIN, admin)
    if rotated:
        print("[1/2] 用 .env 里的管理员密码登录成功（密码此前已轮换）")
    else:
        if not guac.login(DEFAULT_ADMIN, DEFAULT_PASSWORD):
            raise SystemExit("管理员登录失败：既不是 .env 的密码，也不是出厂 guacadmin")
        status, body = guac.change_password(DEFAULT_ADMIN, DEFAULT_PASSWORD, admin)
        if status not in (200, 204):
            raise SystemExit("改管理员密码失败: %s %s" % (status, body))
        print("[1/2] 管理员密码已从出厂默认值改为 .env 中的值")

    # 2. upsert the RDP connection
    payload = connection_payload(env)
    existing = {c["name"]: cid for cid, c in guac.connections().items()}
    if CONNECTION_NAME in existing:
        cid = existing[CONNECTION_NAME]
        status, body = guac.put_connection(cid, payload)
        if status not in (200, 204):
            raise SystemExit("更新连接失败: %s %s" % (status, body))
        print("[2/2] 连接 %s 已更新 (id=%s)" % (CONNECTION_NAME, cid))
    else:
        status, body = guac.post_connection(payload)
        if status != 200:
            raise SystemExit("创建连接失败: %s %s" % (status, body))
        cid = body["identifier"]
        print("[2/2] 连接 %s 已创建 (id=%s)" % (CONNECTION_NAME, cid))

    print("\n浏览器: http://192.168.2.48:%s/guacamole/" % port)
    print("用户:   guacadmin")
    print("密码:   .env 里的 ADMIN_PASSWORD")
    print("连接:   %s  ->  %s:%s (RDP/NLA，密码在连接时询问)"
          % (CONNECTION_NAME, payload["parameters"]["hostname"], payload["parameters"]["port"]))


if __name__ == "__main__":
    main()
