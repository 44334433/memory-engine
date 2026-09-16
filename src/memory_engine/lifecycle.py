"""生命周期状态机（阶段2；蓝图 §6 + 拍板④）。

链路：candidate(期6d)→trial→(转正闸: 近30d 触发≥3 且 采纳率≥60%)→active
      →(90d 无触发/采纳)→decaying(召回降权 0.7)→(180d 无信号)→archived(hidden 不删)
      decaying 近 3d 有命中/采纳 → 复活 active。
全机械判定，信号源=access_events(recall_hit/adopted)，无 LLM；
每次转换写 changelog(op='lifecycle')。线程内嵌 daemon（蓝图 §8.2-4 延迟 60s 启动）。
本模块同时承担本地 WAL 归档滚动清理（阶段1 §8 未尽项，pg_archivecleanup）。
"""
import json
import logging
import re
import subprocess
import threading
from datetime import datetime, timedelta, timezone

from . import config, db

log = logging.getLogger("memory-engine.lifecycle")

WAL_FILE_RE = re.compile(r"^[0-9A-F]{24}(\.[0-9A-F]{24}\.backup|\.partial)?$")
STATES = ("candidate", "trial", "active", "decaying", "archived")

# 各状态 ttl_expires_at 语义（active/archived 无到期概念）
_EXPIRES_SQL = {
    "candidate": "now() + interval '{d} days'",
    "trial": "now() + interval '30 days'",
    "active": "NULL",
    "decaying": "now() + interval '180 days'",
    "archived": "NULL",
}


def _transact(conn, sql: str, params: tuple, frm: str, to: str, reason: str) -> list[str]:
    """按条件批量转换状态 + 逐条写 changelog，返回转换的 memory_id 列表。"""
    with conn.transaction():
        rows = db.fetch_all(conn, sql, params)
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        db.execute(
            conn,
            f"UPDATE memories SET ttl_state=%s, updated_at=now(), "
            f"ttl_expires_at={_EXPIRES_SQL[to].format(d=config.CANDIDATE_DAYS)} WHERE id = ANY(%s)",
            (to, ids),
        )
        for mid in ids:
            db.log_changelog(conn, "lifecycle", mid,
                             {"from": frm, "to": to, "reason": reason})
    return ids


def _signal_exists_sql(days: int) -> str:
    return ("EXISTS (SELECT 1 FROM access_events ae WHERE ae.memory_id = m.id "
            "AND ae.kind IN ('recall_hit','adopted') AND ae.ts > now() - interval '%d days')" % days)


def promote_candidates(conn) -> list[str]:
    """候选期届满（默认 6 天）自动转 trial——机械时间闸。"""
    return _transact(
        conn,
        "SELECT id FROM memories WHERE ttl_state='candidate' "
        "AND created_at <= now() - interval '%d days' LIMIT 500" % config.CANDIDATE_DAYS,
        (),
        "candidate", "trial", f"candidate_period_{config.CANDIDATE_DAYS}d_elapsed")


def promote_trials(conn) -> list[str]:
    """转正闸（拍板④）：近30d recall_hit≥3 且 采纳率 adopted/hit≥60%。"""
    rows = db.fetch_all(
        conn,
        """SELECT m.id,
                  (SELECT count(*) FROM access_events ae WHERE ae.memory_id=m.id
                     AND ae.kind='recall_hit' AND ae.ts > now() - interval '30 days') AS hits,
                  (SELECT count(*) FROM access_events ae WHERE ae.memory_id=m.id
                     AND ae.kind='adopted' AND ae.ts > now() - interval '30 days') AS adopts
           FROM memories m WHERE m.ttl_state='trial' LIMIT 2000""")
    ids = [r["id"] for r in rows
           if r["hits"] >= config.PROMOTE_MIN_HITS
           and r["hits"] > 0
           and r["adopts"] / r["hits"] >= config.PROMOTE_MIN_ADOPT_RATE]
    if not ids:
        return []
    with conn.transaction():
        db.execute(conn,
                   "UPDATE memories SET ttl_state='active', updated_at=now(), ttl_expires_at=NULL "
                   "WHERE id = ANY(%s)", (ids,))
        for mid in ids:
            stats = next(r for r in rows if r["id"] == mid)
            db.log_changelog(conn, "lifecycle", mid,
                             {"from": "trial", "to": "active", "reason": "admission_gate",
                              "hits_30d": stats["hits"], "adopts_30d": stats["adopts"],
                              "adopt_rate": round(stats["adopts"] / stats["hits"], 3)})
    return ids


def decay_trials(conn) -> list[str]:
    """trial 30d 无信号 → decaying（蓝图：trial 30天无信号）。"""
    return _transact(
        conn,
        f"""SELECT id FROM memories m WHERE ttl_state='trial'
             AND created_at <= now() - interval '{config.TRIAL_DECAY_DAYS} days'
             AND NOT {_signal_exists_sql(config.TRIAL_DECAY_DAYS)}
             LIMIT 500""",
        (),
        "trial", "decaying", f"no_signal_{config.TRIAL_DECAY_DAYS}d")


def decay_active(conn) -> list[str]:
    """active 90d 无触发/采纳 → decaying（阶段2 拍板）。"""
    return _transact(
        conn,
        f"""SELECT id FROM memories m WHERE ttl_state='active'
             AND created_at <= now() - interval '{config.ACTIVE_DECAY_DAYS} days'
             AND NOT {_signal_exists_sql(config.ACTIVE_DECAY_DAYS)}
             LIMIT 500""",
        (),
        "active", "decaying", f"no_trigger_{config.ACTIVE_DECAY_DAYS}d")


def revive_decaying(conn) -> list[str]:
    """decaying 近 3d 有命中/采纳 → 复活 active（蓝图：再次命中/采纳）。"""
    return _transact(
        conn,
        f"""SELECT id FROM memories m WHERE ttl_state='decaying'
             AND {_signal_exists_sql(config.REVIVE_WINDOW_DAYS)} LIMIT 500""",
        (),
        "decaying", "active", f"revive_hit_within_{config.REVIVE_WINDOW_DAYS}d")


def archive_decaying(conn) -> list[str]:
    """decaying 180d 无信号 → archived（hidden 不删，行保留、changelog 完整）。"""
    return _transact(
        conn,
        f"""SELECT id FROM memories m WHERE ttl_state='decaying'
             AND created_at <= now() - interval '{config.DECAY_ARCHIVE_DAYS} days'
             AND NOT {_signal_exists_sql(config.DECAY_ARCHIVE_DAYS)}
             LIMIT 500""",
        (),
        "decaying", "archived", f"no_signal_{config.DECAY_ARCHIVE_DAYS}d")


def scan(conn) -> dict:
    """跑一轮全部规则（统一 DB 时钟，测试以数据回拨模拟时间流逝）；返回转换明细。"""
    steps = [
        ("candidate_to_trial", lambda: promote_candidates(conn)),
        ("trial_to_active", lambda: promote_trials(conn)),
        ("trial_to_decaying", lambda: decay_trials(conn)),
        ("active_to_decaying", lambda: decay_active(conn)),
        ("decaying_to_active", lambda: revive_decaying(conn)),
        ("decaying_to_archived", lambda: archive_decaying(conn)),
    ]
    out: dict = {"ts": datetime.now(timezone.utc).isoformat(), "transitions": {}}
    for name, fn in steps:
        try:
            ids = fn()
            out["transitions"][name] = {"count": len(ids), "ids": [str(i) for i in ids][:50]}
        except Exception as e:                      # 单规则故障不拖垮整轮
            log.exception("lifecycle step %s failed", name)
            out["transitions"][name] = {"error": str(e)[:200]}
    return out


def state_summary(conn) -> dict:
    rows = db.fetch_all(conn, "SELECT ttl_state s, count(*) c FROM memories GROUP BY ttl_state")
    dist = {r["s"]: r["c"] for r in rows}
    return {s: dist.get(s, 0) for s in STATES} | {"retired": dist.get("retired", 0)}


def candidates(conn, now: datetime | None = None) -> dict:
    """待转换清单（只读预览，与 scan 同判据）。"""
    now = now or datetime.now(timezone.utc)
    promo = db.fetch_all(
        conn,
        """SELECT m.id, m.bank, m.title, m.ttl_state,
                  (SELECT count(*) FROM access_events ae WHERE ae.memory_id=m.id
                     AND ae.kind='recall_hit' AND ae.ts > %s) AS hits_30d,
                  (SELECT count(*) FROM access_events ae WHERE ae.memory_id=m.id
                     AND ae.kind='adopted' AND ae.ts > %s) AS adopts_30d
           FROM memories m WHERE m.ttl_state IN ('candidate','trial')
           ORDER BY m.access_count DESC LIMIT 100""",
        (now - timedelta(days=30), now - timedelta(days=30)))
    promote = [{"id": str(r["id"]), "bank": r["bank"], "title": r["title"],
                "state": r["ttl_state"],
                "hits_30d": r["hits_30d"], "adopts_30d": r["adopts_30d"],
                "eligible": r["hits_30d"] >= config.PROMOTE_MIN_HITS and r["hits_30d"] > 0
                and r["adopts_30d"] / r["hits_30d"] >= config.PROMOTE_MIN_ADOPT_RATE}
               for r in promo]
    decaying = db.fetch_all(
        conn,
        f"""SELECT id, bank, title, ttl_state FROM memories m
             WHERE ttl_state IN ('trial','active') AND created_at <= %s
               AND NOT {_signal_exists_sql(config.TRIAL_DECAY_DAYS)}
             ORDER BY created_at LIMIT 100""",
        (now - timedelta(days=config.TRIAL_DECAY_DAYS),))
    archiving = db.fetch_all(
        conn,
        f"""SELECT id, bank, title FROM memories m WHERE ttl_state='decaying'
             AND created_at <= %s AND NOT {_signal_exists_sql(config.DECAY_ARCHIVE_DAYS)}
             ORDER BY created_at LIMIT 100""",
        (now - timedelta(days=config.DECAY_ARCHIVE_DAYS),))
    return {"promote": promote,
            "decay": [{"id": str(r["id"]), "bank": r["bank"], "title": r["title"],
                       "from": r["ttl_state"]} for r in decaying],
            "archive": [{"id": str(r["id"]), "bank": r["bank"], "title": r["title"]}
                        for r in archiving]}


MANUAL_ACTIONS = {"promote": "active", "demote": "decaying",
                  "archive": "archived", "revive": "active", "trialize": "trial"}


def transition(conn, memory_id, action: str, reason: str = "") -> dict:
    """人工干预转换（POST /v1/lifecycle/transition；CLI 同源）。"""
    if action not in MANUAL_ACTIONS:
        raise ValueError(f"action 须为 {sorted(MANUAL_ACTIONS)}")
    to = MANUAL_ACTIONS[action]
    row = db.fetch_one(conn, "SELECT ttl_state FROM memories WHERE id=%s", (memory_id,))
    if not row:
        raise LookupError(f"memory {memory_id} 不存在")
    frm = row["ttl_state"]
    with conn.transaction():
        db.execute(conn,
                   f"UPDATE memories SET ttl_state=%s, updated_at=now(), "
                   f"ttl_expires_at={_EXPIRES_SQL[to].format(d=config.CANDIDATE_DAYS)} WHERE id=%s",
                   (to, memory_id))
        db.log_changelog(conn, "lifecycle", memory_id,
                         {"from": frm, "to": to, "reason": f"manual:{action}:{reason}"[:180]})
    return {"id": str(memory_id), "from": frm, "to": to}


def history(conn, limit: int = 50) -> list[dict]:
    return db.fetch_all(
        conn,
        "SELECT seq, ts, memory_id, detail FROM changelog WHERE op='lifecycle' "
        "ORDER BY seq DESC LIMIT %s", (max(1, min(limit, 500)),))


# —————————————————— 本地 WAL 归档滚动清理 ——————————————————

def wal_status() -> dict:
    import os
    d = config.WAL_ARCHIVE_DIR
    try:
        files = [f for f in os.listdir(d) if WAL_FILE_RE.match(f)]
    except OSError as e:
        return {"dir": d, "error": str(e)[:120]}
    return {"dir": d, "files": len(files),
            "oldest": min(files) if files else None, "newest": max(files) if files else None}


def wal_cleanup(now: datetime | None = None, keep: int | None = None) -> dict:
    """滚动清理：保留最新 keep 段，pg_archivecleanup 删除更早段（缺二进制则直删兜底）。"""
    import os
    now = now or datetime.now(timezone.utc)
    keep = keep or config.WAL_KEEP_FILES
    d = config.WAL_ARCHIVE_DIR
    files = sorted(f for f in os.listdir(d) if WAL_FILE_RE.match(f))
    summary = {"ts": now.isoformat(), "deleted": 0, "before": len(files),
               "after": len(files), "kept_from": None,
               "tool": "pg_archivecleanup" if os.path.exists(
                   os.path.join(config.PG_BIN, "pg_archivecleanup")) else "unlink-fallback"}
    if len(files) > keep:
        keep_from = files[-keep]                 # 保留最新 keep 段（含其自身）
        before = len(files)
        tool = os.path.join(config.PG_BIN, "pg_archivecleanup")
        if os.path.exists(tool):
            r = subprocess.run([tool, d, keep_from], capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                raise RuntimeError(f"pg_archivecleanup rc={r.returncode}: {r.stderr[:200]}")
        else:                                    # 兜底：按段名序直删（段名字典序=时间序）
            for f in files:
                if f < keep_from:
                    os.unlink(os.path.join(d, f))
        after = len([f for f in os.listdir(d) if WAL_FILE_RE.match(f)])
        summary.update(deleted=before - after, before=before, after=after, kept_from=keep_from)
    pool = db.PgPool(config.PG_DSN, 1, 1)        # 独立短连接写 meta，不占主池
    try:
        with pool.connection() as conn:
            db.execute(conn, "INSERT INTO engine_meta(key,value) VALUES ('last_wal_cleanup',%s::jsonb) "
                             "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                       (json.dumps(summary),))
    finally:
        pool.close()
    return summary


# —————————————————— 后台线程 ——————————————————

def _loop(eng, stop: threading.Event) -> None:
    if stop.wait(config.LIFECYCLE_START_DELAY_S):   # 蓝图 §8.2-4：延迟 60s 启动
        return
    log.info("lifecycle thread started (interval=%ss)", config.LIFECYCLE_INTERVAL_S)
    last_wal_day = None
    while not stop.wait(config.LIFECYCLE_INTERVAL_S):
        if not getattr(eng, "warm", False):
            continue
        try:
            with eng.db.connection() as conn:
                result = scan(conn)
                eng.lifecycle_last = result
                db.execute(conn,
                           "INSERT INTO engine_meta(key,value) VALUES ('lifecycle_last_scan',%s::jsonb) "
                           "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                           (json.dumps(result, ensure_ascii=False),))
            day = datetime.now(timezone.utc).date().isoformat()
            if day != last_wal_day:                  # 每日一次 WAL 滚动清理
                try:
                    summary = wal_cleanup()
                    last_wal_day = day
                    if summary["deleted"]:
                        log.info("wal cleanup: %s", summary)
                except Exception as e:
                    log.warning("wal cleanup failed: %s", e)
            moved = {k: v["count"] for k, v in result["transitions"].items()
                     if isinstance(v, dict) and v.get("count")}
            if moved:
                log.info("lifecycle transitions: %s", moved)
        except Exception:
            log.exception("lifecycle scan failed")


def start_thread(eng, stop: threading.Event) -> threading.Thread:
    t = threading.Thread(target=_loop, args=(eng, stop), daemon=True, name="lifecycle")
    t.start()
    return t
