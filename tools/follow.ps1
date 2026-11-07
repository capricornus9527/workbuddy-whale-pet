# WorkBuddy 桌宠看门脚本
# 跟随 WorkBuddy 启动/退出：宿主在就确保桌宠在跑，宿主没了就把桌宠关掉。
# 做法参考 Codex 的「API 余额小鲸娘」——常驻进程 + 计划任务，轮询宿主进程。
# 单独运行便于排障：
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\follow.ps1 -Once

param(
    [switch]$Once,
    [int]$IntervalSeconds = 3
)

$ErrorActionPreference = 'SilentlyContinue'

$Root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $Root 'src\pet.py'))) {
    throw '未找到 src\pet.py：请把本脚本放在项目的 tools 目录下'
}
$PidFile = Join-Path $Root 'pet.pid'
$PetScript = Join-Path $Root 'src\pet.py'
$LogFile = Join-Path $Root 'follow.log'

function Write-Log($msg) {
    try {
        $line = '{0} {1}' -f (Get-Date -Format 'MM-dd HH:mm:ss'), $msg
        Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
        # 日志只留最近 200 行，避免无限增长
        $all = Get-Content -LiteralPath $LogFile -ErrorAction SilentlyContinue
        if ($all.Count -gt 200) {
            $all | Select-Object -Last 200 | Set-Content -LiteralPath $LogFile -Encoding UTF8
        }
    } catch { }
}

function Resolve-Pythonw {
    $candidates = @(
        (Join-Path $Root 'tools\venv-tk\Scripts\pythonw.exe'),
        (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe')
    )
    foreach ($p in $candidates) {
        if ($p -and (Test-Path -LiteralPath $p)) { return $p }
    }
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Get-PetPid {
    if (-not (Test-Path -LiteralPath $PidFile)) { return 0 }
    $raw = (Get-Content -LiteralPath $PidFile -TotalCount 1 -ErrorAction SilentlyContinue)
    if (-not ($raw -match '^\d+$')) { return 0 }
    $procId = [int]$raw
    $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($p -and ($p.ProcessName -match '^python')) { return $procId }
    return 0
}

function Test-WorkBuddy {
    # 只认主进程。WorkBuddy 会留下一堆子进程：gpu-process、renderer、
    # utility、crashpad-handler，还有 sidecar / daemon 的 .js 入口和 cli 预热进程。
    # 它们退出得比主进程慢，甚至常驻，拿「有没有 WorkBuddy.exe」当判据会一直为真，
    # 桌宠就永远等不到该退出的那一刻。
    $list = Get-CimInstance Win32_Process -Filter "Name='WorkBuddy.exe'" -ErrorAction SilentlyContinue
    foreach ($p in $list) {
        $c = $p.CommandLine
        if (-not $c) { continue }
        if ($c -match '--type=') { continue }        # GPU / renderer / utility / crashpad
        if ($c -match '\.js') { continue }           # sidecar、daemon-app-server 等 Node 入口
        if ($c -match '--prewarm|codebuddy') { continue }  # cli 预热进程
        return $true
    }
    return $false
}

# ---- 单实例：已有看门在跑就直接退出
# 只用 Mutex。不要用「扫描命令行里有没有 follow.ps1」的做法——
# 那种写法会匹配到它自己的命令行文本，导致看门刚启动就退出。
$created = $false
$mutex = New-Object System.Threading.Mutex($true, 'Global\WorkBuddyPetFollow', [ref]$created)
if (-not $created) {
    Write-Log '已有看门进程在运行，退出'
    return
}

$logShown = $false
$lastLaunch = [DateTime]::MinValue
if (-not $Once) { Write-Log '看门启动' }

while ($true) {
    $hostAlive = Test-WorkBuddy
    $petPid = Get-PetPid

    if ($hostAlive) {
        # 桌宠启动到写完 pet.pid 有一两秒空窗，不加节流会连着拉起两次
        if ($petPid -eq 0 -and ((Get-Date) - $lastLaunch).TotalSeconds -gt 12) {
            $py = Resolve-Pythonw
            if ($py) {
                if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile -Force }
                Start-Process -FilePath $py -ArgumentList ('"' + $PetScript + '"') -WorkingDirectory $Root | Out-Null
                $lastLaunch = Get-Date
                Write-Log "拉起桌面宠物 ($py)"
            } elseif (-not $logShown) {
                Write-Log '找不到 pythonw.exe，无法启动桌宠'
                $logShown = $true
            }
        }
    } else {
        if ($petPid -ne 0) {
            Stop-Process -Id $petPid -Force -ErrorAction SilentlyContinue
            if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile -Force }
            Write-Log 'WorkBuddy 已退出，关闭桌宠'
        }
    }

    if ($Once) {
        $mutex.ReleaseMutex()
        "hostAlive=$hostAlive petPid=$petPid pythonw=$(Resolve-Pythonw)"
        return
    }
    Start-Sleep -Seconds $IntervalSeconds
}
