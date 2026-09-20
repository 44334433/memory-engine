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

BANKS = ("hermes", "hermes-sessions", "knowledge", "reflection", "hermes-docs")
# 评测/隔离专用 bank（env 可扩展，逗号分隔）——多租户/评测场景动态 bank 的最小支持（2026-09-17）
_extra = os.environ.get("MEMORY_ENGINE_EXTRA_BANKS", "")
BANKS = BANKS + tuple(b.strip() for b in _extra.split(",") if b.strip())
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
TRIAL_DECAY_DAYS = int(os.environ.get("MEMORY_ENGINE_TTL_TRIAL_DAYS", "30"))  # trial→decaying 窗（默认=现值30）
ACTIVE_DECAY_DAYS = int(os.environ.get("MEMORY_ENGINE_TTL_DECAY_DAYS", "90"))  # active→decaying 窗（默认=现值90）
DECAY_ARCHIVE_DAYS = int(os.environ.get("MEMORY_ENGINE_TTL_ARCHIVE_DAYS", "180"))  # →archived 窗（默认=现值180）
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

# —— W4（2026-09-19 五轮反馈主题E 残余）：bank 级自适应阈值 ——
# ★变更点（集中一处）：判重 cos 与衰减窗 scale 的 bank 级默认值只能改本节两个 dict；
#   代码侧只经 dedup_cos_for()/decay_scale_for() 查表，任何调用路径不得出现阈值字面量。
#   env 追加/覆盖 "bank=v,bank2=v2"（同 EXTRA_BANKS 最小支持风格；隔离实例实测不改生产值）。
# 未登记 bank → 回退全局默认（DEDUP_SIM / scale 1.0）＝存量行为零变化（hermes-docs、
#   eval_* 等即走回退；回退分支在 tests/test_w4_bank_thresholds.py 有等价断言）。


def _env_bank_map(var: str) -> dict[str, float]:
    """解析 'bank=v,bank2=v2' 形 env 覆盖；坏值=float() 启动期即炸（fail-fast，防静默半生效）。"""
    out: dict[str, float] = {}
    for part in (p.strip() for p in os.environ.get(var, "").split(",")):
        if part:
            k, _, v = part.partition("=")
            out[k.strip()] = float(v)
    return out


# 判重 cos（2026-09-19 实测近邻分布定值，样本=各 bank 最近 150 条同 bank 最近邻 cos，
# 明细与 eyeball 判定=~/.hermes/docs/03-执行落地/执行-W4-自适应阈值-2026-09-19.md）：
#   hermes(0.95)：[0.95,0.97) 带占 29.3%(44/150)，抽 10 对全部为同事实复述（中英/改写对）
#     ——0.97 整带漏放，降到 0.95 收编真重复；
#   reflection(0.95)：带占 13.3%，抽 10 对全部复述对，同上；
#   knowledge(0.98)：带内混有「仅编号不同」异条（outcome-e2e 用例 id 对 cos 0.9655-0.9664，
#     降 0.95 即误杀）——反向收紧到 0.98（实测带内 ≥0.97 为 0，升档近零损失）；
#   废弃条件（写死）：任一 bank 改值后 30 天抽检，若 [新阈值,0.97) 内被跳条目误杀率>20%
#     → 该 bank 回调至全局 0.97 并在本行注释留证。
BANK_DEDUP_COS: dict[str, float] = {
    "hermes": 0.95,
    "reflection": 0.95,
    "knowledge": 0.98,
}
BANK_DEDUP_COS = {**BANK_DEDUP_COS, **_env_bank_map("MEMORY_ENGINE_BANK_DEDUP_COS")}

# 衰减窗 scale（四条轨天数 = base × TYPE_DECAY_FACTORS[type] × scale[bank]，四舍五入取整）。
# 2026-09-19 实测 30d 复用信号（hits/item，全库现算）定值：
#   hermes 10.1 / knowledge 15.8（高频复用=长寿命 → ×1.5 延窗）；
#   hermes-sessions 1.2 且 96.7% 已 faded（废弃归档 bank，技能已定性「价值密度低」→ ×0.5 加速出清）；
#   reflection 4.4 居中 → 不登记=基准 1.0（只登记有实测依据的偏移，不拍脑袋铺满）。
BANK_DECAY_SCALE: dict[str, float] = {
    "hermes": 1.5,
    "knowledge": 1.5,
    "hermes-sessions": 0.5,
}
BANK_DECAY_SCALE = {**BANK_DECAY_SCALE, **_env_bank_map("MEMORY_ENGINE_BANK_DECAY_SCALE")}


def dedup_cos_for(bank: str) -> float:
    """bank 级判重 cos；未登记回退全局 DEDUP_SIM（=现行为）。唯一读取入口。"""
    return BANK_DEDUP_COS.get(bank, DEDUP_SIM)


def decay_scale_for(bank: str) -> float:
    """bank 级衰减窗缩放；未登记回退 1.0（=现行为）。唯一读取入口。"""
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

# —— W3 可插拔重排（2026-09-19，设计-外部反馈裁决全景 W3 项；0.122 批评正面解法）——
# 根因：四路（vector/fts/time/graph）RRF 融合下异构记忆「竞争」而非「甄别」——
# 重排=对 top N 候选做 cross-encoder（Qwen3-Reranker-0.6B，query×doc 联合编码）逐条相关性打分。
# ★变更点（唯一开关入口）：MEMORY_ENGINE_RERANK_ENABLED 缺省 "0"=关；
#   关=recall 重排段完全跳过（零开销，存量行为逐字节不变）；开=RRF 融合后、scored 截断前精排。
RERANK_ENABLED = os.environ.get("MEMORY_ENGINE_RERANK_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
# 模型目录默认挂嵌入模型同级（unit 覆盖 MEMORY_ENGINE_DIR 时自动跟随，与 MODEL_DIR 同母目录）
RERANK_MODEL_DIR = Path(os.environ.get(
    "MEMORY_ENGINE_RERANK_MODEL_DIR", str(MODEL_DIR.parent / "qwen3-reranker-0.6b")))
# 设备：auto=按 SOUL#25③ GPU 总量预算闸选（空载 ≥ RERANK_GPU_MIN_FREE_MIB 才上卡，否则退 CPU 并 log）；可显式 cuda/cpu
RERANK_DEVICE = os.environ.get("MEMORY_ENGINE_RERANK_DEVICE", "auto")
# GPU 预算估算（MiB，#25③ 禁擦线分配）：模型 fp16 ~1250 + 20候选×1024token 激活峰值 ~550 + 余量 ~760；
# 现有 embedder 1.14GB 常驻已计入 free 口径（mem_get_info 读的是真实余量）。
RERANK_GPU_MIN_FREE_MIB = int(os.environ.get("MEMORY_ENGINE_RERANK_GPU_MIN_FREE_MIB", "2560"))
# #23 复杂度择优：只对 top N 精排（全量精排 60+ 候选延迟翻倍无收益）。缺省 10=实测校准：
#   本机 12G 卡（多任务共用、embedder 常驻 1.14GiB）单批 20 条 lm_head 即 ~229ms，20 候选×3批
#   ≈276ms 远超 150ms 预算；10 候选×batch4 ≈110-130ms 达标（2026-09-19 A/B 实测）。卡闲时可 env 调回 20。
RERANK_TOP_N = int(os.environ.get("MEMORY_ENGINE_RERANK_TOP_N", "10"))
RERANK_MAX_LEN = int(os.environ.get("MEMORY_ENGINE_RERANK_MAXLEN", "1024"))  # 单对 query+doc token 上限（记忆正文短于 1024）
# 批大小上限=显存主变量：causal-LM 全位 lm_head 输出 B×L×151936（fp16）；B=8 且 pad 到全局
# 1024 时单算子 1.9-2.1GiB 打爆 12G 卡（09-19 A/B OOM 实证）；padding=longest+预算制打包后受控。
RERANK_BATCH = int(os.environ.get("MEMORY_ENGINE_RERANK_BATCH", "8"))
# token 预算制打包（#23）：批大小按 批内最长L 反推使 N×L≤预算——短文档自动大批、长文档自动
# 小批；固定 B 要么白付 padding 要么爆延迟（12G 卡实测 forward 吞吐 ~300-650 tok/ms）。
RERANK_BATCH_TOKEN_BUDGET = int(os.environ.get("MEMORY_ENGINE_RERANK_BATCH_TOKEN_BUDGET", "2048"))
# 乘法融合：final' = final × (RERANK_FLOOR + (1−RERANK_FLOOR)·p)，p=P(yes)∈[0,1]。
# 刻意不取代 pri/life/stale/tier——重排只做「相关性甄别」，置信度/生命周期语义保留（异构竞争→甄别，
# 但降权体系是拍板资产不可被一段模型分推翻）；floor=0.2 → 判无关条最多被压到 1/5 相对分。
# ★变更点：floor 唯一调整入口=本行 env 默认值。
RERANK_FLOOR = float(os.environ.get("MEMORY_ENGINE_RERANK_FLOOR", "0.2"))
# 任务指令（官方：定制指令 +1~5%，建议英文书写；方向与 EMBED_QUERY_INSTRUCTION 对齐）
RERANK_INSTRUCTION = os.environ.get(
    "MEMORY_ENGINE_RERANK_INSTRUCTION",
    "Given a memory retrieval query, judge whether the memory record answers or supports the query",
)

# —— 多租户预备层（2026-09-19 拍板变更：触发条件由「真实多宿主接入事件」提前为「现在做预备层」，用户显式拍板）——
# ★唯一开关入口：MEMORY_ENGINE_MULTI_TENANT 缺省 "0"=关。
#   关 = recall() 对 filters 零改动（不注入、不拷贝——存量行为逐字节不变，
#        tests/test_multi_tenant_rls.py 关态断言 + 对象同一性断言钉死）；
#   开 = recall 强制注入 tenant_id=config.TENANT_ID 过滤（调用方显式传 filters.tenant_id 时尊重显式值）。
# DB 侧配套 = scripts/migrations/008_rls.sql：策略对未设 app.tenant_id 的会话恒放行，
#   故迁移先行应用 + 开关关 = 双重零行为；启用三步见 README「多租户」。
# 写入侧本批刻意不自动打 tenant_id（预备层不改存量归属；retain 需显式传，见启用三步②）。
MULTI_TENANT = os.environ.get("MEMORY_ENGINE_MULTI_TENANT", "0").strip().lower() in ("1", "true", "yes", "on")
# 服务端默认租户标识（MULTI_TENANT=1 时 recall 缺省过滤值；存量行 tenant_id=NULL 必须先回填才可见）
TENANT_ID = os.environ.get("MEMORY_ENGINE_TENANT_ID", "default")

VERSION = "0.5.1-obs"
