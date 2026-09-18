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

VERSION = "0.4.0-p1c"
