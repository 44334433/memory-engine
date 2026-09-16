"""三路召回 + RRF 融合 + score 分解（蓝图 §4）。
路A pgvector KNN（w=1.0） / 路B PGroonga BM25（w=0.8） / 路C 时序-重要性（w=0.4）
final = rrf × pri(0.9+0.05·priority) × life(ttl/verify) × stale(fresh/aging/stale 拍板降权)
"""
import logging
import time

from . import config, db
from .db import PgPool
from .embedder import Embedder
from .util import vec_to_pg

log = logging.getLogger("memory-engine.recall")

ROUTE_WEIGHTS = (("vector", config.W_VEC), ("fts", config.W_FTS), ("time", config.W_TIME))


def _vis_sql(caller: str | None) -> tuple[str, list]:
    """可见性预过滤：main 全见；其余=自有+public（蓝图 §4 第0步）。"""
    if not caller or caller == "main":
        return "TRUE", []
    return "(owner = %s OR visibility = 'public')", [caller]


def _filters_sql(filters: dict | None) -> tuple[str, list]:
    sql, params = "", []
    f = filters or {}
    if f.get("domain"):
        sql += " AND domain = %s"
        params.append(f["domain"])
    if f.get("tags"):
        sql += " AND tags ?| %s::text[]"
        params.append([str(t) for t in f["tags"]])
    if f.get("staleness"):
        vals = f["staleness"] if isinstance(f["staleness"], (list, tuple)) else [f["staleness"]]
        sql += " AND staleness = ANY(%s::text[])"
        params.append([str(v) for v in vals])
    dr = f.get("date_range") or {}
    if dr.get("from"):
        sql += " AND created_at >= %s"
        params.append(dr["from"])
    if dr.get("to"):
        sql += " AND created_at <= %s"
        params.append(dr["to"])
    if f.get("include_archived"):
        sql += " AND ttl_state <> 'retired'"       # archived 可回捞；retired（用户删除）永不召回
    else:
        sql += " AND ttl_state NOT IN ('archived','retired')"
    return sql, params


def _life_factor(ttl_state: str, verify_status: str, include_archived: bool) -> float:
    base = config.LIFE_WEIGHTS.get(ttl_state, 0.85)
    if ttl_state == "archived" and include_archived:
        base = config.LIFE_ARCHIVED_VISIBLE        # 归档显式可见=0.5
    return base * config.VERIFY_FACTOR.get(verify_status, 1.0)


def recall(pool: PgPool, embedder: Embedder, query: str, bank: str | None, caller: str | None,
           top_k: int, filters: dict | None) -> dict:
    t0 = time.perf_counter()
    qvec_pg = vec_to_pg(embedder.embed_queries([query])[0])
    vis_sql, vis_params = _vis_sql(caller)
    extra_sql, extra_params = _filters_sql(filters)
    include_archived = bool((filters or {}).get("include_archived"))
    degraded = False

    with pool.connection() as conn:
        try:
            rows_a = db.route_vector(conn, bank, vis_sql, vis_params, qvec_pg,
                                     config.TOP_VEC, extra_sql, extra_params)
        except Exception as e:  # 单路故障不拖垮整体（降级语义，蓝图 §11）
            log.warning("route A(vector) failed: %s", e)
            rows_a, degraded = [], True
        try:
            rows_b = db.route_fts(conn, bank, vis_sql, vis_params, query,
                                  config.TOP_FTS, extra_sql, extra_params)
        except Exception as e:
            log.warning("route B(fts) failed: %s", e)
            rows_b, degraded = [], True
        rows_c = db.route_time(conn, bank, vis_sql, vis_params,
                               config.TOP_TIME, extra_sql, extra_params)

    fused: dict = {}
    for name, weight, rows in (("vector", config.W_VEC, rows_a),
                               ("fts", config.W_FTS, rows_b),
                               ("time", config.W_TIME, rows_c)):
        for rank, row in enumerate(rows, start=1):
            entry = fused.setdefault(row["id"], {"rrf": 0.0, "routes": {}})
            entry["rrf"] += weight / (config.RRF_K + rank)
            entry["routes"][name] = rank

    if not fused:
        return {"results": [], "took_ms": round((time.perf_counter() - t0) * 1000, 1),
                "degraded": degraded, "routes": {"vector": len(rows_a), "fts": len(rows_b), "time": len(rows_c)}}

    with pool.connection() as conn:
        meta = db.hydrate(conn, list(fused.keys()))

    scored = []
    for mid, entry in fused.items():
        m = meta.get(mid)
        if m is None:
            continue
        pri = 0.9 + 0.05 * m["priority"]
        life = _life_factor(m["ttl_state"], m["verify_status"], include_archived)
        stale = config.STALE_WEIGHTS.get(m["staleness"], 1.0)
        final = entry["rrf"] * pri * life * stale
        scored.append({
            "id": str(mid), "score": round(final, 6),
            "score_parts": {
                "rrf": round(entry["rrf"], 6), "pri": round(pri, 4),
                "life": round(life, 4), "stale": stale,
                "routes": entry["routes"],
            },
            "title": m["title"], "body": m["body"], "body_ptr": m["body_ptr"],
            "bank": m["bank"], "domain": m["domain"], "tags": m["tags"],
            "owner": m["owner"], "visibility": m["visibility"],
            "ttl_state": m["ttl_state"], "staleness": m["staleness"],
            "priority": m["priority"], "verify_status": m["verify_status"],
            "trigger_term": m["trigger_term"], "source_ref": m["source_ref"],
            "created_at": m["created_at"].isoformat() if m["created_at"] else None,
            "updated_at": m["updated_at"].isoformat() if m["updated_at"] else None,
        })
    scored.sort(key=lambda d: d["score"], reverse=True)
    return {
        "results": scored[:top_k],
        "took_ms": round((time.perf_counter() - t0) * 1000, 1),
        "degraded": degraded,
        "routes": {"vector": len(rows_a), "fts": len(rows_b), "time": len(rows_c)},
    }
