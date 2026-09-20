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

# 各状态 ttl_expires_at 语义（active/archived 无到期概念）。
# ⑪TTL env 化（2026-09-20）：trial/decaying 的字面天数走 config（默认 30/180=现值零行为），
# 与转移窗（TRIAL_DECAY_DAYS/DECAY_ARCHIVE_DAYS）同源，消除双处硬编码漂移。
_EXPIRES_SQL = {
    "candidate": "now() + interval '{d} days'",
    "trial": "now() + interval '{t} days'",
    "active": "NULL",
    "decaying": "now() + interval '{k} days'",
    "archived": "NULL",
}


def _transact(conn, sql: str, params: tuple, frm: str, to: str, reason: str) -> list[str]:
    """按条件批量转换状态 + 逐条写 changelog，返回转换的 memory_id 列表。"""
    with conn.transaction():
        rows = db.fetch_all(conn, sql, params)
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        exp = _EXPIRES_SQL[to].format(d=config.CANDIDATE_DAYS,
                                      t=config.TRIAL_DECAY_DAYS,
                                      k=config.DECAY_ARCHIVE_DAYS)
        db.execute(
            conn,
            f"UPDATE memories SET ttl_state=%s, updated_at=now(), "
            f"ttl_expires_at={exp} WHERE id = ANY(%s)",
            (to, ids),
        )
        for mid in ids:
            db.log_changelog(conn, "lifecycle", mid,
                             {"from": frm, "to": to, "reason": reason})
    return ids


def _signal_exists_sql(days: int) -> str:
    return ("EXISTS (SELECT 1 FROM access_events ae WHERE ae.memory_id = m.id "
            "AND ae.kind IN ('recall_hit','adopted') AND ae.ts > now() - interval '%d days')" % days)


# —————————————————— W2 分层：memory_type 差异化衰减窗（2026-09-18） ——————————————————
# 衰减/归档/零访问四条轨的视界与信号窗统一乘 TYPE_DECAY_FACTORS（semantic 最慢 ×2、
# procedural ×1.5、episodic ×1=基准——episodic 与拍板天数逐位相等，存量行为零变化）。
# promote/revive（信号驱动升级）不分型。candidates() 预览与 scan 同判据（docstring 铁律）。
# W4（2026-09-19）：四条轨再乘 bank 级 scale（config.BANK_DECAY_SCALE，唯一变更点在 config）；
# 未登记 bank 回退 scale=1.0 → 与 W2 输出逐字符相等（存量等价，tests/test_w4 断言）。

def _sql_lit(s: str) -> str:
    """SQL 字符串字面量（bank 名单来自 config dict 键=代码常量，转义仅纵深防御）。"""
    return "'" + s.replace("'", "''") + "'"


def _type_days_case(base_days: int, bank_scale: float = 1.0) -> str:
    """SQL CASE：按 memory_type 得该轨道整数天（base × bank_scale × type 因子，四舍五入）。

    单参调用（bank_scale 缺省 1.0）输出与 W2 逐字符相等——存量等价不变量的实现基座。"""
    parts = [f"WHEN '{t}' THEN {int(round(base_days * bank_scale * config.TYPE_DECAY_FACTORS.get(t, 1.0)))}"
             for t in config.MEMORY_TYPES]
    return f"(CASE memory_type {' '.join(parts)} ELSE {int(round(base_days * bank_scale))} END)::int"


def _bank_type_days_case(base_days: int) -> str:
    """SQL CASE：bank 级 scale × W2 分型窗（W4）。

    只为 scale≠1.0 的登记 bank 生成分支；映射为空/全 1.0 时输出与 _type_days_case
    逐字符相等（=未登记回退现行为，四轨共用本唯一判据源）。"""
    default = _type_days_case(base_days)
    scaled = sorted((b, s) for b, s in config.BANK_DECAY_SCALE.items() if s != 1.0)
    if not scaled:
        return default
    whens = " ".join(f"WHEN bank = {_sql_lit(b)} THEN {_type_days_case(base_days, s)}"
                     for b, s in scaled)
    return f"(CASE {whens} ELSE {default} END)::int"


def _typed_interval(base_days: int) -> str:
    return f"(interval '1 day' * {_bank_type_days_case(base_days)})"


def _signal_exists_sql_typed(base_days: int) -> str:
    return ("EXISTS (SELECT 1 FROM access_events ae WHERE ae.memory_id = m.id "
            "AND ae.kind IN ('recall_hit','adopted') "
            f"AND ae.ts > now() - {_typed_interval(base_days)})")


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
    """trial 30d 无信号 → decaying（蓝图：trial 30天无信号；W2：窗口按类型缩放）。"""
    return _transact(
        conn,
        f"""SELECT id FROM memories m WHERE ttl_state='trial'
             AND created_at <= now() - {_typed_interval(config.TRIAL_DECAY_DAYS)}
             AND NOT {_signal_exists_sql_typed(config.TRIAL_DECAY_DAYS)}
             LIMIT 500""",
        (),
        "trial", "decaying", f"no_signal_{config.TRIAL_DECAY_DAYS}d_type_scaled")


def decay_active(conn) -> list[str]:
    """active 90d 无触发/采纳 → decaying（阶段2 拍板；W2：窗口按类型缩放）。"""
    return _transact(
        conn,
        f"""SELECT id FROM memories m WHERE ttl_state='active'
             AND created_at <= now() - {_typed_interval(config.ACTIVE_DECAY_DAYS)}
             AND NOT {_signal_exists_sql_typed(config.ACTIVE_DECAY_DAYS)}
             LIMIT 500""",
        (),
        "active", "decaying", f"no_trigger_{config.ACTIVE_DECAY_DAYS}d_type_scaled")


def revive_decaying(conn) -> list[str]:
    """decaying 近 3d 有命中/采纳 → 复活 active（蓝图：再次命中/采纳）。"""
    return _transact(
        conn,
        f"""SELECT id FROM memories m WHERE ttl_state='decaying'
             AND {_signal_exists_sql(config.REVIVE_WINDOW_DAYS)} LIMIT 500""",
        (),
        "decaying", "active", f"revive_hit_within_{config.REVIVE_WINDOW_DAYS}d")


# —————————————————— L2 用进废退·信号接线（P1 第三批 2026-09-16） ——————————————————
# 路线图 §自进化 L2：access_events 信号接回 TTL 状态机——「无信号→降权/衰减」的反向
# 「有强信号→延寿/状态升级」补上。既有拍板参数与 revive_decaying（3d）判据一律不动。


def upgrade_adopted(conn) -> list[str]:
    """L2·状态升级：decaying 近 30d 有 adopted（宿主引用上报）→ active。

    adopted=宿主 recall 后实际引用上报，信号强度高于 recall 命中 → 单列
    L2_ADOPT_WINDOW_DAYS=30d 升级窗（recall_hit 复活维持拍板 3d 窗不动）。
    """
    return _transact(
        conn,
        f"""SELECT id FROM memories m WHERE ttl_state='decaying'
             AND EXISTS (SELECT 1 FROM access_events ae WHERE ae.memory_id = m.id
                   AND ae.kind='adopted'
                   AND ae.ts > now() - interval '{config.L2_ADOPT_WINDOW_DAYS} days')
             LIMIT 500""",
        (),
        "decaying", "active", f"l2_adopt_upgrade_{config.L2_ADOPT_WINDOW_DAYS}d")


def _last_access_anchor_sql() -> str:
    """零访问锚 = GREATEST(最近任意 kind 访问事件, created_at)——从未访问=自入库起算连续零访问。"""
    return ("GREATEST(COALESCE((SELECT max(ae.ts) FROM access_events ae "
            "WHERE ae.memory_id = m.id), m.created_at), m.created_at)")


def _decay_zero_access(conn, state: str) -> list[str]:
    return _transact(
        conn,
        f"""SELECT id FROM memories m WHERE ttl_state='{state}'
             AND {_last_access_anchor_sql()} <= now() - {_typed_interval(config.L2_ZERO_ACCESS_DAYS)}
             LIMIT 500""",
        (),
        state, "decaying", f"l2_zero_access_{config.L2_ZERO_ACCESS_DAYS}d_type_scaled")


def decay_zero_access(conn) -> list[str]:
    """L2·提前衰减：trial/active 连续 90d 零访问（任意 kind）→ decaying。

    锚=最近任意访问事件（无访问回退 created_at）；与既有 decay_trials(30d 无信号)/
    decay_active(90d 无触发) 并行不悖（判据不动），本规则为 kind 泛化接线——
    未来新增 access kind 自动纳入「访问」语义，90d 零访问独立成轨（相对 180d
    归档视界为「提前」衰减判定）。
    """
    out: list[str] = []
    for st in ("trial", "active"):     # 分状态转换：changelog 的 from 逐条准确
        out += _decay_zero_access(conn, st)
    return out


def adopt_renew_count(conn) -> int:
    """L2·延寿观测：active 近 30d 有 adopted 的条数（read-only 观测计数，进 scan 摘要）。

    active 无到期概念，延寿机制 = 衰减时钟锚定访问信号（decay_active 的 90d
    无信号判据被 adopted/recall_hit 天然续期）——本计数使「信号→延寿」在扫描
    摘要可观测，不产生额外写放大。
    """
    row = db.fetch_one(
        conn,
        f"""SELECT count(*) c FROM memories m WHERE ttl_state='active'
             AND EXISTS (SELECT 1 FROM access_events ae WHERE ae.memory_id = m.id
                   AND ae.kind='adopted'
                   AND ae.ts > now() - interval '{config.L2_ADOPT_WINDOW_DAYS} days')""")
    return int(row["c"]) if row else 0


def archive_decaying(conn) -> list[str]:
    """decaying 180d 无信号 → archived（hidden 不删，行保留、changelog 完整；W2：窗口按类型缩放）。"""
    return _transact(
        conn,
        f"""SELECT id FROM memories m WHERE ttl_state='decaying'
             AND created_at <= now() - {_typed_interval(config.DECAY_ARCHIVE_DAYS)}
             AND NOT {_signal_exists_sql_typed(config.DECAY_ARCHIVE_DAYS)}
             LIMIT 500""",
        (),
        "decaying", "archived", f"no_signal_{config.DECAY_ARCHIVE_DAYS}d_type_scaled")


def scan(conn) -> dict:
    """跑一轮全部规则（统一 DB 时钟，测试以数据回拨模拟时间流逝）；返回转换明细。"""
    steps = [
        ("candidate_to_trial", lambda: promote_candidates(conn)),
        ("trial_to_active", lambda: promote_trials(conn)),
        ("trial_to_decaying", lambda: decay_trials(conn)),
        ("active_to_decaying", lambda: decay_active(conn)),
        ("decaying_to_active", lambda: revive_decaying(conn)),
        ("decaying_to_active_adopt", lambda: upgrade_adopted(conn)),     # L2：adopted≤30d 强信号升级
        ("trial_to_decaying_zero_access", lambda: _decay_zero_access(conn, "trial")),   # L2：零访问
        ("active_to_decaying_zero_access", lambda: _decay_zero_access(conn, "active")), # L2：零访问
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
    try:                                            # L2 延寿观测（read-only，失败不拖垮整轮）
        out["adopt_renew_observed"] = adopt_renew_count(conn)
    except Exception as e:
        log.warning("adopt_renew_count failed: %s", e)
        out["adopt_renew_observed"] = None
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
             WHERE ttl_state IN ('trial','active')
               AND created_at <= now() - {_typed_interval(config.TRIAL_DECAY_DAYS)}
               AND NOT {_signal_exists_sql_typed(config.TRIAL_DECAY_DAYS)}
             ORDER BY created_at LIMIT 100""")
    archiving = db.fetch_all(
        conn,
        f"""SELECT id, bank, title FROM memories m WHERE ttl_state='decaying'
             AND created_at <= now() - {_typed_interval(config.DECAY_ARCHIVE_DAYS)}
             AND NOT {_signal_exists_sql_typed(config.DECAY_ARCHIVE_DAYS)}
             ORDER BY created_at LIMIT 100""")
    l2_upgrade = db.fetch_all(
        conn,
        f"""SELECT id, bank, title FROM memories m WHERE ttl_state='decaying'
             AND EXISTS (SELECT 1 FROM access_events ae WHERE ae.memory_id = m.id
                   AND ae.kind='adopted'
                   AND ae.ts > now() - interval '{config.L2_ADOPT_WINDOW_DAYS} days')
             ORDER BY updated_at LIMIT 100""")
    l2_zero = db.fetch_all(
        conn,
        f"""SELECT id, bank, title, ttl_state FROM memories m
             WHERE ttl_state IN ('trial','active')
               AND {_last_access_anchor_sql()} <= now() - {_typed_interval(config.L2_ZERO_ACCESS_DAYS)}
             ORDER BY created_at LIMIT 100""")
    return {"promote": promote,
            "decay": [{"id": str(r["id"]), "bank": r["bank"], "title": r["title"],
                       "from": r["ttl_state"]} for r in decaying],
            "archive": [{"id": str(r["id"]), "bank": r["bank"], "title": r["title"]}
                        for r in archiving],
            "l2_upgrade": [{"id": str(r["id"]), "bank": r["bank"], "title": r["title"]}
                           for r in l2_upgrade],
            "l2_zero_access": [{"id": str(r["id"]), "bank": r["bank"], "title": r["title"],
                                "from": r["ttl_state"]} for r in l2_zero]}


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
            # L3 失败回流（P1 第三批）：巡扫周期分析 hard_queries → 调参提案（内部节流，失败不拖垮巡扫）
            try:
                from . import hard_queries
                hq = hard_queries.analyze_hard_queries()
                if hq.get("proposals"):
                    log.info("hard_query proposals: %s", hq["proposals"])
            except Exception as e:
                log.warning("hard_query analysis failed: %s", e)
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
