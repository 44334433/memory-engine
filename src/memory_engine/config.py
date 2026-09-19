"""配置唯一出口：环境变量可覆盖，默认值=蓝图拍板值。"""
import os
from pathlib import Path

HOME = Path.home()
# 数据根目录：MEMORY_ENGINE_HOME 可覆盖（默认 ~/hermes-data）；MEMORY_ENGINE_DIR 仍可整体指定引擎目录
ENGINE_HOME = Path(os.environ.get("MEMORY_ENGINE_HOME", str(HOME / "hermes-data")))
BASE_DIR = Path(os.environ.get("MEMORY_ENGINE_DIR", str(ENGINE_HOME / "memory-engine")))
MODEL_DIR = Path(os.environ.get("MEMORY_ENGINE_MODEL_DIR", str(BASE_DIR / "models/qwen3-embedding-0.6b")))

HOST = os.environ.get("MEMORY_ENGINE_HOST", "127.0.0.1")
# 默认 8766；与其他服务端口冲突时改此环境变量
PORT = int(os.environ.get("MEMORY_ENGINE_PORT", "8766"))

PG_DSN = os.environ.get(
    "MEMORY_ENGINE_PG_DSN",
    "postgresql://memengine@127.0.0.1:5433/memengine?sslmode=disable"
)
# sslmode=disable（阶段2）：本机回环 + trust 认证无加密必要；实测 PG 端 ssl=on(snakeoil) 时
# 多线程「关旧连+新建连」并发会在 libpq/OpenSSL 锁上死锁（strace 实证 FUTEX_WAIT 永久等待）。

EMBED_MODEL = "Qwen/Qwen3-Embedding-0.6B"
EMBED_DIM = 1024
EMBED_DEVICE = os.environ.get("MEMORY_ENGINE_EMBED_DEVICE", "cuda")
EMBED_MAX_LEN = int(os.environ.get("MEMORY_ENGINE_EMBED_MAXLEN", "2048"))
EMBED_BATCH = int(os.environ.get("MEMORY_ENGINE_EMBED_BATCH", "16"))
# Qwen3-Embedding 官方：query 侧拼英文 instruction（+1~5%），document 侧不拼
EMBED_QUERY_INSTRUCTION = os.environ.get(
    "MEMORY_ENGINE_EMBED_INSTRUCTION",
    "Given a memory retrieval query or user context, retrieve the most relevant stored memories",
)

# —— 三路召回 + RRF（蓝图 §4）——
RRF_K = 60
W_VEC, W_FTS, W_TIME = 1.0, 0.8, 0.4
TOP_VEC, TOP_FTS, TOP_TIME = 60, 60, 30
# life 因子（蓝图 §4；candidate 视同 trial=0.85；include_archived 时 archived=0.5 可见）
# decaying=0.7：阶段2 拍板（90d 无触发→decaying 召回降权 0.7；阶段1 曾用 0.6，随本批改版）
LIFE_WEIGHTS = {
    "active": 1.0, "trial": 0.85, "candidate": 0.85,
    "decaying": 0.7, "archived": 0.0, "retired": 0.0,
}
LIFE_ARCHIVED_VISIBLE = 0.5
# verify 修正并入 life（蓝图 §4：stale -5%、verified +2%）
VERIFY_FACTOR = {"verified": 1.02, "stale": 0.95, "unverified": 1.0}
# —— 时效降权（2026-09-16 拍板追加；fresh ≤30d / aging 30-90d / stale >90d）——
STALE_WEIGHTS = {"fresh": 1.0, "aging": 0.9, "stale": 0.7}
STALE_FRESH_DAYS, STALE_AGING_DAYS = 30, 90

# —— 写入质量闸（蓝图 §4 去重 + context 必填）——
DEDUP_SIM = float(os.environ.get("MEMORY_ENGINE_DEDUP_SIM", "0.97"))  # cos 相似度阈值
DEDUP_DAYS = int(os.environ.get("MEMORY_ENGINE_DEDUP_DAYS", "3"))     # 近 N 天语义判重窗口

BANKS = ("hermes", "hermes-sessions", "knowledge", "reflection")
# 评测/隔离专用 bank（env 逗号分隔扩展，与迁移 004 的 CHECK 对齐）
BANKS = BANKS + tuple(b.strip() for b in os.environ.get("MEMORY_ENGINE_EXTRA_BANKS", "").split(",") if b.strip())
PRIORITIES = (1, 2, 3, 4, 5)

POOL_MIN = 2
POOL_MAX = 8

# —— 备份（本盘 backups/ 保留 7 天；外置目录建议异盘挂载，保留 90 天）——
BACKUP_LOCAL_DIR = Path(os.environ.get("MEMORY_ENGINE_BACKUP_LOCAL", str(BASE_DIR / "backups")))
BACKUP_EXT_DIR = Path(
    os.environ.get("MEMORY_ENGINE_BACKUP_EXT", str(ENGINE_HOME / "backup-external"))
)
BACKUP_KEEP_LOCAL_DAYS = 7
BACKUP_KEEP_EXT_DAYS = 90

# —— 生命周期状态机（阶段2；拍板④准入=触发≥3+采纳率≥60%；蓝图 §6）——
CANDIDATE_DAYS = int(os.environ.get("MEMORY_ENGINE_CANDIDATE_DAYS", "6"))   # 候选期(天)→自动转 trial
TRIAL_DECAY_DAYS = 30        # trial 30d 无信号 → decaying（蓝图）
ACTIVE_DECAY_DAYS = 90       # active 90d 无触发/采纳 → decaying（阶段2 拍板）
DECAY_ARCHIVE_DAYS = 180     # decaying 180d 无信号 → archived（蓝图；hidden 不删）
REVIVE_WINDOW_DAYS = 3       # decaying 近 3d 有命中/采纳 → 复活 active（蓝图）
PROMOTE_MIN_HITS = 3         # 近 30d recall_hit ≥3
PROMOTE_MIN_ADOPT_RATE = 0.6  # 采纳率 adopted/hit ≥60%
LIFECYCLE_INTERVAL_S = int(os.environ.get("MEMORY_ENGINE_LIFECYCLE_INTERVAL", "600"))
LIFECYCLE_START_DELAY_S = 60  # 蓝图 §8.2-4：生命周期线程延迟 60s 启动

# —— 整合 consolidate（阶段2；蓝图 §7 /v1/consolidate）——
CONSOLIDATE_RECENT_DAYS = int(os.environ.get("MEMORY_ENGINE_CONSOLIDATE_DAYS", "14"))
CONSOLIDATE_SCAN_LIMIT = int(os.environ.get("MEMORY_ENGINE_CONSOLIDATE_LIMIT", "500"))
CONSOLIDATE_SIM = float(os.environ.get("MEMORY_ENGINE_CONSOLIDATE_SIM", "0.93"))

# —— 本地 WAL 归档滚动清理（阶段2；阶段1 §8 未尽项）——
WAL_ARCHIVE_DIR = os.environ.get(
    "MEMORY_ENGINE_WAL_ARCHIVE_DIR", "/var/lib/postgresql/wal_archive_memengine")
WAL_KEEP_FILES = int(os.environ.get("MEMORY_ENGINE_WAL_KEEP_FILES", "168"))  # ≈7天×24段/天(archive_timeout=3600)
PG_BIN = "/usr/lib/postgresql/18/bin"

# —— 投毒闸（2026-09-16 P0 拍板：错误语义+投毒闸批）——
SOURCE_TIERS = ("user", "agent", "web", "cron")   # 四级来源；缺省=agent（缺元数据按不信任处理）
EXTERNAL_SOURCE_TIERS = ("agent", "web", "cron")  # 外部来源（非用户直输）：默认 trial 低信任入场
EXTERNAL_ENTRY_STATE = "trial"
# 注入扫描范围：all=全量起步（当前拍板）| external_only=收紧态。废弃条件（写死）：
# 误报率实测 >30% 时切 external_only（仅扫非 user 来源），样本库本体不动。
INJECTION_SCAN_SCOPE = os.environ.get("MEMORY_ENGINE_INJECTION_SCAN", "external_only")

# —— P1 第一批（2026-09-16）：嵌入可插拔 + fts-only 降级 + source_tier 降权 ——
# 嵌入 Provider：qwen3=本地 Qwen3-0.6B fp16 CUDA（默认）| openai_compat=OpenAI 兼容远程端点
EMBED_PROVIDER = os.environ.get("MEMORY_ENGINE_EMBED_PROVIDER", "qwen3")
# openai_compat 远程端点（走 LLM_GATEWAY_API_KEY 鉴权；未配 key 时启动可过、调用期失败→recall 降级 fts-only）
LLM_GATEWAY_BASE_URL = os.environ.get("LLM_GATEWAY_BASE_URL", "http://127.0.0.1:8080/v1")
LLM_GATEWAY_API_KEY = os.environ.get("LLM_GATEWAY_API_KEY", "")
EMBED_REMOTE_MODEL = os.environ.get("MEMORY_ENGINE_EMBED_REMOTE_MODEL", "qwen3-embedding-0.6b")
EMBED_REMOTE_TIMEOUT = float(os.environ.get("MEMORY_ENGINE_EMBED_REMOTE_TIMEOUT", "10"))
# 重嵌批次版本：换模时调大，scripts/reembed_backfill.py 批量重嵌 embed_ver<当前 的存量
EMBED_VER = int(os.environ.get("MEMORY_ENGINE_EMBED_VER", "1"))
# source_tier 召回降权（recall 融合阶段乘入 final；user/agent 不降权；web/cron 可配）
TIER_WEIGHTS = {
    "user": 1.0,
    "agent": 1.0,
    "web": float(os.environ.get("MEMORY_ENGINE_TIER_WEIGHT_WEB", "0.85")),
    "cron": float(os.environ.get("MEMORY_ENGINE_TIER_WEIGHT_CRON", "0.9")),
}

# —— P1 第二批（2026-09-16）：双时序 + 知识网络 + 多宿主留位 ——
# 第四路图召回：从三路命中出发 1-2 跳邻拉（递归 CTE），RRF 融合加 graph 分量。
# observe 期低调权重 0.5（可配 MEMORY_ENGINE_W_GRAPH）。
W_GRAPH = float(os.environ.get("MEMORY_ENGINE_W_GRAPH", "0.5"))
GRAPH_HOPS = int(os.environ.get("MEMORY_ENGINE_GRAPH_HOPS", "2"))            # 1-2 跳
GRAPH_SEEDS_PER_ROUTE = int(os.environ.get("MEMORY_ENGINE_GRAPH_SEEDS_PER_ROUTE", "10"))
GRAPH_SEEDS_MAX = int(os.environ.get("MEMORY_ENGINE_GRAPH_SEEDS_MAX", "24"))
GRAPH_MAX_NEIGHBORS = int(os.environ.get("MEMORY_ENGINE_GRAPH_MAX_NEIGHBORS", "30"))
# G15（S1 级盲审硬约束，写死）：矛盾检测 observe-only——contradicts 边只记录进 edges 表，
# 绝不触发 memories.invalid_at 置位；升 enforce 前置条件 = 金标边集 precision>=0.7
# 且 30 天抽检通过。该条件未达成前，任何代码路径不得由 contradicts 边改写 memories。
CONTRADICTION_ENFORCE_PRECONDITION = "gold_edge_precision>=0.7 AND 30d_spotcheck_passed"
# 弱图脚本（scripts/weak_graph_edges.py）：组内每记忆最多连 K 个近邻 peer / 每组最多参与成员数
WEAK_GRAPH_K = int(os.environ.get("MEMORY_ENGINE_WEAK_GRAPH_K", "3"))
WEAK_GRAPH_GROUP_CAP = int(os.environ.get("MEMORY_ENGINE_WEAK_GRAPH_GROUP_CAP", "100"))
# LLM 实体抽取批量脚本（scripts/llm_entity_extract.py）：模型与请求参数（OpenAI 兼容网关）
LLM_EXTRACT_MODEL = os.environ.get("MEMORY_ENGINE_LLM_MODEL", "deepseek-v4-flash")
LLM_EXTRACT_TIMEOUT = float(os.environ.get("MEMORY_ENGINE_LLM_TIMEOUT", "120"))

# —— P1 第三批（2026-09-16）：自进化闭环 L1-L4 接线 ——
# L2 用进废退（路线图 §自进化）：adopted=宿主引用上报（强信号）→ decaying 升级窗 30d
# （recall_hit 复活窗维持拍板 REVIVE_WINDOW_DAYS=3 不动）；零访问=access_events 任意 kind
# 连续 N 天无记录 → trial/active 提前 decaying（锚=GREATEST(最近任意访问, created_at)）。
# 既有拍板参数（TRIAL_DECAY_DAYS/ACTIVE_DECAY_DAYS/REVIVE_WINDOW_DAYS/DECAY_ARCHIVE_DAYS）全部不动。
L2_ADOPT_WINDOW_DAYS = int(os.environ.get("MEMORY_ENGINE_L2_ADOPT_WINDOW_DAYS", "30"))
L2_ZERO_ACCESS_DAYS = int(os.environ.get("MEMORY_ENGINE_L2_ZERO_ACCESS_DAYS", "90"))
# L3 失败回流：recall 零命中/top1 低分 query → STATE_DIR/hard_queries.jsonl
# （去重=TTL 窗口内同 query 跳重；上限 CAP 防膨胀；校准实证：vector 路恒有候选 → 零命中罕见，
#   阈值默认捕捉融合尾部/图-only 领跑，调高即更严）
STATE_DIR = Path(os.environ.get("MEMORY_ENGINE_STATE_DIR", str(BASE_DIR / "state")))
HARD_QUERY_SCORE = float(os.environ.get("MEMORY_ENGINE_HARD_QUERY_SCORE", "0.005"))
HARD_QUERY_CAP = int(os.environ.get("MEMORY_ENGINE_HARD_QUERY_CAP", "500"))
HARD_QUERY_TTL_DAYS = int(os.environ.get("MEMORY_ENGINE_HARD_QUERY_TTL_DAYS", "7"))
HARD_QUERY_MIN_LEN = int(os.environ.get("MEMORY_ENGINE_HARD_QUERY_MIN_LEN", "4"))
HARD_QUERY_PROPOSAL_MIN_HITS = int(os.environ.get("MEMORY_ENGINE_HARD_QUERY_PROPOSAL_MIN", "3"))
HARD_QUERY_ANALYSIS_MIN_S = int(os.environ.get("MEMORY_ENGINE_HARD_QUERY_ANALYSIS_MIN_S", "3600"))
# L1 参数自调（G07 S1 级盲审硬约束：36 题 P@5 二元指标 SE≈7.6pp → 自调固化闸必须
# 配对逐题检验（McNemar 式）+ 连续两轮通过 + 参数快照版本化；时序类参数（衰减天数/状态系数）
# 移出即时闸另立月度人工评审——本框架变异仅限非时序标量 RRF_K/TIER_WEIGHTS）。
AUTOTUNE_DIR = Path(os.environ.get("MEMORY_ENGINE_AUTOTUNE_DIR", str(STATE_DIR / "param_autotune")))
AUTOTUNE_MIN_DELTA = float(os.environ.get("MEMORY_ENGINE_AUTOTUNE_MIN_DELTA", "0.05"))  # ≥基线+5pp
AUTOTUNE_ALPHA = float(os.environ.get("MEMORY_ENGINE_AUTOTUNE_ALPHA", "0.05"))          # McNemar 显著性
AUTOTUNE_ROUNDS = int(os.environ.get("MEMORY_ENGINE_AUTOTUNE_ROUNDS", "2"))             # 连续两轮

# —— W2（2026-09-18）：memory_type 分层（semantic/procedural/episodic）——
MEMORY_TYPES = ("semantic", "procedural", "episodic")
MEMORY_TYPE_DEFAULT = "episodic"   # 存量缺省 + 未知值回退；retain 缺省走 memory_type.classify 启发式
# 衰减窗口系数（lifecycle 各衰减规则的天数 × 系数；#23：整数天，最小改动不引入浮点比较）：
# semantic 最慢 ×2.0、procedural ×1.5、episodic ×1.0=基准。
# 兼容不变量（写死）：episodic 系数恒 1.0——三类之外的存量行为与拍板值逐一相等，
# 既有衰减测试（TRIAL 30d/ACTIVE 90d/ARCHIVE 180d/L2 90d）零回归。
# 仅作用于衰减/归档/零访问轨；revive/promote（信号驱动升级）一律不分型。
TYPE_DECAY_FACTORS: dict[str, float] = {
    "semantic": float(os.environ.get("MEMORY_ENGINE_DECAY_FACTOR_SEMANTIC", "2.0")),
    "procedural": float(os.environ.get("MEMORY_ENGINE_DECAY_FACTOR_PROCEDURAL", "1.5")),
    "episodic": 1.0,   # 基准不可配（兼容不变量）
}

# —— W4: per-bank adaptive thresholds (dedup cos / decay scale) ——
# ★Single change point: per-bank defaults live ONLY in the two dicts below; all call sites
#   read through dedup_cos_for()/decay_scale_for(). No threshold literals elsewhere.
#   Env append/override "bank=v,bank2=v2" (same minimal-support style as MEMORY_ENGINE_EXTRA_BANKS).
# Unregistered bank → global fallback (DEDUP_SIM / scale 1.0) = legacy behavior, zero drift.

def _env_bank_map(var: str) -> dict[str, float]:
    """Parse 'bank=v,bank2=v2' env overrides; malformed value crashes at import (fail-fast)."""
    out: dict[str, float] = {}
    for part in (p.strip() for p in os.environ.get(var, "").split(",")):
        if part:
            k, _, v = part.partition("=")
            out[k.strip()] = float(v)
    return out


# Dedup cos, derived from measured nearest-neighbor distributions (2026-09-19, 150 most-recent
# items per bank; full evidence in the batch report):
#   hermes: 29.3% of samples sit in [0.95,0.97) and a 10-pair eyeball audit found ALL of them to
#     be same-fact rewrites the old flat 0.97 let through → lower to 0.95.
#   reflection: 13.3% in-band, sampled pairs likewise all rewrites → 0.95.
#   knowledge: in-band pairs include distinct records differing only by an embedded id
#     (cos 0.9655-0.9664); lowering would false-suppress them, and in-band ≥0.97 mass measured 0,
#     so raising is near-lossless → 0.98.
#   Rollback condition (written down): 30-day spot-check per changed bank; if false-skip rate of
#     items landing in [new_threshold,0.97) exceeds 20% → revert that bank to the global 0.97.
BANK_DEDUP_COS: dict[str, float] = {
    "hermes": 0.95,
    "reflection": 0.95,
    "knowledge": 0.98,
}
BANK_DEDUP_COS = {**BANK_DEDUP_COS, **_env_bank_map("MEMORY_ENGINE_BANK_DEDUP_COS")}

# Decay window scale (track days = base × TYPE_DECAY_FACTORS[type] × scale[bank], rounded).
# From measured 30-day reuse signals (hits per item, whole-DB query 2026-09-19):
#   hermes 10.1, knowledge 15.8 (high reuse → long-lived, ×1.5 longer windows);
#   hermes-sessions 1.2 with 96.7% already faded (retired session-log bank, low value density
#     → ×0.5 to speed it out of the way); reflection 4.4 mid → unregistered baseline 1.0
#     (register only measured deviations, never blanket the map).
BANK_DECAY_SCALE: dict[str, float] = {
    "hermes": 1.5,
    "knowledge": 1.5,
    "hermes-sessions": 0.5,
}
BANK_DECAY_SCALE = {**BANK_DECAY_SCALE, **_env_bank_map("MEMORY_ENGINE_BANK_DECAY_SCALE")}


def dedup_cos_for(bank: str) -> float:
    """Per-bank dedup cos; unregistered bank falls back to global DEDUP_SIM (= legacy). Sole reader."""
    return BANK_DEDUP_COS.get(bank, DEDUP_SIM)


def decay_scale_for(bank: str) -> float:
    """Per-bank decay window scale; unregistered falls back to 1.0 (= legacy). Sole reader."""
    return BANK_DECAY_SCALE.get(bank, 1.0)

# —— 自进化专项 #1（2026-09-18 拍板 a）：outcome 反馈 API（POST /v1/feedback）——
OUTCOME_TYPES = ("adopted", "corrected", "useless")   # 三值语义对齐 Mem0 feedback
# EMA 平滑系数（Cognee 边权重同构 w+=α(a−w)；量级=调研背书默认）。
# ★变更点：EMA α 唯一调整入口=本行（写死值——本批刻意不做 env 覆盖，防生产/评测参数分叉；
#   调参须走 L1 评审通道同步更新 tests/test_outcome_feedback.py 的递推期望值）。
OUTCOME_EMA_ALPHA = 0.1

# —— W1 核心记忆块（core block，2026-09-18 拍板顺序 W2 之后）——
# 常驻注入区：高价值记忆不经检索直入宿主每轮上下文。宿主拉取式（GET /v1/core-block，
# 复用 freshness-protocol 注入通道模式：注入走宿主 user message 尾部，禁引擎侧强推
# system prompt——前缀缓存铁律）。与 freshness digest 是两种机制：
# digest=游标增量事件摘要快照，core block=条目级原文常驻（无游标、幂等读、每次全量重算）。
CORE_BLOCK_BUDGET_CHARS = int(os.environ.get("MEMORY_ENGINE_CORE_BLOCK_BUDGET", "1500"))
# 默认 1500 对齐 freshness-protocol 注入预算闸（MAX_INJECT_CHARS），两种注入同闸不同源。
CORE_BLOCK_MAX_ITEMS = int(os.environ.get("MEMORY_ENGINE_CORE_BLOCK_MAX_ITEMS", "20"))
# 每轨候选取数硬顶（单表+排序无嵌入计算，P95<50ms 验收线的规模保障）。
# 自动精选轨阈值（2026-09-18 拍板值，env 可覆盖；论证与废弃条件如下）：
CORE_AUTO_TYPES = tuple(os.environ.get("MEMORY_ENGINE_CORE_AUTO_TYPES", "semantic,procedural").split(","))
# episodic 一次性情境不常驻（W2 分层语义：情景记忆靠检索按需召回，常驻=噪音税）。
CORE_MIN_POLARITY = float(os.environ.get("MEMORY_ENGINE_CORE_MIN_POLARITY", "0.5"))
# polarity 高=adopted：EMA α=0.1 下 1−0.9^n≥0.5 ⇔ n≥7 次连续采纳（≈14 天内高频强正反馈），
# 常驻区宁缺勿滥；废弃条件=宿主流量起来后自动轨长期空块（>30 天零自动入选）→ 降 0.3（n≥4）。
CORE_MIN_ADOPT = int(os.environ.get("MEMORY_ENGINE_CORE_MIN_ADOPT", "2"))
# adopt_count≥2：绝对采纳次数下限（与准入闸 PROMOTE_MIN_HITS=3 同量级；防单次偶然采纳入常驻）。
CORE_SCORE_CAP_ADOPT = 10.0   # adopt_count 归一封顶（10 次以后边际为零，防刷计数独大）
CORE_SCORE_ACCESS_CAP = 50.0  # access_count log 归一分母（recall_hit 是弱信号，log 压平长尾）
# 轨内排序权重（启发式序，非校准分——只决定预算内先后，绝对值无意义）：
CORE_W_POLARITY = 0.5   # polarity 主权重（唯一含负反馈的信号，corrected 会拉低出块）
CORE_W_ADOPT = 0.3      # 采纳（宿主引用，强信号）
CORE_W_ACCESS = 0.2     # 访问频次（recall_hit，弱信号，log 归一）

# —— W3 可插拔重排（2026-09-19；0.122 无过滤最差例批评的正面解法：四路 RRF 下异构记忆「竞争」而非「甄别」）——
# ★变更点（唯一开关入口）：MEMORY_ENGINE_RERANK_ENABLED 缺省 "0"=关；
#   关=recall 重排段完全跳过（零开销，存量行为逐字节不变）；开=RRF 融合后、top-k 截断前 cross-encoder 精排。
RERANK_ENABLED = os.environ.get("MEMORY_ENGINE_RERANK_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
# 模型目录默认挂嵌入模型同级（MEMORY_ENGINE_DIR 覆盖时自动跟随）。
RERANK_MODEL_DIR = Path(os.environ.get(
    "MEMORY_ENGINE_RERANK_MODEL_DIR", str(MODEL_DIR.parent / "qwen3-reranker-0.6b")))
# 设备：auto=按 GPU 总量预算闸选（mem_get_info 真实空载余量 ≥ RERANK_GPU_MIN_FREE_MIB 才上卡，
#   否则退 CPU 并 log）；可显式 cuda/cpu。禁擦线分配（09-19 双 OOM 实证）。
RERANK_DEVICE = os.environ.get("MEMORY_ENGINE_RERANK_DEVICE", "auto")
# GPU 预算（MiB）：模型 fp16 ~1250 + 批激活峰值 ~550 + 余量 ~760；embedder 1.14GB 常驻已
# 体现在 free 口径（mem_get_info 读的是真实余量）。
RERANK_GPU_MIN_FREE_MIB = int(os.environ.get("MEMORY_ENGINE_RERANK_GPU_MIN_FREE_MIB", "2560"))
# 复杂度择优：只对 top N 精排（全量精排延迟翻倍无收益）。缺省 10=2026-09-19 A/B 实测
# （10 候选×batch8 ≈110-130ms 达标；20 候选超延迟预算）；卡闲时可 env 调回 20。
RERANK_TOP_N = int(os.environ.get("MEMORY_ENGINE_RERANK_TOP_N", "10"))
# 单对 query+doc token 上限（记忆正文短于 1024）。
RERANK_MAX_LEN = int(os.environ.get("MEMORY_ENGINE_RERANK_MAXLEN", "1024"))
# 批大小上限=显存主变量（causal-LM lm_head 输出 B×L×151936 fp16；B=8 全 pad 到 1024 曾打爆
# 12G 卡）；padding=longest + 下方每批 token 预算制打包后受控。
RERANK_BATCH = int(os.environ.get("MEMORY_ENGINE_RERANK_BATCH", "8"))
RERANK_BATCH_TOKEN_BUDGET = int(os.environ.get("MEMORY_ENGINE_RERANK_BATCH_TOKEN_BUDGET", "2048"))
# 乘法融合 final' = final×(floor+(1−floor)·p)：P(yes) 作调制量而非主分，保 RRF 主序的序
# 守恒性（floor=0.2 ⇒ 重排最多把一条记录的分数乘到 5×）。
RERANK_FLOOR = float(os.environ.get("MEMORY_ENGINE_RERANK_FLOOR", "0.2"))
# 官方模型卡指令头（Transformers 用法节逐字核对 09-19；改动=换校准）。
RERANK_INSTRUCTION = os.environ.get(
    "MEMORY_ENGINE_RERANK_INSTRUCTION",
    "Given a memory retrieval query, judge whether the memory record answers or supports the query",
)

VERSION = "0.5.0-w3"
