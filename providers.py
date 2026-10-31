#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端服务多供应商适配层（ASR / TTS / LLM / 嵌入 / 重排）

所有适配器从 config.json 的分区读取参数，WebUI 修改配置后下一轮对话自动生效。

ASR（统一 OpenAI 兼容 /audio/transcriptions + 音近纠错在上层）：
  siliconflow 硅基流动 | bailian 阿里云百炼 | volcengine 火山方舟 |
  mimo 小米 MiMo | local 本地（whisper.cpp / faster-whisper-server / FunASR 等
  任何提供 OpenAI 兼容转录接口的服务）

TTS：
  mimo 小米 MiMo 音色克隆（chat/completions + DataURL 参考音频）
  doubao 豆包TTS（火山 openspeech HTTP API，appid/token/cluster）
  bailian / cosyvoice / qwen 阿里云百炼（OpenAI 兼容 /audio/speech）
  local 本地 OpenAI 兼容语音服务（CosyVoice local / fish-speech 等）
  gptsovits GPT-SoVITS 本地 API（api_v2 /tts，支持参考音频）

LLM / 嵌入 / 重排：统一 OpenAI 兼容端点（base_url + api_key + model）。
"""

import base64
import json
import struct
import time
import uuid

import logbus
import requests

TIMEOUT = 60


def _pcm_to_wav(pcm, rate=16000, channels=1, bits=16):
    """裸 PCM → 标准 WAV（小端 RIFF 头），供 PyAV 解码。"""
    hdr = (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " +
           struct.pack("<IHHIIHH", 16, 1, channels, rate,
                       rate * channels * bits // 8, channels * bits // 8, bits) +
           b"data" + struct.pack("<I", len(pcm)))
    return hdr + bytes(pcm)


def _wrap_call(kind, obj, meth, req_fn, resp_fn=lambda r: str(r)[:800]):
    """给适配器方法包一层 API 调用追踪（状态/耗时/发送与接收内容 → 终端模块）。"""
    orig = getattr(obj, meth)

    def wrapped(*a, **k):
        t0 = time.monotonic()
        try:
            req = req_fn(obj, *a, **k)
        except Exception:
            req = ""
        try:
            r = orig(*a, **k)
            logbus.push_api(kind, req,
                            ("音频 %d 字节" % len(r)) if isinstance(r, bytes) else resp_fn(r),
                            True, int((time.monotonic() - t0) * 1000))
            return r
        except Exception as e:
            logbus.push_api(kind, req, repr(e), False,
                            int((time.monotonic() - t0) * 1000))
            raise
    setattr(obj, meth, wrapped)
    return obj


def _log(msg):
    try:
        import logbus
        logbus.push(msg)
    except Exception:
        pass


# ============================ 预设（可在 WebUI 修改） ============================
# 官方文档核对（2026-10）：
# - 硅基流动 ASR/TTS：OpenAI 兼容 transcriptions（仅 file+model）
# - 百炼 Qwen-ASR（录音文件）：chat/completions + input_audio（qwen3-asr-flash / fun-asr-flash）
# - 百炼 Qwen-TTS（非实时）：POST /api/v1/services/aigc/multimodal-generation/generation，
#   body {model, input:{text, voice, language_type}} → output.audio.url（24h 有效，需下载）
# - 百炼 CosyVoice（非实时 HTTP）：POST /api/v1/services/audio/tts/SpeechSynthesizer，
#   body {model, input:{text, voice, format, sample_rate}} → output.audio.url（需下载）
# - 百炼音色克隆：POST /api/v1/services/audio/tts/customization，
#   {model:"voice-enrollment", input:{action:"create_voice", target_model, prefix, audio:DataURL}}
#   → output.voice_id；target_model 必须与合成模型一致
# - MiMo TTS：chat/completions + audio.voice=DataURL（voiceclone 模型必填）
# - 豆包 TTS：openspeech.bytedance.com/api/v1/tts，Bearer;{token} 分号格式，code==3000
# - 本地引擎：GPT-SoVITS api_v2（/tts）、FunASR（WebSocket 2pass）、
#   GLM-TTS / VoxCPM / Qwen3-TTS 本地部署普遍走 OpenAI 兼容 /v1/audio/speech

ASR_PRESETS = {
    "siliconflow": {"name": "硅基流动", "base_url": "https://api.siliconflow.cn/v1",
                    "model": "FunAudioLLM/SenseVoiceSmall"},
    "bailian": {"name": "阿里云百炼（Qwen/Fun-ASR）",
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "model": "qwen3-asr-flash"},
    "volcengine": {"name": "火山方舟", "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                   "model": "doubao-seed-asr"},
    "mimo": {"name": "小米 MiMo", "base_url": "https://api.xiaomimimo.com/v1",
             "model": "mimo-v2.5-asr"},
    "funasr": {"name": "本地 FunASR（WebSocket）", "base_url": "ws://127.0.0.1:10095",
               "model": "2pass"},
    "local": {"name": "本地 ASR（OpenAI 兼容）", "base_url": "http://127.0.0.1:1224/v1",
              "model": "whisper-1"},
}

TTS_PRESETS = {
    "mimo": {"name": "小米 MiMo（音色克隆）", "base_url": "https://api.xiaomimimo.com/v1",
             "model": "mimo-v2.5-tts-voiceclone", "format": "wav"},
    "doubao": {"name": "豆包 TTS（火山）", "cluster": "volcano_tts",
               "voice_type": "zh_female_cancan_mars_bigtts", "encoding": "mp3"},
    "bailian": {"name": "百炼 Qwen-TTS（非实时 HTTP）",
                "base_url": "https://dashscope.aliyuncs.com/api/v1",
                "model": "qwen3-tts-flash", "voice": "Cherry", "language_type": "Chinese"},
    "cosyvoice_v35": {"name": "CosyVoice v3.5（百炼 WebSocket，支持克隆）",
                      "base_url": "https://dashscope.aliyuncs.com/api/v1",
                      "model": "cosyvoice-v3.5-flash", "voice": "longxiaochun",
                      "sample_rate": 16000, "volume": 50, "rate": 1.0, "pitch": 1.0},
    "qwen_audio_tts": {"name": "Qwen-Audio-TTS（百炼 WebSocket，支持克隆）",
                       "base_url": "https://dashscope.aliyuncs.com/api/v1",
                       "model": "qwen-audio-3.1-tts-plus", "voice": "longanlingxi",
                       "sample_rate": 16000, "volume": 50, "rate": 1.0, "pitch": 1.0},
    "qwen3_realtime": {"name": "Qwen3-TTS-Realtime（百炼 WebSocket，系统音色）",
                       "base_url": "https://dashscope.aliyuncs.com/api/v1",
                       "model": "qwen3-tts-flash-realtime", "voice": "Cherry",
                       "language_type": "Chinese", "sample_rate": 16000,
                       "volume": 50, "speech_rate": 1.0, "pitch_rate": 1.0},
    "gptsovits": {"name": "GPT-SoVITS v2（本地 api_v2）", "base_url": "http://127.0.0.1:9880",
                  "text_lang": "zh", "prompt_lang": "zh"},
    "glm_tts": {"name": "GLM-TTS（本地 OpenAI 兼容）", "base_url": "http://127.0.0.1:8000/v1",
                "model": "glm-tts", "voice": "default", "format": "wav"},
    "voxcpm": {"name": "VoxCPM（本地 OpenAI 兼容）", "base_url": "http://127.0.0.1:8001/v1",
               "model": "voxcpm", "voice": "default", "format": "wav"},
    "qwen3_tts_local": {"name": "Qwen3-TTS（本地 OpenAI 兼容）", "base_url": "http://127.0.0.1:5048/v1",
                        "model": "qwen3-tts", "voice": "default", "format": "wav"},
    "local": {"name": "自定义本地 TTS（OpenAI 兼容）", "base_url": "http://127.0.0.1:9880/v1",
              "model": "default", "voice": "default", "format": "wav"},
}


# ============================ ASR ============================

class OpenAICompatASR:
    """OpenAI 兼容转录接口：硅基流动 / 火山方舟 / MiMo / 本地（whisper.cpp 等）。"""

    def __init__(self, base_url, api_key, model, timeout=60):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.model = model
        self.timeout = timeout

    def transcribe(self, wav_bytes):
        r = requests.post(
            self.base + "/audio/transcriptions",
            headers={"Authorization": "Bearer " + self.key},
            files={"file": ("audio.wav", wav_bytes, "audio/wav")},
            data={"model": self.model}, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("text", "")


class BailianASR:
    """阿里云百炼录音文件识别（qwen3-asr-flash / fun-asr-flash）：

    OpenAI 兼容 chat/completions + input_audio（官方 qwen-asr-api-reference）。
    """

    def __init__(self, base_url, api_key, model="qwen3-asr-flash", timeout=60):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.model = model
        self.timeout = timeout

    def transcribe(self, wav_bytes):
        b64 = base64.b64encode(wav_bytes).decode()
        r = requests.post(
            self.base + "/chat/completions",
            headers={"Authorization": "Bearer " + self.key,
                     "Content-Type": "application/json"},
            json={"model": self.model, "messages": [{"role": "user", "content": [
                {"type": "input_audio",
                 "input_audio": {"data": "data:audio/wav;base64," + b64, "format": "wav"}},
                {"type": "text", "text": "把音频内容转写为文字"}]}]},
            timeout=self.timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


class FunASRWS:
    """FunASR 在线识别服务（funasr-wss-server，WebSocket 2pass 协议）。

    base_url 形如 ws://127.0.0.1:10095。依赖 websocket-client（pip install websocket-client）。
    """

    def __init__(self, base_url, mode="2pass", timeout=30):
        self.base = base_url
        self.mode = mode
        self.timeout = timeout

    def transcribe(self, wav_bytes):
        import websocket  # websocket-client
        url = self.base if self.base.startswith("ws") else "ws://" + self.base
        ws = websocket.create_connection(url, timeout=self.timeout)
        try:
            ws.send(json.dumps({"mode": self.mode, "chunk_size": [5, 10, 5],
                                "chunk_interval": 10, "wav_name": "cybot",
                                "wav_format": "pcm", "is_speaking": True,
                                "audio_fs": 16000, "itn": True}))
            chunk = 9600                       # 300ms @16k s16
            for i in range(0, len(wav_bytes), chunk):
                ws.send_binary(wav_bytes[i:i + chunk])
                time.sleep(0.03)
            ws.send(json.dumps({"is_speaking": False}))
            texts = []
            end = time.monotonic() + self.timeout
            while time.monotonic() < end:
                try:
                    msg = ws.recv()
                except Exception:
                    break
                if not msg:
                    continue
                try:
                    data = json.loads(msg)
                except ValueError:
                    continue
                if data.get("is_final") or not data.get("is_speaking", True):
                    if data.get("text2") or data.get("text"):
                        texts.append(data.get("text2") or data.get("text"))
                    break
                if data.get("text2") or data.get("text"):
                    texts.append(data.get("text2") or data.get("text"))
            return "".join(texts).strip()
        finally:
            try:
                ws.close()
            except Exception:
                pass


# ============================ TTS ============================

class OpenAICompatTTS:
    """OpenAI 兼容 /audio/speech：百炼（qwen-tts / cosyvoice-v2）/ 本地语音服务。"""

    def __init__(self, base_url, api_key, model, voice="", audio_format="wav", timeout=120):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.model = model
        self.voice = voice
        self.audio_format = audio_format
        self.timeout = timeout

    def tts(self, text):
        r = requests.post(
            self.base + "/audio/speech",
            headers={"Authorization": "Bearer " + self.key,
                     "Content-Type": "application/json"},
            json={"model": self.model, "input": text, "voice": self.voice,
                  "response_format": self.audio_format},
            timeout=self.timeout)
        r.raise_for_status()
        if r.headers.get("Content-Type", "").startswith("application/json"):
            data = r.json()
            b64 = (data.get("audio") or {}).get("data") or data.get("data")
            if b64:
                return base64.b64decode(b64)
            raise ValueError("响应中无音频数据")
        return r.content


class MiMoTTS:
    """小米 MiMo 音色克隆：chat/completions + audio.voice=DataURL 参考音频。"""

    def __init__(self, base_url, api_key, model, voice_ref_b64=None,
                 audio_format="wav", timeout=120):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.model = model
        self.voice_ref_b64 = voice_ref_b64
        self.audio_format = audio_format
        self.timeout = timeout

    def tts(self, text):
        body = {"model": self.model,
                "messages": [{"role": "assistant", "content": text}],
                "audio": {"format": self.audio_format}}
        if self.voice_ref_b64:
            body["audio"]["voice"] = "data:audio/wav;base64," + self.voice_ref_b64
        r = requests.post(
            self.base + "/chat/completions",
            headers={"api-key": self.key, "Content-Type": "application/json"},
            json=body, timeout=self.timeout)
        r.raise_for_status()
        audio = r.json()["choices"][0]["message"].get("audio") or {}
        data = audio.get("data")
        if not data:
            raise ValueError("MiMo 响应中没有音频数据")
        return base64.b64decode(data)


class DoubaoTTS:
    """豆包 TTS（火山引擎 openspeech HTTP API，单次合成）。"""

    def __init__(self, appid, token, cluster="volcano_tts",
                 voice_type="zh_female_cancan_mars_bigtts",
                 encoding="mp3", speed_ratio=1.0, timeout=60):
        self.appid = appid
        self.token = token
        self.cluster = cluster
        self.voice_type = voice_type
        self.encoding = encoding
        self.speed_ratio = speed_ratio
        self.timeout = timeout

    def tts(self, text):
        reqid = uuid.uuid4().hex
        r = requests.post(
            "https://openspeech.bytedance.com/api/v1/tts",
            headers={"Authorization": "Bearer;" + self.token,
                     "Content-Type": "application/json"},
            json={
                "app": {"appid": self.appid, "token": "access_token",
                        "cluster": self.cluster},
                "user": {"uid": "cybot_tianyi"},
                "audio": {"voice_type": self.voice_type, "encoding": self.encoding,
                          "speed_ratio": self.speed_ratio},
                "request": {"reqid": reqid, "text": text,
                            "text_type": "plain", "operation": "query"},
            }, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 3000:
            raise ValueError("豆包 TTS 失败：code=%s %s" % (data.get("code"), data.get("message")))
        return base64.b64decode(data["data"])


class GPTSoVITS_TTS:
    """GPT-SoVITS 本地 api_v2（/tts，支持参考音频与提示文本）。"""

    def __init__(self, base_url, text_lang="zh", prompt_lang="zh",
                 ref_audio_path="", prompt_text="", speed_factor=1.0, timeout=120):
        self.base = base_url.rstrip("/")
        self.text_lang = text_lang
        self.prompt_lang = prompt_lang
        self.ref_audio_path = ref_audio_path
        self.prompt_text = prompt_text
        self.speed_factor = speed_factor
        self.timeout = timeout

    def tts(self, text):
        r = requests.post(
            self.base + "/tts",
            json={"text": text, "text_lang": self.text_lang,
                  "ref_audio_path": self.ref_audio_path,
                  "prompt_text": self.prompt_text, "prompt_lang": self.prompt_lang,
                  "speed_factor": self.speed_factor, "media_type": "wav",
                  "streaming_mode": False},
            timeout=self.timeout)
        r.raise_for_status()
        if r.headers.get("Content-Type", "").startswith("application/json"):
            raise ValueError("GPT-SoVITS 错误：" + r.text[:200])
        return r.content


class BailianQwenTTS:
    """百炼 Qwen-TTS 非实时 HTTP（qwen-tts-api）：

    POST {base}/services/aigc/multimodal-generation/generation
    body {model, input:{text, voice, language_type}} → output.audio.url → 下载音频。
    """

    def __init__(self, base_url, api_key, model, voice, language_type="Chinese", timeout=120):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.model = model
        self.voice = voice
        self.language_type = language_type
        self.timeout = timeout

    def tts(self, text):
        r = requests.post(
            self.base + "/services/aigc/multimodal-generation/generation",
            headers={"Authorization": "Bearer " + self.key,
                     "Content-Type": "application/json"},
            json={"model": self.model,
                  "input": {"text": text[:600], "voice": self.voice,
                            "language_type": self.language_type}},
            timeout=self.timeout)
        r.raise_for_status()
        out = r.json().get("output", {})
        audio = out.get("audio") or {}
        url = audio.get("url")
        if not url:
            data = audio.get("data")
            if data:
                return base64.b64decode(data)
            raise ValueError("Qwen-TTS 响应无音频（%s）" % json.dumps(out)[:200])
        dl = requests.get(url, timeout=self.timeout)
        dl.raise_for_status()
        return dl.content


DASHSCOPE_WS = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
DASHSCOPE_RT = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"


class BailianDuplexTTS:
    """百炼 duplex WebSocket TTS（官方 qwen-audio-tts / cosyvoice client-server events）：

    wss://dashscope.aliyuncs.com/api-ws/v1/inference，Bearer 鉴权。
    run-task → task-started → continue-task(text) → finish-task → binary 音频帧 → task-finished。
    适用于 qwen-audio-3.1-tts-plus/flash、qwen-audio-3.0-tts-plus/flash、
    cosyvoice-v3.5-plus/flash；复刻音色的 target_model 须与 model 一致。
    输出 PCM 16k 单声道 16bit → 包 WAV 头返回（与设备播放链路一致）。
    """

    def __init__(self, api_key, model, voice, sample_rate=16000,
                 volume=50, rate=1.0, pitch=1.0, timeout=120):
        self.key = api_key
        self.model = model
        self.voice = voice
        self.sample_rate = int(sample_rate)
        self.volume = int(volume)
        self.rate = float(rate)
        self.pitch = float(pitch)
        self.timeout = timeout

    def tts(self, text):
        import websocket  # websocket-client
        task_id = str(uuid.uuid4())
        ws = websocket.create_connection(
            DASHSCOPE_WS, header=["Authorization: Bearer " + self.key],
            timeout=self.timeout)
        try:
            ws.send(json.dumps({
                "header": {"action": "run-task", "task_id": task_id,
                           "streaming": "duplex"},
                "payload": {"task_group": "audio", "task": "tts",
                            "function": "SpeechSynthesizer", "model": self.model,
                            "input": {},
                            "parameters": {"text_type": "PlainText",
                                           "voice": self.voice,
                                           "format": "pcm",
                                           "sample_rate": self.sample_rate,
                                           "volume": self.volume,
                                           "rate": self.rate,
                                           "pitch": self.pitch}}}))
            deadline = time.monotonic() + self.timeout
            while True:                                   # 等 task-started
                msg = ws.recv()
                if isinstance(msg, bytes):
                    continue
                h = (json.loads(msg).get("header") or {})
                if h.get("event") == "task-started":
                    break
                if h.get("event") == "task-failed":
                    raise ValueError("DuplexTTS task-failed：%s %s" %
                                     (h.get("error_code"), h.get("error_message")))
                if time.monotonic() > deadline:
                    raise ValueError("DuplexTTS 等待 task-started 超时")
            ws.send(json.dumps({
                "header": {"action": "continue-task", "task_id": task_id,
                           "streaming": "duplex"},
                "payload": {"input": {"text": str(text)}}}))
            ws.send(json.dumps({
                "header": {"action": "finish-task", "task_id": task_id,
                           "streaming": "duplex"},
                "payload": {"input": {}}}))
            pcm = bytearray()
            while True:                                   # 收 binary 音频帧
                msg = ws.recv()
                if isinstance(msg, bytes):
                    pcm.extend(msg)
                    continue
                h = (json.loads(msg).get("header") or {})
                if h.get("event") == "task-finished":
                    break
                if h.get("event") == "task-failed":
                    raise ValueError("DuplexTTS task-failed：%s %s" %
                                     (h.get("error_code"), h.get("error_message")))
            if not pcm:
                raise ValueError("DuplexTTS 未收到音频数据")
            return _pcm_to_wav(pcm, self.sample_rate)
        finally:
            try:
                ws.close()
            except Exception:
                pass


class BailianRealtimeTTS:
    """百炼 Qwen3-TTS Realtime WebSocket（官方 qwen-tts-realtime events）：

    wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3-tts-flash-realtime。
    session.update → session.updated → input_text_buffer.append（server_commit 自动合成）
    → response.audio.delta(base64) × N → response.done → session.finish。
    仅支持系统音色（不支持声音复刻）。输出 PCM → 包 WAV 头返回。
    """

    def __init__(self, api_key, model="qwen3-tts-flash-realtime", voice="Cherry",
                 language_type="Chinese", sample_rate=16000, volume=50,
                 speech_rate=1.0, pitch_rate=1.0, timeout=120):
        self.key = api_key
        self.model = model
        self.voice = voice
        self.language_type = language_type
        self.sample_rate = int(sample_rate)
        self.volume = int(volume)
        self.speech_rate = float(speech_rate)
        self.pitch_rate = float(pitch_rate)
        self.timeout = timeout

    def tts(self, text):
        import websocket  # websocket-client
        url = "%s?model=%s" % (DASHSCOPE_RT, self.model)
        ws = websocket.create_connection(
            url, header=["Authorization: Bearer " + self.key],
            timeout=self.timeout)
        eid = [0]

        def next_id():
            eid[0] += 1
            return "event-%d" % eid[0]

        def send(obj):
            ws.send(json.dumps(obj))

        def recv_event():
            msg = ws.recv()
            return json.loads(msg) if not isinstance(msg, bytes) else {}

        try:
            deadline = time.monotonic() + self.timeout
            while True:                                   # session.created
                ev = recv_event()
                if ev.get("type") == "session.created":
                    break
                if ev.get("type") == "error":
                    raise ValueError("RealtimeTTS：%s" %
                                     json.dumps(ev.get("error", {}), ensure_ascii=False)[:200])
                if time.monotonic() > deadline:
                    raise ValueError("RealtimeTTS 等待 session.created 超时")
            send({"event_id": next_id(), "type": "session.update",
                  "session": {"voice": self.voice,
                              "mode": "server_commit",
                              "language_type": self.language_type,
                              "response_format": "pcm",
                              "sample_rate": self.sample_rate,
                              "volume": self.volume,
                              "speech_rate": self.speech_rate,
                              "pitch_rate": self.pitch_rate}})
            while True:                                   # session.updated
                ev = recv_event()
                if ev.get("type") == "session.updated":
                    break
                if ev.get("type") == "error":
                    raise ValueError("RealtimeTTS：%s" %
                                     json.dumps(ev.get("error", {}), ensure_ascii=False)[:200])
                if time.monotonic() > deadline:
                    raise ValueError("RealtimeTTS 等待 session.updated 超时")
            send({"event_id": next_id(), "type": "input_text_buffer.append",
                  "text": str(text)})
            pcm = bytearray()
            finished = False
            while not finished:                           # response.audio.delta
                ev = recv_event()
                et = ev.get("type")
                if et == "response.audio.delta":
                    pcm.extend(base64.b64decode(ev.get("delta") or ""))
                elif et == "response.done":
                    resp = ev.get("response") or {}
                    if resp.get("status") not in ("completed", "in_progress"):
                        raise ValueError("RealtimeTTS response 状态异常：%s" % resp.get("status"))
                    finished = True
                elif et == "error":
                    raise ValueError("RealtimeTTS：%s" %
                                     json.dumps(ev.get("error", {}), ensure_ascii=False)[:200])
                elif et == "session.finished":
                    finished = True
                if time.monotonic() > deadline:
                    raise ValueError("RealtimeTTS 等待音频超时")
            send({"event_id": next_id(), "type": "session.finish"})
            if not pcm:
                raise ValueError("RealtimeTTS 未收到音频数据")
            return _pcm_to_wav(pcm, self.sample_rate)
        finally:
            try:
                ws.close()
            except Exception:
                pass


def _bailian_json(r, action):
    """解析百炼响应：非 200 时透出响应体中的 code/message（裸 raise_for_status 会吞掉原因）。"""
    if r.status_code != 200:
        try:
            err = r.json()
            detail = "%s: %s" % (err.get("code", ""), err.get("message", ""))
        except ValueError:
            detail = r.text[:200]
        raise ValueError("百炼%s失败（HTTP %d）：%s" % (action, r.status_code, detail.strip()))
    return r.json()


def bailian_voice_clone(base_url, api_key, target_model, audio_b64_dataurl,
                        prefix="cybot", language_hints=None, timeout=120):
    """百炼声音复刻（voice-enrollment HTTP API）：参考音频 → 音色 ID。

    audio_b64_dataurl 形如 data:audio/wav;base64,...；返回 output.voice_id。
    target_model 必须与后续合成使用的模型一致（如 cosyvoice-v3.5-flash / qwen-audio-3.1-tts-plus）。
    """
    body = {"model": "voice-enrollment",
            "input": {"action": "create_voice", "target_model": target_model,
                      "prefix": prefix or "cybot",
                      "audio": audio_b64_dataurl}}
    if language_hints:
        body["input"]["language_hints"] = language_hints[:1]
    base = base_url.rstrip("/")
    if base.endswith("/api/v1"):
        url = base + "/services/audio/tts/customization"
    else:
        url = base + "/api/v1/services/audio/tts/customization"
    r = requests.post(
        url,
        headers={"Authorization": "Bearer " + api_key,
                 "Content-Type": "application/json"},
        json=body, timeout=timeout)
    return _bailian_json(r, "音色克隆").get("output", {}).get("voice_id", "")


def bailian_voice_list(base_url, api_key, page_size=100, timeout=60):
    """百炼声音复刻音色列表（voice-enrollment HTTP API，action=list_voice）。

    返回 [{voice_id, gmt_create, gmt_modified, status}]；
    voice_id 形如 {target_model}-{prefix}-{唯一标识}，前缀即绑定模型。
    """
    body = {"model": "voice-enrollment",
            "input": {"action": "list_voice", "page_index": 0, "page_size": int(page_size)}}
    base = base_url.rstrip("/")
    url = base + "/services/audio/tts/customization" \
        if base.endswith("/api/v1") else base + "/api/v1/services/audio/tts/customization"
    r = requests.post(url, headers={"Authorization": "Bearer " + api_key,
                                    "Content-Type": "application/json"},
                      json=body, timeout=timeout)
    return _bailian_json(r, "音色列表查询").get("output", {}).get("voice_list") or []


# ============================ LLM / 嵌入 / 重排 ============================

class OpenAIChat:
    def __init__(self, base_url, api_key, model, temperature=0.8, max_tokens=300, timeout=60,
                 thinking="default", extra_body=""):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.thinking = (thinking or "default").lower()
        self.extra_body = extra_body or ""

    @staticmethod
    def _apply_thinking(payload, base, model, enable):
        """按模型/端点协议注入思考开关（关闭思考可显著降低延迟与 token 消耗）。"""
        m = (model or "").lower()
        b = (base or "").lower()
        if "glm" in m:
            payload["thinking"] = {"type": "enabled" if enable else "disabled"}
        elif "deepseek" in m and "api.deepseek.com" in b:
            # DeepSeek 官方 API 严格校验参数且无思考开关：按模型名区分，reasoner→chat 即关闭
            if not enable and "reasoner" in m:
                payload["model"] = m.replace("reasoner", "chat")
        elif "dashscope.aliyuncs.com" in b:
            payload["enable_thinking"] = enable     # DashScope 兼容协议
        else:
            # vLLM / SGLang 自托管端点与多数聚合网关：双写两种常见协议
            payload["enable_thinking"] = enable
            payload["chat_template_kwargs"] = {"enable_thinking": enable}

    def chat(self, messages, system=None):
        payload = {"model": self.model,
                   "messages": ([{"role": "system", "content": system}] if system else []) + messages,
                   "temperature": self.temperature, "max_tokens": self.max_tokens}
        if self.thinking in ("off", "on"):
            self._apply_thinking(payload, self.base, self.model, self.thinking == "on")
        if self.extra_body:
            try:
                payload.update(json.loads(self.extra_body))
            except ValueError:
                _log("[警告] LLM extra_body 不是合法 JSON，已忽略：%s" % self.extra_body[:80])
        r = requests.post(self.base + "/chat/completions",
                          headers={"Authorization": "Bearer " + self.key,
                                   "Content-Type": "application/json"},
                          json=payload, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


class OpenAIEmbed:
    available = True

    def __init__(self, base_url, api_key, embed_model, rerank_model, timeout=30):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.embed_model = embed_model
        self.rerank_model = rerank_model
        self.timeout = timeout

    def embed(self, texts):
        if not texts:
            return None
        try:
            r = requests.post(self.base + "/embeddings",
                              headers={"Authorization": "Bearer " + self.key,
                                       "Content-Type": "application/json"},
                              json={"model": self.embed_model,
                                    "input": [str(t)[:1500] for t in texts]},
                              timeout=self.timeout)
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            vecs = [d["embedding"] for d in data]
            return vecs if vecs and all(vecs) else None
        except Exception as e:
            _log("嵌入失败（降级检索）：%r" % e)
            return None

    def embed_one(self, text):
        vecs = self.embed([text]) if text else None
        return vecs[0] if vecs else None

    def rerank(self, query, docs):
        if not docs:
            return None
        try:
            r = requests.post(self.base + "/rerank",
                              headers={"Authorization": "Bearer " + self.key,
                                       "Content-Type": "application/json"},
                              json={"model": self.rerank_model, "query": str(query)[:1500],
                                    "documents": [str(d)[:1500] for d in docs],
                                    "top_n": len(docs)}, timeout=self.timeout)
            r.raise_for_status()
            return [{"index": x["index"], "relevance_score": x["relevance_score"]}
                    for x in r.json().get("results", [])]
        except Exception as e:
            _log("重排失败（余弦序兜底）：%r" % e)
            return None


# ============================ 工厂（从 config 分区构建） ============================

def _prov(cfg, section, name):
    p = dict((cfg.get(section, {}).get("providers", {}) or {}).get(name) or {})
    for k, v in (ASR_PRESETS if section == "asr" else TTS_PRESETS).get(name, {}).items():
        p.setdefault(k, v)
    return p


def build_asr(cfg):
    asr = cfg.get("asr", {})
    provider = asr.get("provider", "siliconflow")
    p = _prov(cfg, "asr", provider)
    if provider == "bailian":
        obj = BailianASR(p["base_url"], p.get("api_key", ""), p.get("model", "qwen3-asr-flash"))
    elif provider == "funasr":
        obj = FunASRWS(p.get("base_url", "ws://127.0.0.1:10095"), p.get("model", "2pass"))
    else:
        obj = OpenAICompatASR(p["base_url"], p.get("api_key", ""), p.get("model", ""))
    return _wrap_call("ASR", obj, "transcribe",
                      lambda o, w: "POST %s model=%s 音频 %d 字节" %
                                   (o.base, getattr(o, "model", ""), len(w)),
                      lambda r: str(r)[:600])


def _tts_req(o, t):
    """TTS 请求日志：类名 + 关键参数（model/voice 等），便于确认实际生效的链路与音色。"""
    attrs = " ".join("%s=%s" % (k, getattr(o, k))
                     for k in ("model", "voice", "cluster", "voice_type", "ref_audio_path")
                     if getattr(o, k, None))
    return "%s(%s) ｜ 文本：%s" % (type(o).__name__, attrs, str(t)[:300])


def build_tts(cfg, voice_ref_b64=None):
    tts = cfg.get("tts", {})
    provider = tts.get("provider", "mimo")
    p = _prov(cfg, "tts", provider)
    if provider == "mimo":
        obj = MiMoTTS(p["base_url"], p.get("api_key", ""), p["model"],
                      voice_ref_b64, p.get("format", "wav"))
    elif provider == "doubao":
        obj = DoubaoTTS(p.get("appid", ""), p.get("token", ""),
                        p.get("cluster", "volcano_tts"), p.get("voice_type", ""),
                        p.get("encoding", "mp3"))
    elif provider == "bailian":
        obj = BailianQwenTTS(p["base_url"], p.get("api_key", ""), p.get("model", "qwen3-tts-flash"),
                             p.get("voice", "Cherry"), p.get("language_type", "Chinese"))
    elif provider == "cosyvoice_v35":
        obj = BailianDuplexTTS(p.get("api_key", ""), p.get("model", "cosyvoice-v3.5-flash"),
                               p.get("voice", "longxiaochun"), p.get("sample_rate", 16000),
                               p.get("volume", 50), p.get("rate", 1.0), p.get("pitch", 1.0))
    elif provider == "qwen_audio_tts":
        obj = BailianDuplexTTS(p.get("api_key", ""), p.get("model", "qwen-audio-3.1-tts-plus"),
                               p.get("voice", "longanlingxi"), p.get("sample_rate", 16000),
                               p.get("volume", 50), p.get("rate", 1.0), p.get("pitch", 1.0))
    elif provider == "qwen3_realtime":
        obj = BailianRealtimeTTS(p.get("api_key", ""), p.get("model", "qwen3-tts-flash-realtime"),
                                 p.get("voice", "Cherry"), p.get("language_type", "Chinese"),
                                 p.get("sample_rate", 16000), p.get("volume", 50),
                                 p.get("speech_rate", 1.0), p.get("pitch_rate", 1.0))
    elif provider == "gptsovits":
        obj = GPTSoVITS_TTS(p["base_url"], p.get("text_lang", "zh"), p.get("prompt_lang", "zh"),
                            p.get("ref_audio_path", ""), p.get("prompt_text", ""))
    else:
        obj = OpenAICompatTTS(p["base_url"], p.get("api_key", ""), p.get("model", "default"),
                              p.get("voice", "default"), p.get("format", "wav"))
    return _wrap_call("TTS", obj, "tts", _tts_req,
                      lambda r: "音频 %d 字节" % len(r))


def build_llm(cfg):
    l = cfg.get("llm", {})
    obj = OpenAIChat(l.get("base_url", ""), l.get("api_key", ""), l.get("model", ""),
                     l.get("temperature", 0.8), l.get("max_tokens", 300),
                     thinking=l.get("thinking", "default"),
                     extra_body=l.get("extra_body", ""))
    return _wrap_call("LLM", obj, "chat",
                      lambda o, m, system=None: json.dumps(
                          {"system": (system or "")[:400],
                           "messages": m}, ensure_ascii=False)[:1400],
                      lambda r: str(r)[:600])


def build_embed(cfg):
    e = cfg.get("embed", {})
    obj = OpenAIEmbed(e.get("base_url", ""), e.get("api_key", ""),
                      e.get("model", "BAAI/bge-m3"), e.get("rerank_model", "BAAI/bge-reranker-v2-m3"))
    _wrap_call("嵌入", obj, "embed",
               lambda o, texts: "模型=%s 文本数=%d" % (o.embed_model, len(texts)),
               lambda r: "向量 %d 组 × %d 维" % (len(r), len(r[0]) if r else 0))
    _wrap_call("重排", obj, "rerank",
               lambda o, q, docs: "模型=%s 查询=%s 候选 %d 条" % (o.rerank_model, str(q)[:120], len(docs)),
               lambda r: "分数 %s" % ["%.3f" % x["relevance_score"] for x in r[:5]])
    return obj
