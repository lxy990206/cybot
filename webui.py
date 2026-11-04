#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""洛天依智能体 · Web 控制台（八模块：概览/模型/TTS/ASR/BOT/记忆/服务端/终端）

随主程序自动启动（仅监听 127.0.0.1），也可独立运行：
  python webui.py      # 独立模式（可编辑配置与人设，设备相关功能显示未连接）

配置热生效：WebUI 保存 → 更新共享 cfg 字典并落盘 → 下一轮对话使用新配置；
嵌入/重排模型配置通过 on_config_changed 回调即时刷新记忆库客户端。
"""

import base64
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import logbus
import providers

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "cybot_data")
REFS_DIR = os.path.join(DATA_DIR, "refs")
os.makedirs(REFS_DIR, exist_ok=True)
VFILES_DIR = os.path.join(DATA_DIR, "voice_files")   # 克隆音频文件服务（百炼声音复刻用）
os.makedirs(VFILES_DIR, exist_ok=True)

_CFG = None        # 共享配置字典（主程序传入 → 热生效）
_VAULT = None
_BOT = None        # TianyiBot 实例（独立模式为 None）
_ON_CONFIG_CHANGED = None
_PORT = 8321
_STARTED_AT = time.time()


def save_cfg():
    with open(os.path.join(ROOT, "config.json"), "w", encoding="utf-8") as f:
        json.dump(_CFG, f, ensure_ascii=False, indent=2)


def _mask(key):
    if not key:
        return ""
    return key if len(key) <= 12 else key[:8] + "…" + key[-4:]


# ============================ HTTP 处理 ============================

class _Handler(BaseHTTPRequestHandler):
    server_version = "TianyiConsole/2.0"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return None

    # ---------- GET ----------

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            return self._send(200, HTML.encode(), "text/html; charset=utf-8")
        if self.path.startswith("/api/overview"):
            return self._send(200, api_overview())
        if self.path.startswith("/api/config"):
            out = {"ok": True, "config": _CFG,
                   "tts_presets": providers.TTS_PRESETS,
                   "asr_presets": providers.ASR_PRESETS}
            return self._send(200, out)
        if self.path.startswith("/api/persona"):
            path = _persona_path()
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return self._send(200, {"ok": True, "persona": f.read()})
            except OSError:
                return self._send(200, {"ok": False, "persona": "", "message": "人设文件不存在"})
        if self.path.startswith("/api/logs"):
            qs = self._qs()
            since = int(qs.get("since", "0"))
            return self._send(200, {"ok": True, "logs": logbus.since(since),
                                    "last": logbus.last_seq()})
        if self.path.startswith("/api/refs"):
            files = sorted(f for f in os.listdir(REFS_DIR)
                           if f.lower().endswith((".wav", ".mp3", ".flac", ".ogg", ".m4a")))
            return self._send(200, {"ok": True, "files": files,
                                    "selected": _CFG.get("tts", {}).get("reference_audio", "")})
        if self.path.startswith("/api/vfiles"):
            files = sorted(f for f in os.listdir(VFILES_DIR)
                           if f.lower().endswith((".wav", ".mp3", ".flac", ".ogg", ".m4a")))
            return self._send(200, {"ok": True, "files": files})
        if self.path.startswith("/api/voices"):
            # 百炼克隆音色列表（list_voice）：voice_id 前缀即绑定的 target_model
            qs = self._qs()
            pname = qs.get("provider", "")
            p = dict(providers.TTS_PRESETS.get(pname, {}))
            p.update((_CFG.get("tts", {}).get("providers", {}) or {}).get(pname, {}))
            if not p.get("api_key"):
                return self._send(200, {"ok": False, "message": "请先填写该供应商 API KEY"})
            try:
                lst = providers.bailian_voice_list(p["base_url"], p["api_key"])
            except Exception as e:
                return self._send(200, {"ok": False, "message": repr(e)[:200]})
            model = p.get("model", "")
            for v in lst:
                v["matched"] = str(v.get("voice_id", "")).startswith(model + "-")
            lst.sort(key=lambda v: (not v["matched"], str(v.get("gmt_create", ""))), reverse=False)
            return self._send(200, {"ok": True, "model": model, "voices": lst})
        if self.path.startswith("/api/list"):
            qs = self._qs()
            items = _VAULT.list_all(qs.get("layer", "short"), unquote(qs.get("q", "")))
            return self._send(200, items)
        if self.path.startswith("/api/export"):
            return self._send(200, _VAULT.export_all())
        if self.path.startswith("/api/health"):
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    # ---------- POST ----------

    def do_POST(self):
        body = self._body()
        if body is None:
            return self._send(400, {"ok": False, "message": "JSON 解析失败"})
        try:
            if self.path.startswith("/api/config"):
                section = body.get("section", "")
                data = body.get("data", {})
                if section not in ("llm", "embed", "asr", "tts", "bot", "server", "device", "weather"):
                    return self._send(400, {"ok": False, "message": "未知配置分区"})
                cur = _CFG.setdefault(section, {})
                for k, v in data.items():
                    cur[k] = v
                save_cfg()
                logbus.push("配置已更新：%s（下一轮对话生效）" % section)
                if _ON_CONFIG_CHANGED:
                    try:
                        _ON_CONFIG_CHANGED(section)
                    except Exception as e:
                        logbus.push("配置变更回调异常：%r" % e)
                return self._send(200, {"ok": True})
            if self.path.startswith("/api/persona"):
                text = str(body.get("persona", ""))
                if not text.strip():
                    return self._send(400, {"ok": False, "message": "人设不能为空"})
                with open(_persona_path(), "w", encoding="utf-8") as f:
                    f.write(text)
                logbus.push("人设已更新（%d 字符），下一轮对话生效" % len(text))
                return self._send(200, {"ok": True})
            if self.path.startswith("/api/refs/upload"):
                name = os.path.basename(str(body.get("name", "ref.wav")))
                if not name.lower().endswith((".wav", ".mp3", ".flac", ".ogg", ".m4a")):
                    name += ".wav"
                raw = base64.b64decode(body.get("b64", ""))
                if not raw:
                    return self._send(400, {"ok": False, "message": "音频内容为空"})
                with open(os.path.join(REFS_DIR, name), "wb") as f:
                    f.write(raw)
                logbus.push("参考音频已上传：%s（%d 字节）" % (name, len(raw)))
                return self._send(200, {"ok": True, "name": name})
            if self.path.startswith("/api/refs/select"):
                name = str(body.get("name", ""))
                path = os.path.join("cybot_data", "refs", name)
                if name and os.path.exists(os.path.join(ROOT, path)):
                    _CFG.setdefault("tts", {})["reference_audio"] = path
                    save_cfg()
                    return self._send(200, {"ok": True, "selected": path})
                return self._send(400, {"ok": False, "message": "文件不存在"})
            if self.path.startswith("/api/refs/delete"):
                name = os.path.basename(str(body.get("name", "")))
                p = os.path.join(REFS_DIR, name)
                if os.path.exists(p):
                    os.remove(p)
                return self._send(200, {"ok": True})
            if self.path.startswith("/api/clone_voice"):
                # 百炼声音复刻：参考音频 → 音色 ID（写回供应商 voice 字段）
                provider = body.get("provider", "cosyvoice")
                ref_name = os.path.basename(str(body.get("ref", "")))
                ref_path = os.path.join(VFILES_DIR, ref_name)   # 优先克隆音频文件服务
                if not os.path.exists(ref_path):
                    ref_path = os.path.join(REFS_DIR, ref_name)  # 兼容旧参考音频目录
                if not os.path.exists(ref_path):
                    return self._send(400, {"ok": False, "message": "参考音频不存在：" + ref_name})
                p = dict(providers.TTS_PRESETS.get(provider, {}))
                p.update((_CFG.get("tts", {}).get("providers", {}) or {}).get(provider, {}))
                if not p.get("api_key"):
                    return self._send(400, {"ok": False, "message": "请先填写该供应商 API KEY"})
                with open(ref_path, "rb") as f:
                    dataurl = "data:audio/wav;base64," + base64.b64encode(f.read()).decode()
                voice_id = providers.bailian_voice_clone(
                    p["base_url"], p["api_key"], p.get("model", "cosyvoice-v2"),
                    dataurl, prefix="cybot", language_hints=["zh"])
                if not voice_id:
                    return self._send(200, {"ok": False, "message": "克隆失败：未返回音色 ID"})
                _CFG.setdefault("tts", {}).setdefault("providers", {}).setdefault(provider, {})["voice"] = voice_id
                save_cfg()
                logbus.push("音色克隆成功（%s）：%s" % (provider, voice_id))
                return self._send(200, {"ok": True, "voice_id": voice_id})
            if self.path.startswith("/api/vfiles"):
                # 克隆音频文件服务：上传 / 删除（cybot_data/voice_files/），列表走 do_GET
                if self.path.startswith("/api/vfiles/upload"):
                    name = os.path.basename(str(body.get("name", "ref.wav")))
                    raw = base64.b64decode(body.get("b64", ""))
                    if not raw:
                        return self._send(400, {"ok": False, "message": "音频内容为空"})
                    with open(os.path.join(VFILES_DIR, name), "wb") as f:
                        f.write(raw)
                    logbus.push("克隆音频已上传：%s（%d 字节）" % (name, len(raw)))
                    return self._send(200, {"ok": True, "name": name})
                if self.path.startswith("/api/vfiles/delete"):
                    name = os.path.basename(str(body.get("name", "")))
                    fp = os.path.join(VFILES_DIR, name)
                    if os.path.exists(fp):
                        os.remove(fp)
                    return self._send(200, {"ok": True})
                return self._send(404, {"error": "not found"})
            if self.path.startswith("/api/test/"):
                return self._send(200, api_test(self.path.rsplit("/", 1)[-1]))
            if self.path.startswith("/api/action/query_status"):
                if _BOT is None:
                    return self._send(200, {"ok": False, "message": "独立模式无设备连接"})
                _BOT.bot.query_status(force=True)
                return self._send(200, {"ok": True, "status": _BOT.bot.device_status})
            if self.path.startswith("/api/action/reconnect"):
                if _BOT is None:
                    return self._send(200, {"ok": False, "message": "独立模式无设备连接"})
                _BOT.reconnect_async()
                return self._send(200, {"ok": True, "message": "重连已发起"})
            if self.path.startswith("/api/action/disconnect"):
                if _BOT is None:
                    return self._send(200, {"ok": False, "message": "独立模式无设备连接"})
                _BOT.disconnect_async()
                return self._send(200, {"ok": True, "message": "断开已发起"})
            if self.path.startswith("/api/delete"):
                _VAULT.delete_memory(body.get("layer"), body.get("id"))
                return self._send(200, {"ok": True})
            if self.path.startswith("/api/toggleKey"):
                r = _VAULT.toggle_key(body.get("id"))
                return self._send(200, {"ok": r is not None, "isKey": r})
            if self.path.startswith("/api/add"):
                _VAULT.manual_add_long(body.get("content", ""), bool(body.get("isKey", True)))
                return self._send(200, {"ok": True})
            if self.path.startswith("/api/consolidate"):
                mode = body.get("mode")
                if mode == "monthly":
                    _VAULT._meta_set("lastLongRun", 0)
                    _VAULT.monthly_consolidation()
                else:
                    _VAULT._meta_set("lastMidDate", None)
                    _VAULT.daily_consolidation()
                return self._send(200, {"ok": True})
            if self.path.startswith("/api/import"):
                ok = _VAULT.import_all(body)
                return self._send(200, {"ok": ok, "message": "" if ok else "schema/appId 校验未通过"})
        except Exception as e:
            logbus.push("[WebUI] 请求异常 %s：%r" % (self.path, e))
            return self._send(500, {"ok": False, "message": repr(e)})
        return self._send(404, {"error": "not found"})

    def _qs(self):
        if "?" not in self.path:
            return {}
        return dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&") if "=" in p)

    def log_message(self, *args):
        pass


# ============================ 数据接口 ============================

def _persona_path():
    return os.path.join(ROOT, _CFG.get("bot", {}).get("persona_file",
                                                      "cybot_data/persona.md"))


def api_overview():
    st = _VAULT.get_stats() if _VAULT else {}
    routine_name, _ = ("未知", "")
    try:
        import cybot as demo
        routine_name = demo.routine_now(tz_name=_CFG.get("bot", {}).get("timezone", ""))[0]
    except Exception:
        pass
    bot_info = {"connected": False, "addr": "", "device_id": "", "state": "",
                "volume": _CFG.get("tts", {}).get("volume", 80)}
    if _BOT is not None:
        b = _BOT.bot
        ds = getattr(b, "device_status", None) or {}
        bot_info = {"connected": bool(b.alive and not b.dead), "addr": "%s:%d" % b.addr,
                    "device_id": getattr(b, "device_id", ""), "state": _BOT.state,
                    "volume": _BOT.volume,
                    "conn_req": bool(getattr(_BOT, "conn_req_listening", False)),
                    "battery": ds.get("battery"), "charging": ds.get("charging"),
                    "rssi": ds.get("rssi")}
    stats = getattr(_BOT, "stats", {}) if _BOT else {}
    asr_key = _CFG.get("asr", {}).get("provider", "")
    tts_key = _CFG.get("tts", {}).get("provider", "")
    return {
        "uptime_s": int(time.time() - _STARTED_AT),
        "bot_name": _CFG.get("bot", {}).get("name", "洛天依"),
        "routine": routine_name,
        "bot": bot_info,
        "memory": st,
        "turns": stats.get("turns", 0),
        "recordings": stats.get("recordings", 0),
        "errors": stats.get("errors", 0),
        "asr_provider": (providers.ASR_PRESETS.get(asr_key, {}) or {}).get("name", asr_key),
        "tts_provider": (providers.TTS_PRESETS.get(tts_key, {}) or {}).get("name", tts_key),
        "llm_model": _CFG.get("llm", {}).get("model", ""),
        "embed_model": _CFG.get("embed", {}).get("model", ""),
        "ref_audio": _CFG.get("tts", {}).get("reference_audio", ""),
    }


def api_test(kind):
    """连通性测试：llm / embed / tts / asr。"""
    try:
        if kind == "llm":
            reply = providers.build_llm(_CFG).chat(
                [{"role": "user", "content": "回复：OK"}], system="只回复 OK")
            return {"ok": True, "message": "LLM 连通：" + reply[:40]}
        if kind == "embed":
            v = providers.build_embed(_CFG).embed_one("连通性测试")
            return {"ok": bool(v), "message": "嵌入维度 %d" % len(v) if v else "嵌入失败"}
        if kind == "tts":
            ref_b64 = None
            ref = _CFG.get("tts", {}).get("reference_audio", "")
            if ref:
                path = ref if os.path.isabs(ref) else os.path.join(ROOT, ref)
                with open(path, "rb") as f:
                    ref_b64 = base64.b64encode(f.read()).decode()
            wav = providers.build_tts(_CFG, ref_b64).tts("你好，我是洛天依。")
            return {"ok": True, "message": "TTS 连通：%d 字节音频" % len(wav)}
        if kind == "weather":
            import weather as weather_mod
            data = weather_mod.get_weather(_CFG, force=True)
            if not data:
                return {"ok": False, "message": "天气获取失败（检查供应商/KEY/城市配置，详见终端日志）"}
            return {"ok": True, "message": "天气：%s %s %s %s" %
                    (data["city"], data["text"], data["temp"] or "", data["wind"] or "")}
        if kind == "asr":
            import io
            import wave
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x00\x00" * 16000)   # 1 秒静音
            text = providers.build_asr(_CFG).transcribe(buf.getvalue())
            return {"ok": True, "message": "ASR 连通（静音识别：%r）" % text[:20]}
        return {"ok": False, "message": "未知测试项"}
    except Exception as e:
        return {"ok": False, "message": "%s 测试失败：%r" % (kind, e)}


# ============================ 页面 ============================

HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>洛天依智能体 · 控制台</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0f1216;color:#d8dee6;font:14px/1.7 "Segoe UI","Microsoft YaHei",sans-serif;display:flex;min-height:100vh}
nav{width:190px;background:#151a21;border-right:1px solid #232a36;padding:16px 10px;flex-shrink:0}
nav h1{font-size:15px;color:#7fd4c1;margin:2px 8px 16px}
nav button{display:block;width:100%;text-align:left;background:none;border:none;color:#9fb0c0;padding:9px 14px;border-radius:8px;cursor:pointer;font-size:13.5px;margin-bottom:2px}
nav button:hover{background:#1c222d;color:#d8dee6}
nav button.on{background:#245a4e;color:#c9f5e8}
main{flex:1;padding:18px 26px;max-width:1080px;overflow-y:auto}
h2{font-size:17px;color:#c9d6e2;border-bottom:1px solid #232a36;padding-bottom:8px;margin:4px 0 16px}
.card{background:#151a21;border:1px solid #232a36;border-radius:12px;padding:14px 18px;margin-bottom:14px}
.card h3{margin:0 0 10px;font-size:14px;color:#9fd8ff}
.row{display:flex;gap:14px;flex-wrap:wrap}
.field{flex:1;min-width:220px;margin-bottom:8px}
.field label{display:block;font-size:12px;color:#9fb0c0;margin-bottom:3px}
input[type=text],input[type=password],input[type=number],select,textarea{width:100%;background:#0e1116;border:1px solid #2a3140;color:#d8dee6;border-radius:8px;padding:7px 11px;font:inherit}
textarea{min-height:80px;resize:vertical}
.btn{background:#1c222d;color:#d8dee6;border:1px solid #2a3140;border-radius:8px;padding:7px 15px;cursor:pointer;font-size:13px}
.btn:hover{border-color:#3d8a76;color:#c9f5e8}
.btn.primary{background:#245a4e;border-color:#3d8a76;color:#c9f5e8}
.btn.warn:hover{border-color:#a86}
.btn.small{padding:3px 10px;font-size:12px}
.msg{font-size:12px;color:#8fd8a0;margin-left:10px}
.err{color:#ffb0b0}
table{width:100%;border-collapse:collapse;background:#12161d;border:1px solid #232a36;border-radius:10px;overflow:hidden}
th,td{padding:8px 11px;border-bottom:1px solid #212734;text-align:left;vertical-align:top;font-size:12.5px}
th{background:#1a2029;color:#9fb0c0;white-space:nowrap}
td.c{white-space:nowrap;color:#9fb0c0}
.tag{display:inline-block;border-radius:6px;padding:1px 8px;font-size:11px;margin-right:4px}
.t-key{background:#5a4a1c;color:#ffd97f}.t-long{background:#1c3a5a;color:#9fd8ff}
.t-mid{background:#2a2a4a;color:#c0b6ff}.t-short{background:#24422e;color:#a8e6b0}
.t-deg{background:#5a2c2c;color:#ffb0b0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
.kpi{background:#151a21;border:1px solid #232a36;border-radius:10px;padding:10px 14px}
.kpi b{display:block;font-size:19px;color:#9fd8ff}
.kpi span{font-size:11.5px;color:#9fb0c0}
.badge{display:inline-block;border-radius:14px;padding:2px 12px;font-size:12px;margin-right:6px}
.b-ok{background:#1d3a2a;color:#8fe6a8}.b-bad{background:#3a1d1d;color:#ff9f9f}
#term{background:#0a0d10;border:1px solid #232a36;border-radius:10px;padding:10px;height:62vh;overflow-y:auto;font:12px/1.55 Consolas,monospace;color:#9fe6b8;white-space:pre-wrap;word-break:break-all}
.content-cell{max-width:520px;white-space:pre-wrap;word-break:break-word}
.hint{font-size:11.5px;color:#6b7888}
</style></head><body>
<nav>
  <h1><span class="botname">角色</span>控制台</h1>
  <button data-p="overview" class="on">概览</button>
  <button data-p="llm">模型设置</button>
  <button data-p="tts">TTS 设置</button>
  <button data-p="asr">ASR 设置</button>
  <button data-p="bot">BOT 设置</button>
  <button data-p="memory">记忆管理</button>
  <button data-p="server">服务端设置</button>
  <button data-p="term">终端</button>
</nav>
<main id="main"></main>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let CFG = null, PRESETS = {tts:{}, asr:{}};
async function api(path, body){
  const opt = body !== undefined ? {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)} : undefined;
  const r = await fetch(path, opt);
  return r.json();
}
const pages = {};

/* ---------- 概览 ---------- */
pages.overview = async el => {
  const o = await api('/api/overview');
  document.querySelectorAll('.botname').forEach(x => x.textContent = o.bot_name || '角色');
  document.title = (o.bot_name || '角色') + ' · 控制台';
  el.innerHTML = `
  <h2>概览</h2>
  <div class="grid">
    <div class="kpi"><b>${o.bot.connected?'在线':'离线'}</b><span>设备连接 ${esc(o.bot.addr)}</span></div>
    <div class="kpi"><b>${o.bot.conn_req?'开启':'关闭'}</b><span>反向连接监听（UDP 32343）</span></div>
    <div class="kpi"><b>${esc(o.bot.device_id||'-')}</b><span>设备 ID</span></div>
    <div class="kpi"><b>${esc(o.routine)}</b><span>当前作息</span></div>
    <div class="kpi"><b>${o.turns}</b><span>累计对话轮次</span></div>
    <div class="kpi"><b>${o.memory.short??0} / ${o.memory.mid??0} / ${o.memory.long??0}</b><span>记忆 短/中/长（核心 ${o.memory.key??0}）</span></div>
    <div class="kpi"><b>${o.recordings}</b><span>录音次数（错误 ${o.errors}）</span></div>
    <div class="kpi"><b>${esc(o.llm_model)}</b><span>LLM</span></div>
    <div class="kpi"><b>${esc(o.tts_provider)} / ${esc(o.asr_provider)}</b><span>TTS / ASR 供应商</span></div>
    <div class="kpi"><b>${Math.floor(o.uptime_s/60)} 分</b><span>运行时长</span></div>
  </div>
  <div class="card" style="margin-top:14px">
    <h3>设备状态与快捷操作</h3>
    <div class="grid">
      <div class="kpi"><b>${o.bot.battery!=null ? o.bot.battery+'%' : '未知'}</b><span>设备电量${o.bot.charging?'（充电中）':''}</span></div>
      <div class="kpi"><b>${o.bot.rssi!=null ? o.bot.rssi+' dBm' : '-'}</b><span>Wi-Fi 信号</span></div>
    </div>
    <div style="margin-top:10px">
      <button class="btn" onclick="api('/api/action/query_status').then(()=>{load()})">立即查询电量</button>
      <button class="btn" onclick="confirm('确认断开与设备的连接？断开后可随时重新连接。')&&api('/api/action/disconnect').then(()=>setTimeout(load,800))">断开连接</button>
      <button class="btn" onclick="api('/api/action/reconnect').then(()=>alert('重连已发起'))">重新连接设备</button>
      <span class="hint">按键：短按上/下=滚动翻页 · 长按上/下=音量± · 短按确认=重听 · 设备端「设置→网络同步→连接管理端」可反向连到本机（需放行 UDP 32343）</span>
    </div>
  </div>`;
};

/* ---------- 配置表单通用 ---------- */
function fields(items, data){
  return '<div class="row">' + items.map(it => {
    const v = esc(data[it.k] ?? it.def ?? '');
    if (it.type === 'select'){
      const opts = (it.opts||[]).map(o => typeof o === 'object' ? o : {v:o, label:o});
      return `<div class="field"><label>${it.label}</label><select id="f_${it.k}">` +
        opts.map(o=>`<option value="${esc(o.v)}" ${o.v===data[it.k]?'selected':''}>${esc(o.label)}</option>`).join('') + '</select></div>';
    }
    const t = it.pw ? 'password' : 'text';
    return `<div class="field"><label>${it.label}</label><input id="f_${it.k}" type="${t}" value="${v}"></div>`;
  }).join('') + '</div>';
}
function readFields(items){ const d={}; items.forEach(it=>{ d[it.k]=$(`#f_${it.k}`).value; }); return d; }

/* ---------- 模型设置 ---------- */
pages.llm = async el => {
  const c = CFG;
  el.innerHTML = `<h2>模型设置</h2>
  <div class="card"><h3>对话 LLM（OpenAI 兼容）</h3>
    ${fields([{k:'base_url',label:'基础 URL'},{k:'api_key',label:'API KEY',pw:1},{k:'model',label:'模型 ID'},
              {k:'thinking',label:'思考模式',type:'select',opts:[{v:'default',label:'跟随默认（不发送开关）'},{v:'off',label:'关闭思考（更快、省 token）'},{v:'on',label:'开启思考（更深但更慢）'}]},
              {k:'extra_body',label:'附加请求参数（JSON，可选）'}], c.llm)}
    <button class="btn primary" onclick="saveSec('llm')">保存</button>
    <button class="btn" onclick="testConn('llm')">测试连通</button><span class="msg" id="msg_llm"></span>
    <p class="hint">思考模式按模型自动适配协议：Qwen3/百炼 → enable_thinking；GLM → thinking.type；DeepSeek → reasoner 自动换 chat；vLLM/聚合网关 → 双写 enable_thinking 与 chat_template_kwargs。「附加参数」可覆盖任意请求字段，如 {"reasoning_effort":"low"}。</p>
  </div>
  <div class="card"><h3>嵌入 &amp; 重排（记忆检索，OpenAI 兼容）</h3>
    ${fields([{k:'base_url',label:'基础 URL'},{k:'api_key',label:'API KEY',pw:1},{k:'model',label:'嵌入模型（bge-m3，1024 维）'},{k:'rerank_model',label:'重排模型'}], c.embed)}
    <button class="btn primary" onclick="saveSec('embed')">保存</button>
    <button class="btn" onclick="testConn('embed')">测试连通</button><span class="msg" id="msg_embed"></span>
    <p class="hint">保存后立即刷新记忆库检索客户端，无需重启主程序。</p>
  </div>`;
};
async function saveSec(sec){
  const el = $(`#msg_${sec}`); const map = {
    llm:['base_url','api_key','model','thinking','extra_body','temperature','max_tokens'],
    embed:['base_url','api_key','model','rerank_model']};
  const ks = map[sec] || collectKeys(sec);
  const data = {}; ks.forEach(k=>{ const f=$(`#f_${k}`); if(f) data[k] = f.value; });
  if (sec==='llm'){
    data.temperature = parseFloat(data.temperature) || 0.8;
    data.max_tokens = parseInt(data.max_tokens) || 300;
    data.thinking = ['default','off','on'].includes(data.thinking) ? data.thinking : 'default';
    data.extra_body = (data.extra_body || '').trim();
  }
  const r = await api('/api/config', {section: sec, data});
  if (el) el.textContent = r.ok ? '已保存 ✓（下一轮对话生效）' : ('失败：'+r.message);
  if(el) el.className = r.ok?'msg':'msg err';
}
function collectKeys(sec){
  return [...document.querySelectorAll(`[id^=f_]`)].map(x=>x.id.slice(2));
}

/* ---------- TTS 设置 ---------- */
pages.tts = async el => {
  const c = CFG.tts, refs = await api('/api/refs'), vfiles = await api('/api/vfiles');
  const names = [...new Set([...Object.keys(PRESETS.tts), ...Object.keys(c.providers||{})])]
    .map(k=>({v:k, label:(PRESETS.tts[k]||{}).name || k}));
  el.innerHTML = `<h2>TTS 设置</h2>
  <div class="card"><h3>供应商</h3>
    ${fields([{k:'provider',label:'当前 TTS 供应商',type:'select',opts:names}], c)}
    <button class="btn primary" onclick="saveSecDyn('tts')">保存并切换</button><span class="hint">切换后下一轮对话生效</span>
  </div>
  <div class="card"><h3>供应商参数（<span id="tp"></span>）</h3><div id="tprov"></div>
    <button class="btn primary" onclick="saveProv('tts')">保存供应商参数</button>
    <button class="btn" onclick="testConn('tts')">测试合成</button>
    <span class="msg" id="msg_tts"></span>
    <p class="hint" id="ttshint"></p>
  </div>
  <div class="card" id="cloneSvcCard"><h3>克隆音频文件服务（百炼声音复刻用）</h3>
    <div class="row"><div class="field"><label>已上传的克隆音频</label>
      <select id="vfsel">${vfiles.files.map(f=>`<option>${esc(f)}</option>`).join('')}</select></div>
      <button class="btn warn" onclick="delVFile()">删除所选</button></div>
    <div class="row"><div class="field"><label>上传克隆音频</label>
      <input type="text" id="vfname" placeholder="文件名，如 cybot_ref.wav">
      <input type="text" id="vfb64" placeholder="base64 内容（或用下方选择本地文件）">
      <input type="file" id="vffile" accept=".wav,.mp3,.flac,.ogg,.m4a">
      <button class="btn" onclick="uploadVFile()">上传</button></div></div>
    <button class="btn primary" id="cloneBtn" style="display:none" onclick="cloneVoice()">开始克隆（用所选音频，绑定当前模型）</button>
    <span class="msg" id="msg_clone"></span>
    <p class="hint">上传参考音频 → 选中 → 「开始克隆」。百炼以 Base64 Data URI 方式读取音频（无需公网 URL），生成的音色 ID 自动绑定当前供应商所选模型。</p>
  </div>
  <div class="card" id="voiceMgmtCard"><h3>克隆音色管理（云端音色列表）</h3>
    <div class="row"><div class="field"><label>音色（✔ = 与当前模型匹配，可直接使用）</label>
      <select id="voicesel" style="min-width:320px"><option value="">（点击「刷新音色列表」加载）</option></select></div>
      <button class="btn" onclick="loadVoices()">刷新音色列表</button>
      <button class="btn primary" onclick="applyVoice()">设为使用中</button></div>
    <span class="msg" id="msg_voice"></span>
    <p class="hint">音色 ID 形如 <code>{绑定模型}-{前缀}-{标识}</code>，绑定模型须与合成模型一致才能调用——不匹配的音色会标 ✗。克隆成功后列表自动刷新并选中。</p>
  </div>
  <div class="card" id="mimoCard"><h3>参考音频（MiMo 音色克隆用）</h3>
    <div class="row"><div class="field"><label>已上传的参考音频</label>
      <select id="refsel">${refs.files.map(f=>`<option ${f===refs.selected?.split('/').pop()?'selected':''}>${esc(f)}</option>`).join('')}</select></div>
      <div class="field"><label>当前选择</label><input type="text" value="${esc(refs.selected)}" disabled></div></div>
    <button class="btn primary" onclick="selectRef()">设为使用中</button>
    <button class="btn warn" onclick="delRef()">删除所选</button>
    <p class="hint">MiMo 克隆使用「当前选择」的音频（tts.reference_audio）。上传：选择本地文件或填 base64。</p>
    <div class="row"><div class="field"><label>上传新参考音频</label>
      <input type="text" id="refname" placeholder="文件名，如 cybot_ref.wav">
      <input type="text" id="refb64" placeholder="base64 内容（或用下方选择本地文件）">
      <input type="file" id="reffile" accept=".wav,.mp3,.flac,.ogg">
      <button class="btn" onclick="uploadRef()">上传</button></div></div>
  </div>`;
  renderTTSProvider();
  $('#f_provider').onchange = () => { renderTTSProvider(); updateCloneBtn('tts'); loadVoices(true); };
  updateCloneBtn('tts');
};
function mergedProv(sec, name){
  // 强制包含 api_key（预设/已存配置缺该键时补空）——保证每个供应商都能在界面填写密钥
  return {api_key: '', ...((sec==='tts'?PRESETS.tts:PRESETS.asr)[name]||{}), ...((CFG[sec].providers||{})[name]||{})};
}
function updateCloneBtn(sec){
  const b = $('#cloneBtn'); if (!b) return;
  const p = $('#f_provider').value;
  // 百炼克隆系：CosyVoice v3.5 与 Qwen-Audio-TTS 走 voice-enrollment（WebSocket）；
  // qwen3_realtime 仅系统音色、bailian 的 qwen3-tts-flash 不支持复刻
  const canClone = (sec==='tts' && ['cosyvoice_v35','qwen_audio_tts'].includes(p));
  b.style.display = canClone ? '' : 'none';
  // 卡片随供应商联动：克隆工作台仅克隆系可见，MiMo 参考音频仅 MiMo 可见
  const cs = $('#cloneSvcCard'), vm = $('#voiceMgmtCard'), mm = $('#mimoCard');
  if (cs) cs.style.display = canClone ? '' : 'none';
  if (vm) vm.style.display = canClone ? '' : 'none';
  if (mm) mm.style.display = (sec==='tts' && p==='mimo') ? '' : 'none';
  const h = $('#ttshint');
  if (h) h.textContent = {mimo:'MiMo：模型须为 *voiceclone，使用上方参考音频克隆音色。',
    cosyvoice_v35:'CosyVoice v3.5：duplex WebSocket 合成；克隆音色的 target_model='+ (mergedProv('tts',p).model||'') +'（模型改了需重新克隆）。',
    qwen_audio_tts:'Qwen-Audio-TTS：duplex WebSocket 合成；克隆音色的 target_model='+ (mergedProv('tts',p).model||'') +'（模型改了需重新克隆）。',
    qwen3_realtime:'Qwen3-TTS-Realtime：Realtime WebSocket，仅系统音色（Cherry/Ethan/Chelsie…），不支持声音复刻。',
    bailian:'Qwen3-TTS-Flash（非实时 HTTP）：使用系统音色（Cherry/Ethan/Chelsie…），不支持声音复刻。',
    gptsovits:'GPT-SoVITS：ref_audio_path 为【TTS 服务端机器上的绝对路径】，prompt_text 为参考音频文本。',
    local:'本地 OpenAI 兼容：GPT-SoVITS api_v2 OpenAI 服务、fish-speech、CosyVoice local 等均适用。',
    glm_tts:'GLM-TTS 本地：默认 OpenAI 兼容 /v1/audio/speech，端口按你的部署调整。',
    voxcpm:'VoxCPM 本地：OpenAI 兼容服务（社区部署常见端口 8001）。',
    qwen3_tts_local:'Qwen3-TTS 本地：OpenAI 兼容部署端口按部署调整。'}[p] || '';
}
async function cloneVoice(){
  const ref = $('#vfsel').value;
  if (!ref) { alert('请先上传并选择克隆音频'); return; }
  if (!confirm('用「'+ref+'」克隆音色并写回 voice 字段？')) return;
  const el = $('#msg_clone'); el.textContent = '克隆中…（约 10-30 秒）'; el.className = 'msg';
  const r = await api('/api/clone_voice', {provider: $('#f_provider').value, ref});
  el.textContent = r.ok ? '克隆成功 ✓ 音色：'+r.voice_id : '克隆失败：'+(r.message||'');
  el.className = r.ok ? 'msg' : 'msg err';
  if (r.ok) { await loadVoices(); const vs=$('#voicesel');
    if (vs) [...vs.options].forEach(o=>{ if(o.value===r.voice_id) vs.value=r.voice_id; }); }
}
async function loadVFiles(){
  const r = await api('/api/vfiles');
  const sel = $('#vfsel'); if (!sel) return;
  sel.innerHTML = r.files.map(f=>`<option>${esc(f)}</option>`).join('');
}
function b64Of(buf){
  // 大文件分块转换：String.fromCharCode(...全部字节) 会因参数超限抛 RangeError
  const u8 = new Uint8Array(buf); let s = '';
  for (let i = 0; i < u8.length; i += 0x8000)
    s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
  return btoa(s);
}
async function uploadVFile(){
  try {
    const file = $('#vffile').files[0];
    let r;
    if (file){
      r = await api('/api/vfiles/upload', {name: file.name, b64: b64Of(await file.arrayBuffer())});
      if (r.ok) { $('#vffile').value = ''; await loadVFiles();
        const sel = $('#vfsel'); if (sel) sel.value = file.name; }
      alert(r.ok ? '上传成功：'+file.name : '上传失败：'+(r.message||''));
      return;
    }
    const name = $('#vfname').value, b64 = $('#vfb64').value.trim();
    if (!name || !b64) { alert('请选择本地文件，或同时填写文件名与 base64'); return; }
    r = await api('/api/vfiles/upload', {name, b64});
    if (r.ok) { $('#vfname').value = ''; $('#vfb64').value = ''; await loadVFiles();
      const sel = $('#vfsel'); if (sel) sel.value = name; }
    alert(r.ok ? '上传成功：'+name : '上传失败：'+(r.message||''));
  } catch(e) { alert('上传异常：'+(e && e.message || e)); }
}
async function delVFile(){
  const name = $('#vfsel').value; if (!name) return;
  if (!confirm('删除 '+name+'？')) return;
  await api('/api/vfiles/delete', {name}); loadVFiles();
}
async function loadVoices(quiet){
  const sel = $('#voicesel'); if (!sel) return;
  const p = $('#f_provider').value;
  if (!['cosyvoice_v35','qwen_audio_tts'].includes(p)) {
    sel.innerHTML = '<option value="">（当前供应商不支持声音复刻）</option>'; return; }
  const r = await api('/api/voices?provider='+p);
  if (!r.ok) {
    sel.innerHTML = '<option value="">（加载失败）</option>';
    const el = $('#msg_voice'); if (el && !quiet) { el.textContent = r.message || '加载失败'; el.className='msg err'; }
    return;
  }
  sel.innerHTML = r.voices.length
    ? r.voices.map(v=>`<option value="${esc(v.voice_id)}">${v.matched?'✔':'✗'} ${esc(v.voice_id)}（${esc(v.status||'')}，${esc(v.gmt_create||'')}）</option>`).join('')
    : '<option value="">（云端暂无音色，请先克隆）</option>';
  const el = $('#msg_voice');
  if (el && !quiet) { el.textContent = '已加载 '+r.voices.length+' 个音色（当前模型：'+r.model+'）'; el.className='msg'; }
}
async function applyVoice(){
  const vid = $('#voicesel').value;
  if (!vid) { alert('请先刷新并选择音色'); return; }
  const p = $('#f_provider').value;
  const r = await api('/api/config', {section: 'tts',
    data: {provider: p, providers: {...CFG.tts.providers, [p]: {...mergedProv('tts', p), voice: vid}}}});
  const el = $('#msg_voice');
  if (el) { el.textContent = r.ok ? '已设为使用中 ✓（下一轮对话生效）：'+vid : '失败'; el.className = r.ok?'msg':'msg err'; }
  if (r.ok) CFG.tts.providers = {...CFG.tts.providers, [p]: {...mergedProv('tts', p), voice: vid}};
}
function renderTTSProvider(){
  const p = $('#f_provider').value;
  $('#tp').textContent = p;
  const merged = mergedProv('tts', p);
  $('#tprov').innerHTML = fields(Object.keys(merged).map(k=>({k,label:(k==='api_key'?'API KEY':k), pw:k==='api_key'})), merged);
}
async function saveProv(sec){
  const p = $('#f_provider').value;
  const box = sec==='tts' ? '#tprov' : '#aprov';
  const merged = mergedProv(sec, p);
  const data = {}; Object.keys(merged).forEach(k=>{
    const f = document.querySelector(box + ' #f_' + k);
    data[k] = f ? f.value : merged[k];
  });
  const r = await api('/api/config', {section: sec, data: {provider: p, providers: {...CFG[sec].providers, [p]: data}}});
  const el = $('#msg_'+sec); if (el) el.textContent = r.ok ? '已保存 ✓' : '失败';
  if (r.ok) {
    CFG[sec].providers = {...CFG[sec].providers, [p]: data};
    if (sec === 'tts') loadVoices(true);   // 保存后自动拉取当前模型下的克隆音色列表
  }
}
async function selectRef(){ const name=$('#refsel').value;
  const r = await api('/api/refs/select', {name}); alert(r.ok?'已设为使用中':'失败'); pages.tts($('#main')); }
async function delRef(){ const name=$('#refsel').value; if(!confirm('删除 '+name+'？')) return;
  await api('/api/refs/delete', {name}); pages.tts($('#main')); }
async function uploadRef(){
  try {
    const file = $('#reffile').files[0];
    if (file){ const b64 = b64Of(await file.arrayBuffer());
      const r = await api('/api/refs/upload', {name: file.name, b64}); alert(r.ok?'上传成功：'+file.name:'失败：'+(r.message||'')); pages.tts($('#main')); return; }
    const name = $('#refname').value, b64 = $('#refb64').value.trim();
    if (!name || !b64) { alert('请填文件名与 base64，或选择本地文件'); return; }
    const r = await api('/api/refs/upload', {name, b64}); alert(r.ok?'上传成功：'+name:'失败：'+(r.message||'')); pages.tts($('#main'));
  } catch(e) { alert('上传异常：'+(e && e.message || e)); }
}

/* ---------- ASR 设置 ---------- */
pages.asr = async el => {
  const c = CFG.asr;
  const names = [...new Set([...Object.keys(PRESETS.asr), ...Object.keys(c.providers||{})])]
    .map(k=>({v:k, label:(PRESETS.asr[k]||{}).name || k}));
  el.innerHTML = `<h2>ASR 设置</h2>
  <div class="card"><h3>供应商</h3>
    ${fields([{k:'provider',label:'当前 ASR 供应商',type:'select',opts:names}], c)}
    <button class="btn primary" onclick="saveSecDyn('asr')">保存并切换</button><span class="hint">支持：硅基流动 / 阿里云百炼（Qwen3-ASR / Fun-ASR，chat+input_audio）/ 火山方舟 / MiMo / 本地 FunASR（WebSocket）/ 本地 OpenAI 兼容（whisper.cpp、faster-whisper-server 等）</span>
  </div>
  <div class="card"><h3>供应商参数（<span id="ap"></span>）</h3><div id="aprov"></div>
    <button class="btn primary" onclick="saveProv('asr')">保存供应商参数</button>
    <button class="btn" onclick="testConn('asr')">测试识别（1 秒静音）</button><span class="msg" id="msg_asr"></span>
  </div>
  <div class="card"><h3>热词与音近纠错</h3>
    <div class="field"><label>热词（逗号分隔）</label><input type="text" id="f_hotwords" value="${esc((c.hotwords||[]).join('，'))}"></div>
    <div class="field"><label>纠错表（JSON：误听 → 标准词）</label><textarea id="f_corrections" style="min-height:160px">${esc(JSON.stringify(c.corrections||{}, null, 1))}</textarea></div>
    <button class="btn primary" onclick="saveAsrLex()">保存词表</button><span class="hint">纠错表在识别后按最长词优先替换</span>
  </div>`;
  renderASRProvider();
  $('#f_provider').onchange = renderASRProvider;
};
function renderASRProvider(){
  const p = $('#f_provider').value;
  $('#ap').textContent = p;
  const merged = mergedProv('asr', p);
  $('#aprov').innerHTML = fields(Object.keys(merged).map(k=>({k,label:(k==='api_key'?'API KEY':k), pw:k==='api_key'})), merged);
}
async function saveAsrLex(){
  let corr; try { corr = JSON.parse($('#f_corrections').value); }
  catch(e){ alert('纠错表 JSON 格式错误'); return; }
  const hotwords = $('#f_hotwords').split(/[，,]/).map(s=>s.trim()).filter(Boolean);
  const r = await api('/api/config', {section:'asr', data:{hotwords, corrections: corr}});
  alert(r.ok?'词表已保存，下一轮识别生效':'失败');
}

/* ---------- BOT 设置 ---------- */
pages.bot = async el => {
  const c = CFG.bot, w = CFG.weather || {}, p = await api('/api/persona');
  el.innerHTML = `<h2>BOT 设置</h2>
  <div class="card"><h3>人设（保存后下一轮对话自动生效）</h3>
    <textarea id="personaText" style="min-height:340px">${esc(p.persona||'')}</textarea>
    <div style="margin-top:10px"><button class="btn primary" onclick="savePersona()">保存人设</button>
    <span class="hint">人设文件：${esc(c.persona_file||'cybot_data/persona.md')}</span></div>
  </div>
  <div class="card"><h3>基础设置</h3>
    ${fields([{k:'name',label:'Bot 名称（人设/内部标识）'},
              {k:'screen_name',label:'硬件屏顶栏名称（留空用 Bot 名称）'},
              {k:'chat_name',label:'对话界面 Bot 署名（留空用 Bot 名称）'},
              {k:'persona_file',label:'人设文件路径'},{k:'expression_dir',label:'表情图目录（预留适配）'}], c)}
    <button class="btn primary" onclick="saveSecDyn('bot')">保存</button>
    <span class="hint">顶栏名称与对话署名可分别设置（如顶栏「洛天依」、对话署名「天依」）；保存后设备屏立即刷新</span>
  </div>
  <div class="card"><h3>身份与作息</h3>
    ${fields([
      {k:'city',label:'BOT 常驻城市（角色叙事：她住在哪）'},
      {k:'timezone',label:'BOT 时区（IANA 名称，如 Asia/Shanghai；作息系统按此时区）'},
      {k:'fixed_image',label:'固定图片（与表情共用屏幕右上显示位，如 cybot_data/fixed_image.png）'}], c)}
    <button class="btn primary" onclick="saveSecDyn('bot')">保存</button>
    <span class="hint">显示优先级：表情图（expressions/表情.png）&gt; 固定图片 &gt; 文字徽章；时区决定作息状态判断时钟</span>
  </div>
  <div class="card"><h3>天气服务（注入对话上下文）</h3>
    ${fields([
      {k:'provider',label:'天气供应商',type:'select',opts:[
        {v:'hfx_mcp',label:'华风爱科天气 MCP'},{v:'qweather',label:'和风天气'},
        {v:'amap',label:'高德地图'},{v:'caiyun',label:'彩云天气'},
        {v:'openweather',label:'OpenWeather'}]},
      {k:'api_key',label:'API KEY / OpenAPI KEY',pw:1},
      {k:'city',label:'天气播报城市（默认取 BOT 常驻城市）'},
      {k:'api_host',label:'和风 API Host（新版账户专属域名，如 https://abc.qweatherapi.com）'},
      {k:'lon',label:'经度（彩云天气必填，如 120.15）'},
      {k:'lat',label:'纬度（彩云天气必填，如 30.27）'},
      {k:'cache_minutes',label:'缓存分钟数（避免刷爆配额）'}], w)}
    <div class="row"><div class="field"><label>MCP 命令（华风爱科：weatherwork-consumer-mcp，需 pip install 且 Python 3.11+）</label><input id="f_mcp_command" type="text" value="${esc(w.mcp_command||'weatherwork-consumer-mcp')}"></div>
    <div class="field"><label>MCP 鉴权环境变量名</label><input id="f_mcp_env_key" type="text" value="${esc(w.mcp_env_key||'WEATHERCN_OPENAPI_KEY')}"></div>
    <div class="field"><label>MCP 工具名（实况=get_current_weather）</label><input id="f_mcp_tool" type="text" value="${esc(w.mcp_tool||'get_current_weather')}"></div>
    <div class="field"><label>MCP 城市参数名（默认 location）</label><input id="f_mcp_arg_city_key" type="text" value="${esc(w.mcp_arg_city_key||'location')}"></div></div>
    <button class="btn primary" onclick="saveWeather()">保存天气配置</button>
    <button class="btn" onclick="testWeather()">测试天气获取</button><span class="msg" id="msg_weather"></span>
    <p class="hint">和风=geoapi+weather/now（新账户用专属域名）；高德=geocode+weatherInfo（城市自动转 adcode）；彩云=realtime（填经纬度）；OpenWeather=/data/2.5/weather；华风爱科=stdio MCP（上方 19 项工具，如 get_current_weather/get_today_weather）。天气随【天气】块注入对话。</p>
  </div>`;
};
async function saveWeather(){
  const w = {}; ['provider','api_key','city','api_host','lon','lat','cache_minutes','mcp_command','mcp_env_key','mcp_tool','mcp_arg_city_key']
    .forEach(k=>{ const f=$('#f_'+k); if(f) w[k]=f.value; });
  w.lon = parseFloat(w.lon)||0; w.lat = parseFloat(w.lat)||0;
  w.cache_minutes = parseInt(w.cache_minutes)||15;
  const r = await api('/api/config', {section:'weather', data:w});
  alert(r.ok?'天气配置已保存（下一轮对话生效）':'失败：'+(r.message||''));
}
async function testWeather(){
  await saveWeather();
  const el = $('#msg_weather'); if (el) el.textContent = '获取中…';
  const r = await api('/api/test/weather');
  if (el){ el.textContent = r.message; el.className = r.ok ? 'msg' : 'msg err'; }
}
async function savePersona(){ const t=$('#personaText').value;
  const r = await api('/api/persona', {persona: t}); alert(r.ok?'人设已保存，下一轮对话生效':'失败'); }

/* ---------- 记忆管理 ---------- */
pages.memory = async el => {
  const s = await api('/api/stats');
  el.innerHTML = `<h2>记忆管理</h2>
  <div class="card"><h3>统计</h3><div class="grid">
    <div class="kpi"><b>${s.short}</b><span>短期（24h）</span></div>
    <div class="kpi"><b>${s.mid}</b><span>中期（每日总结）</span></div>
    <div class="kpi"><b>${s.long}</b><span>长期</span></div>
    <div class="kpi"><b>${s.key}</b><span>核心⭐（永不遗忘）</span></div></div>
    <div style="margin-top:10px">
      <button class="btn" onclick="consolidate('daily')">执行每日整合</button>
      <button class="btn" onclick="consolidate('monthly')">执行 30 天整合</button>
      <button class="btn" onclick="doExport()">导出 v2 契约 JSON</button>
      <button class="btn" onclick="importDlg.showModal()">导入 JSON</button>
      <button class="btn primary" onclick="addDlg.showModal()">＋ 手动写入长期记忆</button>
    </div></div>
  <div class="card"><h3>浏览 / 编辑</h3>
    <div class="toolbar"><select id="mlayer" onchange="loadMem()"><option value="short">短期</option><option value="mid">中期</option><option value="long">长期</option></select>
    <input type="text" id="mq" placeholder="搜索…" style="width:200px" oninput="clearTimeout(window._mqT);window._mqT=setTimeout(loadMem,300)">
    <button class="btn" onclick="loadMem()">刷新</button></div>
    <div id="mlist"></div></div>
  <dialog id="addDlg"><form onsubmit="addLongE(event)"><h3 style="margin-top:0">手动写入长期记忆</h3>
    <textarea id="addText"></textarea><label><input type="checkbox" id="addKey" checked> 标记为核心记忆</label>
    <div style="margin-top:10px;text-align:right"><button class="btn" type="button" onclick="addDlg.close()">取消</button><button class="btn primary" type="submit">写入</button></div></form></dialog>
  <dialog id="importDlg"><form onsubmit="doImportE(event)"><h3 style="margin-top:0">导入契约 v2 JSON</h3>
    <textarea id="importText" style="min-height:200px"></textarea>
    <div style="margin-top:10px;text-align:right"><button class="btn" type="button" onclick="importDlg.close()">取消</button><button class="btn primary" type="submit">导入</button></div></form></dialog>`;
  loadMem();
};
async function loadStats(){ /* 由 loadMem 顺带刷新 */ }
async function loadMem(){
  const layer = $('#mlayer').value, q = $('#mq').value.trim();
  const items = await api('/api/list?layer='+layer+'&q='+encodeURIComponent(q));
  const el = $('#mlist');
  if (!items.length){ el.innerHTML='<div class="hint" style="padding:14px">该层暂无记忆</div>'; return; }
  let head = layer==='short' ? '<tr><th>时间</th><th>角色</th><th>内容</th><th></th></tr>'
    : '<tr><th>日期</th><th>标签</th><th>内容</th><th>提及天数</th><th></th></tr>';
  el.innerHTML = '<table>'+head+items.map(it=>{
    const del = `<button class="btn small warn" onclick="delMem('${it.id}')">删除</button>`;
    if (layer==='short') return `<tr><td class="c">${esc(fmt(it.ts))}</td><td class="c">${it.role==='user'?'用户':'天依'}</td><td class="content-cell">${esc(it.content)}</td><td>${del}</td></tr>`;
    const tag = layer==='long' ? `<span class="tag ${it.isKey?'t-key':'t-long'}">${it.isKey?'⭐核心':'长期'}</span><span class="tag t-deg">${esc(it.source||'')}</span>`
      : `<span class="tag t-mid">中期</span>${it.degraded?'<span class="tag t-deg">降级</span>':''}`;
    const extra = layer==='long' ? `<button class="btn small" onclick="toggleKey('${it.id}')">${it.isKey?'取消核心':'设为核心'}</button>` : '';
    const date = layer==='long' ? (it.lastMentioned?fmt(it.lastMentioned):'-') : (it.dateKey||'-');
    return `<tr><td class="c">${date}</td><td>${tag}</td><td class="content-cell">${esc(it.summary||it.content)}</td><td class="c">${(it.mentionDates||[]).length}</td><td>${del} ${extra}</td></tr>`;
  }).join('')+'</table>';
}
function fmt(ms){ if(!ms) return '-'; const d=new Date(ms); const p=n=>String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`; }
async function delMem(id){ if(!confirm('删除这条记忆？')) return; await api('/api/delete',{layer:$('#mlayer').value,id}); loadMem(); }
async function toggleKey(id){ await api('/api/toggleKey',{id}); loadMem(); }
async function addLongE(e){ e.preventDefault();
  await api('/api/add',{content:$('#addText').value,isKey:$('#addKey').checked}); addDlg.close(); loadMem(); }
async function doImportE(e){ e.preventDefault();
  try{ const d=JSON.parse($('#importText').value); const r=await api('/api/import',d);
    alert(r.ok?'导入成功（LWW 合并）':'拒绝：'+(r.message||'')); importDlg.close(); loadMem(); }
  catch(err){ alert('JSON 解析失败'); } }
async function consolidate(mode){ if(!confirm(mode==='daily'?'执行每日整合？':'执行 30 天整合？')) return;
  const r = await api('/api/consolidate',{mode}); alert(r.ok?'整合完成':'失败'); }

/* ---------- 服务端设置 ---------- */
pages.server = async el => {
  const c = CFG.server, d = CFG.device;
  el.innerHTML = `<h2>服务端设置</h2>
  <div class="card"><h3>Web 服务</h3>
    ${fields([{k:'ui_port',label:'控制台端口（重启后生效）'},{k:'log_buffer',label:'终端日志缓冲条数'},{k:'auto_open_browser',label:'启动时自动打开浏览器',type:'select',opts:['true','false']}], c)}
    <button class="btn primary" onclick="saveSecDyn('server')">保存</button>
  </div>
  <div class="card"><h3>设备连接</h3>
    ${fields([{k:'ip',label:'设备 IP（留空或失联时自动发现）'},{k:'port',label:'端口'},{k:'client_name',label:'PC 显示名称'},{k:'last_device_id',label:'设备 ID（自动记录）'},{k:'auto_discover',label:'自动发现',type:'select',opts:['true','false']},{k:'reconnect_interval',label:'重连间隔（秒）'}], d)}
    <button class="btn primary" onclick="saveSecDyn('device')">保存</button>
    <span class="hint">设备 ID 不随 DHCP 换 IP 变化，自动发现按 ID 锁定设备</span>
  </div>`;
};

/* ---------- 终端 ---------- */
pages.term = async el => {
  el.innerHTML = `<h2>终端</h2>
  <div class="toolbar"><button class="btn" onclick="termAuto=!termAuto; this.textContent=termAuto?'自动滚动：开':'自动滚动：关'">自动滚动：开</button>
  <span class="hint">实时滚动日志：连接 / 识别 / 记忆 / 播放 / 错误，以及每次云端 API 调用的状态、耗时与发送、接收内容（含天气请求与返回）</span></div><div id="term"></div>`;
  termSince = 0; $('#term').innerHTML='';
  if (!termTimer) termTimer = setInterval(pollTerm, 1200);
};
let termSince = 0, termAuto = true, termTimer = null;
async function pollTerm(){
  const el = $('#term');
  if (el){
    const r = await api('/api/logs?since='+termSince);
    for (const line of r.logs){
      const t = new Date(line.ts*1000).toTimeString().slice(0,8);
      el.textContent += `[${t}] ${line.msg}\\n`;
      termSince = line.seq;
    }
    if (r.logs.length && termAuto) el.scrollTop = el.scrollHeight;
  }
}

/* ---------- 路由 ---------- */
async function show(page){
  document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on', b.dataset.p===page));
  const el = $('#main'); el.innerHTML = '<div class="hint" style="padding:20px">加载中…</div>';
  try { await pages[page](el); } catch(e){ el.innerHTML = '<div class="card err">加载失败：'+esc(String(e))+'</div>'; }
}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>show(b.dataset.p));

async function saveSecDyn(sec){
  const box = {tts:'#tprov', asr:'#aprov'}[sec];
  const data = {};
  // 供应商下拉在参数容器之外，必须一并收集——否则「保存并切换」不会真正切换 provider
  const pf = document.querySelector('#f_provider');
  if (pf && (sec === 'tts' || sec === 'asr')) data['provider'] = pf.value;
  const scope = box ? document.querySelectorAll(box + ' [id^=f_]') : document.querySelectorAll('[id^=f_]');
  scope.forEach(f => data[f.id.slice(2)] = f.value);
  const r = await api('/api/config', {section: sec, data});
  if (r.ok && (sec === 'tts' || sec === 'asr')) CFG[sec].provider = data['provider'];
  alert(r.ok ? '已保存并切换 ✓（下一轮对话生效）' : '失败：'+(r.message||''));
}
function collectKeys2(){ return [...document.querySelectorAll('[id^=f_]')].map(x=>x.id.slice(2)); }
async function testConn(kind){
  const el = $('#msg_'+kind); if (el) el.textContent = '测试中…';
  const r = await api('/api/test/'+kind);
  if (el){ el.textContent = r.message; el.className = r.ok ? 'msg' : 'msg err'; }
}
async function init(){ const r = await api('/api/config'); CFG = r.config; PRESETS = {tts: r.tts_presets||{}, asr: r.asr_presets||{}}; await show('overview'); }
init();
</script></body></html>"""


# ============================ 启动 ============================

def start(vault, cfg, bot=None, on_config_changed=None, port=None, daemon=True):
    """启动控制台。bot 为 TianyiBot 实例（独立模式传 None）。"""
    global _VAULT, _CFG, _BOT, _ON_CONFIG_CHANGED, _PORT, _STARTED_AT
    _VAULT, _CFG, _BOT = vault, cfg, bot
    _ON_CONFIG_CHANGED = on_config_changed
    _PORT = port or int(cfg.get("server", {}).get("ui_port", 8321))
    _STARTED_AT = time.time()
    # 绑定 0.0.0.0：允许局域网其他设备访问控制台（Linux 服务器等无本机浏览器的场景）
    server = ThreadingHTTPServer(("0.0.0.0", _PORT), _Handler)
    threading.Thread(target=server.serve_forever, daemon=daemon).start()
    logbus.push("控制台已启动：http://127.0.0.1:%d（八模块，局域网可访问）" % _PORT)
    return server


def standalone():
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    import memory_vault as mv
    vault = mv.MemoryVault(cfg, lambda p: None, embedder=providers.build_embed(cfg))
    start(vault, cfg)
    print("控制台运行中：http://127.0.0.1:%d，Ctrl+C 退出" % cfg.get("server", {}).get("ui_port", 8321))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    standalone()
