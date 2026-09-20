"""/v1/core-block —— W1 核心记忆块生成端点（2026-09-18 拍板顺序 W2 之后）。

GET /v1/core-block?budget_chars=1500&caller=main
  → {text, ids, used_chars, budget_chars, dropped_count, pinned_candidates,
     auto_candidates, truncated, took_ms}

条目级原文（非摘要）：正文原样入块，仅超预算时条目尾部截断加「…」；入选/排序/预算
纯逻辑在 core_block.py（单表双轨候选 + 启发式分排序，零嵌入计算——验收线 P95<50ms）。

与 freshness digest 的机制辨析（正交两通道，宿主可并用）：
- digest：真源变更的游标增量摘要（事件级「⟳vN 自游标vM以来 K 条变更」，投递语义推进
  游标，同一变更只投一次）；
- core block：高价值记忆的条目级原文常驻（无游标、幂等只读、每次全量重算——同一请求
  参数下短时间内多次 GET 结果一致，无消费状态）。
简言之：digest 管「新变化」，core block 管「必须恒可见」。

注入接线纪律（复用 freshness-protocol 注入通道模式）：本端点只**被拉取**，宿主侧
（~/.hermes/plugins/memory-engine provider prefetch 通道）在每轮组装时 GET 本端点并把
text 拼进注入段（user message 尾部）。**引擎绝不强推 system prompt**——system 前缀
任何变动都会击穿宿主前缀缓存（freshness-protocol 拍板铁律同源）。

零副作用铁律：本端点不写 access_events/access_count/changelog（防「注入→计数→更易
注入」自激回路，论证见 core_block.py 模块 docstring）；也不改变任何检索行为。
"""
import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from . import config, core_block as cb, db
from .recall import _vis_sql

log = logging.getLogger("memory-engine.api.core_block")
router = APIRouter(prefix="/v1")


@router.get("/core-block")
def get_core_block(request: Request, caller: str = "main",
                   budget_chars: Optional[int] = Query(
                       None, ge=1, le=10000,
                       description="单次注入字符预算闸（缺省=config.CORE_BLOCK_BUDGET_CHARS"
                                   "=1500，对齐 freshness 注入闸）")):
    t0 = time.perf_counter()
    budget = config.CORE_BLOCK_BUDGET_CHARS if budget_chars is None else budget_chars
    eng = request.app.state.engine
    vis_sql, vis_params = _vis_sql(caller)   # 与 recall 同一可见性闸（main 全见；其他仅 owner/public）
    try:
        out = cb.build_block(db, eng.db, vis_sql, tuple(vis_params), budget)
    except Exception as e:  # noqa: BLE001 —— fail-open 语义在宿主侧，引擎侧真故障必须显式 500
        log.exception("core-block build failed")
        raise HTTPException(500, f"core-block 构建失败: {str(e)[:200]}") from None
    # P4 staleness 观测打点：纯内存时间戳，不写 access_events/changelog/任何库表——
    # 零副作用铁律（防「注入→计数→更易注入」自激）不破；重启进程后归零，metrics 侧如实标注。
    eng.core_block_last_fetch = time.time()
    return {
        "text": out["text"],
        "ids": out["ids"],
        "budget_chars": budget,
        "used_chars": out["used_chars"],
        "dropped_count": out["dropped_count"],
        "pinned_candidates": out["pinned_candidates"],
        "auto_candidates": out["auto_candidates"],
        "truncated": out["dropped_count"] > 0,
        "max_items": config.CORE_BLOCK_MAX_ITEMS,
        "took_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
