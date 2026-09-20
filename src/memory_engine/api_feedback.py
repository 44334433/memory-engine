"""/v1/feedback —— outcome 反馈端点（自进化专项 #1，2026-09-18 拍板 a）。

错误语义沿用 P0 批纪律：坏参=400 带原因（outcome 非法——手动校验映射 400，
不走 pydantic field_validator 的 422，对齐 validate_filters 先例）；id 不存在=404。
L3 落池走 BackgroundTasks（与 /v1/recall 的 hard_query 同一非关键路径模式）。

P2 第一刀（借鉴 E1/M1，2026-09-20）：请求可选 group / retrieved_ids——纯增量字段，
旧 payload 不带=行为与响应逐字节不变（新增响应键 group/retrieved_ids_count 为透出）。
P1 skip 计数透出（借鉴调研 §7 P1）：进程内计数器记「静默跳过」路径（坏参/404/无 query
不落 L3/L3 跳重）——重启归零，持久账在 changelog/access_events；快照进 GET /v1/metrics。
"""
import logging
import threading
import time
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import BaseModel

from . import outcome as outcome_mod

log = logging.getLogger("memory-engine.api.feedback")
router = APIRouter(prefix="/v1")

# —— skip/counter 观测（P1·S2 活性探针的进程侧配平：死掉的路径要有数，禁静默）——
_COUNTERS_LOCK = threading.Lock()
_COUNTERS: dict[str, int] = {
    "applied": 0,               # 反馈成功落库
    "rejected_bad_outcome": 0,  # 400
    "not_found": 0,             # 404
    "l3_skipped_no_query": 0,   # corrected/useless 无 query 可归因→不落 L3（静默路径显式化）
    "l3_skipped_pool": 0,       # 进 L3 但被池闸跳（TTL 重复/超短/写失败）
    "l3_recorded": 0,           # L3 成功落池
    "with_group": 0,            # E1 带组提交
    "with_retrieved_ids": 0,    # M1 带召回集回传
}


def counters_snapshot() -> dict:
    """计数器快照（拷贝，供 /v1/metrics 读；进程内语义，重启归零）。"""
    with _COUNTERS_LOCK:
        return dict(_COUNTERS)


def _bump(name: str) -> None:
    with _COUNTERS_LOCK:
        _COUNTERS[name] += 1


class FeedbackRequest(BaseModel):
    memory_id: uuid.UUID
    outcome: str                       # adopted|corrected|useless（endpoint 内校验→400）
    caller: Optional[str] = None       # 回执归属（进 access_events/changelog，仅截断不校验）
    query: Optional[str] = None        # 可选：宿主原始查询（corrected/useless 的 L3 信号源；
    # 缺省回退该条最近 recall_hit 事件的 query）
    # —— P2 第一刀（E1/M1，均缺省 None=旧语义）——
    group: Optional[str] = None        # E1 组粒度：同一次检索的 N 条共享一个组标识（聚合提交/查询）
    retrieved_ids: Optional[list[uuid.UUID]] = None   # M1 当次召回的 memory ids 回传（定位消费）


def _record_l3(mem_id: str, l3: dict, caller: str | None, bank: str | None) -> None:
    """后台任务：失败回流落池（故障只 log，不反噬已提交的反馈信号）。"""
    try:
        rec = outcome_mod.record_l3_signal(mem_id, l3["query"], caller, bank, l3["reason"])
        _bump("l3_recorded" if rec else "l3_skipped_pool")
    except Exception as e:  # noqa: BLE001 —— L3 文件闸故障显式登记（禁静默），反馈结果不回滚
        _bump("l3_skipped_pool")
        log.warning("L3 feedback signal failed mid=%s: %s", mem_id, e)


@router.post("/feedback")
def feedback(req: FeedbackRequest, request: Request, bg: BackgroundTasks):
    t0 = time.perf_counter()
    if req.outcome not in outcome_mod.OUTCOMES:
        _bump("rejected_bad_outcome")
        raise HTTPException(400, f"outcome 必须为 {list(outcome_mod.OUTCOMES)} 之一，"
                            f"收到 {req.outcome!r}")
    eng = request.app.state.engine
    try:
        with eng.db.connection() as conn:
            res = outcome_mod.apply_feedback(conn, req.memory_id, req.outcome,
                                             req.caller, req.query,
                                             group=req.group, retrieved_ids=req.retrieved_ids)
    except ValueError as e:            # apply_feedback 兜底校验（同上语义，防旁路直调漂移）
        _bump("rejected_bad_outcome")
        raise HTTPException(400, str(e)) from None
    if res is None:
        _bump("not_found")
        raise HTTPException(404, f"memory {req.memory_id} 不存在")
    _bump("applied")
    if res["group"]:
        _bump("with_group")
    if res["retrieved_ids_count"]:
        _bump("with_retrieved_ids")
    if res["l3_signal"]:
        bg.add_task(_record_l3, res["id"], res["l3_signal"], req.caller, res["bank"])
    elif req.outcome in ("corrected", "useless"):
        _bump("l3_skipped_no_query")   # 无 query 可归因=静默跳池，计数透出（P1 ③）
    res["took_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return res


@router.get("/feedback/groups")
def feedback_groups(request: Request,
                    days: int = Query(7, ge=1, le=365),
                    group: str = Query("", max_length=200),
                    limit: int = Query(50, ge=1, le=500)):
    """E1 组粒度查询面（纯读 changelog）：缺省=按组聚合列表；带 group=该组明细（含 M1 retrieved_ids）。"""
    eng = request.app.state.engine
    with eng.db.connection() as conn:
        if group.strip():
            return {"group": group.strip(),
                    "items": outcome_mod.group_detail(conn, group.strip(), limit)}
        return {"window_days": days, "groups": outcome_mod.group_rollup(conn, days, limit)}
