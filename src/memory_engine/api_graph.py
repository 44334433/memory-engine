"""图谱可视化只读 API：GET /v1/graph（P0，2026-09-17）。

entities/edges 弱图 → sigma.js viewer 数据。只读，无任何写入路径。
边权重：P0 常数 1.0 占位 → 多跳批（2026-09-21 迁移 010）透出 edges.weight 真实乘子。
路由注册由 app.create_app 收口（本文件不改既有文件）。
"""
import logging
from typing import Optional

from fastapi import APIRouter, Request

from . import db

log = logging.getLogger("memory-engine.api")
router = APIRouter(prefix="/v1")

DEFAULT_LIMIT = 2000   # 防全量拉爆：无参默认只取最近 2000 条现行边
MAX_LIMIT = 10000      # 显式放大上限（调研：sigma 官方余量 10^4 节点/10^5 边）


def _mem_pred(alias: str, bank: Optional[str], domain: Optional[str]) -> tuple[str, list]:
    """induced-subgraph 谓词：记忆端点必须满足 bank/domain 过滤。返回 (SQL 片段, 位置参数)。"""
    if not bank and not domain:
        return "TRUE", []
    conds, params = [], []
    if bank:
        conds.append("m.bank=%s")
        params.append(bank)
    if domain:
        conds.append("m.domain=%s")
        params.append(domain)
    return (f"EXISTS (SELECT 1 FROM memories m WHERE m.id = e.{alias} "
            f"AND {' AND '.join(conds)})", params)


def _edges_where(bank: Optional[str], domain: Optional[str]) -> tuple[str, list]:
    """现行边 + induced 子图：mem→mem 两端都在过滤集内，mem→entity 只要求 src 端。
    返回 (WHERE 片段, 参数表)——参数顺序与占位符出现顺序严格一致。"""
    if not bank and not domain:
        return "e.invalid_at IS NULL", []
    src_sql, src_p = _mem_pred("src_mid", bank, domain)
    dst_sql, dst_p = _mem_pred("dst_mid", bank, domain)
    sql = (f"e.invalid_at IS NULL AND ("
           f"(e.entity_id IS NOT NULL AND {src_sql}) OR "
           f"(e.dst_mid IS NOT NULL AND {src_sql} AND {dst_sql}))")
    return sql, [*src_p, *src_p, *dst_p]


def _fetch_edges(conn, bank: Optional[str], domain: Optional[str], limit: int) -> list[dict]:
    where, params = _edges_where(bank, domain)
    return db.fetch_all(
        conn,
        f"SELECT e.id, e.src_mid, e.dst_mid, e.entity_id, e.etype, e.weight FROM edges e "
        f"WHERE {where} ORDER BY e.valid_at DESC, e.id LIMIT %s",
        (*params, limit))


def _count_edges(conn, bank: Optional[str], domain: Optional[str]) -> int:
    where, params = _edges_where(bank, domain)
    row = db.fetch_one(conn, f"SELECT count(*) c FROM edges e WHERE {where}", tuple(params))
    return row["c"] if row else 0


def _fetch_mem_nodes(conn, ids: list[str]) -> list[dict]:
    if not ids:
        return []
    return db.fetch_all(
        conn, "SELECT id, title, bank, domain FROM memories WHERE id = ANY(%s::uuid[])",
        (ids,))


def _fetch_ent_nodes(conn, ids: list[str]) -> list[dict]:
    if not ids:
        return []
    return db.fetch_all(conn, "SELECT id, name FROM entities WHERE id = ANY(%s::uuid[])", (ids,))


@router.get("/graph")
def get_graph(request: Request, bank: Optional[str] = None,
              domain: Optional[str] = None, limit: int = DEFAULT_LIMIT):
    """弱图只读快照：{nodes, edges, counts}。节点只含被返回边触及的端点（无孤立点噪声）。"""
    limit = max(1, min(int(limit), MAX_LIMIT))
    eng = request.app.state.engine
    with eng.db.connection() as conn:
        edge_rows = _fetch_edges(conn, bank, domain, limit)
        total_edges = _count_edges(conn, bank, domain)
        mem_ids, ent_ids = set(), set()
        for e in edge_rows:
            mem_ids.add(str(e["src_mid"]))
            if e["dst_mid"] is not None:
                mem_ids.add(str(e["dst_mid"]))
            else:
                ent_ids.add(str(e["entity_id"]))
        mem_rows = _fetch_mem_nodes(conn, list(mem_ids))
        ent_rows = _fetch_ent_nodes(conn, list(ent_ids))

    mem_by_id = {str(r["id"]): r for r in mem_rows}
    ent_by_id = {str(r["id"]): r for r in ent_rows}
    nodes = []
    for mid, r in mem_by_id.items():
        title = (r.get("title") or "").strip()
        nodes.append({"id": mid, "label": title or mid[:8], "type": "memory",
                      "bank": r.get("bank"), "domain": r.get("domain")})
    for eid, r in ent_by_id.items():
        nodes.append({"id": eid, "label": r.get("name") or eid[:8], "type": "entity",
                      "bank": None, "domain": None})   # 实体全局共享，不隶属单 bank

    edges = [{"source": str(e["src_mid"]),
              "target": str(e["dst_mid"] if e["dst_mid"] is not None else e["entity_id"]),
              "relation": e["etype"], "weight": float(e["weight"]) if e.get("weight") is not None else 1.0}
             for e in edge_rows]

    return {"nodes": nodes, "edges": edges,
            "counts": {"nodes": len(nodes), "edges": len(edges),
                       "total_edges": total_edges, "truncated": total_edges > len(edges)}}
