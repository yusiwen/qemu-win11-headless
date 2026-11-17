# win11/bin —— 这台虚机的全部脚本（可读、可改、无第三方依赖）

全部只用 Python 标准库 / POSIX shell，不装任何包。每个脚本头部都有 docstring 讲原理，
这里只做导读：**每个脚本解决什么问题、内部靠什么机制、以及我当时踩的坑**。

> English version: [README.md](README.md)

## 一张图看懂三条通道

```
                ┌────────────── 宿主机 (nuc12) ──────────────┐
 你的 Mac ──ssh──┤                                            │
                │  qmp.sock ◄── QMP ──┐                      │
                │                     ├── QEMU 进程 ── 客机Windows│
                │  qga.sock ◄── QGA ──┘                      │
                │  :5901    ◄── RFB(VNC) ──┘                 │
                └────────────────────────────────────────────┘
```

| 通道 | 谁跟谁说话 | 能干什么 | 不能干什么 |
|---|---|---|---|
| **QMP**（`qmp.sock`） | 宿主 ↔ QEMU 本体 | 电源、状态、截图、改 VNC 密码、设备树 | **不能**执行客机里的命令 |
| **QGA**（`qga.sock`） | 宿主 ↔ 客机里的 qemu-ga | 跑命令、读网络、优雅关机 | 需要客机装 guest tools |
| **RFB/VNC**（`:5901`） | 客户端 ↔ QEMU 图形 | 看画面、点鼠标 | 没有"客户端改分辨率"这回事 |

**最容易踩的认知错误**：以为 QMP 能转发 guest 命令（`qmp.py ... guest-ping` 会报
`CommandNotFound`）。那层转发是 **libvirt** 做的，QEMU 自己不做。客机内的事一律走 `qga.py`。

## Python 脚本

### `qmp.py` —— QEMU 控制面
```bash
./qmp.py /data/vms/win11/qmp.sock query-status
./qmp.py /data/vms/win11/qmp.sock change-vnc-password '{"password":"xxx"}'
```
协议：unix socket 上的 JSON 行协议。连上先收到 `{"QMP":{...}}` 问候语，然后必须发
`{"execute":"qmp_capabilities"}` 才能用其它命令。回复里可能夹着异步**事件**（`{"event":...}`），
所以读的时候要循环跳过非 `return`/`error` 的行——这就是那段 `while True` 的意义。

### `qga.py` —— 客机 agent 客户端
```bash
./qga.py /data/vms/win11/qga.sock guest-ping
./qga.py /data/vms/win11/qga.sock guest-info | head -c 300
./qga.py /data/vms/win11/qga.sock guest-exec '{"path":"C:\\Windows\\System32\\cmd.exe","arg":["/c","ver"],"capture-output":true}'
```
两个坑，代码里都处理了：
1. **qemu-ga 不会主动打招呼**。客户端连上就 `readline()` 会一直等到超时。必须先发
   `{"execute":"guest-sync","arguments":{"id":N}}`，等到 `{"return": N}` 再发真命令。
2. **`guest-shutdown` 会先断连后回话**：客机开始关机、socket 先关，命令的应答永远等不到。
   代码里把"发完 shutdown 后 EOF"当作成功，否则 systemd 会把这次 stop 记成 failed。
   这个脚本就是 `win11.service` 的 `ExecStop`。

### `screen.py` —— 无显示器截图（我最常用的排错工具）
```bash
./screen.py                      # -> /data/vms/win11/logs/screen.png
./screen.py --out /tmp/x.png
```
两步：QMP `screendump` 让 QEMU 把帧缓冲写成 **PPM(P6)**；再手写 PNG
（`签名 + IHDR + IDAT + IEND`，每块是 `长度|类型|数据|CRC32`，扫描行前加一个 filter 字节 0）。
为什么值得读：PPM 打不开，PNG 能直接在预览里看；而且这是客机还没网络、没 agent 时**唯一**的诊断通道。
我靠它确认过三件事：OVMF 认到光盘、`Press any key` 超时掉进 PXE、桌面到底起来了没有。

### `iso-verify.py` —— 不挂载、不 root、不装包地看 ISO 里有什么
```bash
./iso-verify.py /data/vms/iso/virtio.iso 'viostor/w11/amd64' 'guest-tools'
```
ISO9660 就三个事实：卷描述符从第 16 扇区开始（字节 1-5 是 `CD001`）；根目录记录在 PVD 偏移
156，其中 extent LBA 是小端 u32（+2）、长度是 u32（+10）；目录是一串记录
`长度(1) 属性(1) LBA(4) 长度(4) 时间(7) 标志(1) ... 名字长度(1) 名字`，`flags & 2` 是目录。
用途：**装系统前证明 `viostor/w11/amd64` 真在盘上**——因为 virtio-blk 在安装界面不显示磁盘，
唯一的出路就是去那个目录加载驱动。

### `measure-qemu-cpu.py` —— 精确测 QEMU 的 CPU（比 top 准）
```bash
./measure-qemu-cpu.py 12         # 采样 12 秒
```
读 `/proc/<pid>/task/*/stat` 的第 14、15 字段（utime + stime，单位是 `SC_CLK_TCK`），
两端取差值除以时间。`top` 的瞬时值在虚拟化场景里经常骗人——我第一次看到 185%，
精确采样才发现稳态是 100%，185% 是 Windows Update 的瞬时高峰。
**结论应用**：主线程（含 VNC 压缩）占 ~30%，5 个 vCPU 线程各占 8–16%，而客机自报负载
18%（≈1.08 核）——两者吻合，说明没有隐藏开销，全是客机自己在干活。

### `guest-load.py` —— 从宿主问客机"你在忙什么"
```bash
./guest-load.py
```
通过 QGA `guest-exec` 跑 PowerShell：CPU 负载、Top 进程、Windows Update/Defender 服务状态、
待装更新数。技术点是 **base64 + `-EncodedCommand`**：复杂 PowerShell 直接塞进命令行会被
引号和多层 shell 吃掉（我为此浪费了三轮），UTF-16LE base64 编码后当单个参数传，一次就过。

### `guest-ps.py` —— 通用锤子：往客机里塞任意 PowerShell
```bash
./guest-ps.py script.ps1              # 跑完打印输出
./guest-ps.py - < script.ps1          # 从 stdin 读
./guest-ps.py --detach long-job.ps1   # 起任务、打印 pid、立刻返回
```
上面那些脚本本质都是这个模式的定制包装，这一个不预设内容。`--detach` 很关键：删
`C:\Windows.old` 要十几分钟，起任务后把进度写进客机日志，外面轮询即可。输出按 UTF-8
解码、失败回落 GBK —— 因为 Windows PowerShell 走管道时用 OEM 代码页，中文会变乱码。

### `check-update-state.py` —— Windows 更新之后的体检
```bash
./check-update-state.py
```
一次调用回答"更新把什么改坏了"：版本/内部版本、启动时间与运行时长、三个挂起重启标记、
`fDenyTSConnections`（RDP 开关）、`DevicePasswordLessBuildVersion`（Hello 无密码模式）、
`HiberbootEnabled`（快速启动）、时区、UTC、C 盘剩余，再列出最近 12 小时的
1074/6005/6006/6008/41 事件。

为什么值得写成脚本：功能更新会把刻意关掉的开关又打开（实测 `HiberbootEnabled` 从 0 变回 1），
而 RDP 能不能用、微软账户密码还能不能登录，取决于两个注册表值有没有被重写。
靠肉眼看"能连上了"不够，得逐个值核。用法见 `../README-zh_cn.md` §7.12。

### `check-display.py` / `enum-display-modes.py` —— 显示适配器与分辨率
```bash
./check-display.py               # 适配器名、当前分辨率、驱动版本
./enum-display-modes.py          # 预期列模式，实测返回 count=0（见下）
```
`check-display.py` 走 `Win32_VideoController`，能看到"Microsoft Basic Display Adapter /
1280 / 800"，这就是 Window 大小被锁死的根因。
`enum-display-modes.py` 用 P/Invoke 调 `EnumDisplaySettings` 枚举所有模式，**实测返回 0**——
因为 qemu-ga 跑在 Windows 的 **session 0**（服务会话），那里没有交互式桌面。这是个有价值的
负结果：想在宿主机侧枚举客机分辨率，这条路不通，只能在 RDP/交互会话里做。

### `rdp-probe.py` —— 协议级验证 RDP（别再用 nc 骗自己）
```bash
./rdp-probe.py                  # 127.0.0.1:13389
```
**`nc -vz 127.0.0.1 13389` 在这里是假阳性**：QEMU 的 SLIRP 转发自己就把 TCP 挥手做完了，
然后才去连客机。所以客机里 3389 没在听，宿主侧照样"连接成功"。这个脚本发的是真正的
首个 RDP 包（TPKT + X.224 Connection Request，带 mstshash cookie 和 NLA 协商请求），
只有真终端服务才会用 X.224 **Connection Confirm(0xD0)** 回答，并告诉你协商到
`PROTOCOL_HYBRID`（CredSSP/NLA）。

### `enable-rdp.py` —— 幂等开启远程桌面
```bash
./enable-rdp.py
```
一次做完四件事：`fDenyTSConnections=0`、`TermService` 设为自动并启动、启用防火墙规则组
`@FirewallAPI.dll,-28752`（**语言无关组 ID**，中文系统上显示名是"远程桌面"，按名字查会查不到）。
在 GUI 里以为打开了其实没打开，是这个坑最常见的形态。

### `check-rdp.py` —— RDP 准入体检
```bash
./check-rdp.py
```
查开关、监听、NLA 要求、当前登录用户、账户来源（本地/MicrosoftAccount）、远程桌面用户组、
管理员组、防火墙方向。排查"密码对但登不进去"时先看账户来源——微软账户要用 MS 密码，PIN 不行。

### `check-rdp-logon.py` —— 认证失败时读错误码，别猜
```bash
./check-rdp-logon.py
```
读客机安全日志里的 4625（失败登录），带 **Status/SubStatus**：

| SubStatus | 含义 | 该去改什么 |
|---|---|---|
| `0xC000006A` | 密码错误 | 密码本身 |
| `0xC0000064` | 用户不存在 | 用户名格式/拼写 |
| `0xC0000234` | 账户被锁 | 等解锁或查 4740 |
| `0xC0000072` | 账户已禁用 | 启用账户 |
| `0xC000015B` | 未被授予该登录类型 | `SeRemoteInteractiveLogonRight` |

同时打印 `SeRemoteInteractiveLogonRight` 的成员（SID 已翻译成名字）、账户的 enabled /
PrincipalSource / PasswordLastSet、以及 RDP-Tcp 的 securitylayer/usrauth。
**这条把"权限不足""账户不存在""密码错"三种完全不同的病因一次分开**，比反复试密码高效得多。

### `verify-rdp-works.py` —— 确认 RDP 真的通了
```bash
./verify-rdp-works.py
```
三项证据：`DevicePasswordLessBuildVersion`（2 = Hello 无密码模式，0 = 允许密码）、
`Security 4624 LogonType=10`（RemoteInteractive，真正的 RDP 会话）、`PowerShell query user` 的会话表。
**别拿 `LogonType=11`（CachedInteractive）当 RDP 成功** —— 那是控制台缓存登录。

### `fix-guest-time.py` —— 时区与绝对时间
```bash
./fix-guest-time.py [--tz "China Standard Time"]
```
两步：`Set-TimeZone` + QGA `guest-set-time`（传 epoch 纳秒，**时区无关**，不依赖客机能连 NTP）。
为什么需要：`-rtc base=localtime` 只是把宿主的墙上时间塞进 RTC，Windows 仍会按**自己的时区**
解释它。装机时默认落在了 `FLE Standard Time (UTC+2)`，于是显示的钟看着对、**UTC 却快 6 小时** ——
而证书校验、微软账户验证用的是 UTC。这台机器 OOBE 时 PIN 设置失败就是它导致的。

### `check-shutdown-events.py` —— 关机到底干不干净
```bash
./check-shutdown-events.py
```
查三条事件：**1074**（谁发起的关机）、**6006**（事件日志服务停止 = 关机走完了）、
**41**（Kernel-Power：上次是非法断电）。判断标准：有 1074 + 6006、且没有新的 41 = 干净关机。
这是发现"`systemctl stop` 其实是硬断电"的关键证据。

## Shell 脚本

| 脚本 | 作用 |
|---|---|
| `start-vm.sh` | 起 swtpm → 起 QEMU（`-daemonize`）→ 经 QMP 设 VNC 密码。每个参数为什么在那，见 `../README-zh_cn.md` §4 |
| `boot-from-cd.sh` | 从安装盘引导并**代按空格**。Windows 的 "Press any key to boot from CD" 只等 5 秒，headless 必踩 |

## 客机侧 PowerShell

| 脚本 | 作用 |
|---|---|
| `cleanup-windows-old.ps1` | 功能更新后删 `C:\Windows.old` 并做 TRIM，让宿主镜像真的缩小。`rd /s /q` 以 SYSTEM 跑仍会剩下约 40% 文件（属主 TrustedInstaller），所以 takeown+icacls 兜底是**必须**的，不是可选项。用 `guest-ps.py --detach` 起 |

## 一组对照实验的方法（可复用）

1. **先量，再改**：`measure-qemu-cpu.py` 采样 → 改一个变量（比如 `kvm halt_poll_ns`）→ 再采两次。
   我给 `halt_poll_ns` 做了 0 vs 200000 的 A/B：103.6% vs 103.3%，**没差别**，于是改回默认。
   没有收益的调优不要留。
2. **别信单次采样**：同一配置我测了两次（103% 和 76%），差异来自客机本身在跑 Windows Update。
   结论必须建立在"同一状态下重复测"的前提上。
3. **找反证**：要证明"关机是软的"，不能看 `systemctl stop` 有没有报错，要读客机事件日志；
   要证明"光盘能启动"，不能看端口在听，要截图看画面。

## guacamole/bin/：网页网关侧的两个脚本

| 脚本 | 作用 |
|---|---|
| `../guacamole/bin/guac-setup.py` | 幂等引导：把管理员密码从出厂 `guacadmin` 换成 `.env` 里的值，并按 `.env` 创建/更新 `win11-rdp` 连接 |
| `../guacamole/bin/guac-probe.py` | 直接和 guacd 说 Guacamole 协议（`select` → `args` → `connect` → `ready`），把"链路不通"和"凭据不对"分成不同结论。`--hold N` 会在 `ready` 之后继续监听，用有没有帧流判断会话真的活着 |

为什么值得单独写一个协议探针：网页 200、容器 running 都不能证明 RDP 这条路通；
而 `ready` 也不等于认证成功 —— guacd 的 RDP 驱动在自己的线程里连，凭据错了会在
`ready` 之后才表现为连接被关闭。和 `rdp-probe.py` 是同一个思路：不信端口，只信协议。
详细说明见 `../guacamole/README.md` §5。

