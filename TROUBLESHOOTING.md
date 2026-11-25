# Troubleshooting: sessions and keystrokes

**English** | [中文](TROUBLESHOOTING-zh_cn.md)

Three faults that all look like "the gateway is broken" and all turned out to be
somewhere else. They share a shape worth remembering: **Guacamole faithfully
forwards what it is given, so when a keystroke or a session misbehaves the fault
is almost never in Guacamole — it is in the state on one side of it.**

Everything below was reproduced on this deployment (nuc12, Windows 11 26H2,
Guacamole 1.6.0, macOS client on Edge) and verified against logs and guest-side
instrumentation, not guessed from symptoms.

| Symptom | Layer | One-line fix |
|---|---|---|
| Session drops minutes after you switch away | browser tab throttling + guacd's 15 s timeout | stop the tab from being throttled |
| Every reconnect asks for the Windows password | connection has no stored password | store it once |
| `Ctrl+C` opens DevTools instead of copying | Caps Lock state mismatch → guacd adds Shift | make the two caps states agree, reconnect |
| A screenshot will not paste into the guest | RDP clipboard is text-only; the drive had no `drive-path` | send it as a file over drive redirection |

## 1. The session dies minutes after the browser loses focus

**Symptom.** You switch to another app or tab for a short while, come back, and the
desktop is gone — usually with a password prompt (see #2).

**Evidence.** `guacd` says it in one line:

```
guacd[186]: INFO:	User is not responding.
guacd[186]: INFO:	User "@33c66a65..." disconnected (0 users remain)
```

and Tomcat, moments earlier, on the same session:

```
o.a.g.w.GuacamoleWebSocketTunnelEndpoint - Connection to guacd terminated abnormally
java.lang.IllegalStateException: Message will not be sent because the WebSocket session ...
```

**Cause — two layers, and only one of them is tunable.**

- guacd gives the *client* 15 seconds to say anything. That limit is a compile-time
  constant, `#define GUACD_TIMEOUT 15000` in `src/guacd/proc.h`. There is **no**
  configuration option, environment variable or connection parameter for it.
- The client keeps the session alive with a Guacamole `nop` every 5 seconds. When a
  Chromium window is **minimised or fully occluded**, its timers are throttled, and
  that keepalive degrades to roughly 60 seconds in Edge and 37 seconds in Chrome.
  15 s < 37 s, so guacd declares the user gone before the next `nop` can arrive.

A second effect makes it look worse than it is: every reconnect is a **new RDP
session**, and Windows allows one, so the new session kicks the old one —

```
guacd[186]: INFO:	RDP server closed/refused connection: Upstream error.
```

**Fix.**

1. `edge://settings/system` → turn **Sleeping tabs** off, or add the gateway host to
   "Never put these sites to sleep"; set **Efficiency mode** to Balanced or off.
2. Do not minimise the window and do not let another window cover it completely.
   Side-by-side is enough — the occlusion check only fires when the window is
   invisible.
3. Optional belt and braces: launch the browser with throttling disabled. The flags
   only apply to a fresh process, so quit the browser first.

```bash
open -a "Microsoft Edge" --args \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding
```

Server-side there is only one real cure: rebuild `guacamole-server` with
`GUACD_TIMEOUT` raised to ~65 s. That means maintaining a patched `guacd` image
across upgrades — worth it only if the client-side workarounds are not enough.

## 2. Every reconnect asks for the Windows password

**Symptom.** A dark bar with `Password:` and *Continue / Cancel* over an otherwise
black screen. This is **not** the Guacamole login page and **not** Authelia — with
header authentication the login page never appears at all.

**Cause.** The connection stores `username` but no `password`, so every new guacd
session has to ask the client for credentials:

```
$ docker exec win11-guac-db psql -U guacamole_user -d guacamole_db -tAc \
    "select parameter_name from guacamole_connection_parameter where connection_id=1 order by 1"
color-depth|create-drive-path|drive-name|enable-desktop-composition|enable-drive|
enable-font-smoothing|enable-wallpaper|hostname|ignore-cert|port|resize-method|
security|username
```

No `password` row → guacd sends a `required` credential request → that dialog.

**Fix.** Store it once. In the UI: **Settings → Connections → click the connection
name** (the row itself is the link; the `+` expands child connections) **→ Password →
Save**. From then on a dropped session reconnects without typing anything.

Storing it keeps the password in the `guacamole_db` Postgres volume, which is
gitignored and reachable only from the compose network. That is the same trust
already placed in the connection's `username`; if it is unacceptable, the only
alternative is a password prompt after every drop (see #1 to reduce the drops).

## 3. `Ctrl+C` opens DevTools instead of copying

**Symptom.** In a browser *inside* the guest — Chrome and Edge both — `Ctrl+C`
opens the developer tools and nothing is copied. Notepad appears unaffected. It is
intermittent, and it survives reconnects.

**Why Notepad looks fine.** The guest runs a Chinese IME (layout `0804`). The IME
consumes Shift as its 中/英 toggle, so typed text stays lowercase even while Shift
is logically down. Chromium matches keyboard accelerators **before** the IME sees
the key, so it still sees `Ctrl+Shift+C`.

**Evidence.** A key observer inside the interactive session (see #4) shows Shift
arriving with the C, and — the decisive detail — a separate Shift press/release
**around every single character**:

```
16:45:45.147  LSHIFT          <- per-character pulse, not a stuck key
16:45:45.194  (none)
16:45:45.915  C+CTRL+LCTRL+LSHIFT
16:45:46.360  LSHIFT
16:45:46.455  (none)
16:45:46.645  C+LSHIFT
```

A *stuck* Shift would be down continuously; a *per-key* pulse means something is
deliberately pressing Shift to force the intended character case.

**Cause.** Caps Lock state disagreement between the two ends. `guacamole-common-js`
forwards the character as the client sees it — `c` becomes keysym `0x63`, `C`
becomes `0x43`. When the guest's caps state contradicts the keysym, guacd presses
Shift to produce the requested case. Two states, one keyboard: every character then
arrives shifted, and `Ctrl+C` becomes `Ctrl+Shift+C`.

Confirm the client half of it (a Chromium key event is enough):

| Client sends | keysym | guacd must, to keep the case |
|---|---|---|
| `key="c"` | `0x63` | send `c` alone if the guest's caps is off |
| `key="C"` | `0x43` | send `Shift+c` if the guest's caps is off |

**Diagnose in five seconds.** In the guest, switch the IME to **ENG** (taskbar shows
`ENG`) and type `abc`:

- `abc` → clean, the Shift is not being added
- `ABC` or `Abc` → the keyboard state is mismatched, keep reading

Check the two states directly:

```powershell
# inside the guest, as the logged-in user (see #4)
[Console]::CapsLock        # True / False
```
```bash
# on the macOS client
python3 - <<'PY'
import ctypes, ctypes.util
cg = ctypes.CDLL(ctypes.util.find_library('CoreGraphics'))
cg.CGEventSourceKeyState.restype  = ctypes.c_bool
cg.CGEventSourceKeyState.argtypes = [ctypes.c_int32, ctypes.c_ushort]
print("Caps Lock:", cg.CGEventSourceKeyState(0, 57))   # 57 = Caps Lock keycode
PY
```

**Fix.**

1. Press **Caps Lock in the guest** so its state matches the client's.
2. **Reconnect.** A new guacd session re-reads the guest's lock state; the stale
   assumption does not survive it.
3. If it comes back, tap Caps Lock twice (on, then off) right after connecting, which
   resyncs both sides without another reconnect.

**Workarounds that do not involve the keyboard at all.** Right-click → *Copy* inside
the guest, or use Guacamole's clipboard panel (`Ctrl+Alt+Shift` opens the menu).

Also worth knowing: Guacamole does not translate `Cmd` to `Ctrl` (by design — see
GUACAMOLE-1858). On macOS you must press the real **Control** key to copy inside the
guest; `Command+C` arrives as `Win+C`.

## 4. Seeing what the guest actually receives

QGA runs as **SYSTEM in session 0**, so nothing started through `guest-ps.py` can see
the interactive session's keyboard (`GetAsyncKeyState` returns idle) — and `qemu`'s
screendump is no better: `bin/screen.py` captures the **console** session, which for
an RDP guest is the lock screen, not what the RDP user sees.

To observe the real thing, run the probe **in the interactive session** by
registering a scheduled task with an interactive principal (no password needed):

```powershell
# as SYSTEM, via guest-ps.py
$sid = (Get-CimInstance Win32_UserAccount | Where-Object { $_.LocalAccount }).SID   # the logged-in user
$act = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\keyprobe.ps1'
$prn = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName keyprobe -Action $act -Principal $prn -Force
Start-ScheduledTask keyprobe
```

`C:\keyprobe.ps1` polls `GetAsyncKeyState` and logs only changes, which is enough to
see modifier state and its ordering relative to a key:

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

Two traps, both hit while writing this:

- The task inherits the **SYSTEM** environment even though it runs as the user, so
  `$env:TEMP` resolves to `C:\WINDOWS\TEMP` and `$env:USERPROFILE` to the system
  profile. Write to an absolute path under the user's profile.
- A non-elevated process cannot create files in `C:\`. Writing there fails silently
  and the log simply never appears.

To push a script into the guest without a shared folder, serve it from the host and
let the guest fetch it over the SLIRP gateway (`10.0.2.2` is the host):

```bash
# host
python3 -m http.server 8899 --bind 0.0.0.0 --directory /tmp &
# guest
Invoke-WebRequest http://10.0.2.2:8899/probe.ps1 -OutFile C:\probe.ps1 -UseBasicParsing
```

## 5. A screenshot cannot be pasted into the guest

**Symptom.** A screenshot tool on the client copies an image to the local clipboard and
the guest never receives it — paste does nothing. Clicking through the tool's overlay
also opens the guest's Start menu.

**Two unrelated causes.**

**5.1 The clipboard carries text only.** Guacamole's clipboard is a text stream, and
guacd's RDP implementation handles `CF_UNICODETEXT` — no bitmap format. An image copied
on the client therefore has no path into the guest, no matter which tool produced it or
how many clipboard parameters are set. Do not hunt for a switch: there is none.

Send the screenshot as a **file** instead, over RDP drive redirection.

- `enable-drive` alone is not enough. **`drive-path` is required** and must name a
  directory *inside the guacd container*; this deployment shipped without it, so the
  drive existed only as a parameter. Nothing reports the omission — and it cannot be
  checked from outside the session either: queried from session 0 (over QGA),
  `\\tsclient` answers **Access denied** whether or not the drive exists, because the
  RDP redirector is per-session. Verify from inside the connected session.
- Wire it end to end: compose mounts `./drive:/drive` into guacd, the connection sets
  `drive-path=/drive`, and the host directory must be writable by **uid 1000** (guacd's
  user in the image).
- Use it: the Guacamole menu (`Ctrl+Alt+Shift`) has a file panel — upload from the
  client, then open the file in the guest from `\\tsclient\\<drive-name>`.

**5.2 `Command` is forwarded as the Windows key.** The client sends the *identity* of
the key, so a client-side hotkey built on `Cmd` arrives in the guest as `Win`. Double-
tapping `Cmd` to trigger a screenshot tool is therefore two `Win` presses in the guest,
and Windows opens the Start menu. While the tool's overlay has focus Guacamole also
stops receiving keyups, so a modifier can stay logically down — the same class of fault
as #3.

Fix it on the client: give the tool a hotkey that is not a Windows shortcut
(`Ctrl+Option+…` rather than `Cmd`). There is no Guacamole-side switch to suppress
`Meta`, because suppressing it would break real `Win` shortcuts.

## 6. What is *not* a fault

- **Clipboard dead in the browser.** Not a Guacamole setting: over plain HTTP the page
  is not a secure context, `navigator.clipboard` does not exist, and copy/paste fails
  silently. The origin the browser talks to decides this — see
  [guacamole/README.md](guacamole/README.md) §7.
- **`api-session-timeout` blamed for a short drop.** It is measured in **minutes**
  (720 = 12 h) and logs `Sessions will expire after 720 minutes` at startup. A drop
  after a few minutes is #1, not this.
- **Header authentication blamed for the password dialog.** With
  `http-auth-header` the Guacamole login never renders. A `Password:` bar is guacd
  asking the *connection* for credentials — #2.
