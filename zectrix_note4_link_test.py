#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ZECTRIX Note 4 物理链路连通性测试（单文件 / 纯 Python 标准库实现）

只用于验证与 Note 4 的物理链路，不包含 ASR / LLM / TTS / 记忆等高级功能：

  1. 连接管理：按指定 IP/端口直连设备并保持会话（每 5 秒 PING 保活，
     15 秒无确认判定失联）。
  2. 上行测试：持续监听设备按键，准确打印按键类型（上/下/确认）与
     按下方式（短按/长按）。确认键长按会触发设备录音流程，本程序按
     协议确认并记录，不做语音处理。
  3. 下行测试（图片）：启动后自动生成 400x300 的 "Hello Note 4" 静态图，
     mono(1bpp) 格式、zlib 压缩（无文件头），推送至设备屏幕显示。
  4. 下行测试（音频）：每次按键触发后播放一段预置的 16kHz Opus 提示音
     （60ms/帧，已内嵌为 base64，无需任何第三方解码库）。

用法：
  python zectrix_note4_link_test.py <设备IP> [端口]
  例：python zectrix_note4_link_test.py 192.168.1.100
      python zectrix_note4_link_test.py 192.168.1.100 32342

协议：Zectrix LAN v1（UDP 业务端口 32342，16 字节公共头
"ZL" + 版本 + 类型 + 64 位连接编号 + 32 位序号，网络字节序）。
实现依据 SDK 仓库 docs/zectrix-lan-v1.html（v1.5）与 src/ 源码。
"""

import base64
import socket
import struct
import sys
import threading
import time
import zlib
from collections import deque

# ============================ 可调参数 ============================

DEFAULT_IP = "192.168.31.170"  # 未传参数时使用的设备 IP
DEFAULT_PORT = 32342           # Zectrix LAN v1 业务端口
CLIENT_NAME = "Python Link Test"  # 上报给设备的 PC 名称（1~48 字节）
BEEP_VOLUME = 70               # 提示音音量 0~100

# ============================ 协议常量 ============================

MAGIC = b"ZL"
PROTO_VERSION = 1
RESP_FLAG = 0x80

INFO = 1          # 查询设备身份 / 连接编号（不取得管理权）
CONNECT = 2       # 接管设备
PING = 3          # 保活
CLOSE = 4         # 释放管理权
IMAGE_BEGIN = 5   # 图片开始：u8 bpp + u16 压缩长度；序号即图片 ID
IMAGE_DATA = 6    # 图片数据：u32 图片 ID + ≤1024 字节；序号即块号
IMAGE_STATUS = 7  # 图片状态查询：u8 状态 + u64 接收位图
AUDIO_BEGIN = 8   # 播放开始：u8 音量；序号即流 ID
AUDIO_DATA = 9    # 音频数据：u32 流 ID + 一个 Opus 包；序号即帧号
AUDIO_END = 10    # 播放结束：流 ID + 总帧数 + 末帧有效采样 + 原因
AUDIO_STOP = 11   # 提前终止指定流
STATE = 12        # 综合状态查询
RESULT = 13       # 设备回执：图片/播放完成结果
KEY = 14          # 设备按键事件：key(上=1/下=2/确认=3) + action(短=1/长=2)
STATUS = 15       # 电量 / 充电 / RSSI 查询
PALETTE = 16      # 设备色板查询

RESULT_NAMES = {0: "OK", 1: "BUSY", 2: "INVALID", 3: "STALE",
                4: "FAILED", 5: "OLD_REQUEST"}
KEY_NAMES = {1: "上键", 2: "下键", 3: "确认键"}
ACTION_NAMES = {1: "短按", 2: "长按"}
END_REASONS = {0: "正常", 1: "设备主动退出", 2: "保活超时", 3: "网络变化",
               4: "系统中断", 5: "按键打断", 6: "超限", 7: "处理失败"}
PLAYBACK_END_NAMES = {0: "正常完成", 5: "被按键打断", 6: "时长超限"}

# 图片：400x300，bpp=1/2/4 对应每行 50/100/200 字节（mono/gray4/gray16）
CANVAS_W, CANVAS_H = 400, 300
BPP_BYTES_PER_ROW = {1: 50, 2: 100, 4: 200}

# 预置提示音：16kHz 单声道 Opus、60ms/帧（960 采样），约 1.00 秒 880Hz 品笛声
# （开发期用 ffmpeg libopus 编码后内嵌；每帧一个 Opus 包，设备端直接解码）
OPUS_BEEP_FRAME_SIZES = [195, 111, 108, 107, 122, 96, 91, 131, 179, 179,
                         179, 179, 179, 179, 179, 179, 179]
OPUS_BEEP_TAIL_SAMPLES = 640   # 末帧有效采样数（总 16000 采样 = 1.00 秒）
OPUS_BEEP_B64 = (
    "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"
)


def load_beep_frames():
    """把内嵌 base64 还原为一帧一个 Opus 包的列表。"""
    raw = base64.b64decode("".join(OPUS_BEEP_B64))
    frames, pos = [], 0
    for size in OPUS_BEEP_FRAME_SIZES:
        frames.append(raw[pos:pos + size])
        pos += size
    if pos != len(raw):
        raise ValueError("内嵌提示音数据长度不一致")
    return frames


# ============================ 工具函数 ============================

def log(msg):
    t = time.time()
    lt = time.localtime(t)
    stamp = "[%s.%03d]" % (time.strftime("%H:%M:%S", lt), int((t % 1) * 1000))
    print(stamp, msg, flush=True)


def encode_packet(kind, connection, sequence, body=b""):
    return MAGIC + bytes([PROTO_VERSION, kind]) + \
        struct.pack(">QI", connection, sequence) + body


def decode_packet(data):
    if not (16 <= len(data) <= 1200) or data[:3] != MAGIC + bytes([PROTO_VERSION]):
        return None
    kind = data[3]
    connection, sequence = struct.unpack_from(">QI", data, 4)
    return kind, connection, sequence, data[16:]


def result_name(code):
    return RESULT_NAMES.get(code, "码%s" % code)


# ============================ 测试图片 ============================

# 5x7 点阵字体（仅覆盖 "Hello Note 4" 用到的字符），1=黑点
FONT_5X7 = {
    "H": [0b10001, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001],
    "e": [0b01110, 0b10001, 0b10001, 0b11111, 0b10000, 0b01110, 0b00000],
    "l": [0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110],
    "o": [0b01110, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110, 0b00000],
    "N": [0b10001, 0b11001, 0b10101, 0b10011, 0b10001, 0b10001, 0b10001],
    "t": [0b00100, 0b00100, 0b01110, 0b00100, 0b00100, 0b00100, 0b00110],
    "4": [0b00010, 0b00110, 0b01010, 0b10010, 0b11111, 0b00010, 0b00010],
    " ": [0, 0, 0, 0, 0, 0, 0],
}


def make_hello_image():
    """生成 400x300 mono(1bpp) 测试图：白底黑字 "Hello Note 4" + 3px 边框。

    打包规则（协议第 05 节）：逐行存储、最高位像素在左、1bpp 为
    0=白 1=黑，每行 50 字节，总计 15000 字节，无任何文件头。
    """
    scale, gap = 5, 5
    text = "Hello Note 4"
    buf = bytearray(CANVAS_W * CANVAS_H // 8)  # 15000 字节，全 0 = 全白

    def px(x, y):
        buf[y * 50 + (x >> 3)] |= 1 << (7 - (x & 7))

    # 3px 黑色边框，便于确认整幅画布都被正确推送
    for y in range(CANVAS_H):
        for x in (0, 1, 2, CANVAS_W - 3, CANVAS_W - 2, CANVAS_W - 1):
            px(x, y)
    for x in range(CANVAS_W):
        for y in (0, 1, 2, CANVAS_H - 3, CANVAS_H - 2, CANVAS_H - 1):
            px(x, y)

    pitch = 5 * scale + gap
    total = len(text) * 5 * scale + (len(text) - 1) * gap
    x0, y0 = (CANVAS_W - total) // 2, (CANVAS_H - 7 * scale) // 2
    for ci, ch in enumerate(text):
        glyph = FONT_5X7[ch]
        for gy, row in enumerate(glyph):
            for gx in range(5):
                if (row >> (4 - gx)) & 1:
                    for dy in range(scale):
                        for dx in range(scale):
                            px(x0 + ci * pitch + gx * scale + dx,
                               y0 + gy * scale + dy)
    return bytes(buf)


# ============================ 链路测试主体 ============================

class Note4Link:
    """与一台 Note 4 的单连接链路测试会话（Zectrix LAN v1）。"""

    def __init__(self, ip, port, client_name):
        self.addr = (ip, port)
        self.client_name = client_name
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.connect(self.addr)
        self.sock.settimeout(0.5)

        self.conn_id = 0            # 连接编号（INFO 响应取得）
        self.alive = False          # 已接管远程管理
        self.dead = False           # 会话终止
        self._closed = False

        self._control = 1           # 控制序号：CONNECT=1，之后从 2 递增
        self._query = 0             # 只读查询序号：从 1 递增
        self._ping = 0              # PING 序号：从 1 递增
        self._seq_lock = threading.Lock()

        self._pending = {}          # (响应类型, 序号) -> [Event, Packet|None]
        self._pending_lock = threading.Lock()
        self._seen = deque(maxlen=256)   # 设备事件去重 (kind, seq)
        self._results = {}          # (对象类型, 对象 ID) -> 结束原因

        self.last_ping_ok = time.monotonic()
        self._ping_fails = 0
        self._rec_id = 0            # 设备录音流 ID（确认键长按触发）
        self._rec_frames = 0
        self._play_lock = threading.Lock()

    # ---------- 底层收发 ----------

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

    def _exchange(self, kind, connection, sequence, body, total_wait, first_wait=1.5):
        """会话建立阶段的同步请求/响应（同序号同内容重传）。"""
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
        """会话内请求：等待响应（同序号退避重传），返回 (Packet|None, 结果码)。"""
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
                        self._mark_dead("设备返回 STALE（响应类型 0x%02X）" % kind)
                    return resp, code
                if time.monotonic() >= deadline:
                    return None, None    # 结果未知
                first_wait = min(first_wait * 2, 4.0)
        finally:
            with self._pending_lock:
                self._pending.pop(key, None)

    # ---------- 会话建立 ----------

    def connect(self):
        ip, port = self.addr
        log("正在连接设备 %s:%d ..." % (ip, port))

        # 1. INFO：取设备身份与当前连接编号
        resp = self._exchange(INFO, 0, 1, b"", total_wait=5.0)
        if resp is None:
            log("[错误] 设备无响应：请检查 IP/端口、设备 Wi-Fi 及「待机保持 Wi-Fi」设置")
            return False
        body = resp[3]
        if not body or body[0] != 0:
            log("[错误] INFO 查询失败（结果码 %s）" % result_name(body[0] if body else 0xFF))
            return False
        try:
            busy = body[1] != 0
            n = body[2]
            dev_id = body[3:3 + n].decode("ascii")
            m = body[3 + n]
            model = body[4 + n:4 + n + m].decode("utf-8")
            dims = body[4 + n + m:]
        except (IndexError, UnicodeDecodeError):
            log("[错误] INFO 响应格式无法解析")
            return False
        if dims != bytes([1, 144, 1, 44, 0, 0, 0, 0, 0, 0, 0, 0]):
            log("[错误] 设备固件不支持 400x300 完整画布（请配套升级固件）")
            return False
        log("发现设备：ID=%s 型号=%s 画布=%dx%d" % (dev_id, model, CANVAS_W, CANVAS_H))
        if busy:
            log("[错误] 设备正被其他 PC 管理，按协议不抢占，请先让对方断开")
            return False
        self.conn_id = resp[1]
        log("取得连接编号：0x%016X" % self.conn_id)

        # 2. CONNECT：接管设备
        name = self.client_name.encode("utf-8")
        log("发送 CONNECT（PC 名称：%s）..." % self.client_name)
        resp = self._exchange(CONNECT, self.conn_id, 1,
                              bytes([len(name)]) + name, total_wait=15.0)
        if resp is None:
            log("[错误] CONNECT 无响应（设备忙碌或网络异常）")
            return False
        if not resp[3] or resp[3][0] != 0:
            code = resp[3][0] if resp[3] else 0xFF
            log("[错误] 接管被拒绝（结果码 %s）" % result_name(code))
            return False
        self.alive = True
        self.last_ping_ok = time.monotonic()
        log("设备连接成功（已接管远程管理）")

        # 3. 启动接收线程与保活线程
        threading.Thread(target=self._recv_loop, daemon=True).start()
        threading.Thread(target=self._ping_loop, daemon=True).start()
        log("保活已启动：每 5 秒 PING，15 秒无确认判定失联")

        # 4. PALETTE：读取设备色板（mono 推图不依赖，gray4/16 需要）
        resp, code = self.request(PALETTE, b"", self._next_query(), deadline_s=5.0)
        if resp is not None and code == 0 and len(resp[3]) == 21:
            gray4, gray16 = list(resp[3][1:5]), list(resp[3][5:21])
            log("设备色板读取成功：gray4=%s gray16=%s" % (gray4, gray16))
        else:
            why = "超时无响应" if resp is None else result_name(code)
            log("[警告] 设备色板读取失败（%s）；mono 推图不受影响" % why)
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
                self._ping_fails = 0
            else:
                self._ping_fails += 1
                if resp is None:
                    log("[警告] PING 未获确认（连续 %d 次），请检查网络" % self._ping_fails)

    def _recv_loop(self):
        while not self.dead:
            try:
                data = self.sock.recv(1201)
            except socket.timeout:
                if self.alive and time.monotonic() - self.last_ping_ok > 15.0:
                    self._mark_dead("保活超时：15 秒未收到设备 PING 确认")
                continue
            except OSError:
                break
            pkt = decode_packet(data)
            if pkt is None or pkt[1] != self.conn_id:
                continue
            kind, _, seq, body = pkt

            if kind & RESP_FLAG:                     # 请求的响应
                with self._pending_lock:
                    slot = self._pending.pop((kind, seq), None)
                if slot is not None:
                    slot[1] = pkt
                    slot[0].set()
                if body and body[0] == 3:
                    self._mark_dead("设备主动退出（响应 STALE）")
                continue

            # 设备主动消息：先校验
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
            if kind != AUDIO_DATA:                   # 除音频数据外都要确认
                self._send(encode_packet(kind | RESP_FLAG, self.conn_id, seq, b"\x00"))
                if (kind, seq) in self._seen:
                    continue                          # 重复事件只处理一次
                self._seen.append((kind, seq))

            if kind == KEY:
                log("检测到按键：%s-%s" % (KEY_NAMES[body[0]], ACTION_NAMES[body[1]]))
                threading.Thread(target=self._play_beep, daemon=True).start()
            elif kind == RESULT:
                obj_id = struct.unpack_from(">I", body, 0)[0]
                obj, reason = body[4], body[5]
                self._results[(obj, obj_id)] = reason
                if obj == 1:
                    log("收到图片结果回执：图片 ID=%d，结果=%s" %
                        (obj_id, END_REASONS.get(reason, reason)))
            elif kind == CLOSE:
                self._mark_dead("设备已退出远程管理（原因：%s）" %
                                END_REASONS.get(body[0], body[0]))
            elif kind == AUDIO_BEGIN:
                self._rec_id, self._rec_frames = seq, 0
                log("设备开始录音（确认键长按）：流 ID=%d，音频将上报本机" % seq)
            elif kind == AUDIO_DATA:
                if struct.unpack_from(">I", body, 0)[0] == self._rec_id:
                    self._rec_frames += 1
            elif kind == AUDIO_END:
                total = struct.unpack_from(">I", body, 4)[0]
                tail = struct.unpack_from(">H", body, 8)[0]
                log("设备录音结束：设备报告 %d 帧（末帧 %d 采样，本机收到 %d 帧），"
                    "原因=%s（本程序不做语音处理，音频已忽略）" %
                    (total, tail, self._rec_frames,
                     END_REASONS.get(body[10], body[10])))
                self._rec_id = 0

    # ---------- 下行测试：图片推送 ----------

    def push_image(self, data, bpp=1):
        """推送一张已按协议打包的原始位图（zlib 压缩 + 分块 + 轮询状态）。"""
        if self.dead:
            log("[错误] 连接已断开，无法推送图片")
            return False
        if self._rec_id:
            log("[警告] 设备正在录音，本次推图取消（避免 BUSY）")
            return False
        assert len(data) == 15000 * bpp, "位图长度应为 %d 字节" % (15000 * bpp)
        compressed = zlib.compress(data, 6)
        if not (1 <= len(compressed) <= 65535):
            log("[错误] 压缩后长度越界：%d 字节" % len(compressed))
            return False

        begin = bytes([bpp]) + struct.pack(">H", len(compressed))
        seq = self._next_control()
        log("开始推送图片：bpp=%d（%s），原始 %d 字节 → zlib 压缩 %d 字节，"
            "图片 ID=%d" % (bpp, {1: "mono"}.get(bpp, bpp), len(data),
                            len(compressed), seq))
        resp, code = self.request(IMAGE_BEGIN, begin, seq)
        if resp is None:
            log("[错误] IMAGE_BEGIN 超时，结果未知（可用 STATE 查询）")
            return False
        if code != 0:
            log("[错误] 图片推送被拒绝（结果码 %s）" % result_name(code))
            return False

        chunks = [compressed[i:i + 1024] for i in range(0, len(compressed), 1024)]
        all_bits = (1 << len(chunks)) - 1
        received = 0
        log("图片分块：共 %d 块（每块 ≤1024 字节），开始上传 ..." % len(chunks))
        start = time.monotonic()
        refreshing = False
        refresh_start = 0.0
        while True:
            got = self._results.get((1, seq))
            if got is not None:
                if got == 0:
                    log("图片推送成功：设备已确认显示完成（耗时 %.1f 秒）" %
                        (time.monotonic() - start))
                    return True
                log("[错误] 图片推送失败（原因：%s）" %
                    END_REASONS.get(got, got))
                return False

            if not refreshing:
                for i, chunk in enumerate(chunks):
                    if not (received >> i) & 1:
                        self._send(encode_packet(
                            IMAGE_DATA, self.conn_id, i,
                            struct.pack(">I", seq) + chunk))
                        time.sleep(0.002)          # 协议建议块间隔 2ms

            resp, code = self.request(IMAGE_STATUS, struct.pack(">I", seq),
                                      self._next_query(), deadline_s=2.0)
            if resp is not None and code == 0 and len(resp[3]) == 10:
                received |= struct.unpack_from(">Q", resp[3], 2)[0] & all_bits
                status = resp[3][1]
                if status == 3:
                    log("图片推送成功：设备已确认显示完成（耗时 %.1f 秒）" %
                        (time.monotonic() - start))
                    return True
                if status == 4:
                    log("[错误] 图片校验/显示失败（IMAGE_STATUS=FAILED）")
                    return False
                if status == 2 and not refreshing:
                    refreshing = True
                    refresh_start = time.monotonic()
                    log("设备已收齐图片，正在物理刷新（墨水屏刷新需要几秒）...")
            elif resp is None:
                log("[警告] IMAGE_STATUS 查询超时，将继续重试")

            if refreshing:
                if time.monotonic() - refresh_start > 60.0:
                    log("[错误] 物理刷新超过 60 秒未完成")
                    return False
            elif time.monotonic() - start > 30.0:
                reason = self._state_result(1, seq)
                if reason == 0:
                    log("图片推送成功（STATE 查询确认）")
                    return True
                if reason is None:
                    log("[错误] 推图结果未知（30 秒超时且 STATE 无法确认）")
                else:
                    log("[错误] 图片推送失败（原因：%s）" %
                        END_REASONS.get(reason, reason))
                return False
            time.sleep(0.35)

    def _state_result(self, obj, obj_id):
        """STATE 查询最近一次图片/播放结果；返回原因码或 None。"""
        resp, code = self.request(STATE, b"", self._next_query(), deadline_s=2.0)
        if resp is None or code != 0 or len(resp[3]) != 30:
            return None
        at = 20 if obj == 1 else 25
        if struct.unpack_from(">I", resp[3], at)[0] == obj_id:
            return resp[3][at + 4]
        return None

    # ---------- 下行测试：音频播放 ----------

    def _play_beep(self):
        if not self._play_lock.acquire(blocking=False):
            log("[提示] 上一次提示音尚未结束，忽略本次触发")
            return
        try:
            frames = load_beep_frames()
            duration = len(frames) * 0.06
            log("开始播放提示音：16kHz Opus，%d 帧（约 %.2f 秒），音量 %d" %
                (len(frames), duration, BEEP_VOLUME))
            if self._rec_id:
                log("[警告] 设备正在录音，本次播放取消（避免 BUSY）")
                return

            stream_id = self._next_control()
            resp, code = self.request(AUDIO_BEGIN, bytes([BEEP_VOLUME]), stream_id)
            if resp is None:
                log("[错误] AUDIO_BEGIN 超时，播放结果未知")
                return
            if code != 0:
                log("[错误] 播放被拒绝（结果码 %s）" % result_name(code))
                return
            log("音频流已建立（流 ID=%d），按 60ms/帧节奏推送 ..." % stream_id)

            start = time.monotonic()
            for i, frame in enumerate(frames):
                target = start + i * 0.06
                delay = target - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                if self.dead:
                    return
                got = self._results.get((2, stream_id))
                if got is not None:
                    log("提示音%s（播放中被打断）" %
                        PLAYBACK_END_NAMES.get(got, "结束，原因=%s" % got))
                    return
                if time.monotonic() - target > 0.36:
                    log("[错误] 发送调度停顿超过 360ms，中止本次播放")
                    self.request(AUDIO_STOP, struct.pack(">I", stream_id),
                                 self._next_control())
                    return
                self._send(encode_packet(AUDIO_DATA, self.conn_id, i,
                                         struct.pack(">I", stream_id) + frame))

            end_body = struct.pack(">IIHB", stream_id, len(frames),
                                   OPUS_BEEP_TAIL_SAMPLES, 0)
            resp, code = self.request(AUDIO_END, end_body, self._next_control())
            if resp is None:
                log("[警告] AUDIO_END 未获确认，继续等待设备回执")
            elif code != 0:
                log("[提示] AUDIO_END 返回（结果码 %s），以设备回执为准" %
                    result_name(code))
            log("音频帧发送完毕（共 %d 帧），等待设备播放结束 ..." % len(frames))

            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                got = self._results.get((2, stream_id))
                if got is not None:
                    break
                if self.dead:
                    return
                time.sleep(0.2)
            else:
                got = self._state_result(2, stream_id)
            if got == 0:
                log("提示音播放完成（设备确认正常播完）")
            elif got is None:
                log("[警告] 播放结束状态未知（未收到 RESULT 且 STATE 无法确认）")
            else:
                log("提示音%s" % PLAYBACK_END_NAMES.get(got, "结束，原因=%s" % got))
        finally:
            self._play_lock.release()

    # ---------- 退出 ----------

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.alive and not self.dead:
            log("发送 CLOSE，释放设备管理权 ...")
            self.request(CLOSE, b"\x00", self._next_control(), deadline_s=2.0)
        self.dead = True
        try:
            self.sock.close()
        except OSError:
            pass


# ============================ 入口 ============================

def main():
    ip = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_IP
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORT
    print("=" * 64)
    print("ZECTRIX Note 4 物理链路连通性测试（Zectrix LAN v1）")
    print("功能：连接保持 / 按键监听 / 图片推送 / 提示音播放")
    print("=" * 64)

    link = Note4Link(ip, port, CLIENT_NAME)
    try:
        if not link.connect():
            return
        image = make_hello_image()
        link.push_image(image, bpp=1)
        print("-" * 64)
        print("请在设备上按 上/下/确认 键：每次按键都会打印类型与长短按，")
        print("并播放提示音。确认键长按将触发设备录音（本程序仅记录）。")
        print("按 Ctrl+C 退出。")
        print("-" * 64)
        while not link.dead:
            time.sleep(0.25)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，准备退出 ...")
    finally:
        link.close()
        log("测试结束")


if __name__ == "__main__":
    main()
