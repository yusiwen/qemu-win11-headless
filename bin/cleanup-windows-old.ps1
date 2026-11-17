# Remove C:\Windows.old left behind by the 25H2 and 26H2 feature updates, then
# TRIM the volume so the qcow2 on the host actually shrinks (sparse file: the
# guest deleting data does nothing to the host unless TRIM is issued).
#
# Runs detached: 39 万文件要十几分钟，所以进度写进日志，外面轮询日志即可。
$ProgressPreference = 'SilentlyContinue'
$log = 'C:\Windows\Temp\old-cleanup.log'

function W($m) {
    "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m | Out-File $log -Append -Encoding utf8
}
Remove-Item $log -Force -ErrorAction SilentlyContinue

$free0 = [math]::Round((Get-PSDrive C).Free / 1GB, 2)
W "start  whoami=$(whoami)  free=$free0 GB  exists=$(Test-Path C:\Windows.old)"

# `rd /s /q` is much faster than Remove-Item, but it is not sufficient on its
# own: measured here, rd cleared 221k of 390k files and left 168k behind, owned
# by TrustedInstaller. So the takeown+icacls fallback below is required, not
# optional - budget roughly half the total time for it.
cmd /c rd /s /q C:\Windows.old 2>&1 | Out-Null
$left = (Get-ChildItem C:\Windows.old -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object).Count
W "rd done  leftovers=$left  free=$([math]::Round((Get-PSDrive C).Free / 1GB, 2)) GB"

if (Test-Path C:\Windows.old) {
    W "fallback: takeown + icacls + Remove-Item"
    takeown /F C:\Windows.old /R /A /D Y 2>&1 | Out-Null
    icacls C:\Windows.old /grant '*S-1-5-32-544:F' /T /C /Q 2>&1 | Out-Null
    Remove-Item C:\Windows.old -Recurse -Force -ErrorAction SilentlyContinue
    W "fallback done  exists=$(Test-Path C:\Windows.old)  free=$([math]::Round((Get-PSDrive C).Free / 1GB, 2)) GB"
}

W 'retrim start'
Optimize-Volume -DriveLetter C -ReTrim -ErrorAction SilentlyContinue | Out-Null
W "retrim done  free=$([math]::Round((Get-PSDrive C).Free / 1GB, 2)) GB  used=$([math]::Round((Get-PSDrive C).Used / 1GB, 2)) GB"
