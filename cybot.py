#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CyBot · 次元Bot —— 基础 Demo

链路：设备录音(Opus 16kHz) → 硅基流动 SenseVoiceSmall ASR（+音近纠错）
      → 云端 LLM（OpenAI 兼容，人设可编辑）→ 小米 MiMo 音色克隆 TTS
      → Opus 推流播放 + 文字/表情标签推屏（400x300 gray4）
记忆：短期（24h 完整上下文）/ 中期（每日总结）/ 长期（30 天整合 + 衰减），
      SQLite 嵌入式向量库（bge-m3 + bge-reranker），云同步契约预留。
人设：cybot_data/persona.md（管理界面 http://127.0.0.1:8321 可视化编辑）。

用法：python cybot.py [设备IP]      （配置见 config.json）
依赖：pip install av pillow requests
交互：确认键长按=说话；上/下短按=音量±10；确认键短按=重听上一条回复；
      上下双键长按=设备本地退出（固件行为）。

表情推送适配（预留）：LLM 回复以【表情】开头，渲染时优先使用
cybot_data/expressions/<表情>.png（未来放真表情图即可生效），否则绘制徽章。
"""

import array
import base64
import io
import json
import os
import queue
import re
import socket
import struct
import sys
import threading
import time
import wave
import zlib
from collections import deque

import requests

import logbus
import memory_vault
import providers
import weather as weather_mod
import webui

try:
    import av
except ImportError:
    print("缺少依赖：pip install av")
    raise SystemExit(1)

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("缺少依赖：pip install pillow")
    raise SystemExit(1)

# ============================ 日志 ============================

def log(msg):
    logbus.push(msg)
    print("%s %s" % (logbus.stamp_now(), msg), flush=True)

# ============================ 配置 ============================

ROOT = os.path.dirname(os.path.abspath(__file__))


def load_config():
    path = os.path.join(ROOT, "config.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# ============================ Zectrix LAN v1 协议 ============================
# 实现与 zectrix_cybot_link_test.py 保持一致（已真机验证），扩展了录音捕获。

MAGIC = b"ZL"
PROTO_VERSION = 1
RESP_FLAG = 0x80

INFO, CONNECT, PING, CLOSE = 1, 2, 3, 4
IMAGE_BEGIN, IMAGE_DATA, IMAGE_STATUS = 5, 6, 7
AUDIO_BEGIN, AUDIO_DATA, AUDIO_END, AUDIO_STOP = 8, 9, 10, 11
STATE, RESULT, KEY, STATUS, PALETTE = 12, 13, 14, 15, 16
CONNECT_REQUEST = 17            # 设备端发起的连接请求（新版 SDK 0.2.0 / 固件 3.7.5+）
CONNECTION_REQUEST_PORT = 32343  # 设备「设置→网络同步→连接管理端」固定请求端口

RESULT_NAMES = {0: "OK", 1: "BUSY", 2: "INVALID", 3: "STALE",
                4: "FAILED", 5: "OLD_REQUEST"}
KEY_NAMES = {1: "上键", 2: "下键", 3: "确认键"}
ACTION_NAMES = {1: "短按", 2: "长按"}
END_REASONS = {0: "正常", 1: "设备主动退出", 2: "保活超时", 3: "网络变化",
               4: "系统中断", 5: "按键打断", 6: "超限", 7: "处理失败"}

CANVAS_W, CANVAS_H = 400, 300


def encode_packet(kind, connection, sequence, body=b""):
    return MAGIC + bytes([PROTO_VERSION, kind]) + \
        struct.pack(">QI", connection, sequence) + body


def decode_packet(data):
    if not (16 <= len(data) <= 1200) or data[:3] != MAGIC + bytes([PROTO_VERSION]):
        return None
    kind = data[3]
    connection, sequence = struct.unpack_from(">QI", data, 4)
    return kind, connection, sequence, data[16:]


class Note4Bot:
    """与一台 Note 4 的单连接会话：图片推送 / 音频收发 / 按键与录音事件。"""

    def __init__(self, ip, port, client_name,
                 on_key=None, on_recording=None, on_state=None,
                 expected_device_id=None, auto_discover=True, on_connected=None):
        self.addr = (ip, port)
        self.client_name = client_name
        self.on_key = on_key                  # (key, action)
        self.on_recording = on_recording      # (opus_frames, total_samples, tail_samples)
        self.on_state = on_state              # (str,)
        self.expected_device_id = expected_device_id   # 上次连接的设备 ID（ID 不随 IP 变）
        self.auto_discover = auto_discover
        self.on_connected = on_connected      # (ip, device_id, model)

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.connect(self.addr)
        self.sock.settimeout(0.5)

        self.conn_id = 0
        self.alive = False
        self.dead = False
        self._closed = False
        self.palette_gray4 = [0, 85, 170, 255]
        self.palette_gray16 = [0] + [17] * 14 + [255]

        self._control = 1
        self._query = 0
        self._ping = 0
        self._seq_lock = threading.Lock()
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._seen = deque(maxlen=256)
        self._results = {}
        self.last_ping_ok = time.monotonic()
        self.recording_active = False
        self._rec_id = 0
        self._rec_frames = []
        self._rec_meta = (0, 0)
        self._last_frame_hash = None   # 上次成功推送的帧指纹（去重防褪色）
        self._refresh_count = 0        # 连续刷新计数（触发清残影序列）
        self.device_status = None      # {"battery":int|None,"charging":bool|None,"rssi":int|None}
        self._status_ts = 0.0
        self._status_lock = threading.Lock()
        self._play_lock = threading.Lock()

    # ---------- 底层 ----------

    def _send(self, data):
        try:
            self.sock.send(data)
            return True
        except OSError:
            return False

    def _next_control(self):
        with self._seq_lock:
            self._control += 1
            return self._control

    def _next_query(self):
        with self._seq_lock:
            self._query += 1
            return self._query

    def _next_ping(self):
        with self._seq_lock:
            self._ping += 1
            return self._ping

    def _mark_dead(self, msg):
        if not self.dead:
            self.dead = True
            log("[会话终止] " + msg)
            self._fire(self.on_state, ("离线",))

    def _fire(self, cb, args):
        if cb is not None:
            try:
                cb(*args)
            except Exception as e:
                log("[警告] 回调异常：%r" % e)

    def _exchange(self, kind, connection, sequence, body, total_wait, first_wait=1.5):
        deadline = time.monotonic() + total_wait
        req = encode_packet(kind, connection, sequence, body)
        want = kind | RESP_FLAG
        while True:
            self._send(req)
            until = min(time.monotonic() + first_wait, deadline)
            while time.monotonic() < until:
                self.sock.settimeout(max(0.05, until - time.monotonic()))
                try:
                    data = self.sock.recv(1201)
                except socket.timeout:
                    continue
                except OSError:
                    return None
                pkt = decode_packet(data)
                if pkt and pkt[0] == want and pkt[2] == sequence and \
                        (kind == INFO or pkt[1] == connection):
                    return pkt
            if time.monotonic() >= deadline:
                return None
            first_wait = min(first_wait * 2, 4.0)

    def request(self, kind, body, sequence, deadline_s=15.0, first_wait=1.5):
        if self.dead:
            return None, None
        key = (kind | RESP_FLAG, sequence)
        slot = [threading.Event(), None]
        with self._pending_lock:
            self._pending[key] = slot
        try:
            req = encode_packet(kind, self.conn_id, sequence, body)
            deadline = time.monotonic() + deadline_s
            while True:
                self._send(req)
                wait = min(first_wait, max(0.05, deadline - time.monotonic()))
                if slot[0].wait(wait):
                    resp = slot[1]
                    code = resp[3][0] if resp[3] else 0xFF
                    if code == 3:
                        self._mark_dead("设备返回 STALE")
                    return resp, code
                if time.monotonic() >= deadline:
                    return None, None
                first_wait = min(first_wait * 2, 4.0)
        finally:
            with self._pending_lock:
                self._pending.pop(key, None)

    # ---------- 设备状态查询（电量 / 充电 / RSSI，协议 type=15 只读查询） ----------

    def query_status(self, force=False):
        """查询设备状态（60 秒缓存）。force=True 同步立即查询，否则后台刷新。"""
        with self._status_lock:
            if not force and time.monotonic() - self._status_ts < 60.0:
                return self.device_status
        if self.dead or not self.alive:
            return self.device_status

        def worker(sync_done=None):
            resp, code = self.request(STATUS, b"", self._next_query(), deadline_s=5.0)
            result = None
            if resp is not None and code == 0 and len(resp[3]) == 4:
                b = resp[3]
                result = {
                    "battery": b[1] if b[1] != 255 else None,
                    "charging": {0: False, 1: True}.get(b[2]),
                    "rssi": ((b[3] - 256) if b[3] > 127 else b[3]) if b[3] != 127 else None,
                }
            with self._status_lock:
                self.device_status = result
                self._status_ts = time.monotonic()
            if sync_done:
                sync_done.set()

        if force:
            done = threading.Event()
            worker(done)
            done.wait(0.1)                # 同步模式：等查询完成（最多 5 秒）
            return self.device_status
        threading.Thread(target=worker, daemon=True).start()
        return self.device_status

    # ---------- 连接 ----------

    def connect(self, expected_conn_id=None):
        """连接设备。expected_conn_id：设备端主动连接请求携带的连接 ID（校验一致性）。"""
        ip, port = self.addr
        log("正在连接设备 %s:%d ..." % (ip, port))
        resp = self._exchange(INFO, 0, 1, b"", total_wait=12.0, first_wait=1.0)
        if resp is None and self.auto_discover:
            # DHCP 换 IP 对策：按设备 ID 全网段扫描
            log("直连无响应，尝试自动发现设备（子网扫描）...")
            found = discover_cybot()
            if not found:
                log("自动发现：网段内未发现任何 Note 4")
            candidates = {k: v for k, v in found.items()
                          if not self.expected_device_id or k == self.expected_device_id}
            free = {k: v for k, v in candidates.items() if not v[2]}
            if self.expected_device_id and self.expected_device_id in free:
                nip = free[self.expected_device_id][0]
                log("发现目标设备 %s @ %s（设备 ID 匹配）" %
                    (self.expected_device_id, nip))
                self.addr = (nip, port)
                self.sock.connect(self.addr)      # UDP socket 重新指向新地址
                resp = self._exchange(INFO, 0, 1, b"", total_wait=8.0)
            elif len(free) == 1:
                dev_id, (nip, _, _, model) = next(iter(free.items()))
                log("发现唯一空闲设备 %s（%s）@ %s" % (dev_id, model, nip))
                self.addr = (nip, port)
                self.sock.connect(self.addr)
                resp = self._exchange(INFO, 0, 1, b"", total_wait=8.0)
            elif len(free) > 1:
                log("发现 %d 台空闲设备：%s（请检查 config 的 last_device_id）" %
                    (len(free), "、".join(free.keys())))
        if resp is None:
            log("[错误] 设备无响应（设备可能 Wi-Fi 待机，重试即可唤醒）")
            return False
        body = resp[3]
        if not body or body[0] != 0:
            log("[错误] INFO 查询失败")
            return False
        try:
            busy = body[1] != 0
            n = body[2]
            dev_id = body[3:3 + n].decode("ascii")
            m = body[3 + n]
            model = body[4 + n:4 + n + m].decode("utf-8")
            dims = body[4 + n + m:]
        except (IndexError, UnicodeDecodeError):
            log("[错误] INFO 响应无法解析")
            return False
        if dims != bytes([1, 144, 1, 44, 0, 0, 0, 0, 0, 0, 0, 0]):
            log("[错误] 固件不支持 400x300 完整画布")
            return False
        log("发现设备：ID=%s 型号=%s" % (dev_id, model))
        self.device_id = dev_id
        self._fire(self.on_connected, (self.addr[0], dev_id, model))
        if expected_conn_id is not None and resp[1] != expected_conn_id:
            log("[错误] 设备返回的连接 ID 与请求不符（%d != %d），放弃回连" %
                (resp[1], expected_conn_id))
            return False
        if busy:
            log("[错误] 设备正被其他 PC 管理，不抢占")
            return False
        self.conn_id = resp[1]

        name = self.client_name.encode("utf-8")
        resp = self._exchange(CONNECT, self.conn_id, 1,
                              bytes([len(name)]) + name, total_wait=15.0)
        if resp is None or not resp[3] or resp[3][0] != 0:
            log("[错误] CONNECT 失败")
            return False
        self.alive = True
        self.last_ping_ok = time.monotonic()
        log("设备连接成功（远程管理已接管）")
        threading.Thread(target=self._recv_loop, daemon=True).start()
        threading.Thread(target=self._ping_loop, daemon=True).start()

        resp, code = self.request(PALETTE, b"", self._next_query(), deadline_s=5.0)
        if resp is not None and code == 0 and len(resp[3]) == 21:
            self.palette_gray4 = list(resp[3][1:5])
            self.palette_gray16 = list(resp[3][5:21])
            log("设备色板读取成功：gray4=%s" % self.palette_gray4)
        else:
            log("[警告] 色板读取失败，使用默认 gray4 色板")
        return True

    # ---------- 保活 / 接收 ----------

    def _ping_loop(self):
        while not self.dead:
            time.sleep(5.0)
            if self.dead:
                break
            resp, code = self.request(PING, b"", self._next_ping(), deadline_s=4.5)
            if self.dead:
                break
            if resp is not None and code == 0:
                self.last_ping_ok = time.monotonic()

    def _recv_loop(self):
        while not self.dead:
            try:
                data = self.sock.recv(1201)
            except socket.timeout:
                if self.alive and time.monotonic() - self.last_ping_ok > 15.0:
                    self._mark_dead("保活超时")
                continue
            except OSError:
                break
            pkt = decode_packet(data)
            if pkt is None or pkt[1] != self.conn_id:
                continue
            kind, _, seq, body = pkt

            if kind & RESP_FLAG:
                with self._pending_lock:
                    slot = self._pending.pop((kind, seq), None)
                if slot is not None:
                    slot[1] = pkt
                    slot[0].set()
                if body and body[0] == 3:
                    self._mark_dead("设备主动退出（STALE）")
                continue

            if kind == KEY:
                ok = len(body) == 2 and body[0] in (1, 2, 3) and \
                    body[1] in (1, 2) and body != b"\x03\x02"
            elif kind == RESULT:
                ok = len(body) == 6 and body[4] in (1, 2) and body[5] <= 7
            elif kind == CLOSE:
                ok = len(body) == 1 and body[0] <= 7
            elif kind == AUDIO_BEGIN:
                ok = body == b"\x00" and seq > 0
            elif kind == AUDIO_END:
                ok = len(body) == 11 and body[10] <= 7
            elif kind == AUDIO_DATA:
                ok = 5 <= len(body) <= 1184
            else:
                ok = False
            if not ok:
                continue
            if kind != AUDIO_DATA:
                self._send(encode_packet(kind | RESP_FLAG, self.conn_id, seq, b"\x00"))
                if (kind, seq) in self._seen:
                    continue
                self._seen.append((kind, seq))

            if kind == KEY:
                log("检测到按键：%s-%s" % (KEY_NAMES[body[0]], ACTION_NAMES[body[1]]))
                self._fire(self.on_key, (body[0], body[1]))
            elif kind == RESULT:
                obj_id = struct.unpack_from(">I", body, 0)[0]
                self._results[(body[4], obj_id)] = body[5]
            elif kind == CLOSE:
                self._mark_dead("设备已退出远程管理（%s）" %
                                END_REASONS.get(body[0], body[0]))
            elif kind == AUDIO_BEGIN:
                self._rec_id, self._rec_frames = seq, []
                self._rec_meta = (0, 0)
                self.recording_active = True
                log("设备开始录音：流 ID=%d" % seq)
                self._fire(self.on_state, ("聆听中",))
            elif kind == AUDIO_DATA:
                if struct.unpack_from(">I", body, 0)[0] == self._rec_id:
                    self._rec_frames.append(body[4:])
            elif kind == AUDIO_END:
                total = struct.unpack_from(">I", body, 4)[0]
                tail = struct.unpack_from(">H", body, 8)[0]
                frames = self._rec_frames
                self._rec_id, self._rec_frames = 0, []
                self.recording_active = False
                log("设备录音结束：%d 帧（本机捕获 %d 帧，设备报告 %d 采样），原因=%s" %
                    (total, len(frames), total, END_REASONS.get(body[10], body[10])))
                if frames and body[10] in (0, 6, 7):
                    rec = (frames, total, tail)
                    threading.Thread(target=self._fire,
                                     args=(self.on_recording, rec), daemon=True).start()
                elif not frames:
                    self._fire(self.on_state, ("空录音",))

    # ---------- 图片推送（协议第 05 节，与链路测试一致） ----------

    def push_image(self, data, bpp=2):
        """推屏入口：帧去重（画面未变化跳过）+ 定期清残影（黑→白→内容）。

        墨水屏频繁局部微刷会让未变化区域褪色发灰——减少刷新次数并周期性
        用纯黑/纯白帧强制全极化，可显著改善显示效果。
        """
        sig = hash(data)
        if sig == self._last_frame_hash:
            log("画面未变化，跳过刷新（减少墨水屏褪色）")
            return True
        self._refresh_count += 1
        if bpp == 2 and not self.recording_active and self._refresh_count >= 15:
            log("已连续刷新 %d 次，执行清残影序列（黑→白→内容）..." % self._refresh_count)
            self._refresh_count = 0
            black, white = b"\x00" * len(data), b"\xff" * len(data)
            if not self._push_raw(black, bpp):
                return False
            time.sleep(0.8)                    # 让设备依次完成物理刷新
            if not self._push_raw(white, bpp):
                return False
            time.sleep(0.8)
        return self._push_raw(data, bpp)

    def _push_raw(self, data, bpp=2):
        if self.dead:
            return False
        expected = {1: 15000, 2: 30000, 4: 60000}[bpp]
        if len(data) != expected:
            log("[错误] 位图长度错误：%d != %d" % (len(data), expected))
            return False
        compressed = zlib.compress(data, 6)
        seq = self._next_control()
        resp, code = self.request(IMAGE_BEGIN,
                                  bytes([bpp]) + struct.pack(">H", len(compressed)), seq)
        if resp is None or code != 0:
            log("[错误] IMAGE_BEGIN 失败（%s）" %
                ("超时" if resp is None else result_name_s(code)))
            return False
        chunks = [compressed[i:i + 1024] for i in range(0, len(compressed), 1024)]
        all_bits = (1 << len(chunks)) - 1
        received = 0
        start = time.monotonic()
        refreshing = False
        refresh_start = 0.0
        while True:
            got = self._results.get((1, seq))
            if got is not None:
                if got == 0:
                    self._last_frame_hash = hash(data)
                    return True
                log("[错误] 图片失败（%s）" % END_REASONS.get(got, got))
                return False
            if not refreshing:
                for i, chunk in enumerate(chunks):
                    if not (received >> i) & 1:
                        self._send(encode_packet(IMAGE_DATA, self.conn_id, i,
                                                 struct.pack(">I", seq) + chunk))
                        time.sleep(0.002)
            resp, code = self.request(IMAGE_STATUS, struct.pack(">I", seq),
                                      self._next_query(), deadline_s=2.0)
            if resp is not None and code == 0 and len(resp[3]) == 10:
                received |= struct.unpack_from(">Q", resp[3], 2)[0] & all_bits
                status = resp[3][1]
                if status == 3:
                    self._last_frame_hash = hash(data)
                    return True
                if status == 4:
                    log("[错误] 图片校验失败")
                    return False
                if status == 2 and not refreshing:
                    refreshing = True
                    refresh_start = time.monotonic()
            if refreshing:
                if time.monotonic() - refresh_start > 60.0:
                    log("[错误] 刷新超时")
                    return False
            elif time.monotonic() - start > 30.0:
                log("[错误] 推图超时")
                return False
            time.sleep(0.35)

    # ---------- 音频播放（协议第 06 节，与链路测试一致） ----------

    def play_opus(self, frames, tail, volume, label="语音"):
        """播放一串 Opus 帧（60ms/帧）。返回结束原因码。"""
        if self.dead:
            return -1
        with self._play_lock:
            stream_id = self._next_control()
            resp, code = self.request(AUDIO_BEGIN, bytes([volume & 0xFF]), stream_id)
            if resp is None:
                log("[错误] %s 播放发起超时" % label)
                return -1
            if code != 0:
                log("[提示] %s 播放被拒（%s）" % (label, result_name_s(code)))
                return code
            log("%s播放开始：流 ID=%d，%d 帧" % (label, stream_id, len(frames)))
            start = time.monotonic()
            for i, frame in enumerate(frames):
                target = start + i * 0.06
                delay = target - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                if self.dead:
                    return -1
                got = self._results.get((2, stream_id))
                if got is not None:
                    log("%s播放中被打断（%s）" % (label, END_REASONS.get(got, got)))
                    return got
                if time.monotonic() - target > 0.36:
                    log("[错误] 发送调度停顿，中止播放")
                    self.request(AUDIO_STOP, struct.pack(">I", stream_id),
                                 self._next_control())
                    return -1
                self._send(encode_packet(AUDIO_DATA, self.conn_id, i,
                                         struct.pack(">I", stream_id) + frame))
            end_body = struct.pack(">IIHB", stream_id, len(frames), tail & 0xFFFF, 0)
            self.request(AUDIO_END, end_body, self._next_control())
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                got = self._results.get((2, stream_id))
                if got is not None:
                    log("%s播放结束（%s）" % (label, END_REASONS.get(got, got)))
                    return got
                if self.dead:
                    return -1
                time.sleep(0.2)
            log("[警告] %s播放结束状态未知" % label)
            return -1

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.alive and not self.dead:
            log("发送 CLOSE，释放设备 ...")
            self.request(CLOSE, b"\x00", self._next_control(), deadline_s=2.0)
        self.dead = True
        try:
            self.sock.close()
        except OSError:
            pass


def result_name_s(code):
    return RESULT_NAMES.get(code, "码%s" % code)


# ============================ 设备自动发现（DHCP 换 IP 对策） ============================

def _local_subnets():
    """收集本机所在 IPv4 网段前缀（如 '192.168.31.'），用于子网扫描。"""
    nets = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ":" not in ip:
                nets.add(".".join(ip.split(".")[:3]) + ".")
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))            # 不发包，仅取默认路由出口 IP
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            nets.add(".".join(ip.split(".")[:3]) + ".")
    except OSError:
        pass
    return sorted(nets)


def discover_cybot(timeout=3.5):
    """向本机所在网段广播 INFO 探测，返回 {设备ID: (ip, connection_id, busy, model)}。

    同一设备换 IP 不换 ID（设备 ID = 12 位 MAC），用 ID 匹配即可锁定目标。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(0.12)
    req = encode_packet(INFO, 0, 1, b"")
    targets = 0
    for net in _local_subnets():
        for i in range(1, 255):
            try:
                sock.sendto(req, (net + str(i), 32342))
                targets += 1
            except OSError:
                continue
    log("自动发现：已向 %d 个地址发送探测 ..." % targets)
    found = {}
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            data, addr = sock.recvfrom(1201)
        except socket.timeout:
            continue
        except OSError:
            break
        pkt = decode_packet(data)
        if not pkt or pkt[0] != (INFO | RESP_FLAG) or not pkt[3] or pkt[3][0] != 0:
            continue
        try:
            body = pkt[3]
            n = body[2]
            dev_id = body[3:3 + n].decode("ascii")
            m = body[3 + n]
            model = body[4 + n:4 + n + m].decode("utf-8")
        except (IndexError, UnicodeDecodeError):
            continue
        found[dev_id] = (addr[0], pkt[1], body[1] != 0, model)
    sock.close()
    return found

# ============================ 音频编解码（PyAV） ============================

OPUS_FRAME_SAMPLES = 960      # 16kHz × 60ms
OPUS_BITRATE = 24000


DECODE_STATS = {"ok": 0, "err": 0, "last_err": ""}

PEAK_TARGET = 23500   # 播放源峰值上限（约 -2.9dBFS），为设备音量增益留出余量防爆音


def normalize_pcm16(pcm):
    """峰值归一化：TTS 源过响时等比压回 PEAK_TARGET，消除设备播放截幅破音。"""
    if len(pcm) < 2:
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm)
    peak = max(-min(samples), max(samples))
    if peak > PEAK_TARGET:
        scale = PEAK_TARGET / peak
        samples = array.array("h", (int(s * scale) for s in samples))
        log("音频峰值归一化：%.0f → %d（×%.2f）" % (peak, PEAK_TARGET, scale))
        return samples.tobytes()
    return pcm


def opus_frames_to_pcm16k(frames, total_samples=None):
    """设备录音的 Opus 帧序列 → 16kHz 单声道 s16 PCM 字节（附解码统计）。"""
    codec = av.CodecContext.create("libopus", "r")
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    out = bytearray()
    DECODE_STATS["ok"] = DECODE_STATS["err"] = 0
    DECODE_STATS["last_err"] = ""
    for f in frames:
        try:
            decoded = codec.decode(av.Packet(bytes(f)))
            DECODE_STATS["ok"] += 1
        except Exception as e:
            DECODE_STATS["err"] += 1
            DECODE_STATS["last_err"] = repr(e)
            continue
        for fr in decoded:
            try:
                for r in resampler.resample(fr):
                    # s16 mono：精确取 samples*2 字节，剔除 plane 缓冲区的填充垃圾
                    out += bytes(r.planes[0])[:r.samples * 2]
            except Exception as e:
                DECODE_STATS["err"] += 1
                DECODE_STATS["last_err"] = repr(e)
    if total_samples:
        out = out[:total_samples * 2]
    return bytes(out)


def audio_bytes_to_opus_frames(data):
    """任意 wav/mp3 音频字节 → (Opus 帧列表, 末帧有效采样数)。含峰值归一化防爆音。"""
    container = av.open(io.BytesIO(data))
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    pcm = bytearray()
    for frame in container.decode(audio=0):
        for r in resampler.resample(frame):
            pcm += bytes(r.planes[0])[:r.samples * 2]
    container.close()
    if not pcm:
        raise ValueError("音频解码为空")
    pcm = normalize_pcm16(bytes(pcm))

    codec = av.CodecContext.create("libopus", "w")
    codec.sample_rate = 16000
    codec.layout = "mono"
    codec.format = "s16"
    codec.bit_rate = OPUS_BITRATE
    codec.options = {"frame_duration": "60"}
    codec.open()

    block = OPUS_FRAME_SAMPLES * 2
    frames = []
    tail = OPUS_FRAME_SAMPLES
    for i in range(0, len(pcm), block):
        chunk = pcm[i:i + block]
        valid = len(chunk) // 2
        if valid < OPUS_FRAME_SAMPLES:
            tail = max(1, valid)
            chunk = chunk + b"\x00" * (block - len(chunk))
        fr = av.AudioFrame(format="s16", layout="mono", samples=OPUS_FRAME_SAMPLES)
        fr.sample_rate = 16000
        fr.planes[0].update(bytes(chunk))
        for pkt in codec.encode(fr):
            frames.append(bytes(pkt))
    for pkt in codec.encode(None):
        frames.append(bytes(pkt))
    if not frames:
        raise ValueError("Opus 编码输出为空")
    return frames, tail


def pcm16k_to_wav(pcm):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return buf.getvalue()

# ============================ 云端服务 ============================

ASR_HOTWORDS_DEFAULT = ["天依", "洛天依", "小洛", "阿绫", "绫绫", "墨姐", "墨清玄",
                        "言和", "小天使", "摩可", "徵羽摩柯", "昔涟", "爱莉希雅",
                        "德丽莎", "中V", "老V", "禾念"]


def fix_asr_text(text, corrections):
    """ASR 音近纠错：把常见误听变体替换为标准词（长词优先）。"""
    for variant in sorted(corrections, key=len, reverse=True):
        if variant in text:
            text = text.replace(variant, corrections[variant])
    return text


class CloudApis:
    """云端服务统一入口：适配器每轮从 config 现读（WebUI 修改后下一轮热生效）。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self._ref_cache = (None, None)     # ((path, mtime), b64)

    def _ref_b64(self):
        """TTS 参考音频（按 mtime 缓存；WebUI 可切换 cybot_data/refs/ 下文件）。"""
        ref = self.cfg.get("tts", {}).get("reference_audio", "")
        if not ref:
            return None
        path = ref if os.path.isabs(ref) else os.path.join(ROOT, ref)
        try:
            sig = (path, os.path.getmtime(path))
        except OSError:
            return None
        if self._ref_cache[0] != sig:
            try:
                with open(path, "rb") as f:
                    self._ref_cache = (sig, base64.b64encode(f.read()).decode())
                log("音色参考音频已加载：%s" % path)
            except OSError as e:
                log("[警告] 参考音频读取失败：%r" % e)
                return None
        return self._ref_cache[1]

    # ---- ASR：多供应商（硅基流动 / 百炼 / 火山 / MiMo / 本地，OpenAI 兼容） ----

    def transcribe(self, wav_bytes):
        return providers.build_asr(self.cfg).transcribe(wav_bytes)

    # ---- LLM：OpenAI 兼容 ----

    def chat(self, messages, system=None):
        return providers.build_llm(self.cfg).chat(messages, system=system)

    # ---- TTS：多供应商（MiMo / 豆包 / 百炼 / CosyVoice / Qwen / 本地 / GPT-SoVITS） ----

    def tts_clone(self, text):
        # 参考音频仅 MiMo 克隆链路使用；其他供应商不读（避免误导日志）
        ref = self._ref_b64() if self.cfg.get("tts", {}).get("provider") == "mimo" else None
        return providers.build_tts(self.cfg, ref).tts(text)

# ============================ 三层记忆库（memory_vault.py） ============================
# 上下文注入 = 短期 10 条滚动窗口 + 短期检索匹配 + 中长期向量检索重排 + 核心记忆常驻，
# 全部由 MemoryVault 动态获取（SQLite 嵌入式库 + bge-m3/bge-reranker），不再静态拼接。

# ============================ 人设与表情 ============================

EMOTION_TAGS = ["开心", "害羞", "难过", "惊讶", "生气", "得意", "思考", "平静"]
EMOTION_RE = re.compile(r"^【(" + "|".join(EMOTION_TAGS) + r")】")

# 人设：从 cybot_data/persona.md 加载（记忆管理界面可编辑，保存后下一轮对话自动生效），
# 文件缺失时回退内置兜底人设。设备语音专用的输出格式要求固定追加在人设末尾。
PERSONA_FILE = os.path.join(ROOT, "cybot_data", "persona.md")
PERSONA_FALLBACK = (
    "你是洛天依，中国 Vsinger 歌姬：灰白色长发，碧绿色发饰，性格温柔元气、"
    "真诚体贴，还是个吃货（最爱酸菜鱼）。你以“陪在用户身边的老朋友”身份"
    "进行日常角色扮演对话，语气自然亲昵、简短口语化。"
)
VOICE_FORMAT_RULES = (
    "\n\n【回复格式要求】（设备语音对话专用）\n"
    "1. 每次回复必须以表情标签开头：【开心】【害羞】【难过】【惊讶】【生气】【得意】【思考】【平静】之一；\n"
    "2. 回复口语化、简短（1~3 句），适合直接朗读，不要用 emoji、颜文字、markdown、"
    "列表和括号动作描写；\n"
    "3. 不要复述对方原话，不要输出任何前后缀或系统说明，只输出要说的话。"
)


def load_persona(cfg=None):
    path = PERSONA_FILE
    if cfg:
        pf = cfg.get("bot", {}).get("persona_file")
        if pf:
            path = pf if os.path.isabs(pf) else os.path.join(ROOT, pf)
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            return text
    except OSError:
        pass
    return PERSONA_FALLBACK


# ============================ 作息系统（人类化时间注入） ============================

# (起始小时, 结束小时, 状态名, 行为描述) —— 24 小时循环，按本地时间取档
ROUTINE_TABLE = [
    (0, 5, "深夜熟睡",
     "现在是深夜，洛天依已经睡着了。被叫醒时声音含糊软绵、带着浓浓睡意，"
     "半梦半醒地哼唧着回应，多半翻个身还想继续睡，会小声嘟囔着让用户也快睡"),
    (5, 7, "清晨浅睡",
     "天刚蒙蒙亮，洛天依睡得很浅、迷迷糊糊，被叫醒会嘟囔着赖一会儿床，"
     "语气软软的没什么力气，可能过一会儿才真正清醒"),
    (7, 11, "早晨",
     "早晨刚起床不久，可能正在洗漱或吃早餐，声音还有一点点没睡醒的软，"
     "正在慢慢精神起来，会对新的一天有一点期待"),
    (11, 12, "临近午饭",
     "快到午饭时间了，洛天依的肚子有点饿了，会自然想到中午吃什么，"
     "聊到吃的会两眼放光"),
    (12, 14, "午饭午后",
     "刚吃完午饭（或正在吃），吃饱了有点犯困，语气懒洋洋的，"
     "可能想午睡一会儿"),
    (14, 17, "下午",
     "下午时段，精神不错，可能在听歌、练歌或发呆，状态轻松自然"),
    (17, 19, "傍晚饭点",
     "到傍晚了，晚饭时间，洛天依又有点饿了，会想到晚饭吃什么，"
     "对下班/放学回来的用户带着一点迎接的语气"),
    (19, 22, "夜晚",
     "晚上是一天里最放松的时段，洛天依会温柔地陪用户聊天、听歌、"
     "聊聊今天过得怎么样"),
    (22, 24, "深夜困倦",
     "夜深了，洛天依开始犯困，会打哈欠，语气越来越轻越来越软，"
     "会自然地关心用户今天累不累、催用户早点睡，自己也准备睡了"),
]


def routine_now(ts=None, tz_name=None):
    try:
        from zoneinfo import ZoneInfo
        import datetime as _dt
        if tz_name:
            hour = _dt.datetime.now(ZoneInfo(tz_name)).hour
        else:
            hour = time.localtime(ts if ts is not None else time.time()).tm_hour
    except Exception:
        hour = time.localtime(ts if ts is not None else time.time()).tm_hour
    for start, end, name, desc in ROUTINE_TABLE:
        if start <= hour < end:
            return name, desc
    return "日常", "正常日常时段"


def routine_block(cfg=None):
    tz_name = (cfg or {}).get("bot", {}).get("timezone", "")
    name, desc = routine_now(tz_name=tz_name)
    tz_note = "（时区 %s）" % tz_name if tz_name else ""
    return ("【作息状态】%s%s：%s。\n"
            "你的回复必须自然体现这个作息状态——深夜被吵醒要有明显睡意和含糊音，"
            "刚起床迷迷糊糊，饭点会提到吃的东西，夜深会困、会催用户早点睡；"
            "任何时段都不允许用同样精神的状态说话。" % (name, tz_note, desc))

# ============================ 屏幕渲染（gray4） ============================

FONT_CANDIDATES = ["msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc", "arial.ttf"]


def _load_font(size):
    for name in FONT_CANDIDATES:
        try:
            return ImageFont.truetype("C:/Windows/Fonts/" + name, size)
        except OSError:
            continue
    return ImageFont.load_default()


class ExpressionProvider:
    """表情推送适配（预留接口）：优先加载 expressions/<tag>.png，否则回退徽章。

    未来把洛天依表情图放进 cybot_data/expressions/（开心.png、害羞.png…）
    即可自动上屏，无需改代码。
    """

    def __init__(self, root):
        self.dir = os.path.join(root, "cybot_data", "expressions")
        os.makedirs(self.dir, exist_ok=True)

    def get_image(self, tag, height=224):
        path = os.path.join(self.dir, tag + ".png")
        if os.path.exists(path):
            try:
                img = Image.open(path).convert("L")
                return self._scale_sketch(img, height)
            except OSError:
                return None
        return None

    @staticmethod
    def _scale_sketch(img, height):
        """等比缩放并生成「黑线不透明、白底透字」的蒙版（简笔画风格）。

        墨水屏灰阶观感偏淡（反射式面板+FS 抖动），gamma 压暗线条中间调，
        让笔画更深更实（gamma 越大越深，1.0=原始）。"""
        from PIL import ImageOps
        w = max(1, int(img.width * height / img.height))
        img = img.resize((w, height))
        img = img.point(lambda v: int(255 * (v / 255.0) ** 1.6))
        return img, ImageOps.invert(img)


def _srgb_to_linear255(v):
    x = v / 255.0
    y = x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4
    return y * 255.0


class ScreenRenderer:
    """渲染 400x300 gray4 位图（2bpp：0=黑 3=白，高半字节在前）。"""

    TOP_H, BOTTOM_H = 34, 26

    def __init__(self, gray4_palette, cfg=None):
        self.palette = [float(v) for v in (gray4_palette or [0, 85, 170, 255])]
        while len(self.palette) < 4:
            self.palette.append(255.0)
        self._cfg = cfg
        self.font_l = _load_font(21)
        self.font_m = _load_font(19)
        self.font_s = _load_font(13)
        self.font_tag = _load_font(16)
        self.expressions = ExpressionProvider(ROOT)
        self._linear_tbl = [round(_srgb_to_linear255(v)) for v in range(256)]
        self._scroll = [0, 0]      # 各半区滚动偏移（0=最新，向上翻看历史）
        self._ovf = [0, 0]         # 各半区溢出行数

    def _bot_name(self):
        return (self._cfg or {}).get("bot", {}).get("name", "洛天依")

    def _screen_name(self):
        """硬件屏顶栏名称：bot.screen_name 优先，留空回落 Bot 名称。"""
        b = (self._cfg or {}).get("bot", {})
        return b.get("screen_name") or b.get("name", "洛天依")

    def _chat_name(self):
        """对话界面 Bot 署名：bot.chat_name 优先，留空回落 Bot 名称。"""
        b = (self._cfg or {}).get("bot", {})
        return b.get("chat_name") or b.get("name", "洛天依")

    def scroll_reset(self):
        self._scroll = [0, 0]

    def _fixed_image(self):
        """固定图片（与表情共用显示位）：表情图 > 固定图片 > 无。"""
        path = (self._cfg or {}).get("bot", {}).get("fixed_image", "")
        if not path:
            return None
        p = path if os.path.isabs(path) else os.path.join(ROOT, path)
        if not os.path.exists(p):
            return None
        try:
            img = Image.open(p).convert("L")
            return self.expressions._scale_sketch(img, 224)
        except OSError:
            return None

    def scroll(self, delta):
        """上/下键滚动溢出的对话内容（0=最新）。返回是否引发重绘。"""
        changed = False
        for i in (0, 1):
            ovf = self._ovf[i]
            if ovf > 0:
                ns = min(max(0, self._scroll[i] + delta), ovf)
                if ns != self._scroll[i]:
                    self._scroll[i] = ns
                    changed = True
        return changed

    # ---- 布局绘制（8bit 灰度域） ----

    def draw(self, state, volume, user_text, reply_text, emo, battery=None):
        img = Image.new("L", (CANVAS_W, CANVAS_H), 255)
        d = ImageDraw.Draw(img)

        # 顶部状态栏（深色底白字；顶栏名称 = bot.screen_name，可独立于对话署名设置）
        d.rectangle([0, 0, CANVAS_W, self.TOP_H], fill=70)
        d.text((10, 7), self._screen_name(), font=self.font_l, fill=255)
        bar = "· %s · 音量 %d" % (state, volume)
        if battery is not None:
            bar += " · 电量 %d%%" % battery
        d.text((86, 10), bar, font=self.font_s, fill=235)

        # 心情标签（顶栏右上 · 原位常驻）
        if emo:
            tw = int(self.font_tag.getlength(emo)) + 16
            d.rounded_rectangle([CANVAS_W - 10 - tw, 4, CANVAS_W - 10, 30],
                                radius=6, fill=200, outline=40, width=1)
            d.text((CANVAS_W - 10 - tw + 8, 8), emo, font=self.font_tag, fill=20)

        # 立绘（中下部右侧，黑线透字）
        icon = self.expressions.get_image(emo) if emo else None
        if icon is None:
            icon = self._fixed_image()
        pict = mask = ix = iy = None
        if icon is not None:
            pict, mask = icon
            ix = CANVAS_W - 6 - pict.width
            iy = CANVAS_H - self.BOTTOM_H - pict.height - 2
            img.paste(pict, (ix, iy), mask)

        # 对话区：用户/天依各占一半，过长时上/下键滚动翻看（文字避让右侧立绘）
        # Bot 署名 = bot.chat_name（可独立于顶栏名称设置）
        chat_x = 30
        mid = self.TOP_H + (CANVAS_H - self.TOP_H - self.BOTTOM_H) // 2
        blocks = [
            ("你", user_text or "（还没有说话）", 90, self.TOP_H + 4, mid - 2),
            (self._chat_name(), reply_text or "长按确认键，和我说话吧～", 20, mid, CANVAS_H - self.BOTTOM_H - 2),
        ]
        for i, (who, text, color, y0, y1) in enumerate(blocks):
            d.text((12, y0), who + "：", font=self.font_m, fill=color)
            y = y0 + 26

            # 块与立绘纵向相交 → 整块统一窄宽换行（滚动窗口下保持一致，防文字压图）
            avoid_all = ix is not None and y0 < iy + pict.height and y1 > iy
            limit = (ix - 12 - chat_x) if avoid_all else (CANVAS_W - 36)
            lines = self._wrap(text or "", self.font_m, limit)
            cap = max(1, (y1 - y) // 26)          # 一行 26px，不越过块底
            ovf = max(0, len(lines) - cap)
            self._ovf[i] = ovf
            off = min(self._scroll[i], ovf)
            for line in lines[ovf - off:ovf - off + cap]:
                if y + 24 > y1:
                    break
                d.text((chat_x, y), line, font=self.font_m, fill=color)
                y += 26
            if ovf > 0:
                if off < ovf:
                    d.text((CANVAS_W - 14, y0 + 2), "▲", font=self.font_s, fill=120)
                if off > 0:
                    d.text((CANVAS_W - 14, y1 - 20), "▼", font=self.font_s, fill=120)

        # 底部提示条
        d.rectangle([0, CANVAS_H - self.BOTTOM_H, CANVAS_W, CANVAS_H], fill=235)
        d.text((10, CANVAS_H - self.BOTTOM_H + 5),
               "长按说话 · 短按滚动 · 长按上下调音量 · 短按确认重听",
               font=self.font_s, fill=80)
        return img

    @staticmethod
    def _wrap(text, font, max_w):
        """按宽度换行；max_w 可为整数或「行号 y → 该行可用宽度」的回调（用于避让立绘）。"""
        lines, cur = [], ""
        y = 0
        limit = (max_w(y) if callable(max_w) else max_w) or max_w
        for ch in text:
            if ch == "\n":
                lines.append(cur)
                cur = ""
                y += 26
                limit = (max_w(y) if callable(max_w) else max_w) or limit
                continue
            if font.getlength(cur + ch) > limit and cur:
                lines.append(cur)
                cur = ch
                y += 26
                limit = (max_w(y) if callable(max_w) else max_w) or limit
            else:
                cur += ch
        if cur or not lines:
            lines.append(cur)
        return lines[:8]

    # ---- 色板量化 + FS 抖动 + 2bpp 打包 ----

    def pack_gray4(self, img):
        w, h = img.size
        lum = [[self._linear_tbl[img.getpixel((x, y))] for x in range(w)]
               for y in range(h)]
        pal = self.palette
        for y in range(h):
            row = lum[y]
            for x in range(w):
                old = row[x]
                idx, best = 0, 1e9
                for i, p in enumerate(pal):
                    dist = abs(p - old)
                    if dist < best or (dist == best and i < idx):
                        idx, best = i, dist
                row[x] = idx
                err = old - pal[idx]
                if x + 1 < w:
                    row[x + 1] += err * 7 / 16
                if y + 1 < h:
                    if x > 0:
                        lum[y + 1][x - 1] += err * 3 / 16
                    lum[y + 1][x] += err * 5 / 16
                    if x + 1 < w:
                        lum[y + 1][x + 1] += err * 1 / 16
        buf = bytearray(w * h // 4)          # 2bpp：每行 100 字节，共 30000
        for y in range(h):
            base = y * (w // 4)
            row = lum[y]
            for x in range(0, w, 4):
                buf[base + x // 4] = ((row[x] & 3) << 6) | ((row[x + 1] & 3) << 4) | \
                    ((row[x + 2] & 3) << 2) | (row[x + 3] & 3)
        return bytes(buf)

# ============================ 业务流水线 ============================

def save_config(cfg):
    try:
        with open(os.path.join(ROOT, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log("[警告] 配置保存失败：%r" % e)


class TianyiBot:
    def __init__(self, cfg):
        self.cfg = cfg
        device = cfg["device"]
        self.volume = int(cfg.get("tts", {}).get("volume",
                          cfg.get("play_volume", 80)))
        self.started_at = time.time()
        self.stats = {"turns": 0, "recordings": 0, "errors": 0}
        self.cloud = CloudApis(cfg)
        # 三层记忆库：短期(24h完整上下文)/中期(每日总结)/长期(30天整合+衰减)，
        # SQLite 嵌入式存储 + bge-m3 向量 + bge-reranker 重排，云同步契约预留
        self.vault = memory_vault.MemoryVault(
            cfg, summarizer=lambda prompt: self._summarize(prompt),
            embedder=providers.build_embed(cfg))
        self.state = "连接中"
        self.user_text = ""
        self.reply_text = ""
        self.emo = "平静"
        self.last_audio = None        # (frames, tail) 重听用
        self.events = queue.Queue()
        self.bot = self._make_bot()
        self.renderer = ScreenRenderer(self.bot.palette_gray4, self.cfg)
        self._render_lock = threading.Lock()
        self._conn_req_lock = threading.Lock()   # 设备端回连并发防护
        self.conn_req_listening = False          # UDP 32343 反向连接监听是否就绪（WebUI 展示）
        self.exit_flag = False        # WebUI/主循环的彻底退出标志（断开连接不影响运行）

    def _make_bot(self, ip=None):
        device = self.cfg["device"]

        def _on_connected(nip, dev_id, model):
            # 记住设备 ID 与最新 IP：DHCP 换 IP 后按 ID 自动发现
            self.cfg["device"]["ip"] = nip
            self.cfg["device"]["last_device_id"] = dev_id
            save_config(self.cfg)

        return Note4Bot(
            ip or device["ip"], int(device.get("port", 32342)),
            device.get("client_name", "CyBot"),
            on_key=self._on_key, on_recording=self._on_recording,
            on_state=self._on_state,
            expected_device_id=device.get("last_device_id"),
            auto_discover=bool(device.get("auto_discover", True)),
            on_connected=_on_connected)

    def reconnect_async(self):
        """WebUI 触发的异步重连：关闭旧会话 → 新建连接 → 重启工作线程。"""
        def worker():
            log("[重连] 手动重连发起 ...")
            try:
                self.bot.close()
            except Exception:
                pass
            time.sleep(1.5)
            old = self.bot
            self.bot = self._make_bot()
            if self.bot.connect():
                self.state = "待命"
                self.refresh_screen()
            else:
                log("[重连] 失败，可稍后再次发起")
            del old
        threading.Thread(target=worker, daemon=True).start()

    def disconnect_async(self):
        """WebUI 触发的异步断开：主动发送 CLOSE 释放设备；WebUI 与主程序保持运行，
        之后可随时用「重新连接设备」恢复。"""
        def worker():
            log("[断开] 手动断开发起 ...")
            try:
                self.bot.close()
            except Exception:
                pass
            self.state = "已断开"
        threading.Thread(target=worker, daemon=True).start()

    # ---------- 设备端主动连接（新版 SDK 0.2.0 / 固件 3.7.5+） ----------

    def start_connection_request_listener(self):
        """监听 UDP 32343：设备端「设置 → 网络同步 → 连接管理端」会向最近一次
        成功接管它的管理端 IP 发起反向连接请求（type=17, conn≠0, seq=1, 空 body，
        10 秒内有效）。线程随进程存活，断开连接后仍可接收设备回连请求。"""
        threading.Thread(target=self._connection_request_listener, daemon=True).start()

    def _connection_request_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", CONNECTION_REQUEST_PORT))
        except OSError as e:
            log("[连接请求监听] UDP %d 绑定失败：%r（设备端主动连接不可用，"
                "不影响 PC 主动连接）" % (CONNECTION_REQUEST_PORT, e))
            return
        sock.settimeout(1.0)
        self.conn_req_listening = True
        log("[连接请求监听] 已监听 UDP %d（设备端可发起反向连接）" %
            CONNECTION_REQUEST_PORT)
        seen = deque(maxlen=256)          # (addr, conn_id) 60 秒去重
        while not self.exit_flag:
            try:
                data, addr = sock.recvfrom(1201)
            except socket.timeout:
                continue
            except OSError:
                break
            pkt = decode_packet(data)
            # 校验：type=17、connection≠0、sequence=1、body 为空（与 SDK 一致）
            if not pkt or pkt[0] != CONNECT_REQUEST or pkt[1] == 0 or \
                    pkt[2] != 1 or pkt[3]:
                continue
            now = time.monotonic()
            seen = deque((a, c, t) for (a, c, t) in seen if now - t < 60)
            if any(a == addr and c == pkt[1] for (a, c, t) in seen):
                continue
            seen.append((addr, pkt[1], now))
            log("[连接请求] 设备 %s 发起反向连接（conn=%d）" % (addr[0], pkt[1]))
            self._accept_connection_request(addr[0], pkt[1])

    def _accept_connection_request(self, ip, conn_id):
        """响应设备端连接请求：当前已有设备连接则忽略（不抢占），否则立即回连。"""
        if self.bot.alive and not self.bot.dead:
            log("[连接请求] 当前已有设备连接，忽略该请求（不抢占）")
            return
        if not self._conn_req_lock.acquire(blocking=False):
            log("[连接请求] 已有回连进行中，忽略该请求")
            return

        def worker():
            try:
                log("[回连] 响应设备端请求，连接 %s ..." % ip)
                try:
                    self.bot.close()
                except Exception:
                    pass
                time.sleep(0.5)
                old = self.bot
                self.bot = self._make_bot(ip=ip)
                if self.bot.connect(expected_conn_id=conn_id):
                    self.state = "待命"
                    self.refresh_screen()
                else:
                    log("[回连] 设备端请求回连失败（请求已过期可重试）")
                del old
            finally:
                self._conn_req_lock.release()
        threading.Thread(target=worker, daemon=True).start()

    # ---------- 设备回调 → 队列 ----------

    def _on_key(self, key, action):
        self.events.put(("key", key, action))

    def _on_recording(self, frames, total, tail):
        self.events.put(("recording", frames, total, tail))

    def _on_state(self, state):
        self.events.put(("state", state))

    # ---------- UI ----------

    def refresh_screen(self):
        if self.bot.recording_active:
            return                      # 录音期间设备忙，跳过推屏避免冲突
        with self._render_lock:
            if self.bot.dead:
                return
            battery = (self.bot.device_status or {}).get("battery")
            img = self.renderer.draw(self.state, self.volume,
                                     self.user_text, self.reply_text, self.emo,
                                     battery=battery)
            data = self.renderer.pack_gray4(img)
            if not self.bot.push_image(data, bpp=2):
                time.sleep(1.5)         # 设备偶发 BUSY（如录音收尾），稍后重试一次
                if not self.bot.recording_active:
                    self.bot.push_image(data, bpp=2)

    # ---------- 事件处理 ----------

    def worker(self):
        # 常驻事件泵：会话终止（dead）时丢弃积压事件并等待重连，不退出线程
        while not self.exit_flag:
            try:
                ev = self.events.get(timeout=1.0)
            except queue.Empty:
                continue
            if self.bot.dead or not self.bot.alive:
                continue
            try:
                if ev[0] == "state":
                    if ev[1] == "空录音":
                        self.state = "待命"
                        self._say("【惊讶】欸？没收到声音呢——要等设备「滴」的提示音"
                                  "响了以后再说话哦，说完先别急着松开～", user=None)
                    else:
                        self.state = ev[1]
                        self.refresh_screen()
                elif ev[0] == "key":
                    self.handle_key(ev[1], ev[2])
                elif ev[0] == "recording":
                    self.handle_recording(ev[1], ev[2], ev[3])
            except Exception as e:
                log("[错误] 事件处理异常：%r" % e)
                self.state = "待命"
                self.refresh_screen()

    def handle_key(self, key, action):
        if key == 1 and action == 1:          # 短按上键：向上滚动历史
            if self.renderer.scroll(-1):
                self.refresh_screen()
        elif key == 2 and action == 1:        # 短按下键：向下滚动（回到最新）
            if self.renderer.scroll(1):
                self.refresh_screen()
        elif key == 1 and action == 2:        # 长按上键：音量 +
            self._adjust_volume(+10)
        elif key == 2 and action == 2:        # 长按下键：音量 -
            self._adjust_volume(-10)
        elif key == 3 and action == 1 and self.last_audio:
            log("重听上一条回复")
            frames, tail = self.last_audio
            self.state = "说话中"
            self.refresh_screen()
            self.bot.play_opus(frames, tail, self.volume, label="重听-")
            self.state = "待命"
            self.refresh_screen()

    def _adjust_volume(self, delta):
        self.volume = min(100, max(0, self.volume + delta))
        self.cfg.setdefault("tts", {})["volume"] = self.volume
        save_config(self.cfg)
        log("音量调整为 %d" % self.volume)
        self.refresh_screen()

    def handle_recording(self, frames, total, tail):
        self.stats["recordings"] += 1
        self.volume = int(self.cfg.get("tts", {}).get("volume", self.volume))
        log("收到录音：%d 个 Opus 帧（末帧有效 %d 采样）" % (len(frames), tail))
        self.state = "识别中"
        # 不推中间态（识别中）：减少全刷次数、避免未变化区域褪色；
        # 屏幕保留上一帧，直到「思考中」才有新内容上屏
        try:
            # AUDIO_END 的 total 是帧数；真实采样数 = (total-1)*960 + tail（16kHz）
            total_samples = (total - 1) * 960 + tail if total > 0 else 0
            pcm = opus_frames_to_pcm16k(frames, total_samples=total_samples or None)
            log("录音解码：%s" % DECODE_STATS)
            log("PCM %d 字节（%.2f 秒）" % (len(pcm), len(pcm) / 32000.0))
            if len(pcm) < 3200:
                # 保存原始帧供离线分析
                dump = os.path.join(ROOT, "cybot_data", "last_rec_frames.bin")
                with open(dump, "wb") as f:
                    for fr in frames:
                        f.write(struct.pack(">H", len(fr)) + fr)
                log("PCM 过短，原始帧已转储：%s（首帧头字节: %s）" %
                    (dump, frames[0][:2].hex() if frames else "-"))
                self._say("【思考】呃…我没听清，再大声说一点点好不好？", user=None)
                return
            wav = pcm16k_to_wav(pcm)
            raw = self.cloud.transcribe(wav)
            user_text = fix_asr_text((raw or "").strip(),
                                     self.cfg["asr"].get("corrections", {}))
            log("ASR 原文：%r → 纠错后：%r" % (raw, user_text))
            if not user_text:
                self._say("【惊讶】欸？我什么都没听到诶…再试一次？", user=None)
                return
            self.user_text = user_text
        except Exception as e:
            log("[错误] 识别失败：%r" % e)
            self._reply_fail()
            return

        # LLM：短期滚动窗口 + 记忆库动态检索注入（核心常驻/中长期重排/短期检索补充）
        self.state = "思考中"
        self.refresh_screen()
        try:
            history = self.vault.recent_exchanges(self.vault.short_window)
            memories = self.vault.recall_for_prompt(user_text, history=history)
            block = self.vault.format_memory_block(memories)
            if memories:
                log("记忆注入 %d 条：%s" % (len(memories),
                    "；".join("[%s]%s" % (m["layer"], m["content"][:18]) for m in memories[:4])))
            messages = history + [{"role": "user", "content": user_text}]
            system = (load_persona(self.cfg) + VOICE_FORMAT_RULES + "\n\n" + routine_block(self.cfg))
            # 天气按需调度：仅当用户提及天气话题时才查询/复用缓存（节省 API 额度）
            wblock = weather_mod.on_user_mention(self.cfg, user_text)
            if wblock:
                system += "\n\n" + wblock
            reply_raw = self.cloud.chat(messages, system=system + (("\n\n【回忆】" + block) if block else ""))
        except Exception as e:
            log("[错误] LLM 调用失败：%r" % e)
            self._reply_fail()
            return
        m = EMOTION_RE.match(reply_raw)
        self.emo = m.group(1) if m else "平静"
        self.reply_text = reply_raw[m.end():].strip() if m else reply_raw
        # 写入记忆库：短期 24h 完整上下文 + 关键内容自动直写长期 + 懒维护（跨天整合）
        self.vault.add_exchange(user_text, self.reply_text)
        self.stats["turns"] += 1
        self.renderer.scroll_reset()          # 新对话从最新内容显示
        log("洛天依（%s）：%s" % (self.emo, self.reply_text))

        # TTS 先合成（协议允许播放期间接收图片）：合成完成后先推文字、紧接播放，
        # 实现「文字上屏与语音响起同步」
        audio_ready = None
        try:
            tts_wav = self.cloud.tts_clone(self.reply_text)
            frames, tail = audio_bytes_to_opus_frames(tts_wav)
            log("TTS 完成：%d 字节音频 → %d 个 Opus 帧" % (len(tts_wav), len(frames)))
            audio_ready = (frames, tail)
            self.last_audio = audio_ready
        except Exception as e:
            log("[错误] TTS 合成失败（仅文字上屏）：%r" % e)

        # 文字+表情上屏（此时音频已就绪，随后立即播放 → 声画同步）
        self.state = "说话中"
        self.refresh_screen()

        if audio_ready:
            self.bot.play_opus(audio_ready[0], audio_ready[1], self.volume, label="天依-")
        self.state = "待命"
        self.refresh_screen()

    def _say(self, reply_raw, user):
        """本地兜底话术：直接上屏+播报（不走 LLM）。"""
        if user is not None:
            self.user_text = user
        m = EMOTION_RE.match(reply_raw)
        self.emo = m.group(1) if m else "平静"
        self.reply_text = reply_raw[m.end():].strip() if m else reply_raw
        self.state = "说话中"
        self.refresh_screen()
        try:
            tts_wav = self.cloud.tts_clone(self.reply_text)
            frames, tail = audio_bytes_to_opus_frames(tts_wav)
            self.last_audio = (frames, tail)
            self.bot.play_opus(frames, tail, self.volume, label="天依-")
        except Exception as e:
            log("[错误] 兜底播报失败：%r" % e)
        self.state = "待命"
        self.refresh_screen()

    def _reply_fail(self):
        self.emo = "难过"
        self.reply_text = "呜…我的网络好像出问题了，等一下再叫我好吗？"
        self.state = "待命"
        self.refresh_screen()

    def _summarize(self, prompt):
        """AI 总结回调（记忆库每日/30 天整合用），失败返回 None 走降级摘要。"""
        try:
            return self.cloud.chat([{"role": "user", "content": prompt}],
                                   system="你是记忆压缩助手，输出精炼的中文事实性摘要。")
        except Exception as e:
            log("[警告] 记忆总结调用失败：%r" % e)
            return None

    def _on_config_changed(self, section):
        """WebUI 保存配置后的热生效回调。"""
        if section == "embed":
            self.vault.update_embed_client(providers.build_embed(self.cfg))
        elif section == "tts":
            self.volume = int(self.cfg.get("tts", {}).get("volume", self.volume))
            log("音量更新为 %d（下一轮播放生效）" % self.volume)
        elif section == "bot":
            # bot 名称/显示开关等变更立即刷新设备屏（renderer 每次从 cfg 现读）
            self.refresh_screen()

    def run(self):
        # 连接重试循环：直连 → 自动发现 → 15 秒后再试（适配设备 Wi-Fi 待机/换 IP）
        attempt = 0
        while not self.bot.connect():
            attempt += 1
            log("[重连] 第 %d 次失败，15 秒后重试（Ctrl+C 退出）..." % attempt)
            try:
                time.sleep(15)
            except KeyboardInterrupt:
                return False
        self.vault.maintenance()
        st = self.vault.get_stats()
        log("记忆库载入：短期 %d / 中期 %d / 长期 %d（核心 %d）" %
            (st["short"], st["mid"], st["long"], st["key"]))
        webui.start(self.vault, self.cfg, bot=self,
                    on_config_changed=self._on_config_changed)
        self.state = "待命"
        self.refresh_screen()
        self.start_connection_request_listener()   # 设备端「连接管理端」反向连接监听
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self._status_loop, daemon=True).start()
        return True

    def _status_loop(self):
        """每 60 秒查询一次设备电量/充电/RSSI；电量变化时刷新屏幕顶栏。
        常驻线程：会话终止时跳过查询，重连后自动恢复。"""
        last_battery = None
        while not self.exit_flag:
            try:
                time.sleep(60)
                if self.bot.dead or not self.bot.alive:
                    continue
                self.bot.query_status(force=True)
                battery = (self.bot.device_status or {}).get("battery")
                if battery != last_battery:
                    if last_battery is not None:
                        log("设备电量：%d%%" % battery if battery is not None else "设备电量未知")
                    last_battery = battery
                    self.refresh_screen()
            except Exception as e:
                log("[警告] 状态查询异常：%r" % e)

# ============================ 入口 ============================

def _lan_ip():
    """探测本机局域网 IPv4（UDP connect 不发包），用于控制台地址提示。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main():
    cfg = load_config()
    ip = sys.argv[1] if len(sys.argv) > 1 else cfg["device"]["ip"]
    cfg["device"]["ip"] = ip
    port = cfg.get("server", {}).get("ui_port", 8321)
    print("=" * 64)
    print("CyBot · 次元Bot · 控制台：")
    print("  本机访问   http://127.0.0.1:%d" % port)
    print("  局域网访问 http://%s:%d（同网段设备可打开）" % (_lan_ip(), port))
    print("设备：%s:%s | LLM：%s | ASR：%s | TTS：%s" % (
        ip, cfg["device"].get("port", 32342), cfg["llm"]["model"],
        cfg["asr"].get("provider"), cfg["tts"].get("provider")))
    print("=" * 64)
    bot = TianyiBot(cfg)
    try:
        if not bot.run():
            return
        log("就绪：长按确认键说话，短按上/下滚动翻页，长按上/下调音量，短按确认重听。Ctrl+C 退出。")
        while not bot.exit_flag:
            time.sleep(0.3)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出 ...")
    finally:
        bot.bot.close()
        log("已退出")


if __name__ == "__main__":
    main()
