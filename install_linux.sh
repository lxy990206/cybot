#!/usr/bin/env bash
# CyBot 次元Bot · Linux 一键安装
set -e
cd "$(dirname "$0")"

echo "================================================"
echo "  CyBot · 次元Bot - 一键安装（Linux）"
echo "================================================"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[错误] 未找到 python3，请先安装 Python 3.11+"
    echo "       Debian/Ubuntu: sudo apt install python3 python3-pip python3-venv"
    exit 1
fi
python3 --version

if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
    echo "[错误] 缺少 venv/ensurepip 模块（Debian/Ubuntu: sudo apt install python3-venv）"
    exit 1
fi

echo
echo "[1/3] 创建虚拟环境 .venv ..."
if [ -x .venv/bin/python ] && ./.venv/bin/python -c "import pip" >/dev/null 2>&1; then
    echo "       已存在，跳过"
else
    rm -rf .venv                    # 清理上次失败的半成品目录
    if ! python3 -m venv .venv; then
        rm -rf .venv
        echo "[错误] 虚拟环境创建失败（Debian/Ubuntu: sudo apt install python3-venv）"
        exit 1
    fi
fi

echo
echo "[2/3] 安装依赖（av / pillow / requests / websocket-client / 天气 MCP）..."
./.venv/bin/python -m pip install --upgrade pip -i https://pypi.org/simple --quiet
if ! ./.venv/bin/python -m pip install -r requirements.txt -i https://pypi.org/simple; then
    echo "[重试] 官方源失败，改用清华镜像 ..."
    ./.venv/bin/python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
fi

echo
echo "[3/3] 检查系统多媒体库（PyAV 需要 ffmpeg 运行库）..."
if command -v apt >/dev/null 2>&1; then
    if ! ldconfig -p 2>/dev/null | grep -q libavcodec; then
        echo "       建议执行: sudo apt install -y ffmpeg"
    else
        echo "       已就绪"
    fi
fi

echo
if [ ! -f config.json ]; then
    if [ -f config.example.json ]; then
        cp config.example.json config.json
        echo "[提示] 已从 config.example.json 生成 config.json，请编辑填入你的 API Key"
    else
        echo "[错误] 缺少 config.json 与 config.example.json，请确认文件完整"
        exit 1
    fi
fi

echo "================================================"
echo "  安装完成！使用前请编辑 config.json 配置："
echo "    - device.ip        设备 IP（也可自动发现）"
echo "    - llm.api_key      大模型 API Key"
echo "    - asr.api_key      语音识别 API Key"
echo "    - tts.api_key      语音合成 API Key（含克隆音色）"
echo "    - weather.api_key  天气 API Key（可选）"
echo
echo "  防火墙提示（如启用 ufw/firewalld 需放行）："
echo "    - UDP 32342  设备通信（推图/音频/按键）"
echo "    - UDP 32343  设备端「连接管理端」反向连接（固件 3.7.5+）"
echo "    - TCP 8321   WebUI 局域网访问"
echo
echo "  之后运行 ./start_linux.sh 启动即可"
echo "================================================"
