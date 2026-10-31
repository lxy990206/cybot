# 天依物语 · 记忆云同步契约文档（CLOUD_SYNC_CONTRACT）

> 版本：v1.0（2026-08-31）
> 状态：**服务端未实现**。本文档是端侧与未来记忆云服务之间的完整契约——
> 服务端开发者/AI Agent 依据本文档实现服务即可与端侧直接互通，端侧代码无需再改。
> 端侧对应版本：app-dev 1.1.8-Beta(119)（MemoryVault dbVersion 2）。

---

## 1. 时空模型（第一性概念）

```
时空ID (appId)          ← 平行宇宙边界：云端多应用数据混合共存，跨时空物理不可达
  └─ 用户ID (userId)    ← 时空内的用户边界（Milvus partition key / PG RLS 第二策略）
      └─ 对话ID (conversationId) ← 存档线（元数据，非隔离键；默认跨存档延续记忆）
          └─ 记忆记录 (mid/long)
```

| 维度 | 取值 | 稳定性 | 用途 |
|---|---|---|---|
| appId | `tianyi_stories`（稳定技术标识，非显示名「天依物语」） | **一经上云不可改**（改名=数据迁移） | 时空隔离、路由、导出顶层键 |
| userId | 登录账号 ID（本地未登录时为 `local`） | 登录后固定 | 用户隔离、感应权限边界 |
| conversationId | 存档线 ID（`conv_` 前缀） | 每存档一线 | 溯源、按存档过滤（可选功能） |

**两条检索通道（物理分离）**：
- **本时空检索 recall**：严格只查本 appId 数据。跨时空数据永远不进入常规【回忆】。
- **跨时空感应 crossSense【时空回响】**：显式独立只读通道，仅同 userId 的其他 appId 摘要级记忆。叙事上表现为「天依隐约感应到另一个时空的自己与用户的羁绊」——**本质隔离，仅残留回响**。

**隔离为硬性验收项**：时空 A 的凭证常规检索必须查不到时空 B 的任何数据（见 §8 用例 2）。

## 2. 数据契约（exportAll v2 · 端侧已实现）

端侧导出 JSON（`window.memoryVault.exportAll()`）：

```json
{
  "schema": 2,
  "appId": "tianyi_stories",
  "deviceId": "dev_mthg7hvdg8icsp",
  "exportedAt": 1725123456789,
  "mid":  [ /* 中期记忆记录，见下 */ ],
  "long": [ /* 长期记忆记录，见下 */ ],
  "deletedIds": ["m_abc123"],
  "lastSyncAt": 1725100000000
}
```

**注意：无 `short` 字段——短期记忆（24h 完整对话上下文）为设备本地资产，永不上云。**

记忆记录结构（mid 与 long 共用，差异字段注明）：

| 字段 | 类型 | 说明 |
|---|---|---|
| id | string | 记录唯一 ID（`Date.now().toString(36)+random`；端/云/PG/Milvus 同一 id） |
| appId / userId / userName / conversationId | string | 租户元数据（userName 仅展示用，可变，不参与隔离） |
| summary (mid) / content (long) | string | 文本内容 |
| vector | number[] \| null | 1024 维 bge-m3 向量；**null 时服务端补嵌**（见 §5） |
| mentionDates | string[] | 唯一提及日期（`YYYY-MM-DD`）；淘汰判定用其长度 |
| mentions | int | 兼容旧字段 = mentionDates.length |
| degraded | bool | AI 总结失败的降级内容（可被下轮整合重做覆盖） |
| isKey (long) | bool | 核心记忆（永不遗忘/不衰减） |
| dateKey (mid) | string | 归属日期 |
| lastMentioned / createdAt / updatedAt | number | 毫秒时间戳；**updatedAt 为同步游标与 LWW 依据** |
| source (long) | string | `monthly` / `key-auto` / `manual` |
| device_id（服务端补充） | string | 记录写入来源设备（服务端从 deviceId 填充） |

**importAll 端侧规则（服务端 pull 返回结构须满足）**：
- v2 结构同上；端侧校验 `data.appId === 'tianyi_stories'`，**异时空数据拒绝导入**；
- 逐条 `id + updatedAt` 比较，**LWW（Last-Write-Wins）**：仅来者 updatedAt ≥ 本地才覆盖；
- `deletedIds` 为服务端下发 tombstone 时，端侧需删除对应记录（未来 CloudSyncAdapter 实现时处理）。

## 3. 同步协议

- **push**：客户端 `push(exportV2)` → 服务端幂等 upsert（PG 为主）+ tombstone 落库 + 异步投递 Milvus。返回 `{ok, syncedAt, ackedDeletedIds}`——ACK 的墓碑端侧可清理。
- **pull**：客户端 `pull(since, appId)` → 服务端返回 `since < updatedAt` 的全量变更记录 + 未 ACK 墓碑。返回 `{ok, data: <exportV2 结构>, unchanged?: true}`。
- **游标**：`lastSyncAt`（毫秒时间戳）。用服务端 `syncedAt`（服务器时钟）推进，避免客户端时钟漂移。
- **冲突**：LWW by updatedAt。端侧写入本地记录时必然刷新 updatedAt，天然形成版本链。
- **频率**：端侧建议打开 App / 跨天整合后触发；服务端按 appId+userId 限流（如 10 次/分钟）。

## 4. 检索契约（/recall）

```
POST /recall
{ "appId": "tianyi_stories", "userId": "...", "query": "...", "topK": 3 }
→ { "ok": true, "memories": [
     { "layer": "long", "id": "...", "content": "...", "isKey": true, "dateKey": null }
   ] }
```

- 服务端实现：Milvus search（过滤 `appId+userId`，layer ∈ {mid, long}，top 8）→ 服务端 rerank（bge-reranker-v2-m3）→ top 3 返回。
- **返回结构必须兼容端侧【回忆】块注入格式**：`layer('mid'|'long') / id / content / isKey / dateKey`。
- 服务端须同步维护命中计数：mentionDates 当日去重追加 + lastMentioned 续期，并回写 PG（权威）。
- 层级标记渲染：mid→「近期」，long+isKey→「核心」，long→「长期」。

## 5. 目标库建议（Milvus + PostgreSQL）

**职责分工**：PG 为 **source of truth**（事务/计数/tombstone），Milvus 为**派生检索索引**（可随时从 PG 全量 reindex 重建；写失败进重试队列，容忍秒级陈旧，不追求双写强一致）。

**PostgreSQL**：

```sql
CREATE TABLE memories (
  id            VARCHAR(64) NOT NULL,
  app_id        VARCHAR(64) NOT NULL,
  user_id       VARCHAR(64) NOT NULL,
  conversation_id VARCHAR(64),
  layer         SMALLINT NOT NULL,        -- 1=mid 2=long
  content       TEXT NOT NULL,
  is_key        BOOLEAN DEFAULT false,
  mention_dates TEXT[] DEFAULT '{}',      -- 唯一提及日期
  degraded      BOOLEAN DEFAULT false,
  vector_dirty  BOOLEAN DEFAULT false,    -- vector 为 null 待补嵌
  created_at    TIMESTAMPTZ NOT NULL,
  updated_at    TIMESTAMPTZ NOT NULL,     -- 同步游标 + LWW
  last_mentioned_at TIMESTAMPTZ,
  device_id     VARCHAR(64),
  deleted_at    TIMESTAMPTZ,              -- tombstone 软删除
  PRIMARY KEY (app_id, id)                -- 复合主键：时空第一维
);
CREATE INDEX idx_sync ON memories (app_id, user_id, updated_at DESC);
CREATE INDEX idx_tomb ON memories (app_id, deleted_at) WHERE deleted_at IS NOT NULL;

ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
CREATE POLICY app_isolation ON memories USING (app_id = current_setting('app.id'));
```

- tombstone 清理：每日 cron 物理删除 `deleted_at < now() - interval '7 days'` 的记录（7 天内保证多设备传播窗口）。
- userName 仅展示，不入隔离键。

**Milvus**（推荐每应用一个 Database，物理隔离）：

```
Collection: memories
  id        VARCHAR  PK
  user_id   VARCHAR  partition key（同 userId 自动同分区，查询剪枝）
  layer     INT8
  vector    FLOAT_VECTOR(1024)   # bge-m3；HNSW(M=16, efConstruction=200)，metric=IP（归一化后≡COSINE）
检索 expr: app 在 Database 级隔离（双保险可再带 app_id 字段过滤）
```

- **存量 vector 直迁不重嵌**：端侧导出记录自带 vector，服务端直接 upsert；
- 仅 `vector: null`（degraded/降级写入）记录由**服务端补嵌**（服务端持 SiliconFlow 密钥）。

## 6. 安全要求（硬性）

1. **嵌入密钥收归服务端**：SiliconFlow key 只存服务端（KMS/环境变量），服务端做嵌入代理（按 appId 限流计费）。**多用户架构下客户端持全局密钥不可接受**（端侧 XOR 混淆仅防静态提取，防不住动态 hook）。
2. 客户端仅持**应用级 token**（按 app 签发、可撤销、可限频），由原生 SecureStoreBridge 存储（XOR + 私有 SharedPreferences）。
3. 全链路 HTTPS；token 不过期时间 ≤ 90 天，支持 refresh。
4. 日志脱敏：记忆内容不得进访问日志。

## 7. 端侧适配点（服务端上线时只需实现一个 Adapter）

```javascript
class MyCloudAdapter extends CloudSyncAdapter {   // 基类骨架端侧已就位
  async health()   { /* GET /health → {ok, serverTime, supports:{push,pull,recall,crossSense}} */ }
  async push(d)    { /* POST /sync/push   body=exportV2 → {ok, syncedAt, ackedDeletedIds} */ }
  async pull(since, appId) { /* GET /sync/pull?since=..&appId=.. → {ok, data} */ }
  async crossSense(q, o)   { /* POST /cross-recall → [{content,layer,dateKey,sourceApp}] */ }
}
window.memoryVault.attachCloudSync(new MyCloudAdapter());
```

- 端侧 `syncToCloud()` = health + push 组合（失败静默跳过，不打断本地功能）。
- 墓碑 ACK 后端侧可调 `vault._metaSet('tombstones', acked 以外)` 清理（实现时约定）。
- 错误码约定：HTTP 401（token 失效→端侧提示重新登录）、429（限流→退避）、5xx（重试）。

## 8. 验收用例（服务端交付必测）

| # | 场景 | 预期 |
|---|---|---|
| 1 | 多设备同步 | 设备 A 写入记忆 → push → 设备 B pull → B 本地出现该记录（id/updatedAt 一致） |
| 2 | **时空隔离** | 应用 B 凭证对应用 A 的 appId 调 /recall → 必须返回空（硬性红线） |
| 3 | 删除传播 | 设备 A 删除记忆 → tombstone push → 设备 B pull 后本地同步删除 |
| 4 | 感应权限 | 用户 A 无法通过 /cross-recall 读到用户 B 的任何数据 |
| 5 | 感应返回 | /cross-recall 返回条目必须带 sourceApp；topK ≤ 2；仅 mid/long 摘要级 |
| 6 | 感应开关 | 端侧 crossSenseEnabled=false 时端侧不发请求；服务端亦可对该用户强制关闭 |
| 7 | LWW 冲突 | 两设备同 id 不同 updatedAt → 双方 pull 后均收敛为 updatedAt 较大版本 |
| 8 | 降级重做 | degraded=true 记录被端侧整合重做覆盖后 → push → 服务端同 id 记录更新 |
| 9 | 补嵌 | vector=null 记录 push 后 → 服务端补嵌 → /recall 可命中该记录 |
| 10 | 换新存档 | 同用户新 conversationId → /recall 仍返回旧存档记忆（延续语义） |

## 9. 端侧已就绪状态清单（本契约的实现基础）

- MemoryVault dbVersion 2：租户字段迁移回填 / mentionDates 按天去重 / 整合窗口过滤+清空 / keyPatterns 收紧 / degraded 标记
- exportAll v2 / importAll LWW+异时空拒绝 / tombstone 记录（`deleteMemory` 已 await）
- crossSense() 通道 + 【时空回响】prompt 块（AIChatManager，触发纪律：会话限 1 次 / 基础 20% / 关键词 60% / 用户开关）
- CloudSyncAdapter 骨架 + NullCloudAdapter（默认挂载，云操作 no-op）+ SecureStoreBridge（原生 token 存储）
- 单测 28/28（tests/memory_vault_v2.test.js）、浏览器 E2E 7/7

## 10. 跨时空感应协议（【时空回响】专项）

**权限红线（服务端实现必须遵守）**：
- 仅同 userId 跨 appId——**绝不跨用户**；
- 仅 mid/long 摘要级内容（不含长期核心记忆完整原文，服务端只返回 summary 级文本）；
- 只读（不修改源时空任何数据，不写 mentionDates/lastMentioned）；
- topK ≤ 2，服务端按 userId 会话级独立频控（如 6 次/小时）。

**接口**：
```
POST /cross-recall
{ "appId": "tianyi_stories",   // 发起方时空（排除自身）
  "userId": "...",
  "query": "...",
  "topK": 2 }
→ { "ok": true, "echoes": [
     { "content": "...", "layer": "mid", "dateKey": "2026-08-01", "sourceApp": "other_app_id" }
   ] }
```

**Prompt 约定（端侧已实现，服务端仅需供数）**：感应内容以「模糊回响/梦境碎片/似曾相识」的奇幻口吻呈现，明确要求 AI 不将其陈述为确切记忆、不强行提及——保持「本质隔离，仅残留回响」的世界观一致性。

**触发纪律（端侧已实现）**：每会话最多 1 次；基础概率 ~20%；用户提及「梦/似曾相识/另一个世界/平行/前世/既视」时提升至 ~60%；用户可全局关闭（设置 → 数据 → 时空感应）。
