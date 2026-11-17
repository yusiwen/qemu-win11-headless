# Windows 11 虚机（nuc12 · QEMU/KVM）部署方案与使用说明

> 全部内容基于 2026-11-16 在 `nuc12wski5` (192.168.2.48) 上的实测，不是估计值。
> 本文档与虚机同目录（`/data/vms/win11/README.md`），改动虚机时请顺手更新本文档。

---

## 0. 当前状态

| 阶段 | 状态 |
|---|---|
| 宿主软件安装（qemu / ovmf / swtpm） | ✅ 完成 |
| 磁盘、OVMF 变量、TPM 状态目录 | ✅ 完成 |
| 启动脚本、QMP 客户端、systemd unit | ✅ 完成 |
| 虚机启动、UEFI + Secure Boot + TPM 2.0 生效 | ✅ 完成（已截图验证 OVMF 引导光盘成功） |
| **Windows 11 安装（人工图形界面）** | ✅ 完成（23H2 专业版，客机主机名 `vm-win11-nuc12`） |
| guest tools / 远程桌面 / 关闭快速启动 | ✅ 完成（宿主机 13389 → 客机 3389 已连通） |
| `BOOT` 改回 `c`、从硬盘引导 | ✅ 完成（截图确认进到 Windows 桌面） |
| qemu-guest-agent 通道（QGA virtio-serial） | ✅ 完成（`guest-ping` / `guest-exec` 通，qemu-ga 110.0.2） |
| 优雅关机 | ✅ 验证（客机事件 1074「API 关机」+ 6006「日志服务停止」，无 Kernel-Power 41） |
| 基线快照 `clean-install` | ✅ 完成 |
| 宿主开机自启 | ✅ `systemctl is-enabled win11` = enabled |
| `post-setup` 快照 | ✅ 完成（ID 2，RDP 可用态，日常回滚用这个） |
| 网页访问（Guacamole） | ✅ 部署完成，`http://192.168.2.48:8081/guacamole/`；链路验证到 NLA 认证阶段，最后一步需你的 Windows 密码（见 `guacamole/README.md`） |

---

## 1. 方案与取舍

### 为什么用原生 QEMU，而不是 libvirt / Proxmox

- 只有这一台虚机。libvirt 要多一个常驻 daemon、一层存储池/权限模型和看不见的 XML，收益（`virsh` 生命周期管理、在线快照、克隆）在单机单虚机场景用 `systemctl` + `qmp.py` + `qemu-img` 就能覆盖。
- 这台机器是 **k0s 节点**（k0s + calico + docker + ollama 都在跑），重装成 Proxmox 的代价远大于收益。
- 切换条件写在这里，避免以后重新纠结：**出现 3 台以上虚机、需要在线快照/克隆、需要 `virsh` 图形化管理时，改上 libvirt**。届时 `virt-install` 一条命令即可复现本目录的参数。

### 关键选择一览

| 选择 | 取值 | 理由 |
|---|---|---|
| 固件 | OVMF `OVMF_CODE_4M.secboot.fd` + MS 证书 VARS | Win11 安装程序要求 UEFI **且 Secure Boot capable** |
| TPM | swtpm 0.7.3 模拟 TPM 2.0 | Win11 的第二个硬门槛 |
| 磁盘接口 | `virtio-blk` + 驱动盘 | 性能最好；安装时从 virtio-win 盘加载 `viostor` |
| 网卡 | `e1000e`（Win11 自带驱动） | 装完首次启动就有网，OOBE 联网步骤和 RDP 不用等驱动 |
| 网络模式 | SLIRP NAT + `hostfwd 3389→13389` | 不动宿主网络。宿主唯一网卡在跑 k0s，桥接有断网风险且 headless 无法现场恢复 |
| 控制台 | VNC 绑 127.0.0.1:5901 + SSH 隧道 | 不对外暴露；QEMU 的 VNC 即使加密码也是明文传输 |
| 规格 | 6 vCPU / 12 GiB | 16 线程（4P+8E）的约 1/3、62 GiB 的 ~19%，给 k0s/ollama 留余量。**2026-11-17 用户决定保持不降**：该机平时无负载，实测客机空载仅 2%，降规格的收益抵不上重启与再次适配的成本——不要再提议改小 |

---

## 2. 宿主环境（实测）

| 项 | 值 |
|---|---|
| 主机名 | nuc12wski5（192.168.2.48，唯一网卡 `enp100s0`） |
| 系统 | Ubuntu 24.04.5 LTS，kernel 6.8.0-149-generic |
| CPU | i5-1240P（4P + 8E / 16 线程，28 W），`kvm_intel` 已加载 |
| 内存 | 62 GiB（虚机占 12 GiB） |
| 存储 | `/data` = LVM 500 G，底层 Kingston SNV2S2000G NVMe |
| QEMU | 8.2.2（`1:8.2.2+ds-0ubuntu1.18`） |
| OVMF | 2024.02-2ubuntu0.10 |
| swtpm | 0.7.3 |
| 用户权限 | `yusiwen` 已加入 `kvm` 组；免密 sudo 可用 |

---

## 3. 交付清单

```
/data/vms/win11/
├── README.md                 # 本文档
├── win11.qcow2               # 120 GiB 虚拟盘（qcow2, cluster 64K, lazy_refcounts, preallocation=metadata）
├── vm.conf                   # systemd EnvironmentFile，目前 BOOT=d（光盘优先）
├── vnc.pass                  # VNC 密码（600 权限），启动脚本经 QMP 把它设给 QEMU
├── win11.service             # unit 副本，实际安装位置 /etc/systemd/system/win11.service
├── bin/
│   ├── start-vm.sh           # 启动脚本（起 swtpm → 起 qemu → daemonize）
│   ├── boot-from-cd.sh       # 从安装盘引导，并代按 "Press any key"（headless 必须）
│   ├── README.md             # **脚本导读**：每个脚本的原理、踩过的坑、可复用的实验方法
│   ├── qmp.py                # QMP 客户端：query-status / screendump / change-vnc-password（不进客机）
│   ├── qga.py                # 客机 agent 客户端：guest-ping / guest-shutdown / guest-exec（进客机）
│   ├── screen.py             # 截图：QMP screendump + 手写 PPM→PNG（headless 排错主力）
│   ├── iso-verify.py         # 不挂载/不 root 地列 ISO9660 内容，验证驱动盘
│   ├── measure-qemu-cpu.py   # 精确测 QEMU CPU（读 /proc/<pid>/task/*/stat）
│   ├── guest-load.py         # 经 QGA 问客机：负载、Top 进程、更新状态
│   ├── check-display.py      # 显示适配器与当前分辨率
│   ├── enum-display-modes.py # 枚举分辨率（已知返回 0，session 0 限制，见 bin/README）
│   └── check-shutdown-events.py  # 读客机事件日志，验证关机是否干净（1074/6006/41）
├── ovmf/WIN11_VARS.fd        # OVMF 变量区（含微软证书 → Secure Boot 直接生效）
├── tpm/                      # swtpm 状态（tpm2-00.permall）+ swtpm.pid + swtpm.sock
├── logs/                     # swtpm.log、screen.ppm/png（截图）
├── qmp.sock                  # QMP 控制通道（运行期生成）
├── qga.sock                  # 客机 guest-agent 通道（运行期生成）
└── qemu.pid                  # QEMU 主进程 pid（运行期生成）

/data/vms/iso/
├── zh-cn_windows_11_business_editions_version_23h2_updated_jan_2024_x64_dvd_fee59269.iso
│     6,725,859,328 B，MD5 7f7e4fccf6281b5470d8a369cfa0ce2e（与 MSDN 官方值一致）
├── virtio-win-0.1.285.iso    # 789,645,312 B，含 viostor/NetKVM/Balloon/w11/amd64 与 guest-tools
└── win11.iso / virtio.iso    # 上面两个的短名软链，供命令行引用
```

/data/vms/win11/guacamole/          # 网页 RDP 网关（docker compose，独立子系统）
├── docker-compose.yml              # guacd + postgres + guacamole 三容器
├── docker-compose.tls.yml          # 可选：Caddy 自签 TLS 覆盖层
├── Caddyfile                       # 可选：Caddy 反代配置
├── .env                            # 600。DB 密码 / 管理员密码 / RDP 目标
├── initdb.sql                      # Guacamole 库表结构（入库；文件头记录来源镜像 tag）
├── db/                             # postgres 数据目录
├── README.md                       # **这个子系统的完整文档**
└── bin/{guac-setup.py, guac-probe.py}
```

系统侧改动（共 3 处，都可回滚）：

1. `apt install qemu-system-x86 qemu-utils ovmf swtpm`
2. `usermod -aG kvm yusiwen`
3. `/etc/systemd/system/win11.service` + `/etc/apparmor.d/local/usr.bin.swtpm`（见 §7.1）

---

## 4. 启动参数逐条解释

完整命令见 `bin/start-vm.sh`。不解释的参数不要乱删。

| 参数 | 作用 / 为什么必须有 |
|---|---|
| `-machine q35,accel=kvm,smm=on,hpet=off` | q35 才支持 UEFI + SMM；`smm=on` 是 Secure Boot 的前提；关掉 HPET 少一个常驻定时器 |
| `-global driver=cfi.pflash01,property=secure,value=on` | 让 OVMF 的 pflash 按安全模式处理，Secure Boot 才能真正生效 |
| `-drive if=pflash,unit=0,file=OVMF_CODE_4M.secboot.fd` | 固件主体（只读） |
| `-drive if=pflash,unit=1,file=ovmf/WIN11_VARS.fd`（可写副本） | 变量区。用 `OVMF_VARS_4M.ms.fd` 的副本 = 微软证书已预置，Secure Boot 开着且不必手工注册密钥。**这份文件丢了虚机就进不去系统** |
| `-cpu host,migratable=off,hv_relaxed,hv_vapic,hv_spinlocks=0x1fff,hv_time` | 直通宿主机 CPU 特性（Win11 会检查 CPU 型号，`qemu64` 直接不合格）；`hv_*` 是免费的虚拟化优化 |
| `-smp 6,sockets=1,cores=6,threads=1 -m 12G` | 规格，见 §1 |
| `-rtc base=localtime,clock=host,driftfix=slew` | Windows 按本地时间写 CMOS；不加这条客机时间会差 8 小时 |
| `-chardev/tpmdev/tpm-crb` | 把 swtpm 提供的 TPM 2.0 接到客机 |
| `cache=none,aio=io_uring,discard=unmap,detect-zeroes=unmap` | NVMe 上绕过宿主页缓存（宿主断电不坏 qcow2 元数据），客机 TRIM 真正落到 SSD |
| `-device virtio-blk-pci,serial=WIN11DSK` | 高性能块设备；**安装时客机看不到它，必须先加载 viostor 驱动** |
| `-device ide-cd,bus=ide.0 / ide.1` | 两张光盘接 q35 自带 AHCI 的 0/1 口（Win 安装盘 + 驱动盘） |
| `-chardev socket,id=qga0,path=qga.sock,server=on,wait=off` + `-device virtio-serial-pci` + `-device virtserialport,name=org.qemu.guest_agent.0` | 客机 guest-agent 的通道。**没有它 `systemctl stop` 只能硬断电**，而且 `guest-ping`/`guest-exec` 全部不可用。端口名必须是 `org.qemu.guest_agent.0`，Windows 侧 qemu-ga 按这个名字绑定 |
| `-boot order=$BOOT,menu=on` | `d` = 光盘优先（安装期），`c` = 硬盘优先（装完必须改） |
| `-netdev user,hostfwd=tcp:0.0.0.0:13389-:3389` | SLIRP NAT，把宿主机 13389 映射到客机 3389(RDP)。**绑 0.0.0.0**：局域网可直接连 `192.168.2.48:13389`，同时回环仍可用（SSH 隧道方式也保留）。改回 `127.0.0.1` 可让 RDP 只能经隧道访问 |
| `-device qemu-xhci -device usb-tablet` | 绝对坐标指针；没有 usb-tablet 时 VNC 里鼠标会飘到无法操作 |
| `-vnc 127.0.0.1:1,password=on` | 控制台 = 5901 端口，只绑环回 + 强制认证。密码没有命令行参数，`start-vm.sh` 在 QEMU 起来后经 QMP `change-vnc-password` 从 `vnc.pass` 设进去——不加这一步服务器只提供 **安全类型 None**，macOS 自带屏幕共享直接拒连（见 §7.8） |
| `-qmp unix:qmp.sock,server=on,wait=off` | 生命周期与运维通道（关机、状态、截图） |

---

## 5. 安装 Windows 11（人工，图形界面）

### 5.1 连上控制台

```bash
ssh -N -L 5901:127.0.0.1:5901 yusiwen@192.168.2.48    # 保持这个窗口
open vnc://127.0.0.1:5901                              # macOS 自带屏幕共享
# 密码读 /data/vms/win11/vnc.pass（gitignore，不入库）
```

第三方客户端可选 TigerVNC（`brew install --cask tigervnc-viewer`）。RealVNC Viewer 现强制注册账号，不建议。

如果虚机停在 UEFI 启动菜单/PXE/黑屏，重新从光盘引导（会自动代按 "Press any key"）：

```bash
sudo sh /data/vms/win11/bin/boot-from-cd.sh
```

### 5.2 安装步骤要点

1. 语言界面默认中文，直接"下一页" → "现在安装"。
2. 产品密钥点 **"我没有产品密钥"**（商业版 ISO 无家庭版，装完再激活）。
3. 版本：选**专业版**或**企业版**（按你的授权定）。
4. 安装类型：**自定义：仅安装 Windows(高级)**。
5. **磁盘列表是空的** —— 因为 virtio-blk 没有内置驱动：
   - 点 **"加载驱动程序"** → 浏览 → 选 **virtio-win 那张盘**（通常是 D: 或 E:）
   - 进 `viostor\w11\amd64` → 确定 → 出现 `Red Hat VirtIO SCSI controller`
   - 回到列表就能看到 **120.0 GB 磁盘** → "新建" → "应用" → 下一步
6. 安装过程会重启一到两次，**重启时不要再按任何键**（否则又会进安装盘）；如果误按，用 §5.1 的 `boot-from-cd.sh` 只在需要时重来。
7. OOBE 阶段：
   - 卡在"让我们连接到网络"：`Shift+F10` 打开命令行 → `OOBE\BYPASSNRO` （23H2 有效）→ 重启后会多出"我没有 Internet"选项
   - 强制微软账户：`Shift+F10` → `start ms-cxh:localonly` → 直接建本地账户
8. 进桌面后**立刻**做三件事：
   - 打开 virtio-win 光盘，运行 `virtio-win-guest-tools.exe`（装上 balloon、qemu-ga、vioserial 和驱动更新）——**没有 qemu-ga 就无法优雅关机**
   - 设置 → 系统 → 远程桌面 → 打开
   - 控制面板 → 电源选项 → "选择电源按钮的功能" → 关闭 **"启用快速启动"**（否则宿主侧关机/快照会拿到半休眠的脏磁盘）

### 5.3 装完之后（已于 2026-11-17 00:24 执行完毕，保留作为参考）

```bash
# 1. 改为硬盘优先启动
printf "BOOT=c\n" > /data/vms/win11/vm.conf
sudo systemctl restart win11

# 2. 建立基线快照（先关机，再快照，再开机）
sudo systemctl stop win11
qemu-img snapshot -c clean-install /data/vms/win11/win11.qcow2
sudo systemctl start win11

# 3. 开机自启（装完再 enable，避免安装过程中被宿主重启打断）
sudo systemctl enable win11
```

---

## 6. 日常使用

### 启停

| 目的 | 命令 |
|---|---|
| 启动（宿主开机也自动起，前提是 `enable` 过） | `sudo systemctl start win11` |
| 优雅关机（走 QMP → qemu-ga） | `sudo systemctl stop win11` |
| 重启（= 客机硬重启） | `sudo systemctl restart win11` |
| 看状态 | `systemctl status win11` |
| 开机自启 / 取消 | `sudo systemctl enable win11` / `disable` |
| 日志 | `sudo journalctl -u win11 -f`；swtpm 日志见 `logs/swtpm.log` |
| 卡死不响应（等同拔电源） | `sudo systemctl kill -s SIGKILL win11`（下次开机会走磁盘检查） |

启动后约 20–40 秒 Windows 引导完成，RDP 即可连接。

### 访问方式

```bash
# RDP 直连（当前配置，不需要隧道）—— Windows App 里填 192.168.2.48:13389
# 用户名：若用 Microsoft 账户登录 Windows，填你的 Microsoft 账户邮箱；密码填该账户密码（PIN 不适用 RDP）
# 想回到"只经隧道"的模式：把 start-vm.sh 的 hostfwd 改回 127.0.0.1 再重启

# RDP 经隧道（仍然可用，适合不在同一网段时）
ssh -N -L 13389:127.0.0.1:13389 yusiwen@192.168.2.48
# Windows App / Microsoft Remote Desktop 连 127.0.0.1:13389

# VNC（救援通道：RDP 挂了、进 UEFI、蓝屏排错）
ssh -N -L 5901:127.0.0.1:5901 yusiwen@192.168.2.48
open vnc://127.0.0.1:5901        # 密码读 /data/vms/win11/vnc.pass
# 连不上时先用这条自检：nc -vz 127.0.0.1 5901（隧道在不在）
```

### 网页访问（Guacamole）

不想装 Windows App、想用手机或任意电脑的浏览器进来时走这条：

```
http://192.168.2.48:8081/guacamole/     用户 guacadmin，密码在 guacamole/.env 的 ADMIN_PASSWORD
```

登录后点 `win11-rdp`，在提示里输入微软账户密码（连接定义里不留密码）。这条路支持
动态分辨率、双向剪贴板、文件传输（临时目录）和音频。

比原生 RDP 差一点的地方：多一层服务端转码，1080p 下每次操作多几毫秒延迟。
比原生 RDP 强的地方：任何有浏览器的设备都能用，而且换设备不用配隧道。

**Win11 专业版只有 1 个交互式会话**：浏览器连上会把你正在用的 Windows App 会话踢掉，
反之亦然。完整说明、部署细节、自检方法和 6 个真坑见 `guacamole/README.md`。

客机内部拿到的是 SLIRP 私有地址（10.0.2.15），网关 10.0.2.2，DNS 由 QEMU 转发 —— 客机不需要也不应该配静态 IP。

需要"局域网里的真 IP"（其他设备要直连客机的任意端口）时才考虑升级网络模式：改用 macvtap（不动宿主网络）而不是桥接（宿主唯一网卡在跑 k0s，headless 改桥有断网风险）。做法见 git 里的 qemu-kvm-vm-provisioning skill。

### 运维 / 取证

```bash
# 状态
python3 /data/vms/win11/bin/qmp.py /data/vms/win11/qmp.sock query-status

# guest agent 活着没有（装完 guest-tools 才有）
# 注意：QMP **不代理** guest-* 命令，必须走 qga.sock；qga.py 会先发 guest-sync 再发命令
python3 /data/vms/win11/bin/qga.py /data/vms/win11/qga.sock guest-ping
python3 /data/vms/win11/bin/qga.py /data/vms/win11/qga.sock guest-info | head -c 200

# 在客机里跑一条命令（不经 RDP，适合自动化核对）
python3 /data/vms/win11/bin/qga.py /data/vms/win11/qga.sock guest-exec \
  '{"path":"C:\\Windows\\System32\\cmd.exe","arg":["/c","ver"],"capture-output":true}'

# 截图（headless 排错神器；screendump 输出 PPM，可转 PNG 后取回本地看）
python3 /data/vms/win11/bin/qmp.py /data/vms/win11/qmp.sock screendump '{"filename":"/data/vms/win11/logs/screen.ppm"}'

# 实际占用（qcow2 是稀疏的，看 disk size 不是 virtual size）
qemu-img info /data/vms/win11/win11.qcow2

# 宿主侧资源
systemctl status win11 | egrep "Memory|CPU"
```

### 磁盘回收：删了文件，qcow2 不会自己变小

这台虚机的镜像走过两次 Windows 功能更新（23H2 → 25H2 → 26H2），`C:\Windows.old`
一度占到 **37.61 GB / 390,125 个文件**。清理分两步，少一步都白干：

```powershell
# ① 客机内删旧系统（qemu-ga 以 LocalSystem 跑，不需要先 takeown）
cmd /c rd /s /q C:\Windows.old
# ② 关键一步：让宿主上的稀疏文件真正回收
Optimize-Volume -DriveLetter C -ReTrim
```

**为什么第二步不能省**：`win11.qcow2` 是稀疏文件，客机删数据只是把扇区标记为可写，
宿主文件大小不变。启动参数里的 `cache=none,discard=unmap` 负责把客机的 TRIM 转成
宿主上的 punch-hole —— 少了 TRIM，客机里显示 48 GB 空余，宿主 `/data` 一个字节也不还给你。

实测（2026-11-17）：

| 时刻 | 客机 C: 已用 | 宿主 qcow2 |
|---|---|---|
| 清理前 | 70.4 GB | 68.4 GiB |
| 删到一半（8 分钟） | 57.8 GB | 58 GiB ← 边删边缩，说明 discard 这条路是通的 |
| 清理后 | **40.2 GB** | **54.6 GiB** |

两个反直觉的地方：

1. **`rd /s /q` 以 SYSTEM 身份跑也删不干净。** 实测清掉 22.1 万个文件后还剩 **16.8 万个**
   （属主是 TrustedInstaller），必须 takeown + icacls + `Remove-Item` 兜底。总耗时约 15 分钟，
   其中一半花在兜底上。
2. **客机腾出 30 GB，宿主只回收了 14 GB。** 差的这 14 GB 被两个 23H2 时代的内部快照
   （`clean-install`、`post-setup`）钉住了：内部快照保存了当时的 L1 映射表，那些簇即使
   客机已经释放，只要还有快照引用就不能 punch hole。

   删掉这两个过期快照之后（虚机停机 → `qemu-img snapshot -d <tag>` → 开机），文件
   **55 GiB → 32 GiB**，也就是那 23 GB 一次全回来了。**代价与顺序**：删快照不可逆，
   所以先确认新快照（`post-26h2`）已存在、`qemu-img check` 无错、虚机能正常启动，再删旧的。

**结论：一个 qcow2 的实际占用 = 客机当前数据 + 所有内部快照仍引用的历史簇。**
快照不是"免费"的，它钉住的是历史数据；但删它只需停机几秒，不需要重写镜像。

工具：`bin/guest-ps.py`（通用：把任意 PowerShell 塞进客机跑）、
`bin/cleanup-windows-old.ps1`（本次用的脚本，`rd` 失败会退回 takeown+icacls）。

注意 `rd /s /q` 处理 39 万文件要 20 分钟上下，别放在会阻塞的地方跑 —— 用
`guest-ps.py --detach` 起，进度写日志，外面轮询。

### 快照与备份

快照是 **qcow2 内部快照**，存在 `win11.qcow2` 文件内部，不产生额外文件，路径和启动参数都不用改。
成本实测（2026-11-17）：建 `post-setup` 时 disk size 从 27.8 GiB → 27.9 GiB，**只多了 0.1 GiB
的 L1/L2 表元数据**。之后虚机的写入才会真正让文件变大（COW 保留了旧簇供回滚）。

已有两个快照，定位不同：

| ID | TAG | 时间 | 内容 | 用途 |
|---|---|---|---|---|
| 3 | `post-26h2` | 2026-11-17 12:55 | 26H2（`26300.9974`）+ Windows.old 已清理 + 磁盘已 TRIM，客机 40.2 GB | **日常回滚用这个** |

原先的 `clean-install`（23H2 裸装）和 `post-setup`（23H2 + RDP）在两次功能更新后已失效——
回滚过去等于连升两级全部作废——已于 12:58 删除，同时回收 23 GB 宿主空间。
**教训：功能更新后要重估快照的用途，过期快照既没用又占空间。**

**快照不是备份**：它和 qcow2 同处一个文件，宿主机盘坏/文件系统坏/qcow2 元数据坏，快照一起没。

```bash
# 关机后建/列/回滚内部快照
sudo systemctl stop win11
qemu-img snapshot -c <name> /data/vms/win11/win11.qcow2
qemu-img snapshot -l /data/vms/win11/win11.qcow2
qemu-img snapshot -a <name> /data/vms/win11/win11.qcow2
sudo systemctl start win11

# 备份：镜像 + TPM 状态 + OVMF 变量必须一起（同一份状态），别只拷 qcow2
sudo systemctl stop win11
rsync -a --sparse /data/vms/win11/ /backup/win11-$(date +%F)/
sudo systemctl start win11
```

**TPM 状态与磁盘必须同进同退。** 只恢复 qcow2 而用旧/新的 TPM 状态，Windows 会报 "TPM 工作不正常"；万一开了 BitLocker，那就是不可恢复的卷。所以：这台虚机**不要开 BitLocker**（TPM 只是一个文件）。

---

## 7. 排错

### 7.1 swtpm 起不来：`Could not open UnixIO socket: Permission denied`

Ubuntu 的 AppArmor 给 `/usr/bin/swtpm` 配了 profile，只允许 libvirt 的路径（`/var/lib/libvirt/swtpm/`、`/run/libvirt/qemu/swtpm/`）和 `/tmp`、`$HOME`。我们的状态放在 `/data/vms/win11/tpm` 会被 `mknod` 拒绝，而且**报错很隐蔽**（stderr 无输出，只有 `--log` 指定的文件里一行）。

修复方式（已应用，写在 `/etc/apparmor.d/local/usr.bin.swtpm`）：

```
owner /data/vms/win11/tpm/ rw,
owner /data/vms/win11/tpm/** rwk,
owner /data/vms/win11/logs/swtpm.log rwk,
```

生效：`sudo apparmor_parser -r /etc/apparmor.d/usr.bin.swtpm`。
验证：`pgrep -a swtpm` 有进程、`/data/vms/win11/tpm/swtpm.sock` 是 socket。
若还有 `DENIED`，看 `sudo dmesg | grep apparmor`。

### 7.2 开机掉进 PXE / UEFI Shell，不进安装程序

`Press any key to boot from CD or DVD` 只等约 5 秒，headless 没人按键就会超时掉到网络引导。用 `sudo sh bin/boot-from-cd.sh`（重启 + 20 秒内通过 QMP 连续代按空格）。

### 7.3 OVMF 完全不认光盘

先确认两张光盘挂上了：`info block`（QMP）或 `-device ide-cd,bus=ide.0` 的口位是否被占用。q35 的 AHCI 口位是 `ide.0`–`ide.5`。

### 7.4 RDP 连不上

1. 虚机在跑吗：`systemctl is-active win11`
2. 转发在吗：`ss -ltnp | grep 13389`（QEMU 进程持有）
3. **别用 `nc -vz 127.0.0.1 13389` 判断 RDP** —— 它是假阳性：SLIRP 转发自己就把 TCP 握手做完了，
   客机里 3389 没在听也照样"连接成功"。用协议级探测：`bin/rdp-probe.py`
4. 客机侧：远程桌面是否已开（`bin/check-rdp.py`，`fDenyTSConnections` 必须是 0；GUI 里以为开了其实没开很常见，
   用 `bin/enable-rdp.py` 幂等开启）、防火墙是否放行 3389、账户是否在 Administrators 或 Remote Desktop Users、
   Windows 是否还在引导中
5. 凭据（**这是本案最后卡住的地方**）：用户名填你的 **Microsoft 账户邮箱**——即登录 Windows 用的那个邮箱，密码填该账户的密码。
   `PIN` 不管用（NLA 走 CredSSP）；带前缀的 `MicrosoftAccount\<邮箱>` 在这台机器上会解析失败（`0xC0000064`）。
   首次成功的记录是 `4624 LogonType=10 user=<你的 Microsoft 账户邮箱> src=10.0.2.2 auth=Negotiate`
6. **认证被拒时不要反复试密码**，直接读客机的错误码：`bin/check-rdp-logon.py`
   - `0xC000006A` = 密码错（账户/权限都没问题）
   - `0xC0000064` = 用户不存在 —— 例如 `MicrosoftAccount\<邮箱>` 这种写法在本地账户上解析不到
   - `0xC000015B` = 没被授予远程登录权限（该脚本会同时列出 `SeRemoteInteractiveLogonRight` 成员）
7. 微软账户来源的本地用户，其本地密码是**上次在线验证时的缓存**：如果之后在别的设备改过密码，
   本地 Hello/PIN 还能进，但 RDP 会因为密码不匹配被拒。此时最稳的做法是给 RDP 建一个专用本地账户

### 7.5 关机变成"睡眠"

`system_powerdown`（ACPI 电源键）在现代 Windows 上默认动作可能是睡眠。本方案用 QMP `guest-shutdown`，它依赖 **qemu-guest-agent**（来自 virtio-win 的 guest-tools）。没装 agent 时 `systemctl stop` 会退化成 SIGTERM（等同拔电源）。

### 7.6 TPM 相关报错

- 确认三件套都在：`tpm/` 有状态文件、swtpm 进程在跑、qemu 命令行带 `-chardev/-tpmdev/-device tpm-crb`
- 恢复过快照/备份后出现 TPM 告警 → 把 `tpm/` 一起恢复
- 不要开 BitLocker

### 7.7 macOS 屏幕共享连不上 VNC

典型症状：隧道正常、端口通，但"屏幕共享"报连接失败/不兼容。先分三层定位：

```bash
# ① 宿主侧：VNC 在听吗
ssh yusiwen@192.168.2.48 'ss -ltnp | grep 5901'
# ② 隧道：本地端口通不通
nc -vz 127.0.0.1 5901
# ③ 服务器提供哪种认证
python3 -c "import socket;s=socket.create_connection(('127.0.0.1',5901));print(s.recv(12));s.sendall(b'RFB 003.008\n');print(list(s.recv(64)))"
```

`security types` 现在是 `[1, 2]`（1=None, 2=VNC auth）。**如果只有 `[1]`，macOS 屏幕共享会拒绝连接** ——
它不接受"无认证"的 VNC 服务。这就是最初踩到的坑：`-vnc` 只写了 `127.0.0.1:1`
没加 `password=on` 时服务器只提供 None。修复已固化在 `start-vm.sh`
（`password=on` + QMP `change-vnc-password` 从 `vnc.pass` 取值）。

替代客户端（不想输密码时）：`brew install --cask tigervnc-viewer`。
不要用 RealVNC Viewer —— 现在强制注册账号。

### 7.8 性能远不如物理机

这是 28 W 的移动版 CPU，且同时跑着 k0s + docker + ollama。虚机里的慢先看宿主 `top`/`systemd-cgls`，必要时把规格降到 4 vCPU / 8 GiB。另外没有 GPU 直通，Windows 桌面是纯软件渲染：**用 RDP，不要用 VNC 做日常操作**。

---

### 7.9 `guest-ping` 报 `CommandNotFound` / 关机变成硬断电

两个独立的坑，都会表现成"agent 没装"：

1. **QMP 不代理 guest 命令。** 只有 libvirt 才做那层转发，QEMU 8.2 自带的 QMP 里根本没有 `guest-*` 命令
   （`query-commands` 里 `guest-*` 是空列表）。必须直连 chardev socket：
   `python3 bin/qga.py /data/vms/win11/qga.sock guest-ping`。
2. **qemu-ga 不发问候语。** 客户端连上 `qga.sock` 后如果干等 `{"version":...}` 会一直超时；
   必须先发 `{"execute":"guest-sync","arguments":{"id":N}}`，`qga.py` 已经这么做了。

如果 `qga.sock` 连上也 sync 不通，去客机里看服务：`services.msc` → **QEMU Guest Agent** 是否在运行；
`设备管理器` → 是否有 "VirtIO Serial Driver"。宿主机侧可以确认设备在不在：

```bash
python3 bin/qmp.py qmp.sock query-chardev     # qga0 的 frontend-open 应为 true
```

`frontend-open: true` 说明客机驱动已经打开端口，agent 却在沉默 → 客机侧服务没起来。
新增 QGA 设备后，Windows 需要重启一次才会把驱动和端口接上。

**为什么会变成硬断电：** 最初 `start-vm.sh` 没配 virtio-serial 端口，`ExecStop` 里的 `guest-shutdown`
无处可发，systemd 直接 SIGTERM 掐掉 QEMU —— 客机事件日志留下 Kernel-Power **41**
（"系统未正常关机就断电"）。补上通道后同一操作产生 1074 + 6006，41 不再出现。

### 7.10 客机时间快/慢几小时（UTC 偏移）

`-rtc base=localtime` 只把宿主的墙上时间放进 RTC，Windows 仍按**自己的时区**解释它。装机时
默认时区落成了 `FLE Standard Time (UTC+2)`，结果：显示的钟看着对，**UTC 却快 6 小时**。
证书校验、Microsoft 账户验证、Windows Update 都按 UTC 走 —— 这台机器 OOBE 时 PIN 设置失败
就是这个原因。

```bash
bin/fix-guest-time.py      # Set-TimeZone + QGA guest-set-time（传 epoch 纳秒，与 RTC/时区无关）
```

验证标准：客机 `(Get-Date).ToUniversalTime()` 必须与宿主 `date -u` 一致。只改时区不够，
改完时区后绝对偏差仍在（本例差 6 小时），必须再对一次时。

### 7.11 微软账户的 RDP 一直报"密码错误"（0xC000006A）—— 其实是 Windows Hello 无密码模式

**症状**：账户存在、已启用、在 Administrators 组里、`SeRemoteInteractiveLogonRight` 也有，
但每次 RDP 都是 `0xC000006A`（密码错误），换几种用户名写法都不行；而控制台上用 PIN 能正常进。

**根因**：Windows 11 默认打开「为了提高安全性，仅允许对此设备上的 Microsoft 账户使用 Windows Hello 登录」
（无密码登录）。这个模式下本地 SAM 里没有可用的密码验证器，NLA/CredSSP 拿任何密码去校验都失败；
而且锁屏界面干脆不显示"密码"登录项，所以连"用密码登进去试一次"都做不到。

**修复**（在客机里做，一次性）：

1. 设置 → 账户 → 登录选项 → 其他设置 → 关闭「仅允许对此设备上的 Microsoft 账户使用 Windows Hello 登录」
2. 找不到开关时改注册表：
   `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device`
   的 `DevicePasswordLessBuildVersion` 由 `2` 改为 `0`
3. `Win+L` 锁屏 → 点"登录选项" → 选钥匙图标（密码）→ **用微软账户密码登录一次**
   （这一步把密码验证器写进本地缓存，RDP 才有东西可比对）
4. 之后 RDP 用户名填微软账户邮箱即可

验证：`bin/verify-rdp-works.py` —— 应看到 `DevicePasswordLessBuildVersion = 0` 和
`4624 LogonType=10`（RemoteInteractive）记录。`LogonType=11` 是控制台缓存登录，不能当作 RDP 成功。

### 7.12 Windows 功能更新（23H2 → 25H2）之后要复查什么

功能更新（feature update）会重写大量系统设置，有些是我们刻意关掉的。2026-11-17 从
23H2（`22631.6199`）升到 25H2（`26200.9974`）的实测结果：

| 项 | 更新后 | 处置 |
|---|---|---|
| 版本 / 内部版本 | `25H2` / `26200.9974` | ✅ 正常 |
| `fDenyTSConnections` | `0`（RDP 仍开） | ✅ 没被重置 |
| `DevicePasswordLessBuildVersion` | `0`（Hello 无密码模式仍关着） | ✅ 侥幸保住，但**每次大版本更新都要复查**——它一旦回到 2，微软账户密码登录立刻重新失败（§7.11） |
| **`HiberbootEnabled`（快速启动）** | **`1`（被重新打开）** | ⚠️ 已改回 `0`。开着它客机是半休眠状态，`systemctl stop` 和镜像级快照会对磁盘状态撒谎 |
| 时区 | `China Standard Time`，UTC 与宿主差 7 秒 | ✅ 没变 |
| qemu-ga | 存活，`guest-ping` 通 | ✅ 服务没被卸 |
| C: 剩余 | 45.1 GB | ✅ |
| CBS `RebootPending` | `True`（更新后常见，通常第二次重启才清） | 找个没人用的时间再重启一次 |

**排查用一条命令**：

```bash
/data/vms/win11/bin/check-update-state.py
```

它经 QGA 读版本/启动时间/挂起重启、RDP 开关、Hello 无密码模式、快速启动、时区、
UTC、C 盘剩余，并列出最近 12 小时的 1074/6005/6006/6008/41 事件。

**这次重启的实测耗时**（供判断"卡住了"还是"在装"）：

```
11:10:39  客机记录 1074，MoNotificationUx.exe 请求重启
11:13:42  6006 事件日志停止（Windows 此时才真正下线）
11:13:53  客机启动（UEFI → Windows Boot Manager）
11:15:38  RDP 重新应答 NLA
```

即"离线安装 + 重启"这一段客机完全不可达约 **2 分钟**，加上前面的下线准备总共约 5 分钟。
在此期间 `guest=down / rdp=down` 是正常的，不要急着重启宿主或 QEMU。

## 9. 仓库与还原（git）

这个目录本身就是个 git 仓库：**只固化文档和脚本，虚机数据一概不入库**。

入库：

| 路径 | 说明 |
|---|---|
| `README.md` `bin/README.md` `guacamole/README.md` | 三份文档 |
| `bin/*.py` `bin/*.sh` `bin/*.ps1` | 运维脚本（QMP/QGA/协议探针/取证） |
| `win11.service` `vm.conf` | systemd unit 与启动参数（`BOOT`） |
| `guacamole/docker-compose.yml` `docker-compose.tls.yml` `Caddyfile` `guacamole.properties` `.env.example` | 网页网关的全部定义 |
| `guacamole/bin/*.py` `guacamole/bootstrap.sh` | 网关引导与自检 |
| `guacamole/initdb.sql` | 库表结构。虽然由镜像生成，但它决定“重建后有哪些表”，值得 review 和离线可查；文件头记录来源 tag，`bootstrap.sh` 会校验 tag 与 compose 是否一致 |

不入库：

| 路径 | 原因 |
|---|---|
| `win11.qcow2` | 32 GiB 磁盘镜像，是**数据**不是配置 |
| `tpm/` `ovmf/WIN11_VARS.fd` | 虚机状态（TPM 密钥材料、UEFI 启动项）。**必须备份，但不该进版本库** |
| `vnc.pass` `guacamole/.env` | 密码。模板是 `guacamole/.env.example` |
| `qemu.pid` `*.sock` `logs/` `screen-*.png|ppm` | 运行期产物 |
| `guacamole/db/` | postgres 数据目录（属主 root:70，每次运行都在变） |

**新宿主上的还原顺序**：

```bash
# 1. 装包、建目录、拷 ISO、造 qcow2、装 Windows —— 只能人工，过程见 §3/§5
# 2. systemd
sudo cp win11.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now win11
# 3. swtpm 的 AppArmor 放行（否则 TPM 起不来，见 §7.1）
# 4. 想同时要网页入口：
cd guacamole && ./bootstrap.sh     # 缺 .env 会提示先生成
```

**约定：文档里不写任何密码明文，也不写真实的账户邮箱。** 密码只在 `vnc.pass` 和
`guacamole/.env`（都被 `.gitignore` 排除）；账户一律写成 `<你的 Microsoft 账户邮箱>` 这类
占位符——它能说明"填什么"，又不把人名和登录名一起交出去。症状排查靠字段名和环境变量，不靠贴值。

## 8. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-17 | **删掉两个 23H2 过期快照**（`clean-install`、`post-setup`），保留 `post-26h2`。qcow2 **55 → 32 GiB**，`/data` 168G → **146G**；`qemu-img check` 无错误，重启后 guest-ping / RDP / 网页全部正常。实测结论：内部快照会钉住历史簇，删除即可回收，无需重写镜像 |
| 2026-11-17 | **清理 Windows.old 并回收磁盘**：客机 `C:\Windows.old` 37.61 GB / 390,125 文件（两次功能更新的残留）全部删除，客机已用 70.4 → **40.2 GB**；宿主 qcow2 68.4 → **54.6 GiB**，`/data` 182G → 168G。`rd /s /q` 只清掉 22.1 万文件，余下 16.8 万（TrustedInstaller 属主）靠 takeown+icacls 兜底。TRIM 已开（`DisableDeleteNotify=0`）且 `discard=unmap` 生效。剩余约 14 GB 差值被两个 23H2 内部快照钉住。建 `post-26h2` 快照（ID 3）。新增 `bin/guest-ps.py`（通用 QGA PowerShell 执行器）、`bin/cleanup-windows-old.ps1` |
| 2026-11-17 | **客机再次升级 25H2 → 26H2**（`26200.9974` → `26300.9974`，12:05 重启）。复查全部干净：三个挂起重启标记均为 False（比 25H2 那次干净）、`fDenyTSConnections=0`、`DevicePasswordLessBuildVersion=0`、**`HiberbootEnabled=0`（上次改的快速启动在第二次功能更新中保住了）**、时区与 UTC 正常（差 1 秒）、C: 剩余 48.6 GB。锁屏停在 PIN 输入（控制台登录用，RDP 仍用密码） |
| 2026-11-17 | 网页会话超时从默认 60 分钟改为 **720 分钟**（`guacamole.properties` 的 `api-session-timeout`，挂进 `/etc/guacamole/`），启动日志确认生效。同时查明 11:52 的那次掉线是 **docker 守护进程重启**（apt 升级 docker/containerd 导致），不是超时——容器被 `unless-stopped` 自动拉起但会话全丢 |
| 2026-11-17 | **客机升级 23H2 → 25H2**（`22631.6199` → `26200.9974`），Windows Update 触发重启，RDP 中断约 2 分钟后自行恢复。复查结果：RDP 开关、Hello 无密码设置、时区、qemu-ga 均未被重置；**快速启动被重新打开（`HiberbootEnabled=1`）已改回 0**；CBS 仍标记挂起重启。新增 `bin/check-update-state.py`，新增 §7.12 复查清单 |
| 2026-11-17 | **加网页访问**：Apache Guacamole 1.6.0 三容器（guacd/postgres/guacamole）跑在 docker 上，`8081` 出网页，连接 `win11-rdp` 指向 `host.docker.internal:13389`（RDP/NLA）。虚机零改动。链路用自写的 `guac-probe.py`（Guacamole 协议级）验证到 NLA 认证阶段。踩坑：`umask 077` 连带把 `initdb.sql` 变成 0600 导致 postgres 读不到（库表没建）；guacd 的 `ready` 早于认证结果（假成功）；协议里带参数的是 `connect` 而不是 `select`。文档在 `guacamole/README.md` |
| 2026-11-17 | RDP 转发改为 `0.0.0.0:13389`，局域网可直连 `192.168.2.48:13389`（回环/隧道方式同时保留）。踩坑：把注释写在反斜杠续行中间，续行吞掉注释导致后续参数（`-vnc`/`-qmp`）全部丢失，QEMU 报 `gtk initialization failed` |
| 2026-11-17 | 建 `post-setup` 快照（ID 2，01:07:03）。实测快照自身成本 0.1 GiB 元数据，disk size 27.8 → 27.9 GiB。建完重启验证：services active/enabled、guest-ping 通、RDP 应答 NLA、客机 UTC 与宿主差 1 秒 |
| 2026-11-17 | **RDP 打通**：根因是 Win11 默认的 Hello 无密码模式让本地没有密码验证器（`0xC000006A`）；关闭 `DevicePasswordLessBuildVersion` 并密码登录一次后成功，用户名用微软账户邮箱。新增 `verify-rdp-works.py` 核实 |
| 2026-11-17 | 修客机时区（FLE → China Standard Time）+ QGA `guest-set-time` 对时，UTC 偏差 6 小时归零（这解释了 OOBE 的 PIN 失败）。新增 `check-rdp-logon.py`（按 SubStatus 定位认证失败）、`rdp-probe.py`（协议级 RDP 探测，取代 `nc` 假阳性）、`enable-rdp.py`、`fix-guest-time.py` |
| 2026-11-17 | 补 QGA 通道（virtio-serial），新增 `qga.py`；`ExecStop` 改为「发 guest-shutdown 后等 QEMU 退出」，关机被验证为干净（1074+6006，无 Kernel-Power 41）。`BOOT=c`，建 `clean-install` 快照，`enable` 开机自启 |
| 2026-11-16 | VNC 启用认证（`password=on` + QMP `change-vnc-password`）：原来只提供安全类型 None，macOS 屏幕共享拒连。密码存 `vnc.pass` |
| 2026-11-16 | 首次部署：宿主装包、建 120G qcow2、OVMF/TPM 就位、写开始脚本与 systemd unit、AppArmor 放行、从光盘引导成功（截图确认 Windows 安装程序语言界面） |
