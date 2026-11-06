@echo off
chcp 65001 >nul
title CyBot 次元Bot
cd /d "%~dp0"

echo ================================================
echo   CyBot · 次元Bot - 启动
echo   管理界面: http://127.0.0.1:8321（局域网可访问）
echo ================================================

rem 优先使用 install_windows.bat 创建的虚拟环境
set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe

%PY% -c "import av, PIL, requests, websocket" >nul 2>nul
if errorlevel 1 (
    echo [初始化] 首次运行，安装依赖 av / pillow / requests / websocket-client ...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络后重试（或先运行 install_windows.bat）
        pause
        exit /b 1
    )
)

echo [启动] 主程序运行中，按 Ctrl+C 停止...
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8321"
%PY% cybot.py %*
pause
