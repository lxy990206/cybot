#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全局日志总线：主程序与 WebUI 共享的环形日志缓冲（终端模块数据源）。"""

import threading
import time
from collections import deque

_RING = deque(maxlen=800)
_SEQ = 0
_LOCK = threading.Lock()


def push(msg, tag="INFO"):
    """追加一条日志（线程安全，自动截断超长行）。"""
    global _SEQ
    with _LOCK:
        _SEQ += 1
        _RING.append({"seq": _SEQ, "ts": time.time(), "tag": tag,
                      "msg": str(msg)[:500]})


def since(seq):
    """返回 seq 之后的所有日志（轮询增量）。"""
    with _LOCK:
        return [r for r in _RING if r["seq"] > seq]


def tail(n=200):
    with _LOCK:
        return list(_RING)[-n:]


def last_seq():
    with _LOCK:
        return _SEQ


def stamp_now():
    t = time.time()
    lt = time.localtime(t)
    return "[%s.%03d]" % (time.strftime("%H:%M:%S", lt), int((t % 1) * 1000))


# ---------- API 调用追踪（终端模块：状态/耗时/发送与接收内容） ----------

_API = deque(maxlen=300)
_API_SEQ = 0


def push_api(kind, request, response, ok, ms):
    """记录一次云端 API 调用（线程安全，内容自动截断）。

    同时同步进主日志流（tag=API）——终端滚动页直接可见每次调用的
    状态/耗时与发送、接收内容，方便排查问题环节。"""
    global _API_SEQ
    with _LOCK:
        _API_SEQ += 1
        _API.append({"seq": _API_SEQ, "ts": time.time(), "kind": kind,
                     "ok": bool(ok), "ms": int(ms),
                     "request": str(request)[:1600],
                     "response": str(response)[:1600]})
    # 注意：push 内部也加锁，必须在 with 块外调用（Lock 不可重入）
    push("[%s] %s %dms" % (kind, "成功" if ok else "失败", int(ms)), tag="API")
    if request:
        push("[%s 发送] %s" % (kind, str(request)[:380]), tag="API")
    push("[%s 返回] %s" % (kind, str(response)[:380]), tag="API")


def api_since(seq):
    with _LOCK:
        return [r for r in _API if r["seq"] > seq]


def api_all():
    with _LOCK:
        return list(_API)
