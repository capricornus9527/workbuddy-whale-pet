@echo off
chcp 65001 >nul 2>nul
rem ============================================================
rem  一次性环境准备：创建 venv 并安装 Pillow / numpy / zstandard
rem  前置条件：系统 Python 3.9+，且 python.exe 在 PATH 里
rem ============================================================
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo   [错误] 找不到 python。请先安装 Python 3.9+ 并在安装时勾选 Add python.exe to PATH
    echo          下载地址 https://www.python.org/downloads/windows/
    pause
    exit /b 1
)
if not exist "tools\venv-tk\Scripts\python.exe" (
    echo   正在创建虚拟环境 tools\venv-tk ...
    python -m venv tools\venv-tk
    if errorlevel 1 (
        echo   [错误] 创建虚拟环境失败
        pause
        exit /b 1
    )
)
echo   正在安装依赖 Pillow / numpy / zstandard ...
"tools\venv-tk\Scripts\python.exe" -m pip install --upgrade pip
"tools\venv-tk\Scripts\python.exe" -m pip install Pillow numpy zstandard
if errorlevel 1 (
    echo   [错误] 依赖安装失败，请检查网络
    pause
    exit /b 1
)
echo.
echo   环境已就绪。双击 start-pet.bat 启动桌宠。
pause
