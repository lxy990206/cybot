#!/usr/bin/env bash
# CyBot 次元Bot · Linux 一键启动
set -e
cd "$(dirname "$0")"

echo "================================================"
echo "  CyBot · 次元Bot - 启动"
echo "  管理界面: http://127.0.0.1:8321（局域网可访问）"
echo "================================================"

# 优先使用 install_linux.sh 创建的虚拟环境
PY=python3
if [ -x .venv/bin/python ]; then
    PY=./.venv/bin/python
fi

if ! $PY -c "import av, PIL, requests" >/dev/null 2>&1; then
    echo "[初始化] 首次运行，安装依赖 av / pillow / requests ..."
    $PY -m pip install -r requirements.txt \
        || { echo "[错误] 依赖安装失败，请检查网络或改用系统包（python3-av 等）；也可先运行 ./install_linux.sh"; exit 1; }
fi

echo "[启动] 主程序运行中，按 Ctrl+C 停止..."
exec $PY cybot.py "$@"
