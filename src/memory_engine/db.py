"""PG 存储访问层：轻量连接池 + SQL（psycopg3，autocommit + 显式事务）。"""
import json
import logging
import queue
import threading
import time
from contextlib import contextmanager
from typing import Sequence

import psycopg

from . import config
from .util import uuid7

log = logging.getLogger("memory-engine.db")


class PgPool:
    def __init__(self, dsn: str, minsize: int = 2, maxsize: int = 8):
        self.dsn = dsn
        self.maxsize = maxsize
        self._free: queue.LifoQueue = queue.LifoQueue()
        self._created = 0
        self._lock = threading.Lock()
        self._closed = False
        for _ in range(minsize):
            self._free.put(self._new_conn())

    def _new_conn(self) -> psycopg.Connection:
        conn = psycopg.connect(self.dsn, autocommit=True, connect_timeout=3)
        with self._lock:
            self._created += 1
        return conn

    @contextmanager
    def connection(self, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        conn = None
        while conn is None:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                conn = self._free.get(timeout=remaining)
            except queue.Empty:
                with self._lock:
                    if self._created < self.maxsize:
                        conn = self._new_conn()
            if conn is not None and (conn.closed or conn.broken):
                with self._lock:
                    self._created -= 1
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
            if conn is None and time.monotonic() >= deadline:
                # 阶段2 修复：池耗尽/连接死绝时快速失败，禁止无限自旋拖死整个 daemon
                raise TimeoutError(f"pg pool exhausted (max={self.maxsize}, created={self._created})")
        try:
            yield conn
        except Exception as e:
            # 阶段2 修复：业务异常不销毁连接（回滚后还池）；仅真坏连接才关闭。
            # 原实现任何异常都 close → 触发并发重建 → libpq/SSL 死锁（strace 实证）。
            from psycopg import InterfaceError, OperationalError
            broken = isinstance(e, (OperationalError, InterfaceError))
            if broken:
                try:
                    conn.close()
                except Exception:
                    pass
                with self._lock:
                    self._created -= 1
            else:
                try:
                    conn.rollback()
                    self._free.put(conn)
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    with self._lock:
                        self._created -= 1
            raise
        else:
            self._free.put(conn)

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                conn = self._free.get_nowait()
                conn.close()
            except queue.Empty:
                break


def fetch_all(conn, sql: str, params: Sequence = ()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return []
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_one(conn, sql: str, params: Sequence = ()) -> dict | None:
    rows = fetch_all(conn, sql, params)
    return rows[0] if rows else None


def execute(conn, sql: str, params: Sequence = ()) -> None:
    with conn.cursor() as cur:
        cur.execute(sql, params)


# —————————————————— retain ——————————————————

def hash_dup_id(conn, bank: str, chash: str) -> str | None:
    row = fetch_one(
        conn,
        "SELECT id FROM memories WHERE bank=%s AND content_hash=%s AND ttl_state<>'retired' "
        "AND is_current LIMIT 1",   # P1 二批：仅现行版本参与判重（历史版本不锚定新写入）
        (bank, chash),
    )
    return row["id"] if row else None


def semantic_dup(conn, bank: str, qvec: str, days: int, sim: float) -> tuple[str | None, float]:
    """近 N 天同 bank 语义近似判重：返回 (id, cos)。"""
    row = fetch_one(
        conn,
        """SELECT id, 1 - (embedding <=> %s::vector) AS cos
            FROM memories
            WHERE bank=%s AND embedding IS NOT NULL AND ttl_state<>'retired' AND is_current
              AND created_at > now() - (%s || ' days')::interval
            ORDER BY embedding <=> %s::vector LIMIT 1""",
        (qvec, bank, str(days), qvec),
    )
    if row and row["cos"] is not None and float(row["cos"]) >= sim:
        return row["id"], float(row["cos"])
    return None, 0.0


RETAIN_SQL = """
INSERT INTO memories (
  id, bank, domain, trigger_term, title, body, body_ptr, tags, owner, visibility,
  source_type, source_ref, priority, ttl_state, ttl_expires_at, source_tier, contains_pii,
  original_date, staleness, embed_model, embed_dim, embed_ver, content_hash, embedding,
  valid_at, dedup_key, tenant_id, agent_id, memory_type, pinned)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,
        now() + (%s || ' days')::interval,%s,%s,
        %s,%s,%s,%s,1,%s,%s::vector,
        COALESCE(%s::timestamptz, now()),%s,%s,%s,COALESCE(%s::text, 'episodic'),
        COALESCE(%s::boolean, false))
RETURNING id, seq
"""
# 阶段2：user 来源入场态 candidate（候选期 CANDIDATE_DAYS 天→trial，lifecycle 线程机械流转）。
# P0 投毒闸批（2026-09-16）：外部来源（agent/web/cron）默认 trial 低信任入场
# （ttl_state/ttl_expires_days 由调用方按 poison_gate.entry_state 计算）；缺省 source_tier=agent。
# P1 判重 UNIQUE 兜底批（2026-09-16）：dedup_key 追加在参数列末位（index 23），
# 并发竞态下 DB 层 UNIQUE(bank, dedup_key) 兜底，冲突时应用层返回既有条目；存量回填见 scripts/migrations/。
# P1 第二批（2026-09-16）：valid_at（事件时间，None→now()=created_at 同瞬）/tenant_id/agent_id 追加末位
# （index 24-26；参数追加保持 13-23 位不动，P0 params 断言不回退）。
# W2（2026-09-18）：memory_type 追加末位（index 27；同上惯例，既有位序全部不动）。
# None→COALESCE 兜底 'episodic'（显式绑 NULL 会撞 NOT NULL，PG 不回退 DEFAULT）。
# W1（2026-09-18）：pinned 追加末位（index 28；同上惯例）。None→false（retain 不直接钉住，
# 钉住走 PATCH 显式通道；supersede 谱系继承经 fields 显式传值）。


def insert_memory(conn, **f) -> dict:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                RETAIN_SQL,
                (
                    f["id"], f["bank"], f["domain"], f["trigger_term"], f["title"], f["body"],
                    f["body_ptr"], json.dumps(f["tags"]), f["owner"], f["visibility"],
                    f["source_type"], f["source_ref"], f["priority"], f["ttl_state"],
                    f["ttl_expires_days"], f["source_tier"], f["contains_pii"],
                    f["original_date"], f["staleness"], f["embed_model"], f["embed_dim"],
                    f["content_hash"], f["embedding"],
                    f.get("valid_at"),    # P1 二批：事件时间（None=DEFAULT now()，与 created_at 同瞬）
                    f.get("dedup_key"),   # P1：判重 UNIQUE 兜底键（None=NULL，不参与部分唯一索引）
                    f.get("tenant_id"),   # P1 二批：多宿主留位（None=单宿主不分区）
                    f.get("agent_id"),
                    f.get("memory_type"),  # W2：None→DEFAULT 'episodic'（PG 层兜底）
                    f.get("pinned"),       # W1：None→false（supersede 谱系继承显式传值）
                ),
            )
            row = dict(zip(["id", "seq"], cur.fetchone()))
        execute(
            conn,
            "INSERT INTO changelog(op, memory_id, detail) VALUES ('retain', %s, %s::jsonb)",
            (f["id"], json.dumps({"bank": f["bank"], "title": f["title"], "hash": f["content_hash"],
                                  "source_tier": f["source_tier"], "ttl_state": f["ttl_state"]})),
        )
    return row


def log_changelog(conn, op: str, memory_id, detail: dict) -> None:
    execute(conn, "INSERT INTO changelog(op, memory_id, detail) VALUES (%s, %s, %s::jsonb)",
            (op, memory_id, json.dumps(detail, ensure_ascii=False)))


def max_seq(conn) -> int:
    row = fetch_one(conn, "SELECT COALESCE(max(seq),0) AS s FROM memories")
    return int(row["s"])


# —————————————————— recall ——————————————————

def route_vector(conn, bank, vis_sql, vis_params, qvec: str, limit: int, extra_sql: str, extra_params: tuple):
    sql = f"""SELECT id, 1 - (embedding <=> %s::vector) AS cos
              FROM memories
              WHERE embedding IS NOT NULL
                AND ({'bank=%s' if bank else 'TRUE'})
                AND ({vis_sql}) {extra_sql}
              ORDER BY embedding <=> %s::vector LIMIT %s"""
    params = (qvec, *((bank,) if bank else ()), *vis_params, *extra_params, qvec, limit)
    return fetch_all(conn, sql, params)


def route_fts(conn, bank, vis_sql, vis_params, query: str, limit: int, extra_sql: str, extra_params: tuple):
    sql = f"""SELECT id, pgroonga_score(memories) AS score
              FROM memories
              WHERE search_text &@~ %s
                AND ({'bank=%s' if bank else 'TRUE'})
                AND ({vis_sql}) {extra_sql}
              ORDER BY score DESC LIMIT %s"""
    params = (query, *((bank,) if bank else ()), *vis_params, *extra_params, limit)
    return fetch_all(conn, sql, params)


def route_time(conn, bank, vis_sql, vis_params, limit: int, extra_sql: str, extra_params: tuple):
    sql = f"""SELECT id
              FROM memories
              WHERE ttl_state IN ('trial','active','decaying')
                AND ({'bank=%s' if bank else 'TRUE'})
                AND ({vis_sql}) {extra_sql}
              ORDER BY priority DESC, updated_at DESC LIMIT %s"""
    params = (*((bank,) if bank else ()), *vis_params, *extra_params, limit)
    return fetch_all(conn, sql, params)


HYDRATE_COLS = ("id, seq, bank, domain, trigger_term, title, body, body_ptr, tags, owner, "
                "visibility, source_type, source_ref, priority, ttl_state, staleness, "
                "verify_status, created_at, updated_at, original_date, access_count, adopt_count, "
                "source_tier, contains_pii, memory_type, "   # W2：memory_type 进召回结果
                "outcome, polarity")                          # 自进化#1：outcome 反馈信号进 score_parts


def hydrate(conn, ids: list) -> dict:
    if not ids:
        return {}
    rows = fetch_all(conn, f"SELECT {HYDRATE_COLS} FROM memories WHERE id = ANY(%s)", (list(ids),))
    return {r["id"]: r for r in rows}


def record_access(conn, events: list[tuple]) -> None:
    with conn.transaction():
        for mid, kind, caller, query in events:
            execute(conn, "INSERT INTO access_events(memory_id, kind, caller, query) VALUES (%s,%s,%s,%s)",
                    (mid, kind, caller, query))


# —————————————————— 双时序 supersede（P1 第二批 2026-09-16） ——————————————————
# 矛盾更新原语：旧条 invalid_at = 新条 valid_at（时间截断，同一时刻）+ 新条 INSERT 重存。
# 铁律：绝不 DELETE / 绝不改写旧行内容——旧行唯一写动作 = invalid_at 置位（且仅当仍为 NULL）。

def supersede_memory(conn, old_id, fields: dict) -> dict | None:
    """双时序矛盾更新：失效旧条 + 插入新版本，同事务、同一 valid_at（时间截断）。

    fields = insert_memory 的完整字段 + 可选 valid_at（None→同事务 now()，与旧行 invalid_at 同值，
    PG now() 为事务起始时间，两侧自动相等）。旧行已失效（invalid_at 非空）→ 幂等 no-op 返回 None
    （不产生新版本，防重复 supersede 复写）。返回 {"old_id","new_id","valid_at"}。
    """
    valid_at = fields.get("valid_at")
    with conn.transaction():
        # 1) 时间截断：旧条 invalid_at = valid_at（仅当仍现行；已失效→整体 no-op 回滚）
        if valid_at is None:
            done = fetch_one(conn, "UPDATE memories SET invalid_at = now() "
                                   "WHERE id=%s AND invalid_at IS NULL RETURNING id", (old_id,))
        else:
            done = fetch_one(conn, "UPDATE memories SET invalid_at = %s::timestamptz "
                                   "WHERE id=%s AND invalid_at IS NULL RETURNING id", (valid_at, old_id))
        if not done:
            return None
        # 2) 新条 INSERT 重存（valid_at 与上面截断时刻同值；uq_mem_dedup 已收紧为仅现行版本判重）
        row = insert_memory(conn, **fields)
        # 3) 谱系指针（迁移 008/图谱深度批）：旧条指向新条，取代链可 WITH RECURSIVE 直走
        execute(conn, "UPDATE memories SET superseded_by=%s WHERE id=%s", (row["id"], old_id))
        log_changelog(conn, "supersede", old_id,
                      {"new_id": str(row["id"]), "valid_at": str(valid_at) if valid_at else "now()"})
    return {"old_id": str(old_id), "new_id": str(row["id"]),
            "valid_at": str(valid_at) if valid_at else "now()"}


# —————————————————— 知识网络：entities / edges（P1 第二批） ——————————————————
# G15（S1 级盲审硬约束）：矛盾检测 observe-only 起步——contradicts 边只记录进 edges 表，
# 绝不触发 memories.invalid_at 置位、绝不 DELETE memories；
# 升 enforce 的前置条件 = 金标边集 precision>=0.7 且 30 天抽检通过——该条件未达成前，
# 任何代码路径不得由 contradicts 边改写 memories（写死于本注释 + config 注释 + 迁移 002 文件头）。

ENTITY_TYPES = ("person", "org", "project", "concept", "tool", "place", "event", "other")
EDGE_TYPES = ("related", "causal", "parent_child", "contradicts")


def upsert_entity(conn, name: str, etype: str) -> str:
    """按 (etype, lower(name)) 幂等取/建实体，返回 entity id。"""
    if etype not in ENTITY_TYPES:
        etype = "other"
    name = (name or "").strip()
    if not name:
        raise ValueError("entity name 必填")
    row = fetch_one(conn, "SELECT id FROM entities WHERE etype=%s AND lower(name)=lower(%s)",
                    (etype, name))
    if row:
        return str(row["id"])
    execute(conn, "INSERT INTO entities(id, name, etype) VALUES (%s,%s,%s) "
                  "ON CONFLICT (etype, lower(name)) DO NOTHING", (str(uuid7()), name, etype))
    row = fetch_one(conn, "SELECT id FROM entities WHERE etype=%s AND lower(name)=lower(%s)",
                    (etype, name))
    return str(row["id"])


def insert_edge(conn, src_mid, dst_mid=None, entity_id=None, etype="related",
                valid_at=None, source="manual", weight=1.0) -> str | None:
    """插入现行边（三元组唯一索引去重，重复→ON CONFLICT 返回 None）。

    observe-only 铁律（G15）：本函数只写 edges 表，绝不触碰 memories——调用方（LLM 抽取/
    弱图脚本）对 contradicts 边同样只记录；invalid_at 恒为 NULL（边级双时序由 P2 矛盾自动失效接管）。
    weight（多跳批 2026-09-21，迁移 010）：持久边权乘子，缺省 1.0=零行为；消歧/共现度等
    离线通道可落权重于此，召回路遍历按乘积传播（见 graph_expand）。
    """
    if etype not in EDGE_TYPES:
        raise ValueError(f"etype 必须为 {EDGE_TYPES}，收到 {etype!r}")
    if weight is None or float(weight) <= 0:
        raise ValueError(f"weight 必须 > 0，收到 {weight!r}")
    row = fetch_one(
        conn,
        """INSERT INTO edges(id, src_mid, dst_mid, entity_id, etype, valid_at, source, weight)
           VALUES (%s,%s,%s,%s,%s,COALESCE(%s::timestamptz, now()),%s,%s)
           ON CONFLICT DO NOTHING RETURNING id""",
        (str(uuid7()), src_mid, dst_mid, entity_id, etype, valid_at, source, float(weight)),
    )
    return str(row["id"]) if row else None


# —————————————————— 第四路图召回：逐跳 BFS（多跳批 2026-09-21 重构） ——————————————————
# 原实现为单条递归 CTE（P1 二批）：有 UNION 元组去重但无路径防环、无每跳候选上限、
# 边时序只认 invalid_at IS NULL（不吃 as_of）、无衰减。本批升级为 Python 侧逐跳 BFS：
#   ① visited 集合去重（真防环：节点在最小跳定格，杜绝路径爆炸与环路复访）；
#   ② 每跳按边权乘积降序截 top-K（GRAPH_HOP_TOPK），frontier 有界，深度上限 GRAPH_HOPS_MAX；
#   ③ 边/节点双时序：缺省现行边（invalid_at IS NULL，走部分索引零回退）；as_of 给定=
#     「当时的图」（valid_at<=t<invalid_at 窗，节点可见性同窗，走迁移 010 非部分索引）；
#   ④ 途经实体（entity 桥分支）输出 via，供召回侧同名多 etype 消歧归因；
#   ⑤ 持久边权 e.weight 乘积传播（默认 1.0=与旧语义逐字节兼容）。
# 兼容：返回行仍含 id/hop；新增 via/w 键（recall 侧全部 .get() 容错，monkeypatch 旧形状不破）。

def _edge_time_sql(alias: str, as_of) -> tuple[str, list]:
    """边时序谓词：缺省现行（partial index 友好）；as_of=事件时间窗（Graphiti 同构）。"""
    if as_of is None:
        return f"{alias}.invalid_at IS NULL", []
    return (f"{alias}.valid_at <= %s::timestamptz "
            f"AND ({alias}.invalid_at IS NULL OR {alias}.invalid_at > %s::timestamptz)"), [as_of, as_of]


# 单步扩展：frontier 每节点三分支（mem↔mem 双向 + mem↔实体↔mem 桥），各支独立 LIMIT
# 控扇出（防 hub 爆量）；visited 排除=防环+去重（含种子，种子自身不再拉回，与旧语义一致）。
_GRAPH_STEP_SQL = """
SELECT x.nid, x.via, x.w
FROM unnest(%s::uuid[]) AS f(node)
CROSS JOIN LATERAL (
    (SELECT e.dst_mid AS nid, NULL::uuid AS via, COALESCE(e.weight, 1.0) AS w
       FROM edges e
      WHERE e.src_mid = f.node AND e.dst_mid IS NOT NULL AND e.src_mid IS DISTINCT FROM e.dst_mid
        AND ({ef1}) ORDER BY e.id LIMIT %s)
  UNION ALL
    (SELECT e.src_mid, NULL::uuid, COALESCE(e.weight, 1.0)
       FROM edges e
      WHERE e.dst_mid = f.node AND ({ef2}) ORDER BY e.id LIMIT %s)
  UNION ALL
    (SELECT e2.src_mid, e1.entity_id, COALESCE(e1.weight, 1.0) * COALESCE(e2.weight, 1.0)
       FROM edges e1
       JOIN edges e2 ON e2.entity_id = e1.entity_id AND e2.invalid_ok_placeholder
      WHERE e1.src_mid = f.node AND e1.entity_id IS NOT NULL AND ({ef3}) ORDER BY e1.id LIMIT %s)
) x
WHERE x.nid IS NOT NULL AND NOT (x.nid = ANY(%s::uuid[]))
"""


def graph_expand(conn, seeds: list, hops: int, vis_sql: str, vis_params: Sequence,
                 limit: int, as_of=None, per_hop_k: int | None = None) -> list[dict]:
    """逐跳 BFS 图邻拉：返回 [{id, hop, via, w}]（hop 升序、同跳边权乘积降序稳定排序）。

    hops=0/空种子 → []（图路整体关闭）。seeds 恒视为 visited（不再拉回）。
    as_of（datetime|None）：边按事件时间窗过滤；节点输出过滤同步——缺省仅现行+非
    retired/archived 记忆，as_of 口径按时间窗+排除 retired。
    """
    import uuid as _uuid
    seed_ids = [s if isinstance(s, _uuid.UUID) else _uuid.UUID(str(s)) for s in seeds]
    hops = int(hops)
    if not seed_ids or hops <= 0 or limit <= 0:
        return []
    per_hop_k = int(per_hop_k or config.GRAPH_HOP_TOPK)
    visited: set = set(seed_ids)
    frontier = seed_ids
    found: list[dict] = []
    ef1, p1 = _edge_time_sql("e", as_of)
    ef2, p2 = _edge_time_sql("e", as_of)
    ef3a, p3a = _edge_time_sql("e1", as_of)
    ef3b, p3b = _edge_time_sql("e2", as_of)
    step_sql = (_GRAPH_STEP_SQL
                .replace("{ef1}", ef1).replace("{ef2}", ef2)
                .replace("{ef3}", ef3a).replace("e2.invalid_ok_placeholder", ef3b))
    fanout = max(per_hop_k, config.GRAPH_FANOUT_PER_DIR)
    for h in range(1, hops + 1):
        if not frontier or len(found) >= limit:
            break
        params = (frontier, *p1, fanout, *p2, fanout, *p3a, *p3b, fanout, sorted(visited))
        # psycopg uuid[]：list[UUID] 原生适配；sorted 稳定占位（uuid 可排序）
        rows = fetch_all(conn, step_sql, params)
        agg: dict = {}
        for r in rows:
            nid, via, w = r["nid"], r["via"], float(r["w"])
            cur = agg.get(nid)
            if cur is None:
                agg[nid] = [w, {via} if via is not None else set()]
            else:
                if w > cur[0]:
                    cur[0] = w
                if via is not None:
                    cur[1].add(via)
        # 每跳候选上限 top-K（边权乘积降序、同分按 id 稳定）+ 总上限截断
        ranked = sorted(agg.items(), key=lambda kv: (-kv[1][0], str(kv[0])))[:per_hop_k]
        next_frontier = []
        for nid, (w, vias) in ranked:
            if len(found) >= limit:
                break
            found.append({"id": nid, "hop": h, "via": sorted(str(v) for v in vias), "w": w})
            visited.add(nid)
            next_frontier.append(nid)
        frontier = next_frontier
    if not found:
        return []
    # 节点输出过滤（与旧语义一致：记忆节点现行+非 retired/archived；as_of=时间窗口径）
    ids = [row["id"] for row in found]
    if as_of is None:
        pred, pred_params = ("m.is_current AND m.ttl_state NOT IN ('archived','retired')", [])
    else:
        pred = ("COALESCE(m.valid_at, m.created_at) <= %s::timestamptz "
                "AND (m.invalid_at IS NULL OR m.invalid_at > %s::timestamptz) "
                "AND m.ttl_state <> 'retired'")
        pred_params = [as_of, as_of]
    keep = {r["id"] for r in fetch_all(
        conn, f"SELECT m.id FROM memories m WHERE m.id = ANY(%s::uuid[]) AND ({pred}) AND ({vis_sql})",
        (ids, *pred_params, *vis_params))}
    return [row for row in found if row["id"] in keep]


# 第四路图召回：从三路命中（seeds）出发 1-2 跳邻拉（P1 第二批 ⑤）。
# （2026-09-21 多跳批：原单条递归 CTE GRAPH_WALK_SQL 已升级为上方逐跳 BFS graph_expand，
#   旧 SQL 连同其种子自排除/现行边语义一并由新实现覆盖——此处不留死码。）
