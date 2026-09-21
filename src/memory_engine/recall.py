"""三路召回 + RRF 融合 + score 分解（蓝图 §4）。
路A pgvector KNN（w=1.0） / 路B PGroonga BM25（w=0.8） / 路C 时序-重要性（w=0.4）
final = rrf × pri(0.9+0.05·priority) × life(ttl/verify) × stale(fresh/aging/stale)
        × tier(source_tier 降权 P1：web=0.85/cron=0.9 可配)
P1 降级语义（2026-09-16 拍板）：嵌入路失败→降级纯 FTS+时序路（200+degraded+failed_routes）；
503 只留给全路失败（P0「禁吞禁静默」不变，显式降级取代单路失败即 503）。
多跳图谱批（2026-09-21）：图路=逐跳 BFS（graph_hops 参数可配，缺省 config.GRAPH_HOPS=2，
上限 3）；graph 分量 = RRF × 边权乘积 × 衰减^跳-1 × 消歧乘子；as_of 快照口径贯穿边遍历。
"""
import json
import logging
import time
import uuid
from datetime import datetime

from . import config, db
from .db import PgPool
from .embedder import EmbeddingProvider
from .reranker import Qwen3Reranker          # W3：仅类型注解；关=config.RERANK_ENABLED=false→eng.reranker=None，重排段整体跳过
from .util import vec_to_pg

log = logging.getLogger("memory-engine.recall")

ROUTE_WEIGHTS = (("vector", config.W_VEC), ("fts", config.W_FTS), ("time", config.W_TIME))

# —— P0 错误语义批（2026-09-16）：失败显式化，禁吞禁静默 ——


class RecallRouteError(RuntimeError):
    """全路失败（P1 演进：503 只留给全路失败）。

    P0 拍板：路失败必须显式上抛→API 层 503+degraded+retryable；原蓝图 §11
    「单路静默降级为 200 空结果」语义即 P0 修复的错误语义，废弃。
    P1 拍板（2026-09-16）：部分路失败不再 503——改为 200+degraded+failed_routes
    显式降级（宁降级不 503，禁吞不变）；仅当全部尝试路皆失败时上抛本异常→503。
    """

    def __init__(self, failed_routes: dict[str, str]):
        self.failed_routes = failed_routes
        super().__init__(f"recall routes failed: {sorted(failed_routes)}")


_STALENESS_BUCKETS = ("fresh", "aging", "stale")


def validate_filters(filters: dict | None) -> None:
    """recall 参数校验：坏参 ValueError(带原因)，API 层映射 400（P0 前：坏 date_range 裸 500）。"""
    f = filters or {}
    if not isinstance(f, dict):
        raise ValueError(f"filters 必须为对象，收到 {type(f).__name__}")
    dr = f.get("date_range")
    if dr is not None:
        if not isinstance(dr, dict):
            raise ValueError(f"filters.date_range 必须为对象（含 from/to），收到 {type(dr).__name__}")
        for k in ("from", "to"):
            v = dr.get(k)
            if v is None:
                continue
            if not isinstance(v, str):
                raise ValueError(f"filters.date_range.{k} 必须为 ISO8601 字符串，收到 {v!r}")
            try:
                datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError(
                    f"filters.date_range.{k} 非法时间格式: {v!r}（需 ISO8601，如 2026-09-01）") from None
    st = f.get("staleness")
    if st is not None:
        vals = list(st) if isinstance(st, (list, tuple)) else [st]
        bad = [v for v in vals if v not in _STALENESS_BUCKETS]
        if bad:
            raise ValueError(f"filters.staleness 仅允许 {_STALENESS_BUCKETS}，收到非法值 {bad}")
    mt = f.get("memory_type")   # W2 分层：记忆类型过滤（str 或 list，非法值 400 带原因）
    if mt is not None:
        vals = list(mt) if isinstance(mt, (list, tuple)) else [mt]
        bad = [v for v in vals if v not in config.MEMORY_TYPES]
        if bad:
            raise ValueError(f"filters.memory_type 仅允许 {config.MEMORY_TYPES}，收到非法值 {bad}")
    ao = f.get("as_of")   # as-of 快照查询（Graphiti 同构语义）：查该时刻有效的版本，非法值 400 带原因
    if ao is not None:
        if not isinstance(ao, str):
            raise ValueError(f"filters.as_of 必须为 ISO8601 字符串，收到 {ao!r}")
        try:
            datetime.fromisoformat(ao.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"filters.as_of 非法时间格式: {ao!r}（需 ISO8601，如 2026-09-01T00:00:00）") from None


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
    if f.get("tenant_id"):
        sql += " AND tenant_id = %s"      # P1 二批：多宿主过滤（不传=单宿主全量，NULL 行仅无过滤时可见）
        params.append(str(f["tenant_id"]))
    if f.get("agent_id"):
        sql += " AND agent_id = %s"
        params.append(str(f["agent_id"]))
    if f.get("staleness"):
        vals = f["staleness"] if isinstance(f["staleness"], (list, tuple)) else [f["staleness"]]
        sql += " AND staleness = ANY(%s::text[])"
        params.append([str(v) for v in vals])
    if f.get("memory_type"):    # W2 分层：三路由同一 extra_sql，天然全路生效
        vals = f["memory_type"] if isinstance(f["memory_type"], (list, tuple)) else [f["memory_type"]]
        sql += " AND memory_type = ANY(%s::text[])"
        params.append([str(v) for v in vals])
    if f.get("date_range"):
        dr = f["date_range"]
        if dr.get("from"):
            sql += " AND created_at >= %s"
            params.append(dr["from"])
        if dr.get("to"):
            sql += " AND created_at <= %s"
            params.append(dr["to"])
    ao = f.get("as_of")
    if ao:
        # as-of 快照：该时刻有效的版本（valid_at<=t<invalid_at 或其后从未失效）——
        # Graphiti「query what was true at any point in time」同构语义；NULL valid_at 视为 created_at 同瞬（DEFAULT now()）
        sql += " AND COALESCE(valid_at, created_at) <= %s AND (invalid_at IS NULL OR invalid_at > %s)"
        params += [ao, ao]
    else:
        sql += " AND is_current"              # P1 二批：双时序——已失效历史版本不召回（缺省行为零变化）
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


def _graph_seeds(*route_rows: list) -> list:
    """图路种子 = 三路命中并集（每路取前 GRAPH_SEEDS_PER_ROUTE 条，总量截 GRAPH_SEEDS_MAX），保序去重。

    非 UUID id（合成数据/异常调用方）直接跳过——生产路由行的 id 恒为 PG uuid 列，
    此分支仅为兼容假 conn 测试与防御性容错，不产生降级信号。
    """
    per, cap = config.GRAPH_SEEDS_PER_ROUTE, config.GRAPH_SEEDS_MAX
    out: list = []
    seen: set = set()
    for rows in route_rows:
        for row in rows[:per]:
            mid = row["id"]
            try:
                uuid.UUID(str(mid))
            except (ValueError, AttributeError, TypeError):
                log.debug("graph seed 非 UUID，跳过: %r", mid)
                continue
            if mid not in seen:
                seen.add(mid)
                out.append(mid)
                if len(out) >= cap:
                    return out
    return out


def _cos(a: list[float], b: list[float]) -> float:
    """余弦相似度（纯 stdlib；1024 维 ~0.1ms，#23 零成本优化：单次遍历算 dot/norm）。"""
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na ** 0.5 * nb ** 0.5)


def _entity_neighborhood_cos(conn, qvec: list[float], entity_ids: list[str],
                             as_of_dt: datetime | None) -> dict[str, float | None]:
    """实体邻域文档嵌入均值 vs query 嵌入的 cos（件2 消歧信号源）。

    邻域=现行挂在该实体上的记忆嵌入（≤GRAPH_DISAMBIG_NEIGH_MAX 样本）；无可用嵌入→None
    （歧义不可判=不罚，宁保守不误伤）。as_of 口径同图遍历：边/节点按事件时间窗。
    """
    out: dict[str, float | None] = {}
    for eid in entity_ids:
        if as_of_dt is None:
            sql = ("SELECT m.embedding::text AS emb FROM edges e JOIN memories m ON m.id = e.src_mid "
                   "WHERE e.entity_id = %s AND m.embedding IS NOT NULL AND m.is_current "
                   "AND m.ttl_state NOT IN ('archived','retired') AND e.invalid_at IS NULL "
                   "LIMIT %s")
            params = (eid, config.GRAPH_DISAMBIG_NEIGH_MAX)
        else:
            sql = ("SELECT m.embedding::text AS emb FROM edges e JOIN memories m ON m.id = e.src_mid "
                   "WHERE e.entity_id = %s AND m.embedding IS NOT NULL "
                   "AND COALESCE(m.valid_at, m.created_at) <= %s::timestamptz "
                   "AND (m.invalid_at IS NULL OR m.invalid_at > %s::timestamptz) "
                   "AND m.ttl_state <> 'retired' AND e.invalid_at IS NULL LIMIT %s")
            params = (eid, as_of_dt, as_of_dt, config.GRAPH_DISAMBIG_NEIGH_MAX)
        try:
            docs = [json.loads(r["emb"]) for r in db.fetch_all(conn, sql, params) if r["emb"]]
        except (json.JSONDecodeError, TypeError):
            docs = []
        if not docs:
            out[eid] = None
            continue
        dim = len(docs[0])
        mean = [sum(d[i] for d in docs) / len(docs) for i in range(dim)]
        out[eid] = _cos(qvec, mean)
    return out


def _graph_disambiguate(conn, qvec: list[float] | None, rows_g: list[dict],
                        as_of_dt: datetime | None) -> tuple[dict, int]:
    """同名多 etype 实体消歧（件2）：query 嵌入 vs 实体邻域文档嵌入均值选边。

    返回 ({neighbor_id: factor}, collision_count)。策略拍板：entities 唯一约束不动、
    实体名不改——结果只落召回侧边权重乘子（败方 GRAPH_DISAMBIG_PENALTY）；
    cos 差 ≤ GRAPH_DISAMBIG_MARGIN 视为「真歧义」不罚（防假精度）。
    消歧失败=中性放行（图路本体不受累），显式 log，不静默。
    """
    if not config.GRAPH_DISAMBIG or not qvec or not rows_g:
        return {}, 0
    try:
        vias = {str(v) for r in rows_g for v in (r.get("via") or [])}
        if len(vias) < 2:
            return {}, 0
        ents = db.fetch_all(
            conn, "SELECT id, lower(name) AS ln FROM entities WHERE id = ANY(%s::uuid[])",
            ([uuid.UUID(v) for v in vias],))
        by_name: dict = {}
        for e in ents:
            by_name.setdefault(e["ln"], []).append(str(e["id"]))
        collide = {ln: ids for ln, ids in by_name.items() if len(ids) > 1}
        if not collide:
            return {}, 0
        cosmap = _entity_neighborhood_cos(conn, qvec, [i for ids in collide.values() for i in ids],
                                          as_of_dt)
        penalized: set = set()
        for ids in collide.values():
            scored: list = []
            for i in ids:
                c = cosmap.get(i)
                if c is not None:
                    scored.append((c, i))
            scored.sort(reverse=True)
            if len(scored) < 2:
                continue
            best = scored[0][0]
            penalized.update(i for c, i in scored[1:] if best - c > config.GRAPH_DISAMBIG_MARGIN)
        factors: dict = {}
        for r in rows_g:
            rv = [str(v) for v in (r.get("via") or [])]
            if rv and all(v in penalized for v in rv):
                factors[r["id"]] = config.GRAPH_DISAMBIG_PENALTY
        return factors, len(collide)
    except Exception as e:  # noqa: BLE001 —— 消歧子路失败=中性，显式日志（图主路已在手）
        log.warning("graph disambiguation failed (neutral): %s", e)
        return {}, 0


def _tenant_forced_filters(filters: dict | None) -> dict | None:
    """多租户预备层（2026-09-19 拍板变更：触发条件提前，用户显式拍板；DB 侧配套迁移 008_rls.sql）。

    MULTI_TENANT=0（缺省）：原对象直接返回——零改动零拷贝，存量行为逐字节不变。
    MULTI_TENANT=1：强制注入 filters.tenant_id=config.TENANT_ID（调用方显式传值优先）；
      注意 NULL tenant 行过滤后不可见（既有 P1 二批语义「NULL 行仅无过滤时可见」），
      启用前必须按 README「多租户三步」②回填存量 tenant_id，否则全库召回清空。
    """
    if not config.MULTI_TENANT:
        return filters
    f = dict(filters or {})
    if not f.get("tenant_id"):
        f["tenant_id"] = config.TENANT_ID
    return f


def recall(pool: PgPool, embedder: EmbeddingProvider, query: str, bank: str | None, caller: str | None,
           top_k: int, filters: dict | None, reranker: "Qwen3Reranker | None" = None,
           graph_hops: int | None = None) -> dict:
    filters = _tenant_forced_filters(filters)   # 多租户强制过滤（关=原对象直通，零行为）
    t0 = time.perf_counter()
    # 件1 参数：graph_hops=None→config 缺省；0=图路整体关闭；钳到 [0, GRAPH_HOPS_MAX]（API 层已校验，
    # 此处防御性兜底=非 HTTP 调用方同语义）。件3：filters.as_of 贯穿图遍历（「当时的图」）。
    hops = config.GRAPH_HOPS if graph_hops is None else int(graph_hops)
    hops = max(0, min(hops, config.GRAPH_HOPS_MAX))
    as_of_raw = (filters or {}).get("as_of")
    as_of_dt = datetime.fromisoformat(as_of_raw.replace("Z", "+00:00")) if as_of_raw else None
    # —— P1 降级批：嵌入路失败→登记后跳过矢量路，降级纯 FTS+时序路（宁降级不 503/不炸调用）——
    failed_routes: dict[str, str] = {}
    qvec_pg: str | None = None
    try:
        qvec_pg = vec_to_pg(embedder.embed_queries([query])[0])
    except Exception as e:  # noqa: BLE001 —— 嵌入故障显式登记（failed_routes.vector），禁静默
        log.warning("embed failed → fts-only degrade: %s", e)
        failed_routes["vector"] = f"embed: {str(e)[:300]}"
    vis_sql, vis_params = _vis_sql(caller)
    extra_sql, extra_params = _filters_sql(filters)
    include_archived = bool((filters or {}).get("include_archived"))
    # W2 终检闸：graph 邻拉路不携带 filters（既有语义，P1 二批），memory_type 过滤在 hydrate
    # 后的评分层统一把关——任何路由漏过滤都不会把非目标类型泄进结果集。
    mt_filter = (filters or {}).get("memory_type")
    mt_allowed = (set(mt_filter) if isinstance(mt_filter, (list, tuple))
                  else {mt_filter} if mt_filter else None)
    attempted = ("vector", "fts", "time") if qvec_pg is not None else ("fts", "time")
    rows_a: list = []
    rows_b: list = []
    rows_c: list = []

    with pool.connection() as conn:
        if qvec_pg is not None:
            try:
                rows_a = db.route_vector(conn, bank, vis_sql, vis_params, qvec_pg,
                                         config.TOP_VEC, extra_sql, extra_params)
            except Exception as e:  # P0/P1：记录后不立即上抛，统一走「全路失败才 503」判定
                log.warning("route A(vector) failed: %s", e)
                failed_routes["vector"] = str(e)[:300]
        try:
            rows_b = db.route_fts(conn, bank, vis_sql, vis_params, query,
                                  config.TOP_FTS, extra_sql, extra_params)
        except Exception as e:
            log.warning("route B(fts) failed: %s", e)
            failed_routes["fts"] = str(e)[:300]
        try:
            rows_c = db.route_time(conn, bank, vis_sql, vis_params,
                                   config.TOP_TIME, extra_sql, extra_params)
        except Exception as e:
            log.warning("route C(time) failed: %s", e)
            failed_routes["time"] = str(e)[:300]
        # —— P1 第二批：第四路图召回（从三路命中出发 1-2 跳邻拉；observe 期 W_GRAPH=0.5 低调可配）——
        # 图路是增强不是主路：失败只登记 failed_routes.graph（降级显式），不参与「全路失败 503」判定。
        rows_g: list = []
        graph_attempted = False
        graph_meta: dict = {"hops": hops}
        dis_factors: dict = {}
        if config.W_GRAPH > 0 and hops > 0:
            seeds = _graph_seeds(rows_a, rows_b, rows_c)
            graph_meta["seeds"] = len(seeds)
            if seeds:
                graph_attempted = True
                try:
                    rows_g = db.graph_expand(conn, seeds, hops, vis_sql, vis_params,
                                             config.GRAPH_MAX_NEIGHBORS, as_of=as_of_dt)
                    # 件2：同名多 etype 实体消歧（query 嵌入 vs 实体邻域文档嵌入均值选边，
                    # 结果只落边权重乘子；entities 唯一约束/实体名不动）
                    if rows_g and config.GRAPH_DISAMBIG and qvec_pg is not None:
                        dis_factors, n_coll = _graph_disambiguate(
                            conn, json.loads(qvec_pg), rows_g, as_of_dt)
                        graph_meta["disambig_collisions"] = n_coll
                        if dis_factors:
                            graph_meta["disambig_penalized"] = len(dis_factors)
                except Exception as e:  # noqa: BLE001 —— 图路失败显式登记（禁静默），不炸主召回
                    log.warning("route D(graph) failed: %s", e)
                    failed_routes["graph"] = str(e)[:300]
        graph_meta["neighbors"] = len(rows_g)
    if set(failed_routes) >= set(attempted):
        raise RecallRouteError(failed_routes)   # 全路失败→API 503+degraded+retryable（P1 唯一 503 入口）
    degraded = bool(failed_routes)              # 部分路失败=显式降级 200（degraded+failed_routes 透出）

    fused: dict = {}
    for name, weight, rows in (("vector", config.W_VEC, rows_a),
                               ("fts", config.W_FTS, rows_b),
                               ("time", config.W_TIME, rows_c)):
        for rank, row in enumerate(rows, start=1):
            entry = fused.setdefault(row["id"], {"rrf": 0.0, "routes": {}})
            entry["rrf"] += weight / (config.RRF_K + rank)
            entry["routes"][name] = rank
    # graph 分量（多跳批 2026-09-21）：rank 由 graph_expand 的 hop 升序+同跳边权降序给出；
    # 乘子链=衰减^跳-1（件1 ×0.5/跳）× 持久边权乘积（迁移 010）× 消歧因子（件2 败方罚）。
    # monkeypatch/旧形状行无 hop/via/w 键 → .get 缺省=与 P1 二批语义逐字节兼容。
    graph_rrf: dict = {}
    for rank, row in enumerate(rows_g, start=1):
        hop = int(row.get("hop") or 1)
        decay = config.GRAPH_HOP_DECAY ** max(0, hop - 1)
        edge_w = float(row.get("w") if row.get("w") is not None else 1.0)
        dis = float(dis_factors.get(row["id"], 1.0))
        g = config.W_GRAPH / (config.RRF_K + rank) * decay * edge_w * dis
        graph_rrf[row["id"]] = g
        entry = fused.setdefault(row["id"], {"rrf": 0.0, "routes": {}})
        entry["rrf"] += g
        entry["routes"]["graph"] = rank

    if not fused:
        routes = {"vector": len(rows_a), "fts": len(rows_b), "time": len(rows_c)}
        if graph_attempted:
            routes["graph"] = len(rows_g)
        out = {"results": [], "took_ms": round((time.perf_counter() - t0) * 1000, 1),
               "degraded": degraded, "failed_routes": failed_routes, "routes": routes}
        if graph_attempted or hops == 0:
            out["graph_meta"] = graph_meta
        return out

    with pool.connection() as conn:
        meta = db.hydrate(conn, list(fused.keys()))

    scored = []
    for mid, entry in fused.items():
        m = meta.get(mid)
        if m is None:
            continue
        if mt_allowed is not None and m.get("memory_type") not in mt_allowed:
            continue                          # W2 终检闸（图邻拉条目也过筛）
        pri = 0.9 + 0.05 * m["priority"]
        life = _life_factor(m["ttl_state"], m["verify_status"], include_archived)
        stale = config.STALE_WEIGHTS.get(m["staleness"], 1.0)
        tier_w = config.TIER_WEIGHTS.get(m["source_tier"], 1.0)   # P1：web/cron 置信度降权（可配）
        final = entry["rrf"] * pri * life * stale * tier_w
        scored.append({
            "id": str(mid), "score": round(final, 6),
            "score_parts": {
                "schema_version": 1,   # score_parts 结构版本：键增删改名时 +1，下游按此解析
                "rrf": round(entry["rrf"], 6), "pri": round(pri, 4),
                "life": round(life, 4), "stale": stale,
                "tier_weight": round(tier_w, 4),
                "graph": round(graph_rrf.get(mid, 0.0), 6),   # P1 二批：graph 分量透出（observe 期 W_GRAPH=0.5）
                "outcome": m.get("outcome"), "polarity": m.get("polarity"),   # 自进化#1：反馈信号透出（NULL=无信号；本批不参与评分，仅观测）
                "routes": entry["routes"],
            },
            "title": m["title"], "body": m["body"], "body_ptr": m["body_ptr"],
            "bank": m["bank"], "domain": m["domain"], "tags": m["tags"],
            "owner": m["owner"], "visibility": m["visibility"],
            "ttl_state": m["ttl_state"], "staleness": m["staleness"],
            "priority": m["priority"], "verify_status": m["verify_status"],
            "trigger_term": m["trigger_term"], "source_ref": m["source_ref"],
            "source_tier": m["source_tier"], "contains_pii": m["contains_pii"],
            "memory_type": m["memory_type"],   # W2 分层：类型随结果透出
            "created_at": m["created_at"].isoformat() if m["created_at"] else None,
            "updated_at": m["updated_at"].isoformat() if m["updated_at"] else None,
        })
    scored.sort(key=lambda d: (d["score"], d["updated_at"] or ""), reverse=True)
    # RRF 同分 tie-break：updated_at 新者优先（外部反馈#2，2026-09-19）
    # —— W3 可插拔重排（RRF 融合+因子评分之后、top_k 截断之前）——
    # 关（reranker=None）=本段整体条件跳过，零张量零分配，存量行为逐字节不变。
    # 开=只对 top RERANK_TOP_N(20) 候选精排（#23 延迟预算）；乘法融合 final'=final×(floor+(1−floor)·p)，
    #   不推翻 pri/life/stale/tier 拍板资产。重排是增强路非主路：失败→保留既有顺序+
    #   degraded+failed_routes.rerank 显式降级（与 graph 路同语义，永不参与 503 判定，禁静默吞错）。
    if reranker is not None:
        cand = scored[:config.RERANK_TOP_N]
        try:
            docs = [((d["title"] or "") + "\n" + (d["body"] or "")).strip() for d in cand]
            probs = reranker.score(query, docs)
            for d, p in zip(cand, probs):
                f = config.RERANK_FLOOR + (1.0 - config.RERANK_FLOOR) * p
                d["score"] = round(d["score"] * f, 6)
                d["score_parts"]["rerank"] = round(p, 6)   # P(yes) 透出（分数可解释契约）
            scored.sort(key=lambda d: (d["score"], d["updated_at"] or ""), reverse=True)   # 重排后同 tie-break
        except Exception as e:  # noqa: BLE001 —— 增强路失败显式登记降级，保留重排前顺序
            log.warning("rerank failed → keep pre-rerank order (degraded): %s", e)
            failed_routes["rerank"] = str(e)[:300]
            degraded = bool(failed_routes)
    routes = {"vector": len(rows_a), "fts": len(rows_b), "time": len(rows_c)}
    if graph_attempted:
        routes["graph"] = len(rows_g)
    out = {
        "results": scored[:top_k],
        "took_ms": round((time.perf_counter() - t0) * 1000, 1),
        "degraded": degraded,
        "failed_routes": failed_routes,
        "routes": routes,
    }
    if graph_attempted or hops == 0:
        out["graph_meta"] = graph_meta
    return out
