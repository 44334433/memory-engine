"""核心 API：/v1/retain /v1/recall /v1/health /v1/embed（写入质量闸内嵌）。"""
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import psycopg
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, field_validator

from . import config, db, memory_type as mtype, poison_gate, recall as recall_mod
from . import hard_queries
from .util import content_hash, dedup_hash, derive_title, staleness_of, uuid7, vec_to_pg

log = logging.getLogger("memory-engine.api")
router = APIRouter(prefix="/v1")


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(422, f"时间格式非法: {s}（需 ISO8601）")


class RetainItem(BaseModel):
    content: str
    context: str
    title: Optional[str] = None
    tags: list[str] = []
    domain: str = "general"
    trigger_term: Optional[str] = None
    priority: int = 3
    source_type: str = "manual"
    source_ref: Optional[str] = None
    load_hint: Optional[str] = None
    body_ptr: Optional[str] = None
    owner: Optional[str] = None
    visibility: str = "agent"
    original_date: Optional[str] = None
    source_tier: str = "agent"           # 投毒闸：四级来源，缺省=agent（不信任缺省）
    contains_pii: Optional[bool] = None  # 投毒闸预留：PII 判级待拍板，本批只入库
    tenant_id: Optional[str] = None      # P1 二批：多宿主留位（缺省 None=单宿主不分区）
    agent_id: Optional[str] = None
    memory_type: Optional[str] = None    # W2 分层：缺省 None=按内容启发式自动打标（memory_type.classify）

    @field_validator("memory_type")
    @classmethod
    def _memory_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in config.MEMORY_TYPES:
            raise ValueError(f"memory_type 必须为 {config.MEMORY_TYPES}，收到 {v!r}")
        return v

    @field_validator("source_tier")
    @classmethod
    def _source_tier(cls, v: str) -> str:
        try:
            return poison_gate.normalize_tier(v)
        except ValueError as e:
            raise ValueError(str(e)) from None

    @field_validator("context")
    @classmethod
    def _context_required(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("context 必填（写入质量闸：无上下文记忆禁止入库）")
        return v.strip()

    @field_validator("content")
    @classmethod
    def _content_required(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("content 必填且非空")
        return v.strip()


class RetainRequest(BaseModel):
    bank: str
    caller: str = "main"
    items: list[RetainItem]
    dedup: bool = True   # false=跳过 hash/语义判重（评测语料移植/迁移幂等源已去重时用）

    @field_validator("bank")
    @classmethod
    def _bank_valid(cls, v: str) -> str:
        if v not in config.BANKS:
            raise ValueError(f"bank 必须为 {config.BANKS} 之一")
        return v


class RecallRequest(BaseModel):
    query: str
    bank: Optional[str] = None
    caller: str = "main"
    top_k: int = 10
    filters: dict = {}


@router.post("/retain")
def retain(req: RetainRequest, request: Request, bg: BackgroundTasks):
    t0 = time.perf_counter()
    if not req.items:
        raise HTTPException(422, "items 为空")
    eng = request.app.state.engine
    # —— 投毒闸①：注入模式扫描（先于嵌入/入库；命中即拒 422 带样本，不吞不静默）——
    flagged = []
    for i, it in enumerate(req.items):
        if poison_gate.should_scan(it.source_tier):
            hits = poison_gate.scan_injection(f"{it.title or ''}\n{it.content}")
            if hits:
                flagged.append({"index": i, "source_tier": it.source_tier, "patterns": hits})
    if flagged:
        raise HTTPException(422, detail={
            "error": "投毒闸：注入模式命中，拒绝入库",
            "scan_scope": config.INJECTION_SCAN_SCOPE, "items": flagged,
        })
    vectors = eng.embedder.embed_documents([it.content for it in req.items])
    ids, skipped, maxseq, dedup_existing = [], 0, 0, []
    with pool_conn(eng) as conn:
        for it, vec in zip(req.items, vectors):
            ch = content_hash(req.bank, it.content)
            if req.dedup:
                if db.hash_dup_id(conn, req.bank, ch):
                    skipped += 1
                    continue
                dup_id, _sim = db.semantic_dup(conn, req.bank, vec_to_pg(vec),
                                               config.DEDUP_DAYS, config.dedup_cos_for(req.bank))
                if dup_id:
                    skipped += 1
                    continue
            od = _parse_dt(it.original_date)
            # —— 投毒闸②：外部来源（agent/web/cron）默认 trial 低信任入场（user 走 candidate）——
            entry_state = poison_gate.entry_state(it.source_tier)
            expires_days = config.TRIAL_DECAY_DAYS if entry_state == "trial" else config.CANDIDATE_DAYS
            dk = dedup_hash(it.content, it.context)   # P1：UNIQUE 兜底键 sha256(body+US+context)
            try:
                row = db.insert_memory(
                    conn,
                    id=uuid7(), bank=req.bank, domain=it.domain, trigger_term=it.trigger_term,
                    title=it.title or derive_title(it.content), body=it.content,
                    body_ptr=it.body_ptr, tags=list(it.tags), owner=it.owner or req.caller,
                    visibility=it.visibility, source_type=it.source_type, source_ref=it.source_ref,
                    priority=it.priority, original_date=od, staleness=staleness_of(od),
                    embed_model=config.EMBED_MODEL, embed_dim=config.EMBED_DIM,
                    content_hash=ch, embedding=vec_to_pg(vec), dedup_key=dk,
                    ttl_state=entry_state, ttl_expires_days=expires_days,
                    source_tier=it.source_tier, contains_pii=it.contains_pii,
                    tenant_id=it.tenant_id, agent_id=it.agent_id,   # P1 二批：多宿主留位
                    memory_type=it.memory_type or mtype.classify(
                        f"{it.title or ''}\n{it.content}"),           # W2：显式类型优先，缺省启发式
                )
            except psycopg.errors.UniqueViolation:
                # P1 判重 UNIQUE 兜底：并发竞态撞 UNIQUE(bank, dedup_key) → 返回既有条目（非 500 不重试炸）；
                # 查不到对应行=非 dedup_key 冲突（防御），原样上抛不吞。
                existing = db.fetch_one(
                    conn,
                    "SELECT id, seq FROM memories WHERE bank=%s AND dedup_key=%s "
                    "AND ttl_state<>'retired' ORDER BY seq LIMIT 1",
                    (req.bank, dk),
                )
                if not existing:
                    raise
                skipped += 1
                dedup_existing.append(str(existing["id"]))
                continue
            ids.append(str(row["id"]))
            maxseq = max(maxseq, row["seq"])
    return {"ids": ids, "dedup_skipped": skipped, "dedup_existing": dedup_existing, "seq": maxseq,
            "took_ms": round((time.perf_counter() - t0) * 1000, 1)}


@router.post("/recall")
def recall(req: RecallRequest, request: Request, bg: BackgroundTasks):
    if not req.query.strip():
        raise HTTPException(422, "query 必填且非空")
    if req.bank is not None and req.bank not in config.BANKS:
        raise HTTPException(422, f"bank 必须为 {config.BANKS} 之一或 null（跨库）")
    try:
        recall_mod.validate_filters(req.filters)   # 坏参 400 带原因（P0 前：坏 date_range 裸 500）
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    eng = request.app.state.engine
    try:
        res = recall_mod.recall(eng.db, eng.embedder, req.query, req.bank, req.caller,
                                max(1, min(req.top_k, 100)), req.filters,
                                # W3：None=关（缺省），零开销；getattr=兼容旧 fake engine（SimpleNamespace）
                                reranker=getattr(eng, "reranker", None))
    except recall_mod.RecallRouteError as e:
        # P1 语义演进：503 只留给全路失败（部分路失败已在 recall 内降级为 200+degraded+failed_routes）
        raise HTTPException(503, detail={
            "error": "recall 全路失败（P0 显式语义：禁吞成静默空结果）",
            "degraded": True, "retryable": True, "failed_routes": e.failed_routes,
        }) from None
    if res["results"]:
        hit_ids = [r["id"] for r in res["results"]]
        bg.add_task(_record_hits, eng, hit_ids, req.caller, req.query)
    # —— L3 失败回流（P1 第三批）：零命中/低分 query 落困难样本池（后台任务，不入关键路径）——
    top1 = res["results"][0]["score"] if res["results"] else None
    if top1 is None or top1 < config.HARD_QUERY_SCORE:
        reason = "zero_hit" if top1 is None else "low_score"
        bg.add_task(_record_hard_query, req.query, req.caller, req.bank, top1, reason)
    return res


def _record_hard_query(query: str, caller: str, bank: str | None,
                       top1_score: float | None, reason: str) -> None:
    try:
        hard_queries.record_hard_query(query, caller, top1_score, bank, reason)
    except Exception as e:
        log.warning("hard_query record failed: %s", e)


def _record_hits(eng, hit_ids: list, caller: str, query: str) -> None:
    """异步批量写访问事件 + 更新访问计数（不入关键路径，蓝图 §4）。"""
    try:
        with eng.db.connection() as conn:
            db.record_access(conn, [(h, "recall_hit", caller, query[:500]) for h in hit_ids])
            db.execute(conn, "UPDATE memories SET access_count=access_count+1, last_accessed_at=now() WHERE id = ANY(%s)", (hit_ids,))
    except Exception as e:
        log.warning("access_events write failed: %s", e)


@router.get("/health")
def health(request: Request):
    eng = request.app.state.engine
    db_ok, pg_info = False, {}
    try:
        with eng.db.connection() as conn:
            row = db.fetch_one(conn, "SELECT version() v, pg_database_size(current_database()) sz") or {}
            db_ok = bool(row)
            pg_info = {"server": (row.get("v") or "").split(",")[0], "db_size_bytes": row.get("sz")}
            arch = db.fetch_one(conn, "SELECT archived_count a, failed_count f, last_archived_wal w FROM pg_stat_archiver") or {}
            pg_info["archiver"] = {"archived": arch.get("a"), "failed": arch.get("f"), "last_wal": arch.get("w")}
            pg_info["memories"] = (db.fetch_one(conn, "SELECT count(*) c FROM memories") or {"c": 0})["c"]
    except Exception as e:
        pg_info = {"error": str(e)[:200]}
    uptime = time.time() - eng.started_at
    status = "ok" if (db_ok and eng.model_loaded and eng.warm) else "degraded"
    return {
        "status": status, "version": config.VERSION,
        "db": db_ok, "model_loaded": eng.model_loaded, "warm": eng.warm,
        "ready": eng.ready, "uptime_s": round(uptime, 1),
        "port": config.PORT, "pg": pg_info,
        "embed": {"provider": config.EMBED_PROVIDER, "model": config.EMBED_MODEL,
                  "dim": config.EMBED_DIM, "device": eng.embedder.device if eng.embedder else None},
    }


class EmbedRequest(BaseModel):
    input: str | list[str]
    query: bool = False


@router.post("/embed")
def embed(req: EmbedRequest, request: Request):
    eng = request.app.state.engine
    texts = [req.input] if isinstance(req.input, str) else req.input
    if not texts or any(not t.strip() for t in texts):
        raise HTTPException(422, "input 必填且非空")
    vecs = eng.embedder.embed_queries(texts) if req.query else eng.embedder.embed_documents(texts)
    return {"embeddings": vecs, "dim": config.EMBED_DIM, "model": config.EMBED_MODEL}


def pool_conn(eng):
    return eng.db.connection()
