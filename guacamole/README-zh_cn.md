# Guacamole：给 win11 虚机加一层网页 RDP

用 Apache Guacamole 把 `192.168.2.48` 上那台 QEMU/KVM 的 Windows 11 虚机暴露成
"打开浏览器就能用"的远程桌面。**虚机本身一行没改**：不装 agent、不改注册表、
不动 QEMU 启动参数，Guacamole 只是主动去连虚机已经在监听的 RDP。

- 网页入口：`https://vm-win11.example.com/guacamole/`（前置代理 + Authelia）
- 局域网：`http://192.168.2.48:8081/guacamole/`（明文，同样没有登录页）
- 登录用户：`guacadmin`，密码在 `.env` 的 `ADMIN_PASSWORD`（已从出厂默认值改掉）
- 连接名：`win11-rdp`；Windows 密码**不存盘**，每次连接时在浏览器里输入

> English version: [README.md](README.md)

---

## 1. 拓扑：一层网关，不是新桌面

```
你的浏览器（Mac / 手机 / 任何设备）
   │  HTTP :8081                       ← TLS 在前置代理上终结(§7)
   ▼
guacamole 容器（Tomcat + 认证 + 会话）
   │  Guacamole 协议 :4822（容器内网，不对外）
   ▼
guacd 容器（协议翻译：RDP / VNC / SSH → Guacamole）
   │  RDP, NLA
   ▼
host.docker.internal:13389 = 172.17.0.1:13389   ← QEMU hostfwd
   │  SLIRP NAT
   ▼
虚机 10.0.2.15:3389  ← Windows 远程桌面服务（本来就在跑）
```

三个关键点：

1. **guacd 才是"连出去"的那一端**，所以必须让它能解析宿主机。`host.docker.internal`
   通过 `extra_hosts: host-gateway` 解析成 `172.17.0.1`（docker0），而 QEMU 的
   hostfwd 现在绑的是 `0.0.0.0:13389`，所以这条路通。
2. **guacamole（网页）和 guacd 之间不需要暴露端口**，它们在同一个 docker 网络里。
   唯一被"发布"出来的 guacd 端口是我们自己加的 `127.0.0.1:4822`，只为让
   `bin/guac-probe.py` 能在宿主机上做协议级自检，绑的是回环，外部访问不到。
3. **虚机的 RDP 仍然是 `0.0.0.0:13389`**（局域网直连依然可用）。如果你以后想收紧，
   把 hostfwd 改回 `127.0.0.1`，同时给 guacd 换成 `network_mode: host`，
   并把 `.env` 的 `RDP_HOST` 改成 `127.0.0.1` —— 见 §8。

## 2. 目录与文件

```
/data/vms/win11/guacamole/
├── docker-compose.yml        # 三容器：guacd / postgres / guacamole
├── docker-compose.tls.yml    # Caddy TLS 前端,属于正常部署栈（§7）
├── Caddyfile                 # TLS 配置。两个 SNI 坑在注释里（§7.2）
├── caddy-data/               # Caddy 存储：根/中间 CA 与已签发证书。删了就等于换 CA（§7.1）
├── .env                      # 600。DB 密码、管理员密码、RDP 目标、端口
├── .env.example              # 模板（可进版本库）
├── initdb.sql                # 24 KB，库表结构。**入库**，文件头记录来源镜像 tag
├── db/                       # postgres 数据目录（宿主目录，属主 root，别手改）
├── guacamole.properties      # api-session-timeout 等应用配置
├── bootstrap.sh              # 一条命令：库表 → 起容器 → setup → 自检
└── bin/
    ├── guac-setup.py         # 幂等：轮换管理员密码 + 创建/更新 win11-rdp 连接
    └── guac-probe.py         # 协议级自检：直接和 guacd 握手，判定 RDP 链路
```

容器名：`win11-guacd`、`win11-guac-db`、`win11-guacamole`；docker 网络
`win11-guac_guacnet`（compose project 名 `win11-guac`）。

## 3. 从零部署（可重放）

```bash
cd /data/vms/win11/guacamole

# 1) 生成 .env（密码只在宿主机生成，不要复制粘贴到任何地方）
umask 077
printf 'DB_PASSWORD=%s\nADMIN_PASSWORD=%s\nWEB_PORT=8081\nRDP_HOST=host.docker.internal\nRDP_PORT=13389\nRDP_USER=<微软账户邮箱>\n' \
  "$(openssl rand -hex 16)" "$(openssl rand -hex 16)" > .env
chmod 600 .env

# 2) 库表结构：仓库自带 initdb.sql，通常跳过这步。
#    换镜像 tag 时才重生成（必须用镜像自带的脚本）：
# docker run --rm guacamole/guacamole:<tag> /opt/guacamole/bin/initdb.sh --postgresql > initdb.sql
# chmod 644 initdb.sql        # ← 见 §9 坑 1，权限不对 postgres 读不到

# 3) 起三个容器
docker compose -f docker-compose.yml -f docker-compose.tls.yml up -d

# 4) 轮换管理员密码 + 建连接
./bin/guac-setup.py

# 5) 自检
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/guacamole/   # 期望 200
./bin/guac-probe.py                                                         # 见 §5
```

`docker compose up -d` 只在 `db/` 为空时才会执行 `initdb.sql`。已经初始化过的库
不会再跑，想重建就 `docker compose down && sudo rm -rf db && docker compose up -d`。

## 4. 日常使用

浏览器打开 `http://192.168.2.48:8081/guacamole/`（或从外网走前置代理的地址）。开着 header
认证时没有登录页；没开时用 `guacadmin` + `.env` 里的
`ADMIN_PASSWORD` 登录，点 `win11-rdp`，在提示里输入 **微软账户密码**
（用户名已预填 `<微软账户邮箱>`，不要加 `MicrosoftAccount\` 前缀）。

能用到的能力：

| 能力 | 说明 |
|---|---|
| 动态分辨率 | `resize-method=display-update`，浏览器窗口多大，客机分辨率就跟着变 |
| 剪贴板 | 双向，默认开启 |
| 文件传输 | `enable-drive=true`，左边缘可拉出文件面板。落盘在 guacd 容器 `/tmp`，**容器重启即失**，只当临时中转 |
| 音频 | 客机 → 浏览器（`audio/L16`） |
| 多设备 | 手机浏览器也能进，不需要装任何 App |

**限制：Win11 专业版只有一个交互式会话。** 如果你正通过 Windows App 连着，
再用浏览器连同一个账户，浏览器会把 Windows App 那个会话**踢掉**（反之亦然）。
不是 Guacamole 的问题，是 Windows 客户端 SKU 的限制；要并排两个桌面得 Windows Server + RDS。

## 5. 自检：三层证据，别只看容器是否 running

`bin/guac-probe.py` 直接和 guacd 说 Guacamole 协议，不经过浏览器。它的输出把
"链路通不通"和"凭据对不对"分成了不同结论：

```
$ ./bin/guac-probe.py --hold 8
-> select rdp
   target host.docker.internal:13389 user=<邮箱> password=(空，测到认证阶段)
<- args VERSION_1_5_0, 88 个参数
-> connect VERSION_1_5_0 (88 个值)
<- ready id=$8a1c623f-... (0.04s)
<- 8.0s 内收到: cursor×1, mouse×1, nop×1, required×1, sync×1
结论: 会话已经建立但没有画面流动，需要人工看一眼
```

`required` 就是服务端在说"我要凭据"。不带 `--password` 时这就是正常终点；
guacd 的日志会补上另一半证据：

```
INFO:  Security mode: NLA
INFO:  Resize method: display-update
INFO:  RDP server closed/refused connection: Authentication failure (invalid credentials?)
```

**这条 "Authentication failure" 是好事**：说明 guacd 真的把凭据送到了客机的 NLA，
是客机自己拒绝的。链路没问题，只差密码。

带上密码跑同一个脚本会走到 `ready` 之后开始收 `sync`/`img`，结论变成
"会话活着且在推画面"。或者更直接：浏览器连一次，然后在宿主机跑

```bash
/data/vms/win11/bin/verify-rdp-works.py     # 客机 4624 LogonType=10 + query user
docker logs --tail 20 win11-guacd           # 应有 joined connection + 无 error
```

## 5.5 会话超时：切到别的窗口再切回来要不要重新登录

`guacamole.properties` 里的 **`api-session-timeout`** 决定"认证令牌在**无活动**情况下还能
存活多少分钟"，默认 **60**。超了之后浏览器回到登录页，连同已开的 RDP 隧道一起断掉。

现在设成 **720 分钟（12 小时）**：

```properties
# /data/vms/win11/guacamole/guacamole.properties → 容器 /etc/guacamole/guacamole.properties
api-session-timeout: 720
```

改完只需重建网页那个容器（guacd 和数据库不动）：

```bash
cd /data/vms/win11/guacamole
vi guacamole.properties          # 改成你要的分钟数
docker compose up -d guacamole
docker logs win11-guacamole 2>&1 | grep "Sessions will expire"
# 期望：Sessions will expire after 720 minutes of inactivity.
```

这条启动日志是唯一可靠的验证点 —— 改完不出现新数字就是没生效（比如挂载路径写错）。

**几个注意点**：

- 文档只承诺"无活动多久失效"，**没有说 `0` 等于永不过期**，别猜；要"几乎不过期"就给个很大的数（如 `10080` = 7 天）。
- 令牌越长，安全面越大。这台在内网、单人用，12 小时是权衡后的取值。
- 容器里 Tomcat 的 HTTP 会话超时是 **30 分钟**（`/usr/local/tomcat/conf/web.xml`），
  但 Guacamole 的认证走的是 URL 里的 `token` 参数，不依赖 JSESSIONID，所以那 30 分钟
  不参与这个行为。真要连它也一起改，得覆盖 `conf/web.xml`，那会在镜像升级时变成维护负担。
- **窗口被浏览器回收**（Chrome 的 Memory Saver 会丢弃长时间不用的后台标签页）会直接
  掐掉 websocket，这时即使令牌还有效，隧道也断了——回来要重新点一次连接，但不用重新登录。
- 想连 RDP 会话一直活着，**标签页得留着**；把浏览器关了，客机那边的会话就结束了。

## 6. 运维

```bash
cd /data/vms/win11/guacamole
docker compose ps                      # 三个容器都该是 running
docker compose logs -f guacamole       # 网页侧日志
docker logs -f win11-guacd             # 会话侧日志，排错看这个
docker compose restart guacamole       # 只重启网页
docker compose down / up -d            # 全停 / 全起
```

- **别被 `health: starting` 骗到**：guacd 镜像自带的 healthcheck 是 `nc -z 127.0.0.1 4822`，
  但 `Interval` 是 **300 秒**且没有 `StartPeriod`，所以刚启动的 5 分钟内它一直显示
  `starting`，这是计时间隔问题不是故障。要立刻判断生死，用 `./bin/guac-probe.py`。
- **开机自启**：三个容器都是 `restart: unless-stopped`，宿主重启后 docker 自动拉起。
  注意 `docker compose down` 会**删掉**容器，之后不会自启；日常停用请用
  `docker compose stop`。
- **备份**：真正需要备份的只有 `.env` 和 `initdb.sql`；`db/` 里是连接定义和用户表，
  丢了重跑 `./bin/guac-setup.py` 就能重建（管理员密码会回到 `.env` 里那个值）。
  所以不值得为它单独做备份策略。
- **升级**：把两处 `1.6.0` 改成新 tag，`docker compose pull && up -d`。跨大版本时
  确认库表是否需要迁移（官方有 upgrade 脚本）。

## 7. TLS 在前置代理上终结，不在这里

部署形状变了：网页现在是 `0.0.0.0:8081` 明文 HTTP，由 VPS 上的 nginx 通过 WireGuard 访问，
**TLS 在那台 nginx 上终结**：

```
浏览器 --HTTPS--> nginx（VPS，Authelia forward_auth）--wireguard--> nuc12:8081 guacamole
```

这是刻意的，不是漏配。当初逼着这台机器上 TLS 的那个理由（剪贴板）**由前置代理的证书
满足了**：安全上下文取决于浏览器真正对话的源，也就是
`https://vm-win11.example.com`，与本机无关。摘掉 Caddy 之前的实测：经前置 HTTPS
同一个页面 `isSecureContext: true`、`navigator.clipboard` 带 `readText`/`writeText`；
而直连明文 `192.168.2.48:8081` 时是 `undefined`。前置代理解决了后者，不需要 Caddy 陪着。

LAN 这一段走明文，因为它在家里、只有一个用户；WireGuard 隧道本身已经保护了传输。

**唯一的代价**：在局域网里直接用浏览器打开 `http://192.168.2.48:8081`，控制台能用但
**剪贴板失效**。真需要的话把 Caddy 覆盖层加回来（§7.1）——那时这条路上的 TLS 纯粹是为了
剪贴板，不是为了保密。

### 7.1 可选：把 TLS 加回本机

`docker-compose.tls.yml` + `Caddyfile` 依然可用，栈里其他东西都不用改：

```bash
echo 'WEB_PORT=127.0.0.1:8081' >> .env        # 网页退回回环
docker compose -f docker-compose.yml -f docker-compose.tls.yml up -d
# https://192.168.2.48:8443/guacamole/
```

证书来自 Caddy 自己的本地 CA（`tls internal`）——`192.168.2.48` 是内网地址，ACME 不可能
验证。浏览器第一次会警告；导入根证书即可消警：

```bash
docker exec win11-guac-tls cat /data/caddy/pki/authorities/local/root.crt > nuc12-caddy-root.crt
# macOS：双击导入，或者
sudo security add-trusted-cert -d -r trustRoot \
     -k /Library/Keychains/System.keychain nuc12-caddy-root.crt
```

信任前先确认链完整（期望 `TLS: 0`）：

```bash
curl -s -o /dev/null -w 'HTTP %{http_code}  TLS %{ssl_verify_result}
' \
     --cacert nuc12-caddy-root.crt https://192.168.2.48:8443/guacamole/
```

它的根证书（`CN=Caddy Local Authority - 2026 ECC Root`）存在 `./caddy-data/`，删掉该目录
等于换 CA，所有客户端都要重新信任。

### 7.2 两个 SNI 坑，同一条毫无信息量的报错

两者的表现完全一样：**浏览器什么都不说，curl 只报
`TLSv1.3 (IN), TLS alert, internal error (592)` / `error:0A000438`**，Caddy 默认日志级别下
一个字都不记。要在全局块里加 `debug` 才能看到真正原因。

| 现象 | 原因 | 修法 |
|---|---|---|
| 日志里 `certificate obtained successfully` 成功，但每次握手都是 `no certificate matching TLS ClientHello` | TLS 客户端对 **IP 字面量不发 SNI**（RFC 6066 禁止把 IP 放进 `server_name`），Caddy 没有名字可匹配 | 全局块加 `default_sni 192.168.2.48` |
| 同样的报错，且 CA 压根没签发证书 | 站点地址写成了裸的 `https://:8443`，没有可签发的名字 | 写成 `https://192.168.2.48:8443` |

本目录的 `Caddyfile` 两个修法都带，并且把推理写在注释里。别"简化"回 `:8443`，也别
在那个 site 块里加第二个主机名而不设 `default_sni`——那会多签一张证书，然后不带 SNI 的
IP 握手就什么也匹配不上。

### 7.3 结果（开着覆盖层时的样子）

```
$ curl -sk -o /dev/null -w '%{http_code}\n' https://192.168.2.48:8443/guacamole/
200
$ echo | openssl s_client -connect 192.168.2.48:8443 2>/dev/null | openssl x509 -noout -subject -issuer -ext subjectAltName
issuer=CN = Caddy Local Authority - ECC Intermediate
X509v3 Subject Alternative Name: critical
    IP Address:192.168.2.48
$ # 浏览器打开同一地址：
{ "isSecureContext": true, "clipboard": "object", "readText": true, "writeText": true }
```


### 7.4 Header 认证：让登录页消失，把认证交给代理

Guacamole **没有"关闭认证"的开关**——每个连接都必须属于一个已认证的用户，认证层同时决定
这个用户能看到哪些连接。能做的是让它不再追问：`guacamole-auth-header` 接受反向代理放进
请求头的用户名，浏览器就再也看不到登录页。镜像自带该扩展，compose 里一行打开：

```bash
echo 'HEADER_AUTH=true' >> .env          # 对应 compose 的 HTTP_AUTH_ENABLED
docker compose -f docker-compose.yml -f docker-compose.tls.yml up -d guacamole
```

然后用 `guacamole.properties` 指定头名。**不要用 `REMOTE_USER`**——见下。

本机实测（200 = 认证通过，403 = 退回登录页）：

| 请求 | 结果 |
|---|---|
| `POST /guacamole/api/tokens` + `X-Guac-User: guacadmin`，直连 8081 | `200`，`dataSource: header`，连接列表 `win11-rdp` |
| 同样的请求，经过一跳代理 | `200` —— 头穿得过（在 Caddy 上实测；nginx 同理） |
| 不带该头 | `403 Invalid login.`，JDBC 登录页作为兜底仍然可用 |
| `X-Guac-User: nobody-here` | `200` 但**零连接**（认证层信这个名字，权限来自数据库） |

**`REMOTE_USER` 的坑。** 扩展默认读 `REMOTE_USER`，而下划线不是每一跳都原样传递：
直连 webapp 认证成功，**同一个请求**经 Caddy 却返回 `403`——因为 Go 把 `REMOTE_USER`
规范化成了 `Remote_user`。带横线的头名能原样穿过 nginx、Caddy 和 Envoy：

```properties
http-auth-header: X-Guac-User
```

**安全模型——这段最重要。** header 认证是无条件信任这个头的，所以 webapp 必须"只能
被代理访问"。任何能直连 `http://192.168.2.48:8081` 的人，只要发一个
`X-Guac-User: guacadmin` 就完全绕开 2FA。两种收口方式：

1. **防火墙**：nuc12 上只允许 WireGuard 对端 / 路由访问 8081。
2. **SSH 隧道**：代理机 `ssh -N -L 8081:127.0.0.1:8081 …`，上游写
   `http://127.0.0.1:8081`——什么都不暴露，伪造也就无从谈起。

两种都不接受的话，改用 `guacamole-auth-json`：代理发一个用共享密钥签名并加密的
JSON，伪造不了。

**现在不存在"登出 Guacamole"这回事了。** 头永远在，客户端点退出的结果是丢掉 token、
下一个请求立刻被重新认证——人还在里面。**不要**把它"修"成跳转身份提供方的登出：那个会话
被所有受保护的服务共享，为了关掉 VM 控制台而把整个 SSO 登出，是拿一扇窗换整栋楼。

真正管用的三个动作：

| 想达到 | 怎么做 |
|---|---|
| 离开时锁上 | 锁**客机**（`Win+L`）。真正的边界是 Windows 密码，网页会话只是传输通道 |
| 释放唯一的交互式会话 | Guacamole 菜单 → *Disconnect*，或直接关标签页——Windows 侧变成 disconnected，程序继续跑 |
| 作废所有身份 | 到身份提供方自己的界面登出，并接受它影响全部服务 |

如果这个控制台必须比其他服务更快过期，Authelia 可以给它挂一个**独立命名的 cookie 条目**
并单独设 `inactivity`/`expiration`——代价是这个域不再与其他服务共享会话（在这里登录
不会让别的服务也通过认证）。这是设计取舍，不是一个开关。

可直接改用的反代配置（Authelia `forward_auth`、WebSocket 升级、长超时、头注入）：
[`nginx-vm-win11.conf.example`](nginx-vm-win11.conf.example)。Authelia 侧需要为新域名
加 `access_control` 规则（`policy: two_factor`），并让 session cookie 的 domain 覆盖它。

## 8. 收紧：让 RDP 只对 guacd 可见

现在 `192.168.2.48:13389` 对整个局域网开放。如果只想留网页一个入口：

```bash
# 1) QEMU 的 hostfwd 改回回环
sed -i 's/hostfwd=tcp:0\.0\.0\.0:13389/hostfwd=tcp:127.0.0.1:13389/' /data/vms/win11/bin/start-vm.sh
sudo systemctl restart win11          # 会断掉正在用的 RDP 会话

# 2) 让 guacd 用宿主网络，这样它能连 127.0.0.1
#    在 docker-compose.yml 的 guacd 服务下加： network_mode: host
#    同时删掉它的 ports: 段（host 网络下没有端口映射的概念）

# 3) .env 里 RDP_HOST=127.0.0.1，然后
docker compose up -d && ./bin/guac-probe.py
```

`network_mode: host` 下 guacd 直接用宿主网络栈，`ports` 段会被 compose 忽略/报错，
这点别忘。

## 9. 踩过的坑（都是真事）

**坑 1：`umask 077` 会把 `initdb.sql` 也变成 0600，postgres 读不到。**
现象极隐蔽：容器全部 running、网页 200，但登录报认证失败，`\dt` 里一张表都没有。
postgres 日志里只有一行：

```
/usr/local/bin/docker-entrypoint.sh: running /docker-entrypoint-initdb.d/01-initdb.sql
psql: error: .../01-initdb.sql: Permission denied
```

原因是 postgres 镜像以 uid 70 跑初始化脚本，而 `initdb.sql` 是 `yusiwen:yusiwen 0600`。
生成 `.env` 时的 `umask 077` 会扩散到同一次 shell 里所有后续重定向。修法：
`chmod 644 initdb.sql`，然后删掉 `db/` 重新初始化（库已经建过的话不会再跑 initdb）。

**坑 2：guacd 的 `ready` 早于认证结果，别把它当成功。**
`ready` 在 0.04 秒就返回，那时 RDP 驱动还在自己的线程里连。凭据错了不会体现在
`ready` 上，而是之后 guacd **自己关掉连接**，把原因写进日志
（`Authentication failure (invalid credentials?)`）——或者给客户端发 `error` 指令。
所以探针必须在 `ready` 之后继续监听，用有没有 `sync`/`img` 帧流来判断"真活着"。
只看 `ready` 会得到一次假成功。

**坑 3：Guacamole 协议里 `connect` 才是带参数的指令。**
`select` 只带协议名（`6.select,3.rdp;`），服务端回 `args` 列出它接受的全部参数名
和版本号（这台是 `VERSION_1_5_0` 88 个参数），客户端再按 **args 里的顺序**
逐值塞进 `connect`。第一版探针把参数写进了 `select`，guacd 直接
`Guacamole protocol violation` 并断开。

**坑 4：`error` 指令的参数顺序是 `message, status`，不是反过来。**
按 `status, message` 解析会拿到一堆乱码状态码。

**坑 5：REST 的 `GET /connections/{id}` 不返回 `parameters`。**
参数存在 `guacamole_connection_parameter` 表里，API 上要用
`GET /connections/{id}/parameters`，或者直接查库。用前者看到的"参数为空"是错觉。

**坑 6：`guacadmin` 出厂密码是 `guacadmin`，网页一上线就能被扫。**
`bin/guac-setup.py` 第一次运行就会轮换，并且幂等（第二次运行会用新密码登录、跳过轮换）。

**坑 7：Windows 密码不要存进连接定义。**
连接里 `password` 留空，Guacamole 会在浏览器里提示输入。存进去等于在数据库里
明文/弱加密保存你的微软账户密码，而且密码一改就永久失败。要存就单独放
`.env` 并接受它是明文。

**坑 8：`apt upgrade` 升级 docker / containerd 会把所有人踢下线。**
守护进程重启后 `restart: unless-stopped` 会把容器拉起来（`StartedAt` 会变、`RestartCount` 仍是 0），
但 Guacamole 的会话全在内存里，网页侧一律回到登录页。分辨方法：
`systemctl show docker -p ActiveEnterTimestamp` 的时间戳和容器 `StartedAt` 几乎一致 === 是守护进程重启，
不是会话超时。2026-11-17 11:52 就撞过一次（用户自己 apt 操作触发）。

**坑 9："剪贴板不工作"几乎从来不是 Guacamole 的配置问题。**
在动 `disable-copy` / `disable-paste`（默认都是 false）之前，先看访问源：
`isSecureContext` 必须是 `true`。走明文 HTTP 时它是 `false`，`navigator.clipboard` 根本不存在，
剪贴板两个方向都悄无声息地失效。一分钟定案的证据链：连接参数里没有任何剪贴板相关键、
客机 `rdpclip.exe` 在跑、页面走的是 HTTP。最后那条就是元凶，见 §7。

**坑 10：`api-session-timeout` 改了没生效，先看挂载。**
`GUACAMOLE_HOME=/etc/guacamole` 是镜像自带的，但容器里原来没有 `guacamole.properties`，
文件必须**挂进去**（`./guacamole.properties:/etc/guacamole/guacamole.properties:ro`）。
验证只看一行启动日志里的分钟数，别看网页是否还开着。

## 10. 为什么不是 KasmVNC

顺便记下结论，免得以后再绕一圈：**KasmVNC 是 VNC 服务端（自带 X server + 网页客户端），
不是网关**，它没有"连到别人 VNC/RDP"的模式，所以接不上 QEMU 的 VNC 输出。

**Kasm Workspaces**（另一个产品）确实能接"固定基础设施"的 RDP/VNC/SSH 并网页化，
但它内部做这件事的组件就是 **guacd** —— 官方文档原话："Guacd is used for
web-native connections to backend RDP, VNC, and SSH servers"。它多出来的是用户
管理、会话录制、容器隔离、CAS、管理界面，以及一套 DB/redis/agent/nginx 的运维负担
和免费版并发限制。单人用一台 Windows 虚机，Guacamole 就是那个内核本身。

其它候选：MeshCentral 需要往 Windows 里装 agent；JumpServer 是堡垒机形态、偏重；
Windows 原生 RD Web Access 只存在于 Windows Server，Win11 专业版没有；
RustDesk 是 TeamViewer 那类替代品，不是 RDP 网关。

---

## 附：实测记录（2026-11-17 部署当天）

| 项 | 结果 |
|---|---|
| 镜像 | `guacamole/guacd:1.6.0`、`guacamole/guacamole:1.6.0`、`postgres:16-alpine` |
| 容器 | 三个都 running，`restart: unless-stopped` |
| 网页 | `GET /guacamole/` → **200**（回环和局域网都试过） |
| 库表 | `initdb.sql` 793 行，`guacamole_*` 表全部建成 |
| 连接 | `win11-rdp` id=1，13 个参数，已授权给 guacadmin |
| 参数 | hostname=host.docker.internal, port=13389, security=nla, ignore-cert=true, resize-method=display-update, enable-drive=true, color-depth=32 |
| guacd 协商 | `Security mode: NLA`、`Resize method: display-update`、`args` 报 `VERSION_1_5_0` |
| 认证 | 空密码 → 客机 `Authentication failure (invalid credentials?)`（链路通，等真密码） |
| 端口 | 8081 HTTP（0.0.0.0，网页）、4822 guacd（仅 127.0.0.1）；8443 只在启用可选的 Caddy 覆盖层时存在 |
| 会话超时 | 默认 60 分钟 → 改为 **720 分钟**，启动日志确认 `Sessions will expire after 720 minutes of inactivity.` |
| 资源 | 实测常驻内存：guacamole 313 MiB + postgres 30 MiB + guacd 11 MiB ≈ **354 MiB**（`docker stats --no-stream`） |

**尚未由我验证的一步**：用真实 Windows 密码在浏览器里连一次、看到桌面。
这一步需要你的账户密码，我不接触。连过之后跑 `/data/vms/win11/bin/verify-rdp-works.py`
就能从客机侧确认（`4624 LogonType=10`）。
