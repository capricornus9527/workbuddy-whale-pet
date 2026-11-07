@echo off
chcp 65001 >nul 2>nul
rem 跑全套自检（含端到端：会真的把桌宠拉起来导出渲染帧验证，约 15 秒）
cd /d "%~dp0"
set "PY=%~dp0tools\venv-tk\Scripts\python.exe"
if not exist "%PY%" set "PY=python.exe"
"%PY%" "%~dp0src\selftest.py" --e2e
pause
