@echo off
chcp 65001 >nul 2>nul
rem ============================================================
rem  DeepSeek Whale-chan - WorkBuddy desktop pet : launcher
rem  用 pythonw.exe 启动，不会弹黑色控制台窗口
rem  优先用本机已有 Pillow/numpy 的 Python 3.9+，找不到再退回 venv
rem ============================================================
cd /d "%~dp0"

set "PYW=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe"
if exist "%PYW%" (
    start "" "%PYW%" "%~dp0src\pet.py"
    exit /b 0
)

if exist "%~dp0tools\venv-tk\Scripts\pythonw.exe" (
    start "" "%~dp0tools\venv-tk\Scripts\pythonw.exe" "%~dp0src\pet.py"
    exit /b 0
)

echo   没找到可用的 Python 运行时。
echo   先双击 setup.bat 建一个虚拟环境，或者把本文件里的 PYW 改成你的 pythonw.exe 路径。
pause
