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

- **三路混合召回**——稠密向量（pgvector HNSW）+ 中文优化全文（PGroonga）+ 结构化过滤，RRF 融合。每条命中携带 `score_parts`，排序为什么靠前可以审计；字段集本身带版本（`schema_version`），下游可按版本防御性解析。
- **生命周期，而非垃圾场**——每条记忆有 TTL 状态机（`active` → `aged` → `archived` → `retired` → purge），且 purge 管线 fail-closed：当日备份验证+批次异盘导出+健康检查，三者全真才准删，缺一不动。
- **新鲜度协议**——宿主为每个会话记录「版本游标」。会话回来时，新鲜度层把「你上次读取之后的变更」diff 成有预算上限、按域折叠的摘要（本参考实现中 diff 在宿主侧执行，原料是引擎的 changelog 序号）。离开一周？你拿到的恰好是变了什么，而不是消防水管。
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
git clone https://github.com/44334433/memory-engine.git && cd memory-engine

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

## 五分钟上手（demo）

四步从零到可审计记忆——以下每条命令与输出均为 2026-09-20 对同一 `/v1` 面实跑采集（`…` 为省略的 id/文本），数据库层就是快速开始里的 compose 镜像（`docker/`），别无他物。

**1. 启动**（一次性，约 2 分钟：构建镜像+下模型）：

```bash
docker compose up -d                      # PG18 + pgvector + PGroonga，映射 127.0.0.1:5433
pip install -e . && python -m memory_engine.download_model
python -m memory_engine.cli serve &
curl -s localhost:8766/v1/health | jq '.status'    # "ok" 后走下面三步
```

**2. 写入** —— `POST /v1/retain`（context 必填；每次写入记一个 changelog 序号）：

```bash
curl -s -X POST localhost:8766/v1/retain -H 'content-type: application/json' -d '{
  "bank": "knowledge", "caller": "me",
  "items": [{"content": "A 仓库存 42 箱", "context": "demo", "title": "A仓库存", "source_tier": "user"}]}'
# → {"ids":["01a0bc1e-8641-…"],"dedup_skipped":0,"dedup_existing":[],"seq":56076,"took_ms":20.8}
```

**3. 召回** —— `POST /v1/recall`；排序依据带着部件和版本到场：

```bash
curl -s -X POST localhost:8766/v1/recall -H 'content-type: application/json' \
  -d '{"query":"A仓库存多少箱","top_k":3,"bank":"knowledge"}'
# → {"results":[{"id":"01a0bc1e-8641-…","score":0.014631,
#      "score_parts":{"schema_version":1,"rrf":0.016393,"pri":1.05,"life":0.85,
#                     "stale":1.0,"tier_weight":1.0,"graph":0.0,
#                     "outcome":null,"polarity":null,"routes":{"vector":1}},
#      "ttl_state":"candidate","staleness":"fresh","memory_type":"episodic", …}], …}
```

**4. 修订，然后查账** —— 修订走 supersede 不走覆盖，账本可查：

```bash
curl -s -X PATCH localhost:8766/v1/memories/01a0bc1e-8641-… \
  -H 'content-type: application/json' -d '{"body":"A 仓库存 37 箱（修订）","supersede":true}'
# → {"id":"474c9f6d-81c3-…","superseded_from":"01a0bc1e-8641-…","is_current":true, …}

curl -s "localhost:8766/v1/memories/474c9f6d-81c3-…/chain?max_hops=5"
# → versions: [ {position 0, "42 箱", is_current: false, superseded_by: 474c9f6d-…},
#               {position 1, "37 箱", is_current: true } ]
```

一句诚实话（这一步曾是文档与代码分叉处）：新鲜度协议的游标 **digest** 在本参考部署中跑在**宿主侧**——这个 build 没挂 `/v1/freshness/digest` 路由（POST 返回 404，SDK 映射为 `EndpointNotAvailable`）。引擎侧提供宿主做差的原料：每次写入的 changelog 序号 + 上面的版本链。

## 一分钟看懂新鲜度协议

```jsonc
// POST /v1/recall —— 每个答案自带溯源与新鲜度
// （2026-09-20 从 3.9 万条在线实机响应裁剪）
{
  "results": [{
    "id": "01a0bc1e-8641-…",
    "title": "…", "body": "…",
    "score": 0.014631,
    "score_parts": {                        // 可审计的排序依据；schema_version 守护字段集
      "schema_version": 1, "rrf": 0.016393, "pri": 1.05, "life": 0.85,
      "stale": 1.0, "tier_weight": 1.0, "graph": 0.0,
      "outcome": null, "polarity": null, "routes": {"vector": 1}
    },
    "ttl_state": "candidate", "staleness": "fresh", "memory_type": "episodic"
  }]
}
```

写入即查重（对近 30 天余弦 ≥0.97 拒收，context 必填）。宿主保存会话读取游标；下次来访拿到的是基于引擎 changelog 序号算出的 `freshness/digest`（本 build 中差分开在宿主侧，见上方 demo 注记）——预算封顶、按域折叠、同一对象的多次变更折叠为净变更。**没有 digest ≠ 没发生任何事；意思是「没有你还没读过的变更」。**

## 评测口径（摘要；全量见英文 README「Benchmark」节与 `eval/lme/`）

- **公开集**：LongMemEval-S 按 seed 42 采样 500 题（数据集快照 sha `2ec2a55`，官方 2025-09-19 cleaned 版）；session 粒度 recall_all@5，走官方协议——无检索目标的 81 题（absence 30 + 无目标 51）按协议剔除、实评 419 题，BM25 对照列同题集同尺；计分路径钉在官方仓 commit `9e0b455`，全链一键复现 `bash eval/lme/run_eval.sh`。
- **硬件/配置**：单机——RTX 5070 Ti Laptop 12 GB + Ryzen 9 9955HX（16 核）+ 30 GB 内存；PG 18.6 + pgvector + PGroonga；Qwen3-Embedding-0.6B fp16 常驻显存（约 1.2 GB）；引擎 `v0.5.0-w3` 默认参数，rerank 关（0.652 出自 09-16，早于 W3）。
- **口径边界**：纯检索计分、环内无 LLM——0.652 vs 0.528 因此同尺可比，也因此在任何端到端 QA 准确率上（含自家自判 n=100 子样本）都禁止互比。衰减曲线语料为合成 haystack（设计干扰项、零生产记忆，逐题可查 `qa_log.jsonl`）。
- **私有集为何不公开**：36 题生产基线承载真实用户记忆——公开即泄露用户本体，是隐私墙不是便利选择；同类可复现（拿自己的语料跑同协议）、同数据集不可复现。诚实代价：外部无法审计私有轨数字；牵制机制=双闸（参数变更须公开轨+私有轨同过），可审计的一轨对不可审计的一轨握有否决权。
- **6k 处的瓶颈定位**：不是延迟——引擎侧 6k 实测 P95 21–31 ms（预算线 200 ms），39k 端到端含查询嵌入与 HTTP 也只有 65–235 ms；也不是悬崖——合成对抗下每个倍增 2–3pp 是平滑斜率。真正过 6k 后咬人的是**异构语料下的质量竞争**（唯一证据=全库无过滤 0.122 那条注记），所以缓解路线是过滤/类型/rerank 而不是分片。边界注明：衰减曲线是合成分布口径，不承诺任意真实语料的斜率——该外推未实测。

## 生产验证口径

这里的「生产」精确指：作者自有 Hermes Agent 部署的在线记忆层——自 2026-09-16 持续服务（含升级重启），当前 39,221 条现行记忆、约 16 万访问事件（9 个 caller，主 agent `main` 占 15.98 万）、宿主反馈在跑（2026-09-20 实测 28 条带极性信号，auto_outcome 已开）。**不指**：多租户 SaaS 流量、外部用户、第三方长期验证——全部 39,221 行 `tenant_id` 为 NULL，单宿主接线是实测事实（RLS 执行层预备着等一个尚不存在的第二宿主）。这个词只买一样东西：代码注释里记录的故障模式（降级启动被 systemd 杀成重启循环、GPU 锁饥饿、静默永久降级）来自真实流量而非演示构造，修复分别在 `a6a5d54`、`00ebcd8`、`21f6ca8`。

## 诚实的局限

- **单机规模**。为一个 Agent 系统一个运维者设计（生产运行在约 4 万条记忆；无过滤最坏配置实测到 12.2 万条）。多租户**预备层**已落地（RLS 迁移 + `MULTI_TENANT` 开关，默认关=零行为）；真实第二宿主接入时按下方「启用三步」开启，配额/隐私边界仍属届时工作。
- **双轨评测，有意为之**。公开 LongMemEval 集（500 题，`eval/lme/`，一键复现 `bash eval/lme/run_eval.sh`，`--dry-run` 可先看计划）+ 36 条私有生产基线（含真实内容）——同类可复现、同数据集不可复现。参数变更双闸同过才固化。
- **中文优先的 FTS（跨语言影响，未实测处写明）**。PGroonga 是中文召回的承重墙；纯英文部署可换别的 FTS。英文侧：0.652 在含 PGroonga 的配置下对英文语料测得，但公开的逐题结果只存分数不存 `routes`——FTS 在英文上的单独贡献无法从公开数据复审，「不伤英文」是配置层观察、不是消融实测；换英文 FTS 的 A/B 从未跑过。中文侧：公开 benchmark 一个都没有，唯一的中文评测是不可公开的私有基线。登记两个候选、均未立项：英文 FTS-swap A/B、公开 CJK 检索集。
- **删除门禁，行为逐条写死**。`DELETE /v1/memories/{id}` 默认**软删**：行保留（`ttl_state='retired'`）、embedding 清空、changelog 记账、召回不再可见——同一 id `GET` 仍返回（2026-09-20 实机验证：retired 后 GET 200、recall 不可见）。硬删只有 `?purge=true`，自动路径（`scripts/purge_archived.py`）再套三重 fail-closed 闸（当日备份+异盘导出+健康四真）。不声称的东西：**误拦率**——门禁多大概率拦住一次正当删除，没有埋点统计。设计立场是拦错便宜（补前置条件重跑）、删错不可逆；没有数字可以交换这个立场，所以不假装有。
- **嵌入模型有立场**。Qwen3-Embedding-0.6B fp16 CUDA 是实测选型（见 docs/）；纯 CPU 主机能跑但延迟预算重算。
- **uuid7 的 variant 位**尚未完全符合 RFC 9562（时间前缀语义已验证；issue 追踪中）。

## 多租户（预备层）：启用三步

隔离默认**关，且关态完全惰性**：`MULTI_TENANT=0` 时 recall 过滤器原样直通；RLS 策略对未设 `app.tenant_id` 的会话全量放行（daemon/CLI/迁移/备份一切如旧）。关态零行为有 `tests/test_multi_tenant_rls.py` 断言，DB 层语义在影子库实跑验证（未设租户全见 / `SET app.tenant_id='t1'` 只见 t1 / 跨租户写被 `WITH CHECK` 拒）。

真实第二宿主接入时：①`python3 scripts/migrate.py --apply`（幂等）②回填归属
`UPDATE memories SET tenant_id='default' WHERE tenant_id IS NULL;`（NULL 租户行在带过滤的召回下不可见）
此后写入显式带 `tenant_id` ③部署侧设 `MEMORY_ENGINE_MULTI_TENANT=1` +
`MEMORY_ENGINE_TENANT_ID=<本宿主租户>`；需要 DB 级纵深防御时由会话 `SET app.tenant_id`（连接池接线时收敛到
`SET LOCAL` 事务作用域——预备层刻意不做）。


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

**三条边界，写在宣传语到此为止的地方**（2026-09-21 第五轮外部反馈后补，免得读者自己推断）：

1. **图谱 = 轻量图增强召回，不是 temporal KG 服务。** 召回路是实体层上的 1 跳拉回——2026-09-18 全量抽取快照 2,463 实体 / 7,975 条带类型的语义边（2026-09-21 实测现库 3,989 / 13,788），外加可浏览的 2 跳邻居端点。这不是 Zep/Graphiti 的地盘：多跳图推理、实体消歧（现按词面合并，触发条件见 ROADMAP §2）、时序边失效都是 roadmap 项、不在本期。诚实的自证，来自现库计数：`edges` 有 `valid_at`/`invalid_at` 双时态列，而 **13,788 条边里 `invalid_at` 置位数为 0**——schema 是双时序的，失效管线还没接线。
2. **记忆分类 = 有、启发式、情景为主。** 每条记忆标注 `semantic`/`procedural`/`episodic`：宿主可在 retain 时显式传 `memory_type`（`api_core` 校验），或由写入期启发式分类器兜底，TTL 窗口按类型缩放。2026-09-21 实测分布：episodic 23,549 · procedural 14,987 · semantic 921。薄的地方直说：分类器没有标注精度测量，程序性记忆是被存储和衰减调度、不被*执行*——执行语义留在宿主侧。类型深化（分类质量、程序性执行契约）在 roadmap。
3. **生产治理 = 单用户自托管优先。** 多宿主字段已预置（`tenant_id`/`agent_id`、RLS 迁移——全部默认关闭），但放进第二个宿主有两道今天还不存在的硬前置：配额隔离与隐私边界（对应上节「单机规模」条；触发式阶梯见英文 README「Scaling path」节 S2 余项）。过载行为只有 503+`retryable`——没有按客户端的 429 限流、没有熔断。这是多租户路径上点名登记的缺口，不是藏起来的缺口。

## 生态与集成路线

现状、排队项、一条留档更正（2026-09-21 补——第五轮反馈读的是过时快照）：

- **现状：** 类型化 Python SDK——`sdk/python/memoryengine`（MIT、httpx、全 `/v1` 面 + 异常映射、25 例 mock 传输测试，`7694b3f`，2026-09-17）；REST `/v1`；CLI——`src/memory_engine/cli.py`；LangChain 适配器（`integrations/langchain_memory.py`，`930a226`）；零构建图谱 UI（`deploy/graph.html`）；全量 JSONL 备份走 `GET /v1/export`。
- **路线：** **MCP server**——已登记立项拍板（backlog `7dd74d9f`，2026-09-21）：stdio 桥映射 retain/recall/feedback/metrics 端点，是多宿主路线的最短路径、约 1 人日。**TS SDK**——按需；尚无需求信号，刻意不排期。**导入 API**——planned：`GET /v1/export` 已存在但没有对等的重导入端点，备份恢复目前靠宿主侧工具。
- **防误读声明：**「无 Python SDK / 无 license / 无贡献指南」三条对本仓 `main` 均为伪：`sdk/python/` 2026-09-17 已发布，`LICENSE`（MIT）与 `CONTRIBUTING.md` 自首个公开快照（`9daa9c5`，2026-09-16）即在树中。早于这些 commit 的评审描述的是评审的时滞，不是仓库的缺口——请附上你查看时的 commit。

## 运维工具

- [`scripts/purge_archived.py`](scripts/purge_archived.py) — archived-TTL 物理清理，fail-closed 三重前置闸（当日备份在位、批次导出异盘、引擎健康四真）。默认 dry-run；删除只走 HTTP API，禁直改数据库。
- 回归证据文件（`tests/last_smoke.json`、`tests/last_stage2.json`）按次生成不入库；最近一次全量回归：38 项检查 0 失败（约 3.9 万条记忆规模）。

## 许可证

MIT。贡献规范见 [CONTRIBUTING.md](CONTRIBUTING.md)（全绿 pytest+零命中脱敏扫描是合入门槛）。版本大事记见 [CHANGELOG.md](CHANGELOG.md)——以版本号变更为锚、逐条挂 commit，随 release 维护。
