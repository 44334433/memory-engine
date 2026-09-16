"""生命周期与整合 API（阶段2）：
GET  /v1/lifecycle                    查询（状态分布+最近扫描+线程+WAL）
POST /v1/lifecycle/run                触发一轮扫描（同步，SQL-only 毫秒级）
GET  /v1/lifecycle/candidates         待转正/待衰减/待归档清单
POST /v1/lifecycle/transition         人工干预转换（promote/demote/archive/revive/trialize）
GET  /v1/lifecycle/history            生命周期事件流（changelog op=lifecycle）
POST /v1/consolidate                  触发整合（异步队列，返回 operation_id）
GET  /v1/consolidate                  最近整合操作列表
GET  /v1/consolidate/{op_id}          整合进度查询
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from . import config, db, lifecycle as lc, consolidate as consol

log = logging.getLogger("memory-engine.api")
router = APIRouter(prefix="/v1")


class RunRequest(BaseModel):
    dry_run: bool = False


class TransitionRequest(BaseModel):
    memory_id: str
    action: str
    reason: str = ""

    @field_validator("memory_id")
    @classmethod
    def _mid(cls, v: str) -> str:
        if not v or len(v) < 32:
            raise ValueError("memory_id 须为 UUID")
        return v


class ConsolidateRequest(BaseModel):
    days: Optional[int] = None
    limit: Optional[int] = None
    sim: Optional[float] = None
    dry_run: bool = False


@router.get("/lifecycle")
def lifecycle_summary(request: Request):
    eng = request.app.state.engine
    with eng.db.connection() as conn:
        summary = lc.state_summary(conn)
    last = getattr(eng, "lifecycle_last", None)
    if last is None:
        try:
            with eng.db.connection() as conn:
                row = db.fetch_one(conn, "SELECT value FROM engine_meta WHERE key='lifecycle_last_scan'")
            last = row["value"] if row else None
        except Exception:
            last = None
    return {"states": summary,
            "last_scan": last,
            "thread_alive": bool(getattr(eng, "lifecycle_thread", None)
                                 and eng.lifecycle_thread.is_alive()),
            "interval_s": config.LIFECYCLE_INTERVAL_S,
            "rules": {"candidate_days": config.CANDIDATE_DAYS,
                      "promote_min_hits": config.PROMOTE_MIN_HITS,
                      "promote_min_adopt_rate": config.PROMOTE_MIN_ADOPT_RATE,
                      "active_decay_days": config.ACTIVE_DECAY_DAYS,
                      "archive_days": config.DECAY_ARCHIVE_DAYS,
                      "revive_window_days": config.REVIVE_WINDOW_DAYS},
            "wal": lc.wal_status()}


@router.post("/lifecycle/run")
def lifecycle_run(request: Request, body: RunRequest | None = None):
    eng = request.app.state.engine
    dry = bool(body and body.dry_run)
    with eng.db.connection() as conn:
        if dry:
            pending = lc.candidates(conn)
            return {"dry_run": True,
                    "ts": datetime.now(timezone.utc).isoformat(), "pending": pending}
        result = lc.scan(conn)
        eng.lifecycle_last = result
        db.execute(conn,
                   "INSERT INTO engine_meta(key,value) VALUES ('lifecycle_last_scan',%s::jsonb) "
                   "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                   (json.dumps(result, ensure_ascii=False),))
    return result


@router.get("/lifecycle/candidates")
def lifecycle_candidates(request: Request):
    eng = request.app.state.engine
    with eng.db.connection() as conn:
        return lc.candidates(conn)


@router.post("/lifecycle/transition")
def lifecycle_transition(request: Request, body: TransitionRequest):
    eng = request.app.state.engine
    try:
        with eng.db.connection() as conn:
            return lc.transition(conn, body.memory_id, body.action, body.reason)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/lifecycle/history")
def lifecycle_history(request: Request, limit: int = 50):
    eng = request.app.state.engine
    with eng.db.connection() as conn:
        rows = lc.history(conn, limit)
    for r in rows:
        r["memory_id"] = str(r["memory_id"]) if r["memory_id"] else None
        if hasattr(r["ts"], "isoformat"):
            r["ts"] = r["ts"].isoformat()
    return {"items": rows, "count": len(rows)}


@router.post("/consolidate")
def consolidate_submit(request: Request, body: ConsolidateRequest):
    eng = request.app.state.engine
    if body.sim is not None and not (0.5 < body.sim < 1.0):
        raise HTTPException(422, "sim 须在 (0.5,1.0)")
    return consol.submit(eng.db, body.days, body.limit, body.sim, body.dry_run)


@router.get("/consolidate")
def consolidate_list(request: Request):
    eng = request.app.state.engine
    ops = consol.list_ops()
    if not ops:
        ops = consol.load_persisted(eng.db)
    return {"operations": ops, "count": len(ops)}


@router.get("/consolidate/{op_id}")
def consolidate_op(op_id: str, request: Request):
    eng = request.app.state.engine
    op = consol.get_op(op_id)
    if not op:
        persisted = {o["operation_id"]: o for o in consol.load_persisted(eng.db)}
        op = persisted.get(op_id)
    if not op:
        raise HTTPException(404, f"consolidate operation {op_id} 不存在")
    return op
