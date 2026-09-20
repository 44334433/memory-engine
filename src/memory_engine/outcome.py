"""Outcome 反馈信号层（自进化专项 #1，2026-09-18 拍板 a）：POST /v1/feedback 的信号落库 + EMA + L2/L3 接线原语。

Schema 选型（memories 新列 vs 独立表）——落 memories 三列 outcome/polarity/outcome_at，理由：
1) 同构先例：历史六个加列批（source_tier/contains_pii/dedup_key/双时序/多宿主/memory_type）全部
   走「NULL 默认或常量默认 ADD COLUMN + 幂等迁移文件」，PG11+ 常量默认值为元数据级操作不重写行；
   本批三列全可空（NULL=无信号），存量 39k 行零触碰。
2) recall 关键路径：hydrate 是单表按 id 取列，score_parts 透出 outcome 零 JOIN；
   独立表则每次召回要多一次 join/二次查询——给存量检索加税，违背验收线。
3) EMA 是「每条一个演化态」的单行原地更新（w+=α(a−w)），行锁 FOR UPDATE 防并发丢更新；
   事件全历史不丢：逐次信号已进 changelog(op='feedback')，adopted 另进 access_events(kind='adopted')
   （复用既有账本，独立表只会复制这个职责）。
唯一需要独立表的场景=一条记忆并存多评分者/多信号维度（当前无此需求，出现时再迁，列语义可平移）。

EMA（Cognee 边权重同构，w += α·(a − w)）：a=+1(adopted) / −1(corrected|useless)；
prev=NULL 按 0.0（中性起点）——首条信号后 |polarity|=α，重复同向信号按递推收敛于 ±1 且恒不越界。
α=config.OUTCOME_EMA_ALPHA=0.1（写死值，变更点在 config 注释标注；本批不做 env 覆盖，
防生产/评测参数不一致——需要调参走 L1 评审通道改该行）。

L2/L3 接线（读既有代码后定，零新接口、零 lifecycle/hard_queries 代码改动）：
- adopted → 同事务写 access_events(kind='adopted') + adopt_count+1 + last_accessed_at——与
  POST /v1/memories/{id}/adopt 完全同构（该端点就是既有「采纳回执」通道）；L2 三条既有规则
  （upgrade_adopted 30d 升级窗 / decay_* 信号窗续命 / adopt_renew_count 观测）天然消费本事件=护盾。
- corrected/useless → hard_queries.record_hard_query(reason='fb_corrected'/'fb_useless')，
  query 取请求可选 query 字段（宿主上下文真源），缺省回退该条 access_events 最近一条
  recall_hit.query（=把它捞出来的那次查询，失败归因对象）；两者皆无→跳过落池
  （困难样本池是 query 粒度信号，memory 粒度无 query 不臆造）。TTL 跳重/上限防膨胀沿用既有闸。
"""
import logging

from . import config, db

log = logging.getLogger("memory-engine.outcome")

OUTCOMES = ("adopted", "corrected", "useless")
POLARITY_TARGET = {"adopted": 1.0, "corrected": -1.0, "useless": -1.0}   # 三值→±1（Mem0 语义对齐）
_HARDQ_REASON = {"corrected": "fb_corrected", "useless": "fb_useless"}   # record_hard_query reason ≤20 字符

# —— P2 第一刀（借鉴 E1/M1，2026-09-20 拍板「a」）：纯增量可选字段，向后兼容 ——
# E1 组粒度：一次检索召回的 N 条共享一个结局信号（宿主按 group 聚合提交/查询）；
# M1 retrieved ids 回传：feedback 记录关联当次召回的 memory ids（定位消费，免再检索）。
# 落点=changelog(op='feedback').detail jsonb——零新列零迁移（不动 PG CHECK、不动 BANKS）；
# 旧客户端不带字段=detail 无这两键，读写两侧行为逐字节不变。E2 连续域/M2 reason 枚举=第二刀，未做。
GROUP_MAX_LEN = 120            # caller 截断先例同构
RETRIEVED_IDS_CAP = 200        # 组内成员上限（防恶意/误传大包撑爆 jsonb）


def norm_group(group: str | None) -> str | None:
    """E1 组标识归一：strip+截断；空/全空白 → None（=不带组，旧语义）。纯函数可单测。"""
    g = (group or "").strip()
    return g[:GROUP_MAX_LEN] if g else None


def norm_retrieved_ids(ids: list | None) -> list[str] | None:
    """M1 召回集回传归一：str 化 + 上限截断；空列表 → None。纯函数可单测。"""
    out = [str(i) for i in (ids or [])][:RETRIEVED_IDS_CAP]
    return out or None


def ema_next(prev: float | None, target: float, alpha: float | None = None) -> float:
    """EMA 递推纯函数：w' = w + α(a − w)，prev=NULL 视为 0.0；6 位小数防浮点尾噪入库存漂移。"""
    alpha = config.OUTCOME_EMA_ALPHA if alpha is None else alpha
    base = 0.0 if prev is None else float(prev)
    return round(base + alpha * (target - base), 6)


def _latest_recall_query(conn, memory_id) -> str | None:
    """该条最近一次 recall_hit 事件携带的 query（corrected/useless 的 L3 回退信号源）。"""
    row = db.fetch_one(
        conn,
        "SELECT query FROM access_events WHERE memory_id=%s AND kind='recall_hit' "
        "AND query IS NOT NULL AND query <> '' ORDER BY ts DESC LIMIT 1",
        (memory_id,))
    return row["query"] if row else None


def apply_feedback(conn, memory_id, outcome: str, caller: str | None = None,
                   query: str | None = None, group: str | None = None,
                   retrieved_ids: list | None = None) -> dict | None:
    """写 outcome 信号 + EMA 演化 +（adopted 时）L2 事件。行锁串行化同条并发反馈，防 EMA 丢更新。

    铁律（#6 存量零变化）：只写三新列 + access_events/changelog 追加；绝不改 ttl_state/body/
    embedding，绝不触碰 updated_at（route_time 排序键与生命周期锚定的既有语义不借力打力）。
    group/retrieved_ids（P2 第一刀 E1/M1）：仅进 changelog detail jsonb，纯增量零新列；
    缺省 None=不落这两键，与旧行为逐字节一致。
    返回 None=id 不存在（API 层映射 404）；outcome 非法上抛 ValueError（API 层映射 400）。
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome 必须为 {OUTCOMES} 之一，收到 {outcome!r}")
    if not query or not query.strip():
        query = None
    g = norm_group(group)
    rids = norm_retrieved_ids(retrieved_ids)
    with conn.transaction():
        cur = db.fetch_one(
            conn, "SELECT bank, outcome AS prev_outcome, polarity AS prev_polarity "
                  "FROM memories WHERE id=%s FOR UPDATE", (memory_id,))
        if cur is None:
            return None
        prev_p = cur["prev_polarity"]
        new_p = ema_next(prev_p, POLARITY_TARGET[outcome])
        upd = db.fetch_one(
            conn,
            "UPDATE memories SET outcome=%s, polarity=%s, outcome_at=now() "
            "WHERE id=%s RETURNING outcome_at",
            (outcome, new_p, memory_id))
        l2_event = outcome == "adopted"
        if l2_event:
            # L2 护盾：与 /v1/memories/{id}/adopt 同构的事件+计数（同事务，账本与信号不分家）
            db.execute(conn, "INSERT INTO access_events(memory_id, kind, caller) VALUES (%s,'adopted',%s)",
                       (memory_id, (caller or "unknown")[:120]))
            db.execute(conn, "UPDATE memories SET adopt_count=adopt_count+1, last_accessed_at=now() "
                             "WHERE id=%s", (memory_id,))
        detail = {"outcome": outcome, "polarity_prev": prev_p, "polarity": new_p,
                  "alpha": config.OUTCOME_EMA_ALPHA, "caller": (caller or "")[:120]}
        if g:
            detail["group"] = g                      # E1 组粒度（聚合提交/查询键）
        if rids:
            detail["retrieved_ids"] = rids           # M1 当次召回集（消费定位）
        db.log_changelog(conn, "feedback", memory_id, detail)
        l3_query = l3_reason = None
        if outcome in _HARDQ_REASON:
            l3_query = query or _latest_recall_query(conn, memory_id)
            l3_reason = _HARDQ_REASON[outcome]
    return {"id": str(memory_id), "bank": cur["bank"], "outcome": outcome,
            "polarity": new_p, "polarity_prev": prev_p, "ema_alpha": config.OUTCOME_EMA_ALPHA,
            "outcome_at": upd["outcome_at"].isoformat() if upd and upd["outcome_at"] else None,
            "l2_adopt_event": l2_event, "group": g,
            "retrieved_ids_count": len(rids) if rids else 0,
            "l3_signal": {"query": l3_query, "reason": l3_reason} if l3_reason and l3_query else None}


def record_l3_signal(mem_id: str, query: str, caller: str | None, bank: str | None,
                     reason: str) -> dict | None:
    """L3 失败回流：corrected/useless → hard_queries 池（API 层后台任务调用，故障不反噬反馈主路径）。"""
    from . import hard_queries
    return hard_queries.record_hard_query(query, caller, None, bank, reason)


# —— E1 组粒度查询面（P2 第一刀）：changelog detail->>'group' 聚合/明细，纯读 ——

def group_rollup(conn, days: int = 7, limit: int = 50) -> list[dict]:
    """按 group 聚合反馈：次数/三值分布/去重条目数/首末时刻（供 GET /v1/feedback/groups）。"""
    d = max(1, min(int(days), 365))
    return db.fetch_all(
        conn,
        f"""SELECT detail->>'group' AS "group",
                   count(*) AS n,
                   count(*) FILTER (WHERE detail->>'outcome'='adopted')   AS adopted,
                   count(*) FILTER (WHERE detail->>'outcome'='corrected') AS corrected,
                   count(*) FILTER (WHERE detail->>'outcome'='useless')   AS useless,
                   count(DISTINCT memory_id) AS distinct_memories,
                   min(ts) AS first_ts, max(ts) AS last_ts
            FROM changelog
            WHERE op='feedback' AND detail->>'group' IS NOT NULL
              AND ts > now() - interval '{d} days'
            GROUP BY 1 ORDER BY max(ts) DESC LIMIT %s""",
        (max(1, min(int(limit), 500)),))


def group_detail(conn, group: str, limit: int = 100) -> list[dict]:
    """单组明细：该组每条反馈的 memory_id/outcome/retrieved_ids（M1 关联消费定位）。"""
    return db.fetch_all(
        conn,
        """SELECT seq, ts, memory_id,
                  detail->>'outcome' AS outcome,
                  detail->>'caller'  AS caller,
                  detail->'retrieved_ids' AS retrieved_ids
           FROM changelog
           WHERE op='feedback' AND detail->>'group' = %s
           ORDER BY ts DESC LIMIT %s""",
        (group, max(1, min(int(limit), 500))))
