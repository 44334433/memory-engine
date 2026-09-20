"""/v1/metrics —— 借鉴增强观测面（P1+P4，2026-09-20 拍板「a」）。

GET /v1/metrics?days=7 → 四节聚合（全纯读，零行为变更、零写入）：
- feedback_path:     活性探针四段断言（写入→落列→join 非空→消费门槛）
- injected_two_state: 曝光/消费两态计数 + orphan 对照审查（曝光≠反馈纪律的机械可跑版）
- pinned_exemption:  pinned 占库存比（总+per bank）+ core block staleness（进程内注入时距）
- process_counters:  feedback 通路 skip/拒绝计数（进程内，重启归零）
语义真源：研究-借鉴项目增量调研-2026-09-19.md §7 P1/P4；实现细节见 observability.py 模块 docstring。
本端点不在热路径（recall 关键路径零触碰），SQL 成本=索引计数级，观测工具按需拉取。
"""
import logging
import time

from fastapi import APIRouter, HTTPException, Query, Request

from . import api_feedback
from . import config
from . import observability as obs

log = logging.getLogger("memory-engine.api.metrics")
router = APIRouter(prefix="/v1")


@router.get("/metrics")
def metrics(request: Request, days: int = Query(7, ge=1, le=365)):
    eng = request.app.state.engine
    if eng.db is None:
        raise HTTPException(503, "engine db 未就绪")
    try:
        with eng.db.connection() as conn:
            fb = obs.feedback_probe(conn, days)
            inj = obs.injected_two_state(conn, days)
            pin = obs.pinned_share(conn)
    except Exception as e:  # noqa: BLE001 —— 观测端点故障显式化（禁吞成假健康）
        log.exception("metrics build failed")
        raise HTTPException(500, f"metrics 构建失败: {str(e)[:200]}") from None
    now = time.time()
    return {
        "version": config.VERSION,
        "window_days": obs.window_days(days),
        "uptime_s": round(now - eng.started_at, 1),
        "feedback_path": fb,
        "injected_two_state": inj,
        "pinned_exemption": {
            "share": pin,
            "core_block_staleness": obs.core_block_staleness(
                getattr(eng, "core_block_last_fetch", None), eng.started_at, now),
        },
        "process_counters": dict(api_feedback.counters_snapshot(),
                                 scope="process_since_startup"),
    }
