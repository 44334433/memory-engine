"""PG 存储访问层：轻量连接池 + SQL（psycopg3，autocommit + 显式事务）。"""
import json
import logging
import queue
import threading
import time
from contextlib import contextmanager
from typing import Sequence

import psycopg

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
        f"""SELECT id, 1 - (embedding <=> %s::vector) AS cos
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
  valid_at, dedup_key, tenant_id, agent_id, memory_type)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,
        now() + (%s || ' days')::interval,%s,%s,
        %s,%s,%s,%s,1,%s,%s::vector,
        COALESCE(%s::timestamptz, now()),%s,%s,%s,COALESCE(%s::text, 'episodic'))
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
                valid_at=None, source="manual") -> str | None:
    """插入现行边（三元组唯一索引去重，重复→ON CONFLICT 返回 None）。

    observe-only 铁律（G15）：本函数只写 edges 表，绝不触碰 memories——调用方（LLM 抽取/
    弱图脚本）对 contradicts 边同样只记录；invalid_at 恒为 NULL（边级双时序由 P2 矛盾自动失效接管）。
    """
    if etype not in EDGE_TYPES:
        raise ValueError(f"etype 必须为 {EDGE_TYPES}，收到 {etype!r}")
    row = fetch_one(
        conn,
        """INSERT INTO edges(id, src_mid, dst_mid, entity_id, etype, valid_at, source)
           VALUES (%s,%s,%s,%s,%s,COALESCE(%s::timestamptz, now()),%s)
           ON CONFLICT DO NOTHING RETURNING id""",
        (str(uuid7()), src_mid, dst_mid, entity_id, etype, valid_at, source),
    )
    return str(row["id"]) if row else None


# 第四路图召回：从三路命中（seeds）出发 1-2 跳邻拉（P1 第二批 ⑤）。
# 单递归项 + LATERAL 三分支（PG 只允许最后一个 UNION 分支递归，实测 PG18 验证）：
#   记忆↔记忆（双向）+ 记忆↔实体↔记忆（同实体拉回）；失效边/retired/archived/非现行版本一律不拉。
# 占位符全定位：params 顺序 = (*seeds, hops, *vis_params, *seeds, limit)（seeds 出现两次）。
GRAPH_WALK_SQL = """
WITH RECURSIVE walk(node, hop) AS (
    SELECT h.mid, 0 FROM unnest(%s::uuid[]) AS h(mid)
  UNION
    SELECT n.nid, w.hop + 1
    FROM walk w
    CROSS JOIN LATERAL (
        SELECT e.dst_mid AS nid FROM edges e
          WHERE e.src_mid = w.node AND e.dst_mid IS NOT NULL AND e.invalid_at IS NULL
            AND e.src_mid IS DISTINCT FROM e.dst_mid
      UNION
        SELECT e.src_mid FROM edges e
          WHERE e.dst_mid = w.node AND e.invalid_at IS NULL
      UNION
        SELECT e2.src_mid FROM edges e1
          JOIN edges e2 ON e2.entity_id = e1.entity_id AND e2.invalid_at IS NULL
          WHERE e1.src_mid = w.node AND e1.entity_id IS NOT NULL AND e1.invalid_at IS NULL
    ) n
    WHERE w.hop < %s AND n.nid IS NOT NULL AND n.nid <> w.node
)
SELECT w.node AS id, min(w.hop) AS hop
FROM walk w
JOIN memories m ON m.id = w.node
  AND m.is_current AND m.ttl_state NOT IN ('archived','retired')
  AND ({vis_sql})
WHERE w.hop > 0 AND NOT w.node = ANY(%s::uuid[])   -- 种子自身不再拉回（防环自增权）
GROUP BY w.node ORDER BY 2, 1
LIMIT %s
"""


def graph_expand(conn, seeds: list, hops: int, vis_sql: str, vis_params: Sequence,
                 limit: int) -> list[dict]:
    """图邻拉：返回 [{id, hop}]（hop 升序稳定排序，供 RRF rank）。种子/失效/retired 不返回。"""
    if not seeds:
        return []
    import uuid as _uuid
    ids = [s if isinstance(s, _uuid.UUID) else _uuid.UUID(str(s)) for s in seeds]  # uuid[] 原生绑定
    sql = GRAPH_WALK_SQL.format(vis_sql=vis_sql)
    params = (ids, int(hops), *vis_params, ids, int(limit))
    return fetch_all(conn, sql, params)

