"""图谱深度批（2026-09-19，backlog supersede-chain-ashof ①②）：取代链多跳 + 浅跳邻居遍历。

外部批评「图谱深度不足」的直接回应。as-of 快照查询已由同日主会话落地（001fba5），本模块补两项：

① GET /v1/memories/{id}/chain?max_hops=5 —— 取代链全谱系回放：沿 memories.superseded_by
   （迁移 008 一等列，changelog 回填）WITH RECURSIVE 双向行走（回溯到链头+前推到链尾），
   返回链上全部版本 + 各版本时间窗（valid_at/invalid_at）。seed 可以是链上任意节点。
② GET /v1/graph/neighbors?id=&hops=2 —— 实体的浅跳邻域（记忆↔记忆双向 + 记忆↔实体），
   PG 递归 CTE，不引图引擎；支持 as_of 快照口径（边按事件时间窗过滤）与 etype 收窄。

纪律：本文件纯新增，不改 recall.py（W3 并行车）与任何既有端点；防环=visited 数组 + hop 上限
双保险（max_hops 防环是铁律 #3 的硬约束，即使数据被手改出环也有限步终止）；链断点（旧版本被
purge，FK SET NULL）以 truncated 旗标显式承认，不做假缝合。
"""
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from . import db
from .api_core import _parse_dt

log = logging.getLogger("memory-engine.api")
router = APIRouter(prefix="/v1")

CHAIN_MAX_HOPS = 20          # 取代链显式放大上限（防参数打爆）
CHAIN_DEFAULT_HOPS = 5       # 任务规格缺省
NB_MAX_HOPS = 4              # 邻居遍历为「浅跳」定位：>4 视为误用
NB_DEFAULT_HOPS = 2
NB_DEFAULT_LIMIT = 200       # 防 hub 节点 2 跳爆量（39k 库平均度~3，2 跳千级封顶）
NB_MAX_LIMIT = 2000
BODY_EXCERPT = 500           # 链上版本正文节选长度（全链多版本，防响应爆体）


def _jsonable(v):
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    return v


# —————————————————————— ① 取代链多跳 ——————————————————————
# 前进（新→更旧的反向？不：superseded_by 指向新版，fwd=向更新版本）与回溯（找更早版本）
# 各一条递归 CTE；seen 数组防环，min(depth) 聚合天然收敛（手改数据出现入度>1 也不炸不重）。

_CHAIN_FWD_SQL = """
WITH RECURSIVE fwd(node, depth, seen) AS (
    SELECT m.id, 0, ARRAY[m.id] FROM memories m WHERE m.id = %s
  UNION
    SELECT m.superseded_by, f.depth + 1, f.seen || m.superseded_by
    FROM fwd f JOIN memories m ON m.id = f.node
    WHERE f.depth < %s AND m.superseded_by IS NOT NULL
      AND NOT (m.superseded_by = ANY(f.seen))
)
SELECT node, min(depth) AS depth FROM fwd GROUP BY node
"""

_CHAIN_BWD_SQL = """
WITH RECURSIVE bwd(node, depth, seen) AS (
    SELECT m.id, 0, ARRAY[m.id] FROM memories m WHERE m.id = %s
  UNION
    SELECT m.id, b.depth + 1, b.seen || m.id
    FROM bwd b JOIN memories m ON m.superseded_by = b.node
    WHERE b.depth < %s AND NOT (m.id = ANY(b.seen))
)
SELECT node, min(depth) AS depth FROM bwd GROUP BY node
"""


def walk_chain(conn, seed: uuid.UUID, max_hops: int) -> dict:
    """双向走取代链，返回 {versions(有序 head→tail), head, tail, 防环/截断旗标}。纯查询，零写入。"""
    fwd = {str(r["node"]): int(r["depth"]) for r in
           db.fetch_all(conn, _CHAIN_FWD_SQL, (seed, max_hops))}
    bwd = {str(r["node"]): int(r["depth"]) for r in
           db.fetch_all(conn, _CHAIN_BWD_SQL, (seed, max_hops))}
    cycle = truncated_fwd = truncated_bwd = False

    # 链尾判定：fwd 最深处节点的 superseded_by —— NULL=真尾；非空且已访问=环；非空未访问=被 max_hops 截断
    tail_node = max(fwd, key=lambda n: fwd[n]) if fwd else str(seed)
    tail_next = db.fetch_one(conn, "SELECT superseded_by FROM memories WHERE id=%s", (tail_node,))
    if tail_next and tail_next["superseded_by"] is not None:
        nxt = str(tail_next["superseded_by"])
        if nxt in fwd or nxt in bwd:
            cycle = True
        else:
            truncated_fwd = True
    # 链头判定：bwd 最深处节点还有没有前驱指向它
    head_node = max(bwd, key=lambda n: bwd[n]) if bwd else str(seed)
    preds = db.fetch_all(conn, "SELECT id FROM memories WHERE superseded_by=%s", (head_node,))
    for p in preds:
        if str(p["id"]) in bwd or str(p["id"]) in fwd:
            cycle = True
        else:
            truncated_bwd = True

    # 组装：bwd 深度降序（链头→seed 前缀）+ fwd 深度升序（seed 后继）；环场景 bwd/fwd 集合重叠，
    # 按访问序去重——诚实返回「已访问段+cycle_detected」，不静默吞。
    ordered: list[str] = []
    for nid in sorted(bwd, key=lambda n: (-bwd[n], n)):
        ordered.append(nid)
    for nid in sorted(fwd, key=lambda n: (fwd[n], n)):
        if nid not in ordered:
            ordered.append(nid)

    rows = db.fetch_all(
        conn,
        "SELECT id, seq, bank, domain, title, body, valid_at, invalid_at, is_current, "
        "ttl_state, superseded_by FROM memories WHERE id = ANY(%s::uuid[]) ORDER BY valid_at, id",
        ([uuid.UUID(n) for n in ordered],))
    by_id = {str(r["id"]): r for r in rows}
    versions = []
    for pos, nid in enumerate(ordered):
        r = by_id.get(nid)
        if not r:
            continue
        body = r["body"] or ""
        versions.append({
            "position": pos,
            "hop_from_seed": fwd.get(nid, -bwd.get(nid, 0)),
            "id": nid, "seq": r["seq"], "bank": r["bank"], "domain": r["domain"],
            "title": r["title"],
            "body": body[:BODY_EXCERPT],
            "body_truncated": len(body) > BODY_EXCERPT,
            "valid_at": _jsonable(r["valid_at"]), "invalid_at": _jsonable(r["invalid_at"]),
            "is_current": r["is_current"], "ttl_state": r["ttl_state"],
            "superseded_by": _jsonable(r["superseded_by"]),
        })
    return {"versions": versions,
            "head": versions[0]["id"] if versions else None,
            "tail": versions[-1]["id"] if versions else None,
            "cycle_detected": cycle,
            "truncated_forward": truncated_fwd, "truncated_back": truncated_bwd}


@router.get("/memories/{mid}/chain")
def get_memory_chain(mid: uuid.UUID, request: Request, max_hops: int = CHAIN_DEFAULT_HOPS):
    """取代链回放：返回链上全部版本+时间窗。seed 可为链上任意节点；404=节点不存在。

    max_hops 双向各限步（缺省 5，钳到 1..20）；防环=递归内 visited 数组+步数上限双保险，
    cycle_detected/truncated_* 显式暴露异常拓扑，不静默截断。"""
    max_hops = max(1, min(int(max_hops), CHAIN_MAX_HOPS))
    eng = request.app.state.engine
    with eng.db.connection() as conn:
        if not db.fetch_one(conn, "SELECT id FROM memories WHERE id=%s", (mid,)):
            raise HTTPException(404, f"memory {mid} 不存在")
        res = walk_chain(conn, mid, max_hops)
    res.update({"seed": str(mid), "max_hops": max_hops, "length": len(res["versions"])})
    return res


# —————————————————————— ② 浅跳邻居遍历（2 跳邻域） ——————————————————————
# 无向语义：mem↔mem 双向、mem↔entity（entity 边 src→entity，反向经 entity 拉回 src）。
# 边过滤：缺省=现行边；as_of 给定=「该时刻生效」的事件时间窗（Graphiti 同构，与 001fba5 对齐）。
# 节点输出过滤与 graph_expand 召回路一致：缺省仅现行+非 retired/archived 记忆节点（途经不算），
# as_of 口径则按时间窗判定；实体节点无时序列恒可见。环防护=seen 数组（每条邻边只回访一次新节点）。

_NB_WALK_SQL = """
WITH RECURSIVE nb(node, hop, parent, edge_id, seen) AS (
    SELECT %s::uuid, 0, NULL::uuid, NULL::uuid, ARRAY[%s::uuid]
  UNION
    SELECT x.nid, nb.hop + 1, nb.node, x.eid, nb.seen || x.nid
    FROM nb
    JOIN LATERAL (
        SELECT COALESCE(e.dst_mid, e.entity_id) AS nid, e.id AS eid
          FROM edges e WHERE e.src_mid = nb.node AND ({ef})
        UNION ALL
        SELECT e.src_mid, e.id
          FROM edges e WHERE e.dst_mid = nb.node AND ({ef})
        UNION ALL
        SELECT e.src_mid, e.id
          FROM edges e WHERE e.entity_id = nb.node AND ({ef})
    ) x ON x.nid IS NOT NULL AND NOT (x.nid = ANY(nb.seen))
    WHERE nb.hop < %s
)
SELECT node, min(hop) AS hop, (array_agg(parent ORDER BY hop))[1] AS parent,
       (array_agg(edge_id ORDER BY hop))[1] AS edge_id
FROM nb WHERE hop > 0 GROUP BY node
ORDER BY hop, node LIMIT %s
"""


def _edge_filter(as_of, etype: Optional[str]) -> tuple[str, list]:
    """返回 (SQL 片段, 位置参数)——单分支用，三分支各带一份。"""
    conds, params = [], []
    if as_of is not None:
        conds.append("e.valid_at <= %s::timestamptz "
                     "AND (e.invalid_at IS NULL OR e.invalid_at > %s::timestamptz)")
        params.extend([as_of, as_of])
    else:
        conds.append("e.invalid_at IS NULL")
    if etype:
        conds.append("e.etype = %s")
        params.append(etype)
    return " AND ".join(conds), params


def neighbors(conn, seed: uuid.UUID, seed_type: str, hops: int, as_of=None,
              etype: Optional[str] = None, limit: int = NB_DEFAULT_LIMIT) -> dict:
    """浅跳邻域：BFS 到 hops 层，返回 {nodes, edges, counts}。纯查询。"""
    ef, efp = _edge_filter(as_of, etype)
    sql = _NB_WALK_SQL.replace("{ef}", ef)
    params: list = [seed, seed, *efp, *efp, *efp, hops, limit]
    rows = db.fetch_all(conn, sql, tuple(params))

    node_ids = {str(seed)} | {str(r["node"]) for r in rows}
    # 端点分类：memories/entities 双查（uuid 不跨表重用；一次 ANY 批量，代价 O(deg)）
    uuids = [uuid.UUID(n) for n in node_ids]
    mem_by_id = {str(r["id"]): r for r in db.fetch_all(
        conn, "SELECT id, title, bank, domain, is_current, ttl_state, valid_at, invalid_at "
              "FROM memories WHERE id = ANY(%s::uuid[])", (uuids,))}
    ent_by_id = {str(r["id"]): r for r in db.fetch_all(
        conn, "SELECT id, name, etype FROM entities WHERE id = ANY(%s::uuid[])", (uuids,))}

    def _visible(nid: str) -> bool:
        m = mem_by_id.get(nid)
        if m is None:
            return nid in ent_by_id                      # 实体无时序列
        if as_of is not None:
            va = m["valid_at"]
            if va is not None and va > as_of:
                return False
            iv = m["invalid_at"]
            if iv is not None and iv <= as_of:
                return False
            return True
        return bool(m["is_current"]) and m["ttl_state"] not in ("archived", "retired")

    nodes = []
    vis_ids = {str(seed)}
    hop_of = {str(r["node"]): int(r["hop"]) for r in rows}
    for r in rows:
        nid = str(r["node"])
        if not _visible(nid):
            continue
        vis_ids.add(nid)
        m, en = mem_by_id.get(nid), ent_by_id.get(nid)
        nodes.append({"id": nid, "hop": hop_of[nid], "type": "memory" if m else "entity",
                      "label": ((m.get("title") or nid[:8]) if m else (en.get("name") or nid[:8])),
                      "bank": m.get("bank") if m else None,
                      "domain": m.get("domain") if m else None})
    # 树边：parent→node（两端均可见才保留）
    tree = [(str(r["parent"]), str(r["node"]), _jsonable(r["edge_id"])) for r in rows]
    tree = [(p, n, e) for (p, n, e) in tree if n in vis_ids and p in vis_ids]
    edge_ids = [uuid.UUID(e) for (_, _, e) in tree if e]
    rel = {str(x["id"]): x["etype"] for x in db.fetch_all(
        conn, "SELECT id, etype FROM edges WHERE id = ANY(%s::uuid[])",
        (edge_ids,))} if edge_ids else {}
    edges_out = [{"source": p, "target": n, "relation": rel.get(e), "hop": hop_of[n]}
                 for (p, n, e) in tree]

    seed_row = (mem_by_id.get(str(seed)) or {}) if seed_type == "memory" else (ent_by_id.get(str(seed)) or {})
    return {
        "seed": {"id": str(seed), "type": seed_type,
                 "label": (seed_row.get("title") or seed_row.get("name") or str(seed)[:8]),
                 "visible": _visible(str(seed))},
        "hops": hops, "as_of": _jsonable(as_of), "etype_filter": etype,
        "nodes": nodes, "edges": edges_out,
        "counts": {"nodes": len(nodes), "edges": len(edges_out),
                   "visited": len(rows), "truncated": len(rows) >= limit},
    }


@router.get("/graph/neighbors")
def get_graph_neighbors(request: Request, id: uuid.UUID,
                        hops: int = NB_DEFAULT_HOPS, as_of: Optional[str] = None,
                        etype: Optional[str] = None, limit: int = NB_DEFAULT_LIMIT):
    """浅跳邻居遍历（缺省 2 跳）：{seed, nodes[{id,type,label,hop}], edges[{source,target,relation,hop}]}。

    hops 钳到 1..4；limit 钳到 1..2000（邻接爆炸护栏）；as_of=ISO8601 快照口径（边+节点按事件时间窗）；
    etype ∈ {related, causal, parent_child, contradicts} 收窄。404=seed 既非记忆也非实体。"""
    hops = max(1, min(int(hops), NB_MAX_HOPS))
    limit = max(1, min(int(limit), NB_MAX_LIMIT))
    if etype is not None and etype not in db.EDGE_TYPES:
        raise HTTPException(422, f"etype 必须为 {db.EDGE_TYPES}")
    as_of_dt = _parse_dt(as_of)
    eng = request.app.state.engine
    with eng.db.connection() as conn:
        row = db.fetch_one(conn, "SELECT id FROM memories WHERE id=%s", (id,))
        seed_type = "memory" if row else None
        if seed_type is None:
            row = db.fetch_one(conn, "SELECT id FROM entities WHERE id=%s", (id,))
            seed_type = "entity" if row else None
        if seed_type is None:
            raise HTTPException(404, f"节点 {id} 不存在（memory/entity 双查皆空）")
        return neighbors(conn, id, seed_type, hops, as_of_dt, etype, limit)
