# 排障：会话与按键

[English](TROUBLESHOOTING.md) | **中文**

三个故障，表面上都像"网关坏了"，实际都不在网关里。它们有同一个形状，值得记住：
**Guacamole 忠实地转发它拿到的东西——所以当一次按键或一个会话出问题，毛病几乎
从来不在 Guacamole 里，而在它某一侧的状态里。**

下面每一条都是在这套部署上复现出来的（nuc12、Windows 11 26H2、Guacamole 1.6.0、
macOS 客户端用 Edge），结论来自日志和客机内的观测，不是从症状猜的。

| 症状 | 出问题的层 | 一句话修法 |
|---|---|---|
| 切到别的窗口几分钟后会话掉线 | 浏览器后台节流 + guacd 的 15 秒超时 | 别让标签页被节流 |
| 每次重连都要输入 Windows 密码 | 连接里没存密码 | 存一次 |
| `Ctrl+C` 打开开发者工具而不是复制 | Caps Lock 状态不一致 → guacd 补发 Shift | 让两侧 caps 状态一致后重连 |

## 1. 切走一会儿，会话就断了

**现象.** 切到别的应用或标签页一小会儿，回来时桌面没了——通常还伴随一个密码框
（见第 2 条）。

**证据.** `guacd` 只用一行就说了：

```
guacd[186]: INFO:	User is not responding.
guacd[186]: INFO:	User "@33c66a65..." disconnected (0 users remain)
```

而同一时刻稍早，Tomcat 那边是：

```
o.a.g.w.GuacamoleWebSocketTunnelEndpoint - Connection to guacd terminated abnormally
java.lang.IllegalStateException: Message will not be sent because the WebSocket session ...
```

**原因——两层，其中只有一层可调.**

- guacd 只给客户端 **15 秒** 的时间说点什么。这个上限是编译期常量：
  `src/guacd/proc.h` 里的 `#define GUACD_TIMEOUT 15000`。**没有**任何配置项、环境
  变量或连接参数能改它。
- 客户端靠每 **5 秒** 一个 Guacamole `nop` 维持会话。而 Chromium 的窗口一旦被
  **最小化或被完全遮挡**，定时器就被节流：那条 5 秒心跳在 Edge 里退化成约 60 秒、
  Chrome 里约 37 秒。15 秒 < 37 秒，于是 guacd 在下一个 `nop` 到达之前就判定用户
  失联。

还有一个次要效应让事情看起来更糟：每次重连都是一个**新的 RDP 会话**，而 Windows
只允许一个，新会话会把旧的踢掉——

```
guacd[186]: INFO:	RDP server closed/refused connection: Upstream error.
```

**修法.**

1. `edge://settings/system` → 关掉**睡眠标签页**（Sleeping tabs），或把网关主机加进
   "永不让这些站点进入睡眠"；**效率模式**设为"均衡"或关闭。
2. 不要最小化窗口，也不要让别的窗口把它完全盖住。并排就够——遮挡检测只在窗口完全
   不可见时才触发。
3. 可选的双保险：带禁用节流的参数启动浏览器。这些参数只对新进程生效，所以先
   完全退出浏览器。

```bash
open -a "Microsoft Edge" --args \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding
```

服务端只有一种真解法：重新编译 `guacamole-server`，把 `GUACD_TIMEOUT` 提到约 65 秒。
代价是每次升级都要维护一个打过补丁的 `guacd` 镜像——只有客户端那几条办法不够用时
才值得。

## 2. 每次重连都要求输入 Windows 密码

**现象.** 黑屏上浮出一条深色横条，写着 `Password:` 和 *Continue / Cancel*。这**不是**
Guacamole 的登录页，**也不是** Authelia 的——在 header 认证下登录页根本不会出现。

**原因.** 连接里存了 `username` 却没有 `password`，于是每个新的 guacd 会话都要向
客户端要一次凭据：

```
$ docker exec win11-guac-db psql -U guacamole_user -d guacamole_db -tAc \
    "select parameter_name from guacamole_connection_parameter where connection_id=1 order by 1"
color-depth|create-drive-path|drive-name|enable-desktop-composition|enable-drive|
enable-font-smoothing|enable-wallpaper|hostname|ignore-cert|port|resize-method|
security|username
```

没有 `password` 这一行 → guacd 发出 `required` 凭据请求 → 就是那个对话框。

**修法.** 存一次即可。界面里：**Settings → Connections → 点连接名本身**（那一行就是
链接；左边的 `+` 是展开子连接）**→ Password → Save**。之后掉线重连不用再输入任何东西。

代价是密码会留在 `guacamole_db` 这个 Postgres 卷里，它已被 gitignore，且只能从
compose 网络访问。这跟连接里本来就存着 `username` 是同一级别的信任；如果不能接受，
唯一的选择就是每次掉线后手工输一次（那就用第 1 条把掉线频率降下来）。

## 3. `Ctrl+C` 打开开发者工具，复制失效

**现象.** 在客机**里面**的浏览器里——Chrome 和 Edge 都一样——按 `Ctrl+C` 弹出开发者
工具，什么都没复制。记事本看起来却正常。它时好时坏，而且重连之后依然存在。

**为什么记事本看起来正常.** 客机装着中文输入法（布局 `0804`）。输入法把 Shift 当作
中/英切换吞掉，所以即便 Shift 在逻辑上是按下的，打出的字仍是小写。而 Chromium 匹配
快捷键**发生在输入法之前**，它看到的仍然是 `Ctrl+Shift+C`。

**证据.** 在交互式会话里跑一个按键观察器（见第 4 条），能看到 Shift 与 C 同时到达；
而决定性的细节是——**每一个字符前后都伴随一次独立的 Shift 按下/松开**：

```
16:45:45.147  LSHIFT          <- 逐字符脉冲，不是卡住的键
16:45:45.194  (none)
16:45:45.915  C+CTRL+LCTRL+LSHIFT
16:45:46.360  LSHIFT
16:45:46.455  (none)
16:45:46.645  C+LSHIFT
```

*卡住*的 Shift 会一直处于按下状态；而**逐键脉冲**说明有东西在刻意按 Shift 来强制
字符的大小写。

**原因.** 两端的 Caps Lock 状态不一致。`guacamole-common-js` 按客户端看到的样子转发
字符——`c` 是 keysym `0x63`，`C` 是 `0x43`。当客机的 caps 状态与这个 keysym 矛盾时，
guacd 就按一下 Shift 来凑出所要求的大小写。两个状态、一个键盘：此后每个字符都带着
Shift 到达，`Ctrl+C` 就成了 `Ctrl+Shift+C`。

客户端这一半可以直接验证（一个 Chromium 按键事件就够）：

| 客户端发出 | keysym | 客机 caps 关闭时，guacd 必须 |
|---|---|---|
| `key="c"` | `0x63` | 只发 `c` |
| `key="C"` | `0x43` | 发 `Shift+c` |

**五秒钟判据.** 在客机里把输入法切到 **ENG**（任务栏显示 `ENG`），敲 `abc`：

- 出 `abc` → 干净，没有被补 Shift
- 出 `ABC` 或 `Abc` → 键盘状态错位，继续往下看

直接查两边的状态：

```powershell
# 客机内，以登录用户身份（见第 4 条）
[Console]::CapsLock        # True / False
```
```bash
# macOS 客户端上
python3 - <<'PY'
import ctypes, ctypes.util
cg = ctypes.CDLL(ctypes.util.find_library('CoreGraphics'))
cg.CGEventSourceKeyState.restype  = ctypes.c_bool
cg.CGEventSourceKeyState.argtypes = [ctypes.c_int32, ctypes.c_ushort]
print("Caps Lock:", cg.CGEventSourceKeyState(0, 57))   # 57 = Caps Lock 键码
PY
```

**修法.**

1. 在**客机里**按一次 **Caps Lock**，让它的状态与客户端一致。
2. **重连。** 新的 guacd 会话会重读客机的锁定键状态；那个错误的假设活不过重连。
3. 如果又出现，就在连上之后立刻按**两下** Caps Lock（开→关），不用重连也能把两侧
   重新对齐。

**完全不碰键盘的绕过办法.** 客机里右键 → *Copy*；或者用 Guacamole 的剪贴板面板
（`Ctrl+Alt+Shift` 唤出菜单）。

另外值得一提：Guacamole **不会**把 `Cmd` 翻译成 `Ctrl`（这是有意为之，见
GUACAMOLE-1858）。在 macOS 上要在客机里复制，必须按真正的 **Control** 键；
`Command+C` 到客机里是 `Win+C`。

## 4. 看清客机到底收到了什么

QGA 以 **SYSTEM 身份跑在 session 0**，所以经 `guest-ps.py` 启动的任何东西都看不到
交互式会话的键盘（`GetAsyncKeyState` 恒为空）——`qemu` 的 screendump 也一样没用：
`bin/screen.py` 抓的是**控制台**会话，对一台走 RDP 的客机来说那就是锁屏画面，不是
RDP 用户看到的桌面。

要观测真实输入，得把探针跑在**交互式会话**里——注册一个交互式主体的计划任务即可
（不需要密码）：

```powershell
# 以 SYSTEM 身份，通过 guest-ps.py 执行
$sid = (Get-CimInstance Win32_UserAccount | Where-Object { $_.LocalAccount }).SID   # 登录中的用户
$act = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\keyprobe.ps1'
$prn = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName keyprobe -Action $act -Principal $prn -Force
Start-ScheduledTask keyprobe
```

`C:\keyprobe.ps1` 轮询 `GetAsyncKeyState`，只在状态变化时记录，足够看清修饰键状态
以及它相对于按键的先后：

```powershell
$sig = '[DllImport("user32.dll")] public static extern short GetAsyncKeyState(int vKey);'
$K = Add-Type -MemberDefinition $sig -Name U -Namespace W -PassThru
$log = 'C:\Users\<you>\keyprobe.log'
$vks = @{ 0x11='CTRL'; 0xA0='LSHIFT'; 0xA1='RSHIFT'; 0x12='ALT'; 0x5B='LWIN'; 0x14='CAPS'; 0x43='C' }
$prev = ''
while ($true) {
    $held = @(); foreach ($vk in $vks.Keys) { if ($K::GetAsyncKeyState($vk) -band 0x8000) { $held += $vks[$vk] } }
    $cur = (@($held | Sort-Object) -join '+'); if (-not $cur) { $cur = '(none)' }
    if ($cur -ne $prev) { ('{0}`t{1}' -f (Get-Date -Format 'HH:mm:ss.fff'), $cur) | Add-Content $log; $prev = $cur }
    Start-Sleep -Milliseconds 25
}
```

两个坑，都是写这一段时踩到的：

- 这个任务虽然以用户身份运行，却**继承了 SYSTEM 的环境**：`$env:TEMP` 会解析成
  `C:\WINDOWS\TEMP`，`$env:USERPROFILE` 会指到系统配置目录。日志必须写用户配置目录
  下的绝对路径。
- 非提权进程**不能**在 `C:\` 根目录建文件。写在那里会静默失败，日志就是永远不出现。

要往客机里塞脚本又不想建共享目录，从宿主发出去、让客机经 SLIRP 网关取即可
（`10.0.2.2` 就是宿主）：

```bash
# 宿主
python3 -m http.server 8899 --bind 0.0.0.0 --directory /tmp &
# 客机
Invoke-WebRequest http://10.0.2.2:8899/probe.ps1 -OutFile C:\probe.ps1 -UseBasicParsing
```

## 5. 看起来像故障、其实不是的

- **浏览器里剪贴板失效.** 不是 Guacamole 的设置问题：在纯 HTTP 下页面不是安全上下文，
  `navigator.clipboard` 根本不存在，复制/粘贴静默失效。这件事由浏览器实际对话的源
  决定——见 [guacamole/README-zh_cn.md](guacamole/README-zh_cn.md) §7。
- **把短时间掉线算到 `api-session-timeout` 头上.** 它的单位是**分钟**（720 = 12 小时），
  启动时会打印 `Sessions will expire after 720 minutes`。几分钟就掉线属于第 1 条，
  不是它。
- **把密码框算到 header 认证头上.** 开了 `http-auth-header` 之后 Guacamole 的登录页
  根本不会渲染。一个 `Password:` 横条是 guacd 在向**连接**要凭据——第 2 条。
