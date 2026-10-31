@echo off
chcp 65001 >nul
title CyBot 次元Bot - 安装
cd /d "%~dp0"

echo ================================================
echo   CyBot · 次元Bot - 一键安装（Windows）
echo ================================================

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.11+ 并勾选 "Add to PATH"
    echo        下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)
python --version

echo.
echo [1/3] 创建虚拟环境 .venv ...
if not exist .venv (
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 虚拟环境创建失败
        pause
        exit /b 1
    )
) else (
    echo        已存在，跳过
)

echo.
echo [2/3] 安装依赖（av / pillow / requests / 华风爱科天气 MCP）...
".venv\Scripts\python.exe" -m pip install --upgrade pip -i https://pypi.org/simple --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.org/simple
if errorlevel 1 (
    echo [重试] 官方源失败，改用清华镜像 ...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络后重试
        pause
        exit /b 1
    )
)

echo.
echo [3/3] 检查配置文件 ...
if not exist config.json (
    echo [错误] 缺少 config.json（应随项目分发），请确认文件完整
    pause
    exit /b 1
)

echo.
echo ================================================
echo   安装完成！使用前请编辑 config.json 配置：
echo     - device.ip        设备 IP（也可自动发现）
echo     - llm.api_key      大模型 API Key
echo     - asr.api_key      语音识别 API Key
echo     - tts.api_key      语音合成 API Key（含克隆音色）
echo     - weather.api_key  天气 API Key（可选）
echo.
echo   之后运行 start_windows.bat 启动即可
echo ================================================
pause
