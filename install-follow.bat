@echo off
chcp 65001 >nul 2>nul
rem 安装「跟随 WorkBuddy 启动/退出」：注册登录时的计划任务，并立刻跑一次
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install-follow.ps1"
echo.
pause
