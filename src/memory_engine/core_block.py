"""W1 核心记忆块（core block，2026-09-18 拍板顺序 W2 之后）——入选与渲染纯逻辑。

与 freshness digest 的机制辨析（两者都是宿主侧注入，但正交，勿混）：
- freshness digest：真源**变更**的游标增量摘要快照（事件级、自上次消费以来的净变更、投递语义
  推进游标），回答「有什么新变化」；
- core block：高价值记忆的**条目级原文**常驻（无游标、幂等只读、每次全量重算），
  回答「无论检索与否都必须让模型看见什么」。本模块输出原文而非摘要——条目级
  原文是「不经检索直入上下文」承诺的最低保真形态，摘要化会引入二次失真通道。

入选双轨（阈值论证见 config.py W1 块注释）：
- pinned 轨：人工显式标记（PATCH /v1/memories/{id} {"pinned":true}），无条件优先；
- auto 轨：memory_type ∈ CORE_AUTO_TYPES（默认 semantic/procedural）且
  polarity ≥ CORE_MIN_POLARITY 且 adopt_count ≥ CORE_MIN_ADOPT；轨内按启发式加权分
  （polarity 0.5 / adopt 0.3 / access 0.2）降序。
两轨共同底线：is_current（历史版本禁注入）、ttl_state ∉ {retired, archived}（退役/归档
=已死状态不常驻；decaying/trial/candidate 保留——polarity/adopt 门槛本身就是质量闸，
且 archived 在召回侧权重 0.0，语义对齐）。

自激防护（设计守卫，写死）：core-block 读取**绝不**写 access_events / access_count——
若注入即计数，则 注入→access 升→auto 分升→更易注入 形成正反馈回路，
polarity/adopt 的宿主反馈信号（POST /v1/feedback）将被引擎侧自循环稀释。

可见性：复用 recall._vis_sql（caller=main 全见；其他 caller 仅 owner 本人或 public），
与检索路同一闸，防 core block 成为旁路泄漏通道。
"""
import logging
import math

from . import config

log = logging.getLogger("memory-engine.core_block")

HEADER = "【核心记忆 core block·常驻区，不经检索】"
ENTRY_MIN_TAIL = 24   # 剩余预算放不下 头部+24 字正文 时整条弃入（防碎片条目；碎片比缺失更伤）

# 候选取数（每轨独立 SQL，各带 LIMIT 防互相饿死；单表谓词过滤，零嵌入计算）
PINNED_SQL = ("SELECT id, seq, bank, title, body, memory_type, priority, polarity, "
              "adopt_count, access_count, TRUE AS pinned "
              "FROM memories "
              "WHERE pinned AND is_current AND ttl_state NOT IN ('retired','archived') "
              "AND {vis} ORDER BY seq DESC LIMIT %s")
AUTO_SQL = ("SELECT id, seq, bank, title, body, memory_type, priority, polarity, "
            "adopt_count, access_count, FALSE AS pinned "
            "FROM memories "
            "WHERE is_current AND ttl_state NOT IN ('retired','archived') "
            "AND memory_type = ANY(%s) AND adopt_count >= %s AND polarity >= %s "
            "AND {vis} "
            "ORDER BY polarity DESC NULLS LAST, adopt_count DESC, seq DESC LIMIT %s")


def auto_score(row: dict) -> float:
    """auto 轨启发式分 ∈[0,1]。polarity 在合格带 [CORE_MIN_POLARITY,1] 内线性拉伸
    （带内极差只 0.5 时保留分辨率；低于门槛=0，供 pinned 轨/polarity=NULL 兜底）。"""
    p = row.get("polarity")
    lo = config.CORE_MIN_POLARITY
    pn = 0.0 if p is None else max(0.0, min(1.0, (float(p) - lo) / max(1e-9, 1.0 - lo)))
    adopt = row.get("adopt_count") or 0
    access = row.get("access_count") or 0
    a = min(max(adopt, 0) / config.CORE_SCORE_CAP_ADOPT, 1.0)
    x = min(math.log1p(max(access, 0)) / math.log1p(config.CORE_SCORE_ACCESS_CAP), 1.0)
    return (config.CORE_W_POLARITY * pn + config.CORE_W_ADOPT * a
            + config.CORE_W_ACCESS * x)


def rank(rows: list[dict]) -> list[dict]:
    """pinned 轨整体优先（轨内 priority 降、seq 降=同优先级新者先）；auto 轨分数降序
    （同分 seq 降）。稳定确定性排序，测试可复现。"""
    pinned = [r for r in rows if r.get("pinned")]
    auto = [r for r in rows if not r.get("pinned")]
    pinned.sort(key=lambda r: (-int(r.get("priority") or 3), -int(r.get("seq") or 0)))
    auto.sort(key=lambda r: (-auto_score(r), -int(r.get("seq") or 0)))
    return pinned + auto


def render_block(rows: list[dict], budget_chars: int) -> dict:
    """按预算贪心渲染（高分先入→超预算自然从尾部=低分处截断）。

    返回 {text, ids, used_chars, dropped_count}；不变量：len(text) ≤ budget_chars
    （极端小预算下连表头都裁进预算，budget<1 返回空块）。空 rows → 空块（空库合法态）。
    """
    ordered = rank(rows)
    segs: list[str] = []
    ids: list[str] = []
    used = 0   # 含分隔 \n 的实际占用
    for r in ordered:
        tag = "pinned" if r.get("pinned") else "auto"
        mt = r.get("memory_type") or "?"
        meta = f"- ({r['bank']}/{mt}·{tag}) {r.get('title') or ''}"
        sep = 1 if segs else 0
        room = budget_chars - used - sep - len(meta) - 2   # ": " 连接符
        if not segs:
            room -= len(HEADER) + 1                        # 首条预留表头
        if room < ENTRY_MIN_TAIL:
            break                                          # 本条及之后全部弃入（序已降）
        body = (r.get("body") or "").strip()
        if len(body) > room:
            body = body[:room - 1] + "…"                   # 条目级原文超预算→截尾标记
        segs.append(f"{meta}: {body}")
        ids.append(str(r["id"]))
        used += sep + len(meta) + 2 + len(body)
    if not segs:
        text = ""
    else:
        text = HEADER + "\n" + "\n".join(segs)
    if len(text) > budget_chars:      # 防御性硬截（贪心已保证不超，双保险）
        text = text[:budget_chars]
    return {"text": text, "ids": ids, "used_chars": len(text),
            "dropped_count": len(ordered) - len(ids)}


def build_block(db_mod, pool, vis_sql: str, vis_params: tuple,
                budget_chars: int) -> dict:
    """双轨候选取数 + 去重 + 渲染（供 API 层调用；db_mod/pool 注入便于无库单测）。"""
    lim = max(1, config.CORE_BLOCK_MAX_ITEMS)
    with pool.connection() as conn:
        pinned_rows = db_mod.fetch_all(
            conn, PINNED_SQL.format(vis=vis_sql), (*vis_params, lim))
        auto_rows = db_mod.fetch_all(
            conn, AUTO_SQL.format(vis=vis_sql),
            (list(config.CORE_AUTO_TYPES), config.CORE_MIN_ADOPT,
             config.CORE_MIN_POLARITY, *vis_params, lim))
    seen: set[str] = set()
    rows = []
    for r in pinned_rows + auto_rows:   # pinned 同时满足 auto 条件会双轨重复——保 pinned 行（轨标正确）
        k = str(r["id"])
        if k not in seen:
            seen.add(k)
            rows.append(r)
    out = render_block(rows, budget_chars)
    out["pinned_candidates"] = len(pinned_rows)
    out["auto_candidates"] = len(auto_rows)
    return out
