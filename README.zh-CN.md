# memory-engine

**给 LLM Agent 的生命周期管理记忆引擎——带真源锚定、新鲜度协议与机械回写验证。**

[English](README.md) | 简体中文

不是一个又一个「会话级记忆存储」。大多数 Agent 记忆方案回答的是「我们聊过什么」——这个回答的是：**「哪些记忆现在仍然为真？新鲜度如何？自我上次读取以来谁改了它？」**

已在单机部署中 7×24 小时服务一个多 Agent 系统，生产实证：

| 指标 | 数值 | 测量口径 |
|---|---|---|
| LongMemEval-S R@5 | **0.652** vs 0.528 官方 BM25 基线 | 500 题全量，官方评测口径；逐题结果入库（`eval/lme/`） |
| 召回延迟 | **P95 ≈ 65–235 ms**（39k 条记忆端到端；类型过滤后更快） | 生产 daemon；规模衰减曲线见英文版 Benchmark 节 |
| 迁移 | 2,303 条记忆迁移，3,681 条会话流条目退役 | 一次性迁移+新鲜度审计 |

*基线为同一语料上的生产 FTS 检索；LongMemEval 基准集全量公开（`eval/lme/`），36 条生产基线保持私有（含真实内容）——参数变更须双闸同过。*

---

## 为什么再造一个记忆引擎？

现有方案分两派，我们需要的是第三派：

1. **向量库**（pgvector、Qdrant……）——只是检索原语，没有生命周期。你写入的每条记忆都不死且同样「新鲜」——这不是特性，是缺陷。
2. **Agent 记忆框架**——用着方便，但保留语义是黑盒：摘要有损且不可逆，而且当现实在记忆底下变了的时候，没有任何机制告诉你**哪条**记忆过期了。

我们在生产环境用框架时撞的正是这堵墙：压缩破坏了溯源、过期记忆作为「现状」复活、且没有任何机械手段验证「说了 vs 做了」。memory-engine 就是修复本身，抽出并硬化：

- **三路混合召回**——稠密向量（pgvector HNSW）+ 中文优化全文（PGroonga）+ 结构化过滤，RRF 融合。每条命中携带 `score_parts`，排序为什么靠前可以审计。
- **生命周期，而非垃圾场**——每条记忆有 TTL 状态机（`active` → `aged` → `archived` → `retired` → purge），且 purge 管线 fail-closed：当日备份验证+批次异盘导出+健康检查，三者全真才准删，缺一不动。
- **新鲜度协议**——宿主为每个会话记录「版本游标」。会话回来时，引擎把「你上次读取之后的变更」diff 成有预算上限、按域折叠的摘要。离开一周？你拿到的恰好是变了什么，而不是消防水管。
- **查询时新鲜度闸**——日期分桶衰减（fresh / aging / stale）+已知过期关键词扫描+配置迁移后的内容-现实一致性校验。查重防重复；新鲜度闸防「复活」。
- **真源锚定**——记忆指向事实本体而非复制它。一个真相，多个索引。

## 架构

```
                       ┌────────────────────────────────────────────┐
 agent / cron / hooks ─▶│  FastAPI daemon  (127.0.0.1:8766)          │
                       │  /v1/retain  /v1/recall  /v1/memories/{id} │
                       │  /v1/health  /v1/export  /v1/admin/backup  │
                       └───────┬───────────────────┬────────────────┘
                               │                   │
                    ┌──────────▼─────────┐  ┌──────▼───────────────┐
                    │ Embedder (CUDA)    │  │ PostgreSQL 18 :5433  │
                    │ Qwen3-Emb 0.6B     │  │ pgvector (HNSW)      │
                    │ fp16, 1.1 GB 显存  │  │ PGroonga (中文 FTS)  │
                    └────────────────────┘  │ JSONB (score_parts)  │
                                            └──────────────────────┘
```

单进程、单数据库、零外部服务。嵌入模型常驻显存；冷启动预热纳入健康检查（`/v1/health` 断言 db+model+warm+ready 四真）。

## 快速开始

```bash
git clone https://github.com/<you>/memory-engine.git && cd memory-engine

# 1. 数据库（PostgreSQL 18 + pgvector + PGroonga）
docker compose up -d

# 2. 嵌入模型（下载 Qwen3-Embedding-0.6B）
python -m memory_engine.download_model

# 3. 配置
cp .env.example .env   # 设置 MEMORY_ENGINE_HOME，需要时设 LLM_GATEWAY_API_KEY

# 4. 运行
pip install -e .
python -m memory_engine.cli serve          # 或用 deploy/ 下的 systemd unit
curl -s localhost:8766/v1/health | jq      # 期望: status=ok, 四真全 true

# 5. 测试
pytest tests/ -v
```

## 一分钟看懂新鲜度协议

```jsonc
// POST /v1/recall —— 每个答案自带溯源与新鲜度
{
  "hits": [{
    "id": "m_01J9…",
    "body": "…",
    "score_parts": {"dense": 0.41, "fts": 0.22, "struct": 0.10},  // 可审计的排序依据
    "freshness": {"bucket": "aging", "age_days": 47, "last_verified": "2026-09-10"}
  }]
}
```

写入即查重（对近 30 天余弦 ≥0.97 拒收，context 必填）。宿主保存会话读取游标；下次来访调 `POST /v1/freshness/digest`——预算封顶、按域折叠、同一对象的多次变更折叠为净变更。**没有 digest ≠ 没发生任何事；意思是「没有你还没读过的变更」。**

## 诚实的局限

- **单机规模**。为一个 Agent 系统一个运维者设计（生产运行在约 4 万条记忆；无过滤最坏配置实测到 12.2 万条）。没有分片方案。需要多租户，今天这不是对的工具。
- **双轨评测，有意为之**。公开 LongMemEval 集（500 题，`eval/lme/`，完全可复现）+ 36 条私有生产基线（含真实内容）——同类可复现、同数据集不可复现。参数变更双闸同过才固化。
- **中文优先的 FTS**。PGroonga 是中文召回的承重墙；纯英文部署可能想换别的 FTS 扩展。
- **嵌入模型有立场**。Qwen3-Embedding-0.6B fp16 CUDA 是实测选型（见 docs/）；纯 CPU 主机能跑但延迟预算重算。
- **uuid7 的 variant 位**尚未完全符合 RFC 9562（时间前缀语义已验证；issue 追踪中）。

## 定位

| | memory-engine | 向量库 | Agent 记忆框架 |
|---|---|---|---|
| 生命周期 / TTL | ✅ 一等公民 | ❌ | ⚠️ 看框架 |
| 回访会话的新鲜度 diff | ✅ 游标制 | ❌ | ❌ |
| 召回可解释性 | ✅ score_parts | ⚠️ 裸分数 | ❌ |
| fail-closed 删除管线 | ✅ | n/a | ❌ |
| 任意框架即插即用 | ❌ 宿主集成 | ✅ | ✅ |
| 运维重量 | daemon+1 数据库 | 服务 | 通常较轻 |

为 [Hermes Agent](https://hermes-agent.nousresearch.com/docs)（Nous Research 开源的 agent 框架）构建的记忆层；超大工具输出的「引用化+回查」思路参考 [NeverFull 压缩代理](https://github.com/061115xhsm/NeverFull-NeverStop-LLM-Context-Compaction-Proxy)（设计研究——尚未接线）。

## 运维工具

- [`scripts/purge_archived.py`](scripts/purge_archived.py) — archived-TTL 物理清理，fail-closed 三重前置闸（当日备份在位、批次导出异盘、引擎健康四真）。默认 dry-run；删除只走 HTTP API，禁直改数据库。
- 回归证据文件（`tests/last_smoke.json`、`tests/last_stage2.json`）按次生成不入库；最近一次全量回归：38 项检查 0 失败（约 3.9 万条记忆规模）。

## 许可证

MIT。贡献规范见 [CONTRIBUTING.md](CONTRIBUTING.md)（全绿 pytest+零命中脱敏扫描是合入门槛）。
