@echo off
chcp 65001 >nul 2>nul
rem 卸载「跟随 WorkBuddy 启动/退出」：删计划任务、停看门、关桌宠
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\uninstall-follow.ps1"
echo.
pause
