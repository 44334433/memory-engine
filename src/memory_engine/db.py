"""PG 存储访问层：轻量连接池 + SQL（psycopg3，autocommit + 显式事务）。"""
import json
import logging
import queue
import threading
import time
from contextlib import contextmanager
from typing import Sequence

import psycopg

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
        "SELECT id FROM memories WHERE bank=%s AND content_hash=%s AND ttl_state<>'retired' LIMIT 1",
        (bank, chash),
    )
    return row["id"] if row else None


def semantic_dup(conn, bank: str, qvec: str, days: int, sim: float) -> tuple[str | None, float]:
    """近 N 天同 bank 语义近似判重：返回 (id, cos)。"""
    row = fetch_one(
        conn,
        f"""SELECT id, 1 - (embedding <=> %s::vector) AS cos
            FROM memories
            WHERE bank=%s AND embedding IS NOT NULL AND ttl_state<>'retired'
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
  original_date, staleness, embed_model, embed_dim, embed_ver, content_hash, embedding, dedup_key)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,
        now() + (%s || ' days')::interval,%s,%s,
        %s,%s,%s,%s,1,%s,%s::vector,%s)
RETURNING id, seq
"""
# 阶段2：user 来源入场态 candidate（候选期 CANDIDATE_DAYS 天→trial，lifecycle 线程机械流转）。
# P0 投毒闸批（2026-09-16）：外部来源（agent/web/cron）默认 trial 低信任入场
# （ttl_state/ttl_expires_days 由调用方按 poison_gate.entry_state 计算）；缺省 source_tier=agent。
# P1 判重 UNIQUE 兜底批（2026-09-16）：dedup_key 追加在参数列末位（index 23），
# 并发竞态下 DB 层 UNIQUE(bank, dedup_key) 兜底，冲突时应用层返回既有条目；存量回填见 scripts/migrations/。


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
                    f.get("dedup_key"),   # P1：判重 UNIQUE 兜底键（None=NULL，不参与部分唯一索引）
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
                "source_tier, contains_pii")


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
