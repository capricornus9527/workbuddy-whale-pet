# 安装「跟随 WorkBuddy 启停」：注册计划任务 + 立刻跑一次看门
# 与 Codex 的 API 余额小鲸娘同一套办法，任务名不同，互不影响。

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Follow = Join-Path $Root 'tools\follow.ps1'
$TaskName = 'WorkBuddy Pet Whale'
$WinPS = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'

if (-not (Test-Path -LiteralPath $Follow)) { throw "找不到 $Follow" }
if (-not (Test-Path -LiteralPath (Join-Path $Root 'src\pet.py'))) { throw '项目结构不完整：缺 src\pet.py' }

# 已有旧任务先摘掉，避免路径残留指向旧位置
$old = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($old) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }

# 用 conhost --headless 包一层启动。
# 原因：powershell.exe 是控制台程序，由任务计划直接拉起时，Windows 会先创建
# 一个 conhost 控制台窗口再让 -WindowStyle Hidden 生效，结果窗口一直留在桌面上。
# --headless 让 conhost 压根不创建窗口，是彻底的静默方式。
# （VBScript 在新版 Windows 上已改为按需功能，wscript 包装方案不可用）
$Conhost = Join-Path $env:WINDIR 'System32\conhost.exe'
$action = New-ScheduledTaskAction -Execute $Conhost `
    -Argument ('--headless "' + $WinPS + '" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $Follow + '"') `
    -WorkingDirectory $Root

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -DontStopOnIdleEnd

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description 'WorkBuddy 桌宠：跟随 WorkBuddy 启动与退出' -Force | Out-Null

# 立刻启动一次，不必等下次登录
Start-ScheduledTask -TaskName $TaskName

Start-Sleep -Seconds 4
$info = Get-ScheduledTask -TaskName $TaskName
$run = Get-ScheduledTaskInfo -TaskName $TaskName
$petAlive = $false
$pidFile = Join-Path $Root 'pet.pid'
if (Test-Path -LiteralPath $pidFile) {
    $p = (Get-Content -LiteralPath $pidFile -TotalCount 1)
    if ($p -match '^\d+$') { $petAlive = [bool](Get-Process -Id ([int]$p) -ErrorAction SilentlyContinue) }
}

Write-Host ''
Write-Host ('任务名     : ' + $TaskName)
Write-Host ('状态       : ' + $info.State)
Write-Host ('上次结果   : ' + $run.LastTaskResult)
Write-Host ('触发条件   : 登录时启动（' + $env:USERNAME + '）')
Write-Host ('看门脚本   : ' + $Follow)
Write-Host ('桌宠在运行 : ' + $petAlive)
Write-Host ''
if ($petAlive) {
    Write-Host '已生效：WorkBuddy 开着它就在，WorkBuddy 关掉它会自己退出。'
} else {
    Write-Host '任务已注册，但桌宠还没起来。若 WorkBuddy 当前没开，这是正常的；'
    Write-Host '打开 WorkBuddy 后 3 秒内应出现。排查看 follow.log。'
}
