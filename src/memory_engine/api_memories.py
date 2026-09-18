""" memories 管理 + export + admin/backup：/v1/memories /v1/export /v1/admin/backup """
import json
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from . import config, db
from .api_core import _parse_dt
from .backup import run_backup
from .util import content_hash, staleness_of, vec_to_pg

log = logging.getLogger("memory-engine.api")
router = APIRouter(prefix="/v1")

EDITABLE_TEXT = ("title", "body", "domain", "trigger_term", "load_hint",
                 "owner", "visibility", "source_ref")
STATES = ("candidate", "trial", "active", "decaying", "archived", "retired")
VERIFY = ("verified", "stale", "unverified")
VIS = ("public", "agent", "private")

LIST_COLS = ("id, seq, bank, domain, trigger_term, title, body, body_ptr, tags, owner, "
             "visibility, source_type, source_ref, priority, ttl_state, staleness, "
             "verify_status, created_at, updated_at, original_date, access_count, adopt_count, "
             "source_tier, contains_pii, "
             "valid_at, invalid_at, is_current, tenant_id, agent_id, "   # P1 二批：双时序+多宿主
             "memory_type, "                                             # W2 分层
             "(embedding IS NOT NULL) AS has_embedding")


class PatchRequest(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    tags: Optional[list[str]] = None
    domain: Optional[str] = None
    trigger_term: Optional[str] = None
    load_hint: Optional[str] = None
    owner: Optional[str] = None
    visibility: Optional[str] = None
    source_ref: Optional[str] = None
    priority: Optional[int] = None
    ttl_state: Optional[str] = None
    verify_status: Optional[str] = None
    original_date: Optional[str] = None
    tenant_id: Optional[str] = None      # P1 二批：多宿主留位
    agent_id: Optional[str] = None
    memory_type: Optional[str] = None    # W2 分层：类型可人工纠偏
    supersede: bool = False              # P1 二批：双时序矛盾更新（旧条失效+新条重存，id 会变）
    valid_at: Optional[str] = None       # supersede 事件时间（缺省=now()；时间截断两侧同值）

    @field_validator("memory_type")
    @classmethod
    def _mtype(cls, v):
        if v is not None and v not in config.MEMORY_TYPES:
            raise ValueError(f"memory_type 必须为 {config.MEMORY_TYPES}")
        return v

    @field_validator("ttl_state")
    @classmethod
    def _state(cls, v):
        if v is not None and v not in STATES:
            raise ValueError(f"ttl_state 必须为 {STATES}")
        return v

    @field_validator("verify_status")
    @classmethod
    def _verify(cls, v):
        if v is not None and v not in VERIFY:
            raise ValueError(f"verify_status 必须为 {VERIFY}")
        return v

    @field_validator("visibility")
    @classmethod
    def _vis(cls, v):
        if v is not None and v not in VIS:
            raise ValueError(f"visibility 必须为 {VIS}")
        return v


@router.get("/memories")
def list_memories(request: Request, bank: Optional[str] = None, state: Optional[str] = None,
                  owner: Optional[str] = None, domain: Optional[str] = None,
                  tenant_id: Optional[str] = None, agent_id: Optional[str] = None,
                  memory_type: Optional[str] = None,
                  q: Optional[str] = None, limit: int = 20, offset: int = 0):
    eng = request.app.state.engine
    limit = max(1, min(limit, 200))
    where, params = ["TRUE"], []
    if bank:
        where.append("bank=%s"); params.append(bank)
    if state:
        where.append("ttl_state=%s"); params.append(state)
    if owner:
        where.append("owner=%s"); params.append(owner)
    if domain:
        where.append("domain=%s"); params.append(domain)
    if tenant_id:
        where.append("tenant_id=%s"); params.append(tenant_id)   # P1 二批：多宿主可查
    if agent_id:
        where.append("agent_id=%s"); params.append(agent_id)
    if memory_type:
        where.append("memory_type=%s"); params.append(memory_type)  # W2 分层：类型过滤
    if q:
        where.append("search_text &@~ %s"); params.append(q)
    wsql = " AND ".join(where)
    with eng.db.connection() as conn:
        rows = db.fetch_all(conn, f"SELECT {LIST_COLS} FROM memories WHERE {wsql} ORDER BY seq DESC LIMIT %s OFFSET %s", (*params, limit, offset))
        total = (db.fetch_one(conn, f"SELECT count(*) c FROM memories WHERE {wsql}", tuple(params)) or {"c": 0})["c"]
    return {"items": [_jsonable(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/memories/{mid}")
def get_memory(mid: uuid.UUID, request: Request):
    eng = request.app.state.engine
    with eng.db.connection() as conn:
        row = db.fetch_one(conn, f"SELECT {LIST_COLS} FROM memories WHERE id=%s", (mid,))
    if not row:
        raise HTTPException(404, f"memory {mid} 不存在")
    return _jsonable(row)


@router.patch("/memories/{mid}")
def patch_memory(mid: uuid.UUID, req: PatchRequest, request: Request):
    eng = request.app.state.engine
    if req.supersede and req.body is None:
        raise HTTPException(422, "supersede=true 需同时提供新 body（双时序重存以内容修订为前提）")
    sets, params = [], []
    with eng.db.connection() as conn:
        cur = db.fetch_one(conn, "SELECT bank, title, body, original_date FROM memories WHERE id=%s", (mid,))
        if not cur:
            raise HTTPException(404, f"memory {mid} 不存在")
        if req.body is not None and not req.supersede:
            # P1 二批（双时序）：历史版本禁止就地改写内容——内容修订必须走 supersede=true（旧失效+新重存）
            hist = db.fetch_one(conn, "SELECT is_current FROM memories WHERE id=%s", (mid,))
            if hist and not hist["is_current"]:
                raise HTTPException(409, "该条已是历史版本（invalid_at 非空），禁止就地改写内容；"
                                         "请改用 supersede=true 重存新版本")
        if req.supersede:
            return _patch_supersede(eng, conn, mid, cur, req)
        new_title = req.title if req.title is not None else cur["title"]
        new_body = req.body if req.body is not None else cur["body"]
        new_od = _parse_dt(req.original_date) if req.original_date else cur["original_date"]
        for f in EDITABLE_TEXT:
            v = getattr(req, f)
            if v is not None:
                sets.append(f"{f}=%s"); params.append(v)
        if req.priority is not None:
            if req.priority not in config.PRIORITIES:
                raise HTTPException(422, f"priority 须在 {config.PRIORITIES}")
            sets.append("priority=%s"); params.append(req.priority)
        if req.tags is not None:
            sets.append("tags=%s::jsonb"); params.append(json.dumps(req.tags))
        if req.tenant_id is not None:
            sets.append("tenant_id=%s"); params.append(req.tenant_id)   # P1 二批：多宿主可改
        if req.agent_id is not None:
            sets.append("agent_id=%s"); params.append(req.agent_id)
        if req.memory_type is not None:
            sets.append("memory_type=%s"); params.append(req.memory_type)  # W2：人工纠偏通道
        if req.ttl_state is not None:
            sets.append("ttl_state=%s"); params.append(req.ttl_state)
            if req.ttl_state == "retired":
                sets.append("embedding=NULL")
        if req.verify_status is not None:
            sets.append("verify_status=%s"); params.append(req.verify_status)
            if req.verify_status in ("verified", "stale"):
                sets.append("last_verified=now()")
        if req.original_date is not None or (req.body is not None and cur["original_date"] is None):
            sets.append("original_date=%s"); params.append(new_od)
            sets.append("staleness=%s"); params.append(staleness_of(new_od))
        if req.body is not None:
            sets.append("content_hash=%s"); params.append(content_hash(cur["bank"], new_body))
            sets.append("embed_model=%s"); params.append(config.EMBED_MODEL)
            sets.append("embed_dim=%s"); params.append(config.EMBED_DIM)
            sets.append("embed_ver=%s"); params.append(config.EMBED_VER)   # P1：改文重嵌=当前批次版本
            vec = eng.embedder.embed_documents([new_body])[0]   # 改文即重嵌（GPU 在事务外：持池连接不做嵌入）
            sets.append("embedding=%s::vector"); params.append(vec_to_pg(vec))
        sets.append("updated_at=now()")
        params.append(mid)
        # P1 原子化批：memories UPDATE + changelog INSERT 包同一事务（原：非原子，改文成功+账本缺账可能）
        with conn.transaction():
            row = db.fetch_one(conn, f"UPDATE memories SET {', '.join(sets)} WHERE id=%s RETURNING {LIST_COLS}", tuple(params))
            db.log_changelog(conn, "update", mid, {"patched": [s.split("=")[0] for s in sets]})
    if not row:
        raise HTTPException(404, f"memory {mid} 不存在")
    return _jsonable(row)


def _patch_supersede(eng, conn, mid: uuid.UUID, cur: dict, req: "PatchRequest") -> dict:
    """P1 二批：双时序 supersede 补丁路径。

    旧条 invalid_at = 新条 valid_at（时间截断，同一时刻）+ 新条 INSERT 重存——绝不 DELETE/UPDATE
    复写历史。新版本继承旧条事实字段（ttl_state/source_tier/dedup_key 等），patch 字段覆盖其上；
    嵌入按新 body 重算（GPU/网络调用在事务外）。旧行已失效→409（幂等防复写）。
    """
    full = db.fetch_one(conn, "SELECT * FROM memories WHERE id=%s", (mid,))
    if not full:
        raise HTTPException(404, f"memory {mid} 不存在")
    new_body: str = req.body or ""
    new_od = _parse_dt(req.original_date) if req.original_date else full["original_date"]
    vec = eng.embedder.embed_documents([new_body])[0]   # 重嵌在事务外（持池连接不做嵌入）
    fields = dict(
        id=uuid.uuid4(),
        bank=full["bank"],
        domain=req.domain if req.domain is not None else full["domain"],
        trigger_term=req.trigger_term if req.trigger_term is not None else full["trigger_term"],
        title=req.title if req.title is not None else full["title"],
        body=new_body,
        body_ptr=full["body_ptr"],
        tags=req.tags if req.tags is not None else full["tags"],
        owner=req.owner if req.owner is not None else full["owner"],
        visibility=req.visibility if req.visibility is not None else full["visibility"],
        source_type=full["source_type"],
        source_ref=req.source_ref if req.source_ref is not None else full["source_ref"],
        priority=req.priority if req.priority is not None else full["priority"],
        ttl_state=req.ttl_state if req.ttl_state is not None else full["ttl_state"],
        ttl_expires_days=None,
        source_tier=full["source_tier"],
        contains_pii=full["contains_pii"],
        original_date=new_od,
        staleness=staleness_of(new_od),
        embed_model=config.EMBED_MODEL, embed_dim=config.EMBED_DIM,
        content_hash=content_hash(full["bank"], new_body),
        embedding=vec_to_pg(vec),
        dedup_key=full["dedup_key"],        # 同一事实谱系共享判重键（uq_mem_dedup 已收紧为仅现行判重）
        valid_at=_parse_dt(req.valid_at),
        tenant_id=req.tenant_id if req.tenant_id is not None else full["tenant_id"],
        agent_id=req.agent_id if req.agent_id is not None else full["agent_id"],
        memory_type=req.memory_type if req.memory_type is not None else full["memory_type"],  # W2：谱系继承
    )
    res = db.supersede_memory(conn, mid, fields)
    if res is None:
        raise HTTPException(409, f"memory {mid} 已被 supersede（invalid_at 非空），拒绝重复失效复写")
    new_row = db.fetch_one(conn, f"SELECT {LIST_COLS} FROM memories WHERE id=%s", (res["new_id"],))
    out = _jsonable(new_row) if new_row else {"id": res["new_id"]}
    out["superseded_from"] = str(mid)
    return out


@router.delete("/memories/{mid}")
def delete_memory(mid: uuid.UUID, request: Request, purge: bool = False):
    """DELETE=retired 语义（蓝图 §7：行保留+changelog，embedding 清空）；purge=true 硬删行。"""
    eng = request.app.state.engine
    with eng.db.connection() as conn, conn.transaction():
        if purge:
            row = db.fetch_one(conn, "DELETE FROM memories WHERE id=%s RETURNING id", (mid,))
            state = "deleted"
        else:
            row = db.fetch_one(
                conn,
                "UPDATE memories SET ttl_state='retired', embedding=NULL, updated_at=now() "
                "WHERE id=%s AND ttl_state<>'retired' RETURNING id", (mid,))
            state = "retired"
        if not row:
            raise HTTPException(404, f"memory {mid} 不存在或已是 retired")
        db.log_changelog(conn, "delete", mid, {"purge": purge, "state": state})
    return {"id": str(mid), "state": state}


@router.post("/memories/{mid}/adopt")
def adopt_memory(mid: uuid.UUID, request: Request, caller: str = "main"):
    """采纳回执（准入闸信号源之一：access_events kind='adopted'，蓝图 §7）。"""
    eng = request.app.state.engine
    with eng.db.connection() as conn, conn.transaction():
        row = db.fetch_one(conn, "SELECT id FROM memories WHERE id=%s", (mid,))
        if not row:
            raise HTTPException(404, f"memory {mid} 不存在")
        db.execute(conn, "INSERT INTO access_events(memory_id, kind, caller) VALUES (%s,'adopted',%s)",
                   (mid, caller[:120]))
        db.execute(conn, "UPDATE memories SET adopt_count=adopt_count+1, last_accessed_at=now() WHERE id=%s", (mid,))
        # P1 原子化批：adopt 记账进 changelog（账本真源）与 UPDATE 同事务，杜绝「计数变+账本缺账」
        db.log_changelog(conn, "adopt", mid, {"caller": caller[:120]})
        cnt = db.fetch_one(conn, "SELECT adopt_count FROM memories WHERE id=%s", (mid,)) or {"adopt_count": 0}
    return {"id": str(mid), "adopt_count": cnt["adopt_count"]}


@router.post("/memories/{mid}/reembed")
def reembed_memory(mid: uuid.UUID, request: Request):
    """单条重嵌（CLI retrain；不改正文，仅重算向量+changelog op=reembed）。"""
    eng = request.app.state.engine
    with eng.db.connection() as conn:
        row = db.fetch_one(conn, "SELECT body FROM memories WHERE id=%s", (mid,))
        if not row:
            raise HTTPException(404, f"memory {mid} 不存在")
        vec = eng.embedder.embed_documents([row["body"]])[0]
        with conn.transaction():
            db.execute(conn,
                       "UPDATE memories SET embedding=%s::vector, embed_model=%s, embed_dim=%s, "
                       "embed_ver=%s, updated_at=now() WHERE id=%s",
                       (vec_to_pg(vec), config.EMBED_MODEL, config.EMBED_DIM, config.EMBED_VER, mid))
            db.log_changelog(conn, "reembed", mid,
                             {"dim": config.EMBED_DIM, "model": config.EMBED_MODEL,
                              "embed_ver": config.EMBED_VER})
    return {"id": str(mid), "reembedded": True, "dim": config.EMBED_DIM}


@router.get("/export")
def export_memories(request: Request, since_seq: int = 0, limit: int = 100000):
    """JSONL 流式导出（备份/迁移复用，蓝图 §7）。不含 embedding（重嵌管线负责向量）。"""
    eng = request.app.state.engine

    def gen():
        cursor = since_seq
        batch = 500
        sent = 0
        while sent < limit:
            with eng.db.connection() as conn:
                rows = db.fetch_all(conn, f"SELECT {LIST_COLS} FROM memories WHERE seq>%s ORDER BY seq LIMIT %s", (cursor, min(batch, limit - sent)))
            if not rows:
                break
            for r in rows:
                yield json.dumps(_jsonable(r), ensure_ascii=False, default=str) + "\n"
                cursor = r["seq"]
                sent += 1
            if len(rows) < batch:
                break

    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Content-Disposition": "attachment; filename=memories-export.jsonl"})


@router.post("/admin/backup")
def admin_backup(request: Request):
    eng = request.app.state.engine
    return run_backup(eng.db, trigger="api")


def _jsonable(row: dict) -> dict:
    out = dict(row)
    for k, v in out.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, uuid.UUID):
            out[k] = str(v)
    return out
