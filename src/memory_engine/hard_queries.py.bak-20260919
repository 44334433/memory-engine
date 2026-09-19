"""L3 失败回流（P1 第三批 2026-09-16）：recall 零命中/低分 query → 困难样本池 → 周期分析。

漏斗（路线图 §自进化 L3「池子有 TTL 防膨胀；分析产出=参数提案进 L1」）：
  recall API（后台任务，不入关键路径）→ STATE_DIR/hard_queries.jsonl
    （去重=TTL 窗口内同 query 跳重；上限 HARD_QUERY_CAP=500，写满丢最旧）
  → 生命周期巡扫周期分析（节流 HARD_QUERY_ANALYSIS_MIN_S，默认 1h）
    → 跨≥2 天且命中≥HARD_QUERY_PROPOSAL_MIN 的 query 簇产出调参提案 JSON
      落 STATE_DIR/param_autotune/candidates/（status=pending_review，供 L1 评审，不自动改参）。

纯文件实现（无 DB 依赖）：写池在 recall 后台任务中调用，绝不允许拖垮 recall 主路径。
校准实证（2026-09-16 生产 6k 库）：vector 路恒有候选 → 零命中罕见（仅 caller 可见性/bank
过滤场景），top1 分数集中在 0.016-0.020 且与相关性弱相关 → 低分阈值默认 0.005 捕捉
融合尾部/图-only 领跑，池子的价值=跨天频次聚类信号，非单次相关性判定。
"""
import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config

log = logging.getLogger("memory-engine.hardq")

_lock = threading.Lock()


def normalize_query(q: str) -> str:
    """归一化：压缩空白 + 小写（去重键语义）。"""
    return re.sub(r"\s+", " ", (q or "").strip()).lower()


def qkey(q: str) -> str:
    return hashlib.sha256(normalize_query(q).encode("utf-8")).hexdigest()


def hard_queries_path() -> Path:
    return Path(config.STATE_DIR) / "hard_queries.jsonl"


def read_all() -> list[dict]:
    """读全池（坏行跳过不炸；文件不存在=空池）。"""
    p = hard_queries_path()
    try:
        raw = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            if isinstance(d, dict) and d.get("key"):
                out.append(d)
        except (ValueError, TypeError):
            continue
    return out


def _write_all(entries: list[dict]) -> None:
    p = hard_queries_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
                   encoding="utf-8")
    tmp.replace(p)


def record_hard_query(query: str, caller: str | None, top1_score: float | None,
                      bank: str | None, reason: str) -> dict | None:
    """零命中/低分 query 落池。TTL 内重复 / 超短 query → None（跳重）。线程安全。"""
    q = (query or "").strip()
    if len(q) < config.HARD_QUERY_MIN_LEN:
        return None
    key = qkey(q)
    now = time.time()
    ttl_s = config.HARD_QUERY_TTL_DAYS * 86400
    with _lock:
        entries = read_all()
        for e in entries:
            if e.get("key") == key and now - float(e.get("ts_epoch", 0) or 0) < ttl_s:
                return None
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "ts_epoch": round(now, 3), "key": key, "query": q[:300],
               "caller": (caller or "")[:60], "bank": bank,
               "top1_score": top1_score, "reason": (reason or "")[:20]}
        entries.append(rec)
        if len(entries) > config.HARD_QUERY_CAP:      # 上限防膨胀：丢最旧
            entries = entries[-config.HARD_QUERY_CAP:]
        try:
            _write_all(entries)
        except OSError as e:
            log.warning("hard_queries write failed: %s", e)
            return None
    return rec


def _analysis_marker() -> Path:
    return Path(config.AUTOTUNE_DIR) / ".last_hardq_analysis"


def analyze_hard_queries(now: float | None = None, force: bool = False) -> dict:
    """周期分析：query 簇（跨≥2 天 且 命中≥PROPOSAL_MIN）→ 调参提案 JSON（幂等，已有不重产）。

    节流：距上次分析 < HARD_QUERY_ANALYSIS_MIN_S 直接跳过（force=True 强制）。
    返回 {scanned, clusters, proposals: [路径]}。
    """
    now = now if now is not None else time.time()
    if not force:
        try:
            if now - _analysis_marker().stat().st_mtime < config.HARD_QUERY_ANALYSIS_MIN_S:
                return {"scanned": 0, "clusters": 0, "proposals": [],
                        "skipped": "throttled"}
        except OSError:
            pass
    entries = read_all()
    clusters: dict[str, dict] = {}
    for e in entries:
        c = clusters.setdefault(e["key"], {"query": e["query"], "hits": 0,
                                           "days": set(), "callers": set(),
                                           "reasons": set()})
        c["hits"] += 1
        c["days"].add(str(e.get("ts", ""))[:10])
        c["callers"].add(str(e.get("caller", ""))[:60])
        c["reasons"].add(str(e.get("reason", ""))[:20])
    prop_dir = Path(config.AUTOTUNE_DIR) / "candidates"
    prop_dir.mkdir(parents=True, exist_ok=True)
    made: list[str] = []
    for key, c in clusters.items():
        if c["hits"] < config.HARD_QUERY_PROPOSAL_MIN_HITS or len(c["days"]) < 2:
            continue
        fn = prop_dir / f"hardq-{key[:12]}.json"
        if fn.exists():
            continue                                   # 幂等：同簇已有提案不重产
        proposal = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": "hard_query_cluster",
            "query": c["query"], "query_key": key,
            "hits": c["hits"], "active_days": sorted(c["days"]),
            "callers": sorted(x for x in c["callers"] if x),
            "reasons": sorted(x for x in c["reasons"] if x),
            "suggestion": "review_synonym_or_rerank",   # 供 L1/人工评审的方向标签，不自动改参
            "status": "pending_review",
        }
        fn.write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")
        made.append(str(fn))
    try:
        _analysis_marker().parent.mkdir(parents=True, exist_ok=True)
        _analysis_marker().touch()
    except OSError as e:
        log.warning("analysis marker write failed: %s", e)
    if made:
        log.info("hard_query proposals: %d new", len(made))
    return {"scanned": len(entries), "clusters": len(clusters), "proposals": made}
