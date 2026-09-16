"""后台整合（阶段2；蓝图 §7 /v1/consolidate）。

近因相似聚合 → 去重合并候选 → 合并：canonical 保留原文与 id，
source_ref 链 = 各源 source_ref + 被并条目 id（全程可溯源）；被并条目 → archived(hidden 不删)。
异步任务队列 + 进度查询；无 LLM（纯向量相似 + 机械合并）。
操作记录镜像 engine_meta('consolidate_ops')，daemon 重启后仍可查最近 20 条。
"""
import json
import logging
import queue
import threading
import time
import uuid

from . import config, db

log = logging.getLogger("memory-engine.consolidate")

_lock = threading.Lock()
_ops: dict[str, dict] = {}
_queue: "queue.Queue[dict]" = queue.Queue()
_worker_thread: threading.Thread | None = None


def _persist(conn) -> None:
    recent = sorted(_ops.values(), key=lambda o: o.get("created_at", ""), reverse=True)[:20]
    db.execute(
        conn,
        "INSERT INTO engine_meta(key,value) VALUES ('consolidate_ops',%s::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
        (json.dumps(recent, ensure_ascii=False),))


def submit(pool, days: int | None = None, limit: int | None = None,
           sim: float | None = None, dry_run: bool = False) -> dict:
    """入队一次整合任务，返回 operation_id（进度经 GET /v1/consolidate/{id} 查询）。"""
    op_id = uuid.uuid4().hex[:12]
    op = {"operation_id": op_id, "status": "queued", "stage": "queued",
          "params": {"days": days or config.CONSOLIDATE_RECENT_DAYS,
                     "limit": limit or config.CONSOLIDATE_SCAN_LIMIT,
                     "sim": sim or config.CONSOLIDATE_SIM,
                     "dry_run": bool(dry_run)},
          "progress": {"scanned": 0, "pairs": 0, "groups": 0, "merged": 0, "archived": 0},
          "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "started_at": None, "finished_at": None, "took_ms": None, "error": None}
    with _lock:
        _ops[op_id] = op
        try:
            with pool.connection() as conn:
                _persist(conn)
        except Exception as e:
            log.warning("persist consolidate op failed: %s", e)
    _queue.put({"op_id": op_id, "pool": pool})
    _ensure_worker(pool)
    return {"operation_id": op_id, "status": "queued"}


def _ensure_worker(pool) -> None:
    global _worker_thread
    with _lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            stop = threading.Event()
            _worker_thread = threading.Thread(
                target=_worker, args=(pool, stop), daemon=True, name="consolidate-worker")
            _worker_thread.start()


def _worker(pool, stop: threading.Event) -> None:
    log.info("consolidate worker started")
    while not stop.is_set():
        try:
            job = _queue.get(timeout=5.0)
        except queue.Empty:
            continue
        try:
            _run(pool, job["op_id"])
        except Exception as e:
            log.exception("consolidate op %s failed", job["op_id"])
            with _lock:
                op = _ops.get(job["op_id"])
                if op:
                    op.update(status="failed", error=str(e)[:300],
                              finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))


def _set(op_id: str, **kv) -> None:
    with _lock:
        op = _ops.get(op_id)
        if op:
            op.update(kv)


def _run(pool, op_id: str) -> None:
    with _lock:
        op = _ops[op_id]
        params = dict(op["params"])
    t0 = time.perf_counter()
    _set(op_id, status="running", stage="scanning", started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))

    with pool.connection() as conn:
        rows = db.fetch_all(
            conn,
            """SELECT id, bank, embedding, priority, access_count, adopt_count,
                      title, body, source_ref, tags
               FROM memories
               WHERE embedding IS NOT NULL AND ttl_state IN ('candidate','trial','active')
                 AND created_at > now() - (%s || ' days')::interval
               ORDER BY seq DESC LIMIT %s""",
            (str(params["days"]), params["limit"]))
    with _lock:
        _ops[op_id]["progress"]["scanned"] = len(rows)

    # —— 近因相似聚合：逐条在同 bank 内找最近邻，cos≥sim 记为合并候选对 ——
    edges: list[tuple[int, int]] = []
    with pool.connection() as conn:
        for i, row in enumerate(rows):
            nn = db.fetch_all(
                conn,
                """SELECT id, 1 - (embedding <=> %s::vector) AS cos
                   FROM memories
                   WHERE id <> %s AND bank = %s AND embedding IS NOT NULL
                     AND ttl_state IN ('candidate','trial','active')
                     AND 1 - (embedding <=> %s::vector) >= %s
                   ORDER BY embedding <=> %s::vector LIMIT 3""",
                (row["embedding"], row["id"], row["bank"],
                 row["embedding"], params["sim"], row["embedding"]))
            for other in nn:
                j = next((k for k, r in enumerate(rows) if r["id"] == other["id"]), None)
                if j is not None and j != i:
                    edges.append((i, j) if i < j else (j, i))
            with _lock:
                _ops[op_id]["progress"]["pairs"] = len(edges)
    # 求并查集分组
    parent = list(range(len(rows)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        parent[find(a)] = find(b)
    groups: dict[int, list[int]] = {}
    for i in range(len(rows)):
        groups.setdefault(find(i), []).append(i)
    merge_groups = [g for g in groups.values() if len(g) >= 2]
    with _lock:
        _ops[op_id]["progress"]["groups"] = len(merge_groups)

    if not merge_groups:
        _set(op_id, status="done", stage="done", took_ms=round((time.perf_counter() - t0) * 1000, 1),
             finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        with pool.connection() as conn:
            with _lock, conn.transaction():
                _persist(conn)
        return

    # —— 去重合并：canonical=优先级>访问量>正文最长；其余归档并留 source_ref 链 ——
    _set(op_id, stage="merging")
    merged = archived = 0
    with pool.connection() as conn:
        for g in merge_groups:
            if params["dry_run"]:
                merged += 1
                archived += len(g) - 1
                continue
            members = [rows[i] for i in g]
            canon = max(members, key=lambda r: (r["priority"], r["access_count"], len(r["body"] or "")))
            others = [r for r in members if r["id"] != canon["id"]]
            chain: list[str] = []
            for r in members:
                if r["source_ref"] and r["source_ref"] not in chain:
                    chain.append(r["source_ref"])
            merged_ids = [str(r["id"]) for r in others]
            tags: set = set()
            for r in members:
                tags |= set(r["tags"] or [])
            with conn.transaction():
                db.execute(
                    conn,
                    """UPDATE memories SET tags=%s::jsonb, source_ref=%s,
                              access_count=access_count+%s, adopt_count=adopt_count+%s,
                              updated_at=now()
                       WHERE id=%s""",
                    (json.dumps(sorted(tags), ensure_ascii=False),
                     json.dumps({"merged_from": merged_ids, "source_refs": chain},
                                ensure_ascii=False),
                     sum(r["access_count"] for r in others),
                     sum(r["adopt_count"] for r in others),
                     canon["id"]))
                db.execute(
                    conn,
                    """UPDATE memories SET ttl_state='archived', updated_at=now(),
                              ttl_expires_at=NULL WHERE id = ANY(%s)""",
                    (merged_ids,))
                db.log_changelog(conn, "consolidate", canon["id"],
                                 {"merged_from": merged_ids, "source_refs": chain,
                                  "group_size": len(members)})
            merged += 1
            archived += len(others)
            with _lock:
                _ops[op_id]["progress"].update(merged=merged, archived=archived)

    _set(op_id, status="done", stage="done", took_ms=round((time.perf_counter() - t0) * 1000, 1),
         finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    with pool.connection() as conn:
        with _lock, conn.transaction():
            _persist(conn)


def get_op(op_id: str) -> dict | None:
    with _lock:
        return _ops.get(op_id)


def list_ops(limit: int = 20) -> list[dict]:
    with _lock:
        ops = sorted(_ops.values(), key=lambda o: o.get("created_at", ""), reverse=True)
    return ops[:limit]


def load_persisted(pool, limit: int = 20) -> list[dict]:
    """daemon 重启后从 engine_meta 恢复最近操作（只读展示）。"""
    try:
        with pool.connection() as conn:
            row = db.fetch_one(conn, "SELECT value FROM engine_meta WHERE key='consolidate_ops'")
        return (row["value"] if row else [])[:limit]
    except Exception:
        return []
