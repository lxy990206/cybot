#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三层记忆库 MemoryVault（嵌入式 SQLite 向量数据库 · 多时空云就绪 v2）

对齐「天依物语」MemoryVault.js 与 docs/CLOUD_SYNC_CONTRACT.md 数据契约。

时空模型（多租户预留）：
  appId(时空ID) → userId(用户ID) → conversationId(存档线) → 记忆记录(mid/long)
  - recall() 永远只返回本 appId 数据
  - crossSense() 跨时空感应：独立只读通道，本地单时空恒返回空，云端激活

三层记忆：
  短期：24h 内完整对话上下文（设备本地资产，不上云）；10 条滚动注入 + 检索匹配结合
  中期：每日对当天上下文的 AI 总结（带向量，mentionDates 按天去重计数）
  长期：每 30 天对中期记忆的总结；唯一提及天数 <3 的到期中期自动丢弃；
        60 天未被提及自动遗忘；关键记忆（关系/生日/身份/共同记忆）永不遗忘且直写长期

向量后端：硅基流动 BAAI/bge-m3（1024 维）+ BAAI/bge-reranker-v2-m3 重排序
降级链：向量检索 → 重排 → 动态阈值注入；rerank 失败用余弦序；全失败用 bigram 重叠
云同步：attach_cloud_sync(adapter)（health/push/pull/crossSense 契约，服务端未实现）
"""

import json
import math
import os
import random
import re
import sqlite3
import struct
import threading
import time

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))


def _log(msg):
    t = time.time()
    lt = time.localtime(t)
    print("[%s.%03d] [MemoryVault] %s" %
          (time.strftime("%H:%M:%S", lt), int((t % 1) * 1000), msg), flush=True)


# ============================ 嵌入客户端（硅基流动） ============================

class EmbeddingClient:
    """BAAI/bge-m3（1024 维）嵌入 + BAAI/bge-reranker-v2-m3 重排序。"""

    def __init__(self, base_url, api_key, embed_model="BAAI/bge-m3",
                 rerank_model="BAAI/bge-reranker-v2-m3", timeout=30):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.embed_model = embed_model
        self.rerank_model = rerank_model
        self.timeout = timeout
        self.available = True

    def embed(self, texts):
        """批量嵌入 → 向量二维数组；失败返回 None（调用方降级）。"""
        if not texts:
            return None
        try:
            r = requests.post(
                self.base + "/embeddings",
                headers={"Authorization": "Bearer " + self.key,
                         "Content-Type": "application/json"},
                json={"model": self.embed_model, "input": [str(t)[:1500] for t in texts]},
                timeout=self.timeout)
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            vecs = [d["embedding"] for d in data]
            return vecs if vecs and all(vecs) else None
        except Exception as e:
            _log("嵌入失败（降级检索）：%r" % e)
            return None

    def embed_one(self, text):
        if not text:
            return None
        vecs = self.embed([text])
        return vecs[0] if vecs else None

    def rerank(self, query, docs):
        """重排序 → [{index, relevance_score}]；失败返回 None（余弦序兜底）。"""
        if not docs:
            return None
        try:
            r = requests.post(
                self.base + "/rerank",
                headers={"Authorization": "Bearer " + self.key,
                         "Content-Type": "application/json"},
                json={"model": self.rerank_model, "query": str(query)[:1500],
                      "documents": [str(d)[:1500] for d in docs], "top_n": len(docs)},
                timeout=self.timeout)
            r.raise_for_status()
            results = r.json().get("results", [])
            return [{"index": x["index"], "relevance_score": x["relevance_score"]}
                    for x in results]
        except Exception as e:
            _log("重排失败（余弦序兜底）：%r" % e)
            return None


# ============================ 云同步适配器（契约预留） ============================

class NullCloudSyncAdapter:
    """默认适配器：全部 no-op，crossSense 恒空。服务端上线后替换实现即可。"""

    def health(self):
        return {"ok": False, "message": "未配置云端同步"}

    def push(self, export_v2):
        return {"skipped": True, "message": "未配置云端同步"}

    def pull(self, since, app_id):
        return {"skipped": True, "message": "未配置云端同步"}

    def cross_sense(self, query, opts=None):
        return []


# ============================ 记忆库主体 ============================

class MemoryVault:
    APP_ID = "tianyi_stories"
    USER_ID = "local"

    # 关键记忆模式（称呼/生日/身份/偏好/共同记忆）：命中即直写长期，永不遗忘
    KEY_PATTERNS = re.compile(
        r"生日|出生|\d+\s*岁|我叫|我的名字|叫我|昵称|我是做|我的职业|我的工作|在读|学校|毕业|"
        r"纪念日|女朋友|男朋友|老婆|老公|结婚|订婚|同居|我们曾经|约定|承诺|一起去|我们一起|旅行|"
        r"上次我们|我最喜欢|我最爱|我的爱好|养了|宠物|家人|爸爸|妈妈|弟弟|妹妹")

    def __init__(self, cfg, summarizer, api_key=None, embedder=None):
        """summarizer(prompt)->str|None：AI 总结回调（复用对话 LLM，失败返回 None 走降级）。
        embedder：外部嵌入客户端（需有 embed/embed_one/rerank/available），缺省内置硅基流动客户端。"""
        m = cfg.get("memory", {})
        self.dir = os.path.join(ROOT, m.get("dir", "cybot_data/memory"))
        os.makedirs(self.dir, exist_ok=True)
        self.db_path = os.path.join(self.dir, m.get("db", "vault.db"))
        self.app_id = m.get("app_id", self.APP_ID)
        self.user_id = m.get("user_id", self.USER_ID)

        self.short_ttl = int(m.get("short_ttl_hours", 24)) * 3600 * 1000
        self.forget_ms = int(m.get("forget_days", 60)) * 86400000
        self.consolidate_ms = int(m.get("consolidate_days", 30)) * 86400000
        self.min_mentions = int(m.get("min_mentions", 3))
        self.short_window = int(m.get("short_window", 10))
        self.recall_top_k = int(m.get("recall_top_k", 4))
        self.recall_min_score = float(m.get("recall_min_score", 0.35))
        self.recall_short_top_k = int(m.get("recall_short_top_k", 3))
        self.core_limit = int(m.get("core_limit", 8))
        self.inject_limit = int(m.get("inject_limit", 12))

        e = m.get("embed", {})
        if embedder is not None:
            self.embedder = embedder
        else:
            self.embedder = EmbeddingClient(
                e.get("base_url", "https://api.siliconflow.cn/v1"), api_key,
                e.get("model", "BAAI/bge-m3"), e.get("rerank_model", "BAAI/bge-reranker-v2-m3"))
        self._summarize = summarizer
        self.cloud_sync = NullCloudSyncAdapter()

        self._lock = threading.RLock()
        self._maint_lock = threading.Lock()     # 整合任务互斥（防懒维护与手动触发并发重复）
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self._init_tables()
        self._migrate_legacy()
        self.is_initialized = True
        _log("初始化完成：%s（向量后端 bge-m3 / reranker-v2-m3）" % self.db_path)

    # ---------- 存储层 ----------

    def _init_tables(self):
        with self._lock:
            c = self.db.cursor()
            c.execute("""CREATE TABLE IF NOT EXISTS short_term (
                id TEXT PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL, ts INTEGER NOT NULL)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_short_ts ON short_term(ts)")
            mid_cols = """id TEXT PRIMARY KEY, dateKey TEXT, summary TEXT NOT NULL,
                vector BLOB, appId TEXT, userId TEXT, userName TEXT, conversationId TEXT,
                mentionDates TEXT DEFAULT '[]', mentions INTEGER DEFAULT 0,
                degraded INTEGER DEFAULT 0, createdAt INTEGER, updatedAt INTEGER,
                lastMentioned INTEGER"""
            c.execute("CREATE TABLE IF NOT EXISTS mid_term (%s)" % mid_cols)
            long_cols = mid_cols.replace("summary TEXT NOT NULL", "content TEXT NOT NULL") + \
                ", isKey INTEGER DEFAULT 0, source TEXT DEFAULT 'manual'"
            c.execute("CREATE TABLE IF NOT EXISTS long_term (%s)" % long_cols)
            c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
            self.db.commit()

    @staticmethod
    def _vec_pack(vec):
        return struct.pack("<%df" % len(vec), *vec) if vec else None

    @staticmethod
    def _vec_unpack(blob):
        if not blob:
            return None
        return list(struct.unpack("<%df" % (len(blob) // 4), blob))

    def _meta_get(self, k, default=None):
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def _meta_set(self, k, v):
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
                            (k, json.dumps(v, ensure_ascii=False)))
            self.db.commit()

    @staticmethod
    def _date_key(ts=None):
        lt = time.localtime(ts if ts is not None else time.time())
        return "%04d-%02d-%02d" % (lt.tm_year, lt.tm_mon, lt.tm_mday)

    @staticmethod
    def _now():
        return int(time.time() * 1000)

    @staticmethod
    def _new_id():
        return "%x%s" % (int(time.time() * 1000),
                         "".join(random.choice("0123456789abcdefghijklmnopqrstuvwxyz")
                                 for _ in range(6)))

    def _tenant(self):
        return {"appId": self.app_id, "userId": self.user_id, "userName": "",
                "conversationId": self._meta_get("conversationId") or
                (self._meta_set("conversationId", "conv_" + self._new_id()) or
                 self._meta_get("conversationId"))}

    # ---------- 旧版 JSON 记忆迁移（v1 → SQLite，一次性） ----------

    def _migrate_legacy(self):
        with self._lock:
            n = self.db.execute("SELECT COUNT(*) FROM short_term").fetchone()[0] + \
                self.db.execute("SELECT COUNT(*) FROM long_term").fetchone()[0]
            if n > 0:
                return
            base = self.dir
            try:
                short = json.load(open(os.path.join(base, "short.json"), encoding="utf-8"))
                for t in short.get("turns", []):
                    self.db.execute("INSERT OR IGNORE INTO short_term VALUES(?,?,?,?)",
                                    (self._new_id(), t.get("role", "user"),
                                     t.get("content", ""), self._now()))
            except (OSError, ValueError):
                pass
            try:
                long = json.load(open(os.path.join(base, "long.json"), encoding="utf-8"))
                for fact in long.get("facts", []):
                    self.add_long(fact, is_key=True, source="manual", commit=False)
                mid = json.load(open(os.path.join(base, "mid.json"), encoding="utf-8"))
                if mid.get("summary"):
                    self.add_long(mid["summary"], is_key=False, source="manual", commit=False)
            except (OSError, ValueError):
                pass
            self.db.commit()
            _log("旧版 JSON 记忆迁移完成")

    # ================= 短期记忆（24h 完整上下文 · 本地资产） =================

    def add_exchange(self, user_text, ai_text):
        """写入短期；用户话命中关键模式则同时直写长期（永不遗忘）。"""
        now = self._now()
        with self._lock:
            for role, content in (("user", user_text), ("assistant", ai_text)):
                if content:
                    self.db.execute("INSERT INTO short_term VALUES(?,?,?,?)",
                                    (self._new_id(), role, content, now))
            self.db.execute("DELETE FROM short_term WHERE ts < ?", (now - self.short_ttl,))
            self.db.commit()
        if user_text and self.KEY_PATTERNS.search(user_text):
            self.add_long(user_text, is_key=True, source="key-auto")
            _log("关键记忆直写长期：%s" % user_text[:30])
        self.maintenance_lazy()

    def recent_exchanges(self, limit=10):
        """LLM 滚动窗口：最近 N 条消息（时间正序）。"""
        with self._lock:
            rows = self.db.execute(
                "SELECT role, content FROM short_term ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

    def recall_short(self, query, top_k=None, exclude_norms=None):
        """短期检索匹配：bigram 重叠，排除滚动窗口内已注入内容（防重复混乱）。"""
        if not self.is_initialized or not query:
            return []
        top_k = top_k or self.recall_short_top_k
        exclude = set(exclude_norms or [])
        with self._lock:
            rows = self.db.execute(
                "SELECT id, content FROM short_term ORDER BY ts DESC LIMIT 200").fetchall()
        ranked = []
        for rid, content in rows:
            norm = self.norm_text(content)
            if not norm or norm in exclude:
                continue
            s = self._overlap(query, content)
            if s > 0.15:
                ranked.append({"layer": "short", "id": rid, "content": content, "s": s})
        ranked.sort(key=lambda x: -x["s"])
        return [{k: r[k] for k in ("layer", "id", "content")} for r in ranked[:top_k]]

    # ================= 中期记忆（每日总结） =================

    def daily_consolidation(self):
        """当天上下文 → AI 总结 → 中期（带向量）；成功后清空已整合短期（交接）。"""
        if not self._maint_lock.acquire(blocking=False):
            return False                          # 已有整合任务在跑，跳过
        try:
            return self._daily_consolidation_locked()
        finally:
            self._maint_lock.release()

    def _daily_consolidation_locked(self):
        today = self._date_key()
        with self._lock:
            if self._meta_get("lastMidDate") == today:
                return False
            since = self._meta_get("lastConsolidationTs", 0)
            rows = self.db.execute(
                "SELECT id, role, content, ts FROM short_term WHERE ts >= ? ORDER BY ts",
                (since,)).fetchall()
            if not rows:
                self._meta_set("lastMidDate", today)
                return False
        dialogue = "\n".join(("用户" if r[1] == "user" else "天依") + ": " + r[2] for r in rows)
        summary = self._summarize(
            "把以下一段时间的对话整理成一份简洁的记忆摘要（150字以内），"
            "保留重要的个人信息、偏好、约定、情绪事件，用第三人称陈述，不要寒暄：\n" + dialogue)
        degraded = not summary
        content = summary or self._local_summarize(rows)
        vec = None if degraded else self._vec_pack(self.embedder.embed_one(content))
        date_key = self._date_key((rows[-1][3] / 1000))
        now = self._now()
        with self._lock:
            self.db.execute(
                "INSERT INTO mid_term(id,dateKey,summary,vector,appId,userId,userName,"
                "conversationId,mentionDates,mentions,degraded,createdAt,updatedAt,lastMentioned) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self._new_id(), date_key, content, vec, self.app_id, self.user_id, "",
                 self._tenant()["conversationId"], json.dumps([]), 0, int(degraded), now, now, now))
            for r in rows:
                self.db.execute("DELETE FROM short_term WHERE id=?", (r[0],))
            self.db.commit()
            self._meta_set("lastConsolidationTs", rows[-1][3])
            self._meta_set("lastMidDate", today)
        _log("每日整合完成：%s%s" % (date_key, "（AI 总结失败，降级内容下轮重做）" if degraded else ""))
        return True

    # ================= 长期记忆（30 天整合 + 衰减遗忘） =================

    def monthly_consolidation(self):
        """到期中期：提及天数≥3 → AI 总结入长期；<3 自动丢弃；长期 60 天衰减。"""
        if not self._maint_lock.acquire(blocking=False):
            return False
        try:
            return self._monthly_consolidation_locked()
        finally:
            self._maint_lock.release()

    def _monthly_consolidation_locked(self):
        now = self._now()
        with self._lock:
            if now - self._meta_get("lastLongRun", 0) < self.consolidate_ms:
                return False
            mids = self._rows("mid_term")
            mature = [m for m in mids if now - (m["createdAt"] or 0) >= self.consolidate_ms]
            keep = [m for m in mature if len(m["mentionDates"]) >= self.min_mentions]
            dropped = len(mature) - len(keep)
            for m in mature:
                if len(m["mentionDates"]) < self.min_mentions:
                    self._delete_row("mid_term", m["id"], tombstone=True)
            if keep:
                joined = "\n".join("[%s] %s" % (m["dateKey"], m["summary"]) for m in keep)
                summary = self._summarize(
                    "把以下一个月的记忆摘要提炼成一份长期核心记忆（250字以内），"
                    "只保留最重要的事实与关系，用第三人称简洁陈述：\n" + joined)
                content = summary or joined[:500]
                vec = self._vec_pack(self.embedder.embed_one(content))
                self.db.execute(
                    "INSERT INTO long_term(id,dateKey,content,vector,appId,userId,userName,"
                    "conversationId,mentionDates,mentions,degraded,createdAt,updatedAt,"
                    "lastMentioned,isKey,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self._new_id(), self._date_key(), content, vec, self.app_id,
                     self.user_id, "", self._tenant()["conversationId"], json.dumps([]), 0,
                     int(not summary), now, now, now, 0, "monthly"))
            forgotten = 0
            for l in self._rows("long_term"):
                if not l["isKey"] and l["lastMentioned"] and \
                        now - l["lastMentioned"] > self.forget_ms:
                    self._delete_row("long_term", l["id"], tombstone=True)
                    forgotten += 1
            self.db.commit()
            self._meta_set("lastLongRun", now)
        _log("30天整合：入长期 %d 条，丢弃低频中期 %d 条，遗忘长期 %d 条" %
             (len(keep) if keep else 0, dropped, forgotten))
        return True

    def add_long(self, content, is_key=False, source="manual", commit=True):
        if not content:
            return None
        vec = self._vec_pack(self.embedder.embed_one(content))
        now = self._now()
        with self._lock:
            self.db.execute(
                "INSERT INTO long_term(id,dateKey,content,vector,appId,userId,userName,"
                "conversationId,mentionDates,mentions,degraded,createdAt,updatedAt,"
                "lastMentioned,isKey,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self._new_id(), self._date_key(), content, vec, self.app_id, self.user_id,
                 "", self._tenant()["conversationId"], json.dumps([]), 0, 0, now, now, now,
                 int(is_key), source))
            if commit:
                self.db.commit()
        return content

    # ================= 动态检索（上下文注入核心） =================

    def recall(self, query, top_k=None, min_score=None):
        """向量初筛 top8 → 重排 → 动态阈值注入（全滤保最优 1 条防失忆）。

        返回 [{layer('mid'|'long'), id, content, isKey, dateKey}]，命中即计提及。
        """
        if not self.is_initialized or not query:
            return []
        top_k = top_k or self.recall_top_k
        min_score = self.recall_min_score if min_score is None else min_score
        pool = [{"layer": "mid", "ref": m, "text": m["summary"]}
                for m in self._rows("mid_term")] + \
               [{"layer": "long", "ref": l, "text": l["content"]}
                for l in self._rows("long_term")]
        if not pool:
            return []

        ranked = []
        qvec = self.embedder.embed_one(query)
        scored = []
        if qvec:
            for p in pool:
                if p["ref"]["vector"]:
                    scored.append((self._cosine(qvec, p["ref"]["vector"]), p))
            scored.sort(key=lambda x: -x[0])
            top8 = [p for _, p in scored[:8]]
            if top8:
                rr = self.embedder.rerank(query, [p["text"] for p in top8])
                if rr:
                    picked = sorted(rr, key=lambda x: -x["relevance_score"])
                    picked = [p for p in picked if p["relevance_score"] >= min_score][:top_k]
                    if not picked and rr:
                        picked = [max(rr, key=lambda x: x["relevance_score"])]
                    ranked = [top8[p["index"]] for p in picked if 0 <= p["index"] < len(top8)]
                else:
                    ranked = top8[:top_k]
        if not ranked:
            ranked = sorted(pool, key=lambda p: -self._overlap(query, p["text"]))
            ranked = [p for p in ranked if self._overlap(query, p["text"]) > 0.15][:top_k]

        self._mark_mentioned(ranked)
        return [{"layer": r["layer"], "id": r["ref"]["id"], "content": r["text"],
                 "isKey": bool(r["ref"].get("isKey")),
                 "dateKey": r["ref"].get("dateKey")} for r in ranked]

    def _mark_mentioned(self, ranked):
        """命中即视为提及：mentionDates 当日去重计数；lastMentioned 续期（衰减续命）。"""
        today, now = self._date_key(), self._now()
        with self._lock:
            for r in ranked:
                ref = r["ref"]
                dates = ref["mentionDates"]
                if today not in dates:
                    dates.append(today)
                ref["mentions"] = len(dates)
                ref["lastMentioned"] = now
                ref["updatedAt"] = now
                table = "mid_term" if r["layer"] == "mid" else "long_term"
                col = "summary" if r["layer"] == "mid" else "content"
                self.db.execute(
                    "UPDATE %s SET mentionDates=?, mentions=?, lastMentioned=?, updatedAt=? "
                    "WHERE id=?" % table,
                    (json.dumps(dates), len(dates), now, now, ref["id"]))
            self.db.commit()

    def get_core_memories(self, limit=None):
        """核心(⭐)记忆常驻集：isKey 长期记忆，防失忆底线（无论检索是否命中都注入）。"""
        limit = limit or self.core_limit
        rows = sorted((l for l in self._rows("long_term") if l["isKey"]),
                      key=lambda l: -(l.get("lastMentioned") or 0))[:limit]
        return [{"layer": "long", "id": l["id"], "content": l["content"],
                 "isKey": True, "dateKey": l.get("dateKey")} for l in rows]

    # ---------- 综合检索（供对话注入一次调用） ----------

    def recall_for_prompt(self, user_text, history=None):
        """合并检索：核心常驻 → 中长期检索重排 → 短期检索补充（id 去重，总量护栏）。"""
        history = history or []
        query = self.retrieval_query(user_text, history)
        midlong = self.recall(query)
        cores = self.get_core_memories()
        window_norms = [self.norm_text(h["content"]) for h in history]
        shorts = self.recall_short(query, exclude_norms=window_norms)
        seen, out = set(), []
        for m in cores + midlong + shorts:
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            out.append(m)
        return out[:self.inject_limit]

    @staticmethod
    def format_memory_block(memories):
        if not memories:
            return ""
        lines = []
        for m in memories:
            tag = ("核心" if m["isKey"] else "长期") if m["layer"] == "long" \
                else ("中期" if m["layer"] == "mid" else "近期")
            date = "(%s)" % m["dateKey"] if m.get("dateKey") else ""
            lines.append("- [%s]%s %s" % (tag, date, m["content"]))
        return ("你回忆起来的一些相关记忆（自行判断是否自然提及，不要罗列式复述）：\n"
                + "\n".join(lines))

    def retrieval_query(self, text, history=None):
        """检索查询构造：当前消息过短（归一化 <8 字）时拼上前一条用户消息增强信号。"""
        t = str(text or "").strip()
        if len(self.norm_text(t)) >= 8:
            return t
        prev = next((h["content"] for h in reversed(history or [])
                     if h["role"] == "user" and h["content"] != t), None)
        return ("%s %s" % (prev, t)) if prev else t

    # ================= 维护任务 =================

    def update_embed_client(self, client):
        """热更新嵌入/重排客户端（WebUI 修改嵌入模型配置后调用）。"""
        self.embedder = client
        _log("嵌入客户端已热更新：%s" % type(client).__name__)

    def maintenance(self):
        """启动/每日触发：每日整合补做 + 30 天整合 + 衰减。"""
        try:
            self.daily_consolidation()
            self.monthly_consolidation()
        except Exception as e:
            _log("维护任务失败：%r" % e)

    def maintenance_lazy(self):
        """每次写入后检查：跨天则后台补做每日整合（不阻塞对话）。"""
        if self._meta_get("lastMidDate") != self._date_key():
            threading.Thread(target=self.maintenance, daemon=True).start()

    # ================= 可视化管理接口 =================

    def list_all(self, layer, query=""):
        table = {"short": "short_term", "mid": "mid_term", "long": "long_term"}[layer]
        with self._lock:
            rows = self.db.execute("SELECT * FROM %s" % table).fetchall()
            cols = [d[0] for d in self.db.execute("SELECT * FROM %s LIMIT 0" % table).description]
        items = []
        for row in rows:
            rec = dict(zip(cols, row))
            rec.pop("vector", None)
            if "mentionDates" in rec:
                rec["mentionDates"] = json.loads(rec.get("mentionDates") or "[]")
            items.append(rec)
        items.sort(key=lambda r: -(r.get("ts") or r.get("createdAt") or 0))
        if query:
            items = [r for r in items if query in json.dumps(r, ensure_ascii=False)]
        return items

    def delete_memory(self, layer, rid):
        table = {"short": "short_term", "mid": "mid_term", "long": "long_term"}[layer]
        self._delete_row(table, rid, tombstone=(layer != "short"))

    def toggle_key(self, rid):
        with self._lock:
            row = self.db.execute(
                "SELECT isKey FROM long_term WHERE id=?", (rid,)).fetchone()
            if not row:
                return None
            nk = 0 if row[0] else 1
            self.db.execute("UPDATE long_term SET isKey=?, updatedAt=? WHERE id=?",
                            (nk, self._now(), rid))
            self.db.commit()
        return bool(nk)

    def manual_add_long(self, content, is_key=True):
        self.add_long(content, is_key=is_key, source="manual")

    def get_stats(self):
        with self._lock:
            s = self.db.execute("SELECT COUNT(*) FROM short_term").fetchone()[0]
            m = self.db.execute("SELECT COUNT(*) FROM mid_term").fetchone()[0]
            l, k = self.db.execute(
                "SELECT COUNT(*), SUM(isKey) FROM long_term").fetchone()
        return {"short": s, "mid": m, "long": l, "key": k or 0,
                "embedAvailable": self.embedder.available,
                "appId": self.app_id, "userId": self.user_id}

    # ================= 云端同步预留（契约见 docs/CLOUD_SYNC_CONTRACT.md） =================

    def attach_cloud_sync(self, adapter):
        if adapter:
            self.cloud_sync = adapter
            _log("云同步适配器已挂载：%s" % type(adapter).__name__)

    def sync_to_cloud(self):
        data = self.export_all()
        result = self.cloud_sync.push(data)
        if result.get("ok") and result.get("syncedAt"):
            self._meta_set("lastSyncAt", result["syncedAt"])
            acked = result.get("ackedDeletedIds") or []
            if acked:
                tombs = [t for t in self._meta_get("tombstones", []) if t not in acked]
                self._meta_set("tombstones", tombs)
        return result

    def export_all(self):
        did = self._meta_get("deviceId")
        if not did:
            did = "dev_" + self._new_id()
            self._meta_set("deviceId", did)
        return {
            "schema": 2, "appId": self.app_id, "deviceId": did,
            "exportedAt": self._now(),
            "mid": [self._export_row(r, "summary") for r in self._rows("mid_term")],
            "long": [self._export_row(r, "content") for r in self._rows("long_term")],
            "deletedIds": self._meta_get("tombstones", []),
            "lastSyncAt": self._meta_get("lastSyncAt", 0),
        }

    def _export_row(self, rec, text_col):
        out = dict(rec)
        out.pop("vector_raw", None)
        out["vector"] = rec["vector"]          # _rows 已解码为 float list（契约原生字段 summary/content）
        return out

    def import_all(self, data):
        """幂等 upsert（id+updatedAt LWW）；拒绝异时空数据（防污染）。"""
        if not data or data.get("schema") != 2:
            return False
        if data.get("appId") != self.app_id:
            _log("拒绝导入异时空数据：%s ≠ %s" % (data.get("appId"), self.app_id))
            return False
        for table, key in (("mid_term", "mid"), ("long_term", "long")):
            for rec in data.get(key) or []:
                if not rec.get("id"):
                    continue
                if self._row_updated(table, rec["id"]) > (rec.get("updatedAt") or 0):
                    continue
                vec = rec.get("vector")
                text_val = rec.get("summary") or rec.get("content") or ""
                with self._lock:
                    base_ph = "?,?,?,?,?,?,?,?,?,?,?,?,?,?"   # 14 列：mid/long 公共部分
                    ph = base_ph + (",?,?" if table == "long_term" else "")
                    self.db.execute(
                        "INSERT OR REPLACE INTO %s(id,dateKey,%s,vector,appId,userId,"
                        "userName,conversationId,mentionDates,mentions,degraded,createdAt,"
                        "updatedAt,lastMentioned%s) VALUES(%s)" % (
                            table, "summary" if table == "mid_term" else "content",
                            ",isKey,source" if table == "long_term" else "", ph),
                        (rec["id"], rec.get("dateKey"), text_val,
                         self._vec_pack(vec) if vec else None,
                         rec.get("appId", self.app_id), rec.get("userId", self.user_id),
                         rec.get("userName", ""), rec.get("conversationId", ""),
                         json.dumps(rec.get("mentionDates", [])),
                         len(rec.get("mentionDates", [])), int(rec.get("degraded", False)),
                         rec.get("createdAt", 0), rec.get("updatedAt", 0),
                         rec.get("lastMentioned", 0)) +
                        ((int(rec.get("isKey", False)), rec.get("source", "manual"))
                         if table == "long_term" else ()))
        with self._lock:
            self.db.commit()
        return True

    def cross_sense(self, query, top_k=2):
        """跨时空感应【时空回响】：本地单时空恒返回空（契约预留，云端激活）。"""
        try:
            return self.cloud_sync.cross_sense(query, {
                "appId": self.app_id, "userId": self.user_id,
                "topK": min(top_k, 2)}) or []
        except Exception:
            return []

    # ---------- 内部工具 ----------

    def _rows(self, table):
        with self._lock:
            cur = self.db.execute("SELECT * FROM %s" % table)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        out = []
        for r in rows:
            r["vector_raw"] = r.get("vector")
            r["vector"] = self._vec_unpack(r.get("vector"))
            r["mentionDates"] = json.loads(r.get("mentionDates") or "[]")
            r["isKey"] = bool(r.get("isKey"))
            out.append(r)
        return out

    def _row_updated(self, table, rid):
        with self._lock:
            row = self.db.execute(
                "SELECT updatedAt FROM %s WHERE id=?" % table, (rid,)).fetchone()
        return row[0] if row else 0

    def _delete_row(self, table, rid, tombstone=False):
        with self._lock:
            self.db.execute("DELETE FROM %s WHERE id=?" % table, (rid,))
            self.db.commit()
        if tombstone:
            tombs = self._meta_get("tombstones", [])
            if rid not in tombs:
                tombs.append(rid)
            self._meta_set("tombstones", tombs[-500:])

    @staticmethod
    def norm_text(s):
        return re.sub(r"[\s，。！？!?.,、~～…（）()\[\]【】*·：:；;'\"「」『』]", "",
                      str(s or "").lower())

    @staticmethod
    def _cosine(a, b):
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na and nb else 0.0

    @staticmethod
    def _overlap(q, t):
        grams = lambda s: {s[i:i + 2] for i in range(len(s) - 1)}
        gq, gt = grams(MemoryVault.norm_text(q)), grams(MemoryVault.norm_text(t))
        if not gq or not gt:
            return 0.0
        return len(gq & gt) / len(gq)

    @staticmethod
    def _local_summarize(rows):
        user_msgs = [r[2] for r in rows if r[1] == "user"][:10]
        return "当天聊到：" + "；".join(user_msgs)[:200]
