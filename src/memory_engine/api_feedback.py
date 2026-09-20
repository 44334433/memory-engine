"""/v1/feedback —— outcome 反馈端点（自进化专项 #1，2026-09-18 拍板 a）。

错误语义沿用 P0 批纪律：坏参=400 带原因（outcome 非法——手动校验映射 400，
不走 pydantic field_validator 的 422，对齐 validate_filters 先例）；id 不存在=404。
L3 落池走 BackgroundTasks（与 /v1/recall 的 hard_query 同一非关键路径模式）。
"""
import logging
import time
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

from . import outcome as outcome_mod

log = logging.getLogger("memory-engine.api.feedback")
router = APIRouter(prefix="/v1")


class FeedbackRequest(BaseModel):
    memory_id: uuid.UUID
    outcome: str                       # adopted|corrected|useless（endpoint 内校验→400）
    caller: Optional[str] = None       # 回执归属（进 access_events/changelog，仅截断不校验）
    query: Optional[str] = None        # 可选：宿主原始查询（corrected/useless 的 L3 信号源；
    # 缺省回退该条最近 recall_hit 事件的 query）


def _record_l3(mem_id: str, l3: dict, caller: str | None, bank: str | None) -> None:
    """后台任务：失败回流落池（故障只 log，不反噬已提交的反馈信号）。"""
    try:
        outcome_mod.record_l3_signal(mem_id, l3["query"], caller, bank, l3["reason"])
    except Exception as e:  # noqa: BLE001 —— L3 文件闸故障显式登记（禁静默），反馈结果不回滚
        log.warning("L3 feedback signal failed mid=%s: %s", mem_id, e)


@router.post("/feedback")
def feedback(req: FeedbackRequest, request: Request, bg: BackgroundTasks):
    t0 = time.perf_counter()
    if req.outcome not in outcome_mod.OUTCOMES:
        raise HTTPException(400, f"outcome 必须为 {list(outcome_mod.OUTCOMES)} 之一，"
                            f"收到 {req.outcome!r}")
    eng = request.app.state.engine
    try:
        with eng.db.connection() as conn:
            res = outcome_mod.apply_feedback(conn, req.memory_id, req.outcome,
                                             req.caller, req.query)
    except ValueError as e:            # apply_feedback 兜底校验（同上语义，防旁路直调漂移）
        raise HTTPException(400, str(e)) from None
    if res is None:
        raise HTTPException(404, f"memory {req.memory_id} 不存在")
    if res["l3_signal"]:
        bg.add_task(_record_l3, res["id"], res["l3_signal"], req.caller, res["bank"])
    res["took_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return res
