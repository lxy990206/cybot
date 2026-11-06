# CyBot · 次元Bot

![version](https://img.shields.io/badge/version-v1.0.3-blue) ![python](https://img.shields.io/badge/python-3.11%2B-green) ![platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey)

基于 **ZECTRIX Note 4 墨水屏设备** 的 AI 角色扮演智能体。设备端语音对话、墨水屏对话 UI、三层记忆系统、多供应商大模型接入、WebUI 八模块控制台，一体化开箱即用。

## 功能特性

- **语音对话闭环**：设备录音（Opus 16kHz）→ ASR 音近纠错 → LLM（人设注入）→ TTS 音色克隆 → 流式推放（WS 边合成边推流，降低首响延迟），文字与语音同步上屏
- **三层记忆系统**：短期（24h 完整上下文）/ 中期（每日 AI 总结）/ 长期（30 天整合 + 60 天衰减），核心记忆常驻防失忆；SQLite 嵌入式向量库（bge-m3 + bge-reranker 重排）
- **多供应商适配**：LLM / ASR / TTS 统一适配层，开箱支持小米 MiMo（音色克隆）、阿里云百炼（Qwen3-TTS / Qwen-Audio-TTS / CosyVoice v3.5 duplex WebSocket / Realtime WebSocket / 声音复刻）、DeepSeek、硅基流动、豆包、GPT-SoVITS、FunASR 等本地服务
- **天气按需调度**：华风爱科天气 MCP / 和风 / 高德 / 彩云 / OpenWeather；提及天气才查询，未指定城市时默认使用播报城市，1 小时缓存，多城市独立缓存，节省 API 额度
- **墨水屏对话 UI**：400×300 gray4 渲染、表情 / 固定图片避让换行、帧去重 + 清残影防褪色、FS 抖动、顶栏电量实时显示
- **WebUI 控制台**（局域网可访问）：概览 / 模型 / TTS / ASR / BOT / 记忆管理 / 服务端 / 终端 八模块；克隆音频文件服务 + 云端音色管理（音色绑定模型校验）
- **交互按键**：短按上/下滚动翻页 · 长按上/下调音量 · 短按确认重听 · 长按确认说话
- **设备端主动连接**（固件 3.7.5+）：设备端「设置 → 网络同步 → 连接管理端」可反向连到 PC（监听 UDP 32343，连接 ID 校验、去重、不抢占）
- **设备 IP 热管理**：WebUI 保存 IP/端口即时生效自动重连；按设备 ID（MAC）记忆设备，IP 变化自动找回并回写；WebUI 优先启动，设备未连接也可配置
- **一键部署**：Windows / Linux 安装与启动脚本，虚拟环境隔离

## 快速开始

```bash
# 1. 安装（创建 .venv 并安装依赖）
Windows:  install_windows.bat
Linux:    ./install_linux.sh

# 2. 配置：安装脚本会自动从 config.example.json 生成 config.json，编辑填入你的 API Key
#    （设备 IP 可留空，自动发现按设备 ID 锁定）

# 3. 启动
Windows:  start_windows.bat
Linux:    ./start_linux.sh
```

控制台：`http://127.0.0.1:8321`（同网段设备可经 `http://<主机IP>:8321` 访问）。

> 防火墙提示：如启用防火墙，请放行 **UDP 32342**（设备通信）、**UDP 32343**（设备端反向连接）、**TCP 8321**（WebUI）。

## 设备与 SDK

- 设备：ZECTRIX Note 4 墨水屏（400×300，zectrix-s3-epaper-4.2），推荐固件 **3.7.5+**
- 设备协议：Zectrix LAN v1（UDP，wire_version 1），实现与官方 SDK 0.2.0 / 协议文档 1.6 逐项核对一致
- SDK 上游仓库：**https://github.com/itopinion/zectrix-local-push-sdk**（Rust；本仓库内嵌 `zectrix-local-push-sdk/` 快照随版本同步更新，协议细节见其 `docs/`）

## 目录结构

```
cybot/
├── cybot.py                 # 主程序（协议 / 渲染 / 业务流水线 / 入口）
├── webui.py                 # WebUI 控制台（八模块）
├── providers.py             # LLM / ASR / TTS / 嵌入 / 重排 多供应商适配层
├── memory_vault.py          # 三层记忆库（SQLite 向量检索 + AI 整合）
├── weather.py               # 天气聚合（MCP + 多供应商 HTTP，按需调度）
├── logbus.py                # 全局日志总线（终端滚动页数据源）
├── config.example.json      # 配置模板（复制为 config.json 后填写密钥）
├── install_*.bat/sh         # 一键安装脚本
├── start_*.bat/sh           # 一键启动脚本
├── docs/                    # 云同步契约等文档
├── zectrix_note4_link_test.py   # 设备链路测试工具
├── cybot_data/              # 运行数据（不入库）：记忆库 / 人设 / 参考音频
└── zectrix-local-push-sdk/  # 厂商本地推送 SDK（Rust，快照）
                             # 上游：https://github.com/itopinion/zectrix-local-push-sdk
```

## 隐私说明

`config.json`（API 密钥）与 `cybot_data/`（记忆数据、人设、参考音频）已被 `.gitignore` 排除，**不会**进入版本库。首次部署请从 `config.example.json` 复制并填写自己的密钥。

## 版本

- **v1.0.3** — WS 流式 TTS（百炼 duplex/realtime 边合成边推流，降低首响延迟）；天气未指定城市时默认使用播报城市；设备 IP 热设置 + 按设备 ID（MAC）自动重连找回；WebUI 优先启动（设备未连接也可配置）
- **v1.0.2** — 适配 SDK 0.2.0（固件 3.7.5），新增设备端主动连接（UDP 32343）；事件泵/状态轮询常驻化
- **v1.0.0** — 首个稳定版本。

## 未来计划

- [ ] 表情包支持（动态表情集与情绪联动）
- [ ] 音乐播放
- [ ] 接入 Trae、ZCode、OpenCode 等平台
- [ ] 设备操控能力
- [ ] 更细的 Bot 设定

---

Developer: **wywdcy（我有我的次元）- 小精灵**
