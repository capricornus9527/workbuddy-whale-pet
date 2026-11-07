# 卸载「跟随 WorkBuddy 启停」：删任务、停看门、关桌宠
$ErrorActionPreference = 'SilentlyContinue'

$Root = Split-Path -Parent $PSScriptRoot
$TaskName = 'WorkBuddy Pet Whale'
$PidFile = Join-Path $Root 'pet.pid'

$task = Get-ScheduledTask -TaskName $TaskName
if ($task) {
    Stop-ScheduledTask -TaskName $TaskName
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host '已删除计划任务'
} else {
    Write-Host '没有找到计划任务（可能本来就没装）'
}

# 停看门进程：命令行里带 follow.ps1 的 powershell
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*follow.ps1*' } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force
        Write-Host ('已停看门进程 ' + $_.ProcessId)
    }

if (Test-Path -LiteralPath $PidFile) {
    $p = Get-Content -LiteralPath $PidFile -TotalCount 1
    if ($p -match '^\d+$') {
        Stop-Process -Id ([int]$p) -Force
        Write-Host '已关闭桌宠'
    }
    Remove-Item -LiteralPath $PidFile -Force
}

Write-Host '完成。以后需要恢复，双击 install-follow.bat。'
