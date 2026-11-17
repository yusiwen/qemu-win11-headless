# Guacamole — a browser gateway in front of the win11 VM

Apache Guacamole exposes the Windows 11 guest on `192.168.2.48` as a remote desktop
that works from any browser. **The VM itself is untouched**: no agent installed, no
registry change, no QEMU flag altered — Guacamole simply connects to the RDP port the
guest already listens on.

- Web UI: `http://192.168.2.48:8081/guacamole/`
- Login: `guacadmin`, password in `.env` (`ADMIN_PASSWORD`, rotated off the factory value)
- Connection: `win11-rdp`; the Windows password is **never stored** — the browser prompts for it every time

> 中文版：[README-zh_cn.md](README-zh_cn.md)

---

## 1. Topology: one gateway, not a new desktop

```
your browser (Mac / phone / anything)
   │  HTTP :8081                       ← this is the only "web" part
   ▼
guacamole container (Tomcat + auth + session)
   │  Guacamole protocol :4822 (docker-internal, not published)
   ▼
guacd container (protocol translation: RDP / VNC / SSH → Guacamole)
   │  RDP, NLA
   ▼
host.docker.internal:13389 = 172.17.0.1:13389   ← QEMU hostfwd
   │  SLIRP NAT
   ▼
guest 10.0.2.15:3389  ← Windows Remote Desktop (already running)
```

Three things matter here:

1. **guacd is the side that dials out**, so it must be able to resolve the host.
   `host.docker.internal` resolves to `172.17.0.1` (docker0) via
   `extra_hosts: host-gateway`, and QEMU's hostfwd binds `0.0.0.0:13389`, so the path
   works.
2. **No port needs to be published between the webapp and guacd** — they share a docker
   network. The one guacd port that *is* published (`127.0.0.1:4822`) exists only so
   `bin/guac-probe.py` can do a protocol-level self-test from the host; it is loopback-only.
3. **The guest's RDP is still on `0.0.0.0:13389`** (direct LAN access keeps working).
   To tighten that later, bind the hostfwd back to `127.0.0.1`, switch guacd to
   `network_mode: host` and set `RDP_HOST=127.0.0.1` — see §8.

## 2. Files

```
/data/vms/win11/guacamole/
├── docker-compose.yml        # three containers: guacd / postgres / guacamole
├── docker-compose.tls.yml    # optional: Caddy + self-signed TLS (§7)
├── Caddyfile                 # optional: Caddy reverse-proxy config
├── .env                      # 600. DB password, admin password, RDP target, port
├── .env.example              # template (safe to commit)
├── initdb.sql                # 24 KB schema. TRACKED; its header records the image tag
├── db/                       # postgres data dir (host path, owned by root — don't edit)
├── guacamole.properties      # api-session-timeout and friends
├── bootstrap.sh              # one command: schema → compose up → setup → probe
└── bin/
    ├── guac-setup.py         # idempotent: rotate admin password + create/update win11-rdp
    └── guac-probe.py         # protocol-level self-test against guacd
```

Containers: `win11-guacd`, `win11-guac-db`, `win11-guacamole`; docker network
`win11-guac_guacnet` (compose project `win11-guac`).

## 3. Deploying from scratch (replayable)

```bash
cd /data/vms/win11/guacamole

# 1) generate .env (passwords are created on the host and never copied anywhere)
umask 077
printf 'DB_PASSWORD=%s\nADMIN_PASSWORD=%s\nWEB_PORT=8081\nRDP_HOST=host.docker.internal\nRDP_PORT=13389\nRDP_USER=<your microsoft account email>\n' \
  "$(openssl rand -hex 16)" "$(openssl rand -hex 16)" > .env
chmod 600 .env

# 2) schema: initdb.sql ships with the repo, so this step is normally skipped.
#    Regenerate only when bumping the image tag (must use the image's own script):
# docker run --rm guacamole/guacamole:<tag> /opt/guacamole/bin/initdb.sh --postgresql > initdb.sql
# chmod 644 initdb.sql        # ← see §9 pitfall 1; wrong mode and postgres cannot read it

# 3) start the three containers
docker compose up -d

# 4) rotate the admin password and define the connection
./bin/guac-setup.py

# 5) self-test
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/guacamole/   # expect 200
./bin/guac-probe.py                                                         # see §5
```

Or just `./bootstrap.sh`, which does steps 2-5 and refuses to continue without a `.env`.

`docker compose up -d` only runs `initdb.sql` when `db/` is empty. An already-initialised
database is not re-initialised; to start over:
`docker compose down && sudo rm -rf db && docker compose up -d`.

## 4. Using it

Open `http://192.168.2.48:8081/guacamole/`, log in as `guacadmin` with
`ADMIN_PASSWORD` from `.env`, click `win11-rdp`, and enter the **Microsoft account
password** at the prompt (the username is pre-filled — do not add a
`MicrosoftAccount\` prefix).

| Capability | Notes |
|---|---|
| Dynamic resolution | `resize-method=display-update`: the guest follows the browser window |
| Clipboard | bidirectional, on by default |
| File transfer | `enable-drive=true`; the panel slides out from the left edge. Files land in the guacd container's `/tmp` — **lost on container restart**, so treat it as a transfer buffer |
| Audio | guest → browser (`audio/L16`) |
| Any device | a phone browser works; no app to install |

**Limitation: Windows 11 Pro allows exactly one interactive session.** If you are
connected through the Windows App and then connect the same account from a browser, the
browser **kicks the other session out** (and vice versa). That is the client SKU, not
Guacamole; two side-by-side desktops need Windows Server + RDS.

## 5. Self-test: three layers of evidence, not "the container is running"

`bin/guac-probe.py` speaks the Guacamole protocol to guacd directly, without a browser.
Its output separates "the path is broken" from "the credentials are wrong":

```
$ ./bin/guac-probe.py --hold 8
-> select rdp
   target host.docker.internal:13389 user=<email> password=***
<- args VERSION_1_5_0, 88 parameters
-> connect VERSION_1_5_0 (88 values)
<- ready id=$8a1c623f-... (0.04s)
<- within 8.0s: cursor×1, mouse×1, nop×1, required×1, sync×1
verdict: session established but no frames flowing — needs a human look
```

`required` is the server saying "I need credentials". Without `--password` that is the
correct place to stop; guacd's log supplies the other half:

```
INFO:  Security mode: NLA
INFO:  Resize method: display-update
INFO:  RDP server closed/refused connection: Authentication failure (invalid credentials?)
```

**That "Authentication failure" is good news**: it proves guacd delivered credentials to
the guest's NLA and the guest rejected them. The path is fine; only the password is missing.

With a real password the same script goes past `ready` and starts receiving `sync`/`img`,
and the verdict becomes "session alive and streaming". Or, more directly: connect once in
a browser, then on the host run

```bash
/data/vms/win11/bin/verify-rdp-works.py     # guest 4624 LogonType=10 + query user
docker logs --tail 20 win11-guacd           # expect "joined connection", no error
```

## 5.5 Session timeout: switching windows should not log you out

**`api-session-timeout`** in `guacamole.properties` decides how many minutes an
authentication token survives **inactivity**; the default is **60**. Past that the browser
returns to the login page and the open RDP tunnel dies with it.

It is set to **720 minutes (12 h)**:

```properties
# /data/vms/win11/guacamole/guacamole.properties → container /etc/guacamole/guacamole.properties
api-session-timeout: 720
```

Only the webapp container needs recreating (guacd and the database stay up):

```bash
cd /data/vms/win11/guacamole
vi guacamole.properties          # minutes
docker compose up -d guacamole
docker logs win11-guacamole 2>&1 | grep "Sessions will expire"
# expect: Sessions will expire after 720 minutes of inactivity.
```

That startup log line is the only reliable check — no new number means it did not take
(a wrong mount path, for instance).

Notes:

- The manual only promises "how long until an idle token dies"; it **does not say `0`
  means forever**, so don't guess. For "effectively never", use a large number (`10080` = 7 days).
- The longer the token, the bigger the exposure. Single user on a home LAN → 12 h is a
  deliberate trade.
- Tomcat's HTTP session timeout inside the container is **30 minutes**
  (`/usr/local/tomcat/conf/web.xml`), but Guacamole authenticates with the `token`
  parameter rather than JSESSIONID, so those 30 minutes do not participate. Changing them
  would mean overriding `conf/web.xml`, which becomes a maintenance burden on image upgrades.
- **A browser discarding the tab** (Chrome's Memory Saver drops long-idle background tabs)
  kills the websocket outright; the token may still be valid, but the tunnel is gone — you
  reconnect the connection, you don't log in again.
- To keep the RDP session itself alive, **leave the tab open**; close the browser and the
  guest-side session ends.

## 6. Operations

```bash
cd /data/vms/win11/guacamole
docker compose ps                      # all three should be running
docker compose logs -f guacamole       # webapp side
docker logs -f win11-guacd             # session side — this is where you debug
docker compose restart guacamole       # webapp only
docker compose down / up -d            # stop / start everything
```

- **Do not be fooled by `health: starting`**: the guacd image's healthcheck is
  `nc -z 127.0.0.1 4822` with an `Interval` of **300 s** and no `StartPeriod`, so for the
  first five minutes it always reads `starting`. That is a timer, not a fault. For an
  immediate answer use `./bin/guac-probe.py`.
- **Autostart**: all three containers are `restart: unless-stopped`, so docker brings them
  back after a host reboot. Note `docker compose down` **deletes** the containers and they
  will not come back; use `docker compose stop` for day-to-day stops.
- **Backup**: only `.env` and `initdb.sql` matter. `db/` holds users and connection
  definitions, and re-running `./bin/guac-setup.py` rebuilds them (the admin password
  returns to the `.env` value), so it does not deserve its own backup policy.
- **Upgrade**: change the two `1.6.0` tags, then `docker compose pull && up -d`. On major
  versions check whether the schema needs the official upgrade scripts.

## 7. Optional: TLS in front of the web UI

`docker-compose.tls.yml` + `Caddyfile` are ready; one command switches to TLS:

```bash
cd /data/vms/win11/guacamole
sed -i 's/^WEB_PORT=.*/WEB_PORT=127.0.0.1:8081/' .env       # web UI drops back to loopback
docker compose -f docker-compose.yml -f docker-compose.tls.yml up -d
# https://192.168.2.48:8443/guacamole/
```

The certificate comes from Caddy's local CA (`tls internal`) — `192.168.2.48` is a
private address, so ACME cannot validate it. Browsers warn on first visit; to silence
that, import Caddy's root certificate:

```bash
docker exec win11-guac-tls cat /data/caddy/pki/authorities/local/root.crt > caddy-local-root.crt
# macOS: Keychain Access → System → import → Always Trust
```

**Why bother**: plain HTTP means the Microsoft account password you type in the browser
crosses the LAN in clear text. A home network is low risk, but any compromised device on
the same segment can sniff it. Direct RDP to `192.168.2.48:13389` does not have this
problem — RDP carries TLS + NLA itself.

## 8. Tightening: make RDP visible only to guacd

Right now `192.168.2.48:13389` is open to the whole LAN. To leave only the web entry point:

```bash
# 1) bind the QEMU hostfwd back to loopback
sed -i 's/hostfwd=tcp:0\.0\.0\.0:13389/hostfwd=tcp:127.0.0.1:13389/' /data/vms/win11/bin/start-vm.sh
sudo systemctl restart win11          # drops whatever RDP session is active

# 2) let guacd use the host network so it can reach 127.0.0.1
#    add to the guacd service in docker-compose.yml:  network_mode: host
#    and delete its ports: block (port mapping is meaningless on the host network)

# 3) set RDP_HOST=127.0.0.1 in .env, then
docker compose up -d && ./bin/guac-probe.py
```

Under `network_mode: host`, guacd uses the host stack directly and `ports:` is ignored or
rejected by compose — don't forget that.

## 9. Pitfalls (all of them real)

**1. `umask 077` turns `initdb.sql` into 0600 and postgres cannot read it.**
The symptom is well hidden: containers all running, HTTP 200, but login fails and `\dt`
shows no tables. The only clue is one line in the postgres log:

```
/usr/local/bin/docker-entrypoint.sh: running /docker-entrypoint-initdb.d/01-initdb.sql
psql: error: .../01-initdb.sql: Permission denied
```

The postgres image runs init scripts as uid 70, while `initdb.sql` was `user:user 0600` —
`umask 077` set for `.env` leaked into every later redirect in that same shell. Fix:
`chmod 644 initdb.sql`, delete `db/` and re-initialise (an already-created database will
not re-run initdb).

**2. guacd's `ready` arrives before authentication — never treat it as success.**
`ready` came back in 0.04 s while the RDP driver was still connecting on its own thread.
Bad credentials do not show up there; instead guacd **closes the connection** afterwards
and logs the reason (`Authentication failure (invalid credentials?)`), or sends the client
an `error`. The probe therefore has to keep listening past `ready` and judge liveness by
whether `sync`/`img` frames flow. Reading only `ready` gives a false pass.

**3. In the Guacamole protocol, `connect` is the instruction that carries parameters.**
`select` carries only the protocol name (`6.select,3.rdp;`); the server answers with
`args`, listing every parameter name it accepts and its version (this host: `VERSION_1_5_0`,
88 parameters), and the client then fills a `connect` with one value per name **in the
order args gave them**. The first version of the probe put parameters in `select`; guacd
answered `Guacamole protocol violation` and hung up.

**4. `error` takes `(message, status)`, not the other way round.**
Parsing as `status, message` yields garbage status codes.

**5. REST `GET /connections/{id}` does not return `parameters`.**
They live in the `guacamole_connection_parameter` table; over the API use
`GET /connections/{id}/parameters`, or query the database. "Parameters look empty" there
is an illusion.

**6. `guacadmin` ships with the password `guacadmin`, and the web UI is scannable the
moment it is up.** `bin/guac-setup.py` rotates it on first run and is idempotent (the
second run logs in with the new password and skips rotation).

**7. Do not store the Windows password in the connection definition.**
Leave `password` empty and Guacamole prompts in the browser. Storing it means keeping a
Microsoft account password in the database (plaintext or weakly encrypted), and it breaks
permanently the moment the password changes. If you must store it, keep it in `.env` and
accept that it is plaintext there.

**8. `apt upgrade` of docker / containerd logs everyone out.**
After the daemon restarts, `restart: unless-stopped` brings the containers back
(`StartedAt` changes, `RestartCount` stays 0) — but Guacamole's sessions live in memory, so
every browser returns to the login page. Diagnosis: `systemctl show docker -p
ActiveEnterTimestamp` matches the containers' `StartedAt` almost exactly ⇒ daemon restart,
not session timeout. This happened here on 2026-11-17 at 11:52 (triggered by an `apt` run).

**9. `api-session-timeout` "didn't work" — check the mount first.**
`GUACAMOLE_HOME=/etc/guacamole` is baked into the image, but the container had no
`guacamole.properties` at all; the file has to be **mounted**
(`./guacamole.properties:/etc/guacamole/guacamole.properties:ro`). Verify by the minute
count in the startup log, not by whether the web UI is still open.

## 10. Why not KasmVNC

Recorded here so the loop does not get walked twice: **KasmVNC is a VNC server** (its own
X server plus a web client), **not a gateway**. It has no "connect to someone else's
VNC/RDP" mode, so it cannot attach to QEMU's VNC output.

**Kasm Workspaces** (a different product) can take "fixed infrastructure" RDP/VNC/SSH and
web-enable it — but the component that does it is **guacd**. The upstream docs say so:
"Guacd is used for web-native connections to backend RDP, VNC, and SSH servers." What it
adds is user management, session recording, container isolation, CAS, an admin UI, plus a
DB/redis/agent/nginx stack and a free-tier concurrency cap. One person driving one Windows
VM: Guacamole is that kernel.

Other candidates: MeshCentral needs an agent inside Windows; JumpServer is a bastion, too
heavy; Windows' own RD Web Access exists only on Windows Server — not in Win11 Pro;
RustDesk is a TeamViewer-class tool, not an RDP gateway.

---

## Appendix: measured on deployment day (2026-11-17)

| Item | Result |
|---|---|
| Images | `guacamole/guacd:1.6.0`, `guacamole/guacamole:1.6.0`, `postgres:16-alpine` |
| Containers | all three running, `restart: unless-stopped` |
| Web UI | `GET /guacamole/` → **200** (tested on loopback and from the LAN) |
| Schema | `initdb.sql` 795 lines (793 + 2 provenance), all `guacamole_*` tables created |
| Connection | `win11-rdp` id=1, 13 parameters, granted to guacadmin |
| Parameters | hostname=host.docker.internal, port=13389, security=nla, ignore-cert=true, resize-method=display-update, enable-drive=true, color-depth=32 |
| guacd negotiation | `Security mode: NLA`, `Resize method: display-update`, `args` reports `VERSION_1_5_0` |
| Authentication | empty password → guest `Authentication failure (invalid credentials?)` (path OK, awaiting the real password) |
| Ports | 8081 web (0.0.0.0), 4822 guacd (127.0.0.1 only) |
| Session timeout | default 60 min → **720 min**, confirmed by `Sessions will expire after 720 minutes of inactivity.` |
| Memory | measured: guacamole 313 MiB + postgres 30 MiB + guacd 11 MiB ≈ **354 MiB** (`docker stats --no-stream`) |

**One step I have not verified**: logging in with the real Windows password in a browser
and seeing the desktop. That needs your account password, which I do not handle. After you
connect once, `/data/vms/win11/bin/verify-rdp-works.py` confirms it from the guest side
(`4624 LogonType=10`).
