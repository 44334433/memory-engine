"""观测面（借鉴调研 P1/P4 增强，2026-09-20 拍板「a」；语义真源=研究-借鉴项目增量调研-2026-09-19.md §7）。

三节，全部纯读 SQL / 纯内存 / 纯文件——零行为变更（不写库、不改评分、不改任何既有端点）：
- feedback_path（P1·S2 join-liveness）：feedback 写入→召回消费链路四段活性断言
  writes(changelog 账)→landed(memories.outcome_at 落列)→join(polarity 非空率)→consume
  (core-block auto 轨门槛面>0)。探针只证活性、不证明疗效（SLM 459/459 教训原文）；
  verdict=no_writes 时问题在宿主采集侧不在权重侧（#23 零成本必做条款）。
- injected_two_state（P1·S3 曝光≠反馈）：recall_hit(曝光态) 与 adopted(消费态) 两态分计数
  + orphan 机械审查——adopt_count>0 但库内无 kind='adopted' 事件=曝光漏进正信号的疑点行，
  期望恒 0；injected kind（schema 注释预留）全时计数=登记其「未写入、纯遥测」现状。
  已知例外（拍板设计非缺陷）：lifecycle 复活/延寿窗按 last_accessed（含 recall_hit）锚定，
  属「曝光延寿」而非「正向消费」——adopt 门槛/EMA/core block/upgrade_adopted 只认 adopted。
- pinned_exemption（P4·K2）：pinned 占库存比（总+per bank，Kleos 43% 钉满权的量化护栏位）
  + core block staleness（距最近一次 GET /v1/core-block 的时长）。staleness 读数用进程内
  时间戳（api_core_block 成功构建时打点）——读取零 DB 副作用铁律不破；重启即归零（如实标注）。
"""
import logging
import time
from datetime import datetime, timezone

from . import config, db, hard_queries

log = logging.getLogger("memory-engine.observability")

_FB_REASONS = ("fb_corrected", "fb_useless")   # L3 池中来自 feedback 通路的 reason


def window_days(days) -> int:
    """观测窗口夹取：非法→7，范围 [1,365]（f-string 注入防护：进 SQL 前必过本函数）。"""
    try:
        d = int(days)
    except (TypeError, ValueError):
        d = 7
    return max(1, min(d, 365))


def classify_probe(writes: int, landed: int, eligible: int) -> str:
    """活性探针判定（纯函数可单测）：四态=链路断点的机械定位。

    writes=窗口内写过 feedback 的 distinct 条目数；landed=其中 outcome_at 同窗落列数；
    eligible=消费门槛面（core-block auto 轨谓词）条目数。
    """
    if writes == 0:
        return "no_writes"                # 窗口内无反馈→宿主采集侧问题（探针结论，非权重侧）
    if landed == 0:
        return "write_only"               # changelog 有账而列无落=写入链断（同款死回路）
    if eligible == 0:
        return "joined_no_consume"        # 信号已落但未产生 core-block 门槛面条目（观察期）
    return "alive"


def feedback_probe(conn, days=7) -> dict:
    """feedback 写入→召回消费链路活性（S2 双探针：写侧 join 断言 + 消费门槛面）。"""
    d = window_days(days)
    win = f"now() - interval '{d} days'"
    writes = (db.fetch_one(
        conn, f"SELECT count(*) c FROM changelog WHERE op='feedback' AND ts > {win}")
        or {"c": 0})["c"]
    by_outcome = {r["outcome"]: r["c"] for r in db.fetch_all(
        conn, f"SELECT detail->>'outcome' outcome, count(*) c FROM changelog "
              f"WHERE op='feedback' AND ts > {win} GROUP BY 1")}
    distinct_writes = (db.fetch_one(
        conn, f"SELECT count(DISTINCT memory_id) c FROM changelog "
              f"WHERE op='feedback' AND ts > {win}") or {"c": 0})["c"]
    # landed 口径=「本窗写过 feedback 的 distinct 条目里，outcome_at 也确实落进本窗」的行数：
    # 同条多次反馈只落一列（last-write-wins），用行账对比会虚报断链——distinct join 才是 S2 断言。
    landed = (db.fetch_one(
        conn, f"SELECT count(*) c FROM (SELECT DISTINCT memory_id FROM changelog "
              f"WHERE op='feedback' AND ts > {win}) w "
              f"JOIN memories m ON m.id = w.memory_id WHERE m.outcome_at > {win}")
        or {"c": 0})["c"]
    pj = db.fetch_one(
        conn, "SELECT count(*) total, count(*) FILTER (WHERE polarity IS NOT NULL) nn "
              "FROM memories WHERE is_current AND ttl_state <> 'retired'") or {}
    eligible = (db.fetch_one(
        conn, "SELECT count(*) c FROM memories WHERE is_current "
              "AND ttl_state NOT IN ('retired','archived') "
              "AND memory_type = ANY(%s) AND adopt_count >= %s AND polarity >= %s",
        (list(config.CORE_AUTO_TYPES), config.CORE_MIN_ADOPT, config.CORE_MIN_POLARITY))
        or {"c": 0})["c"]
    entries = hard_queries.read_all()
    fb_l3 = [e for e in entries if e.get("reason") in _FB_REASONS]
    cutoff = time.time() - d * 86400
    return {
        "window_days": d,
        "feedback_writes": writes,                  # 行账（changelog 每信号一行）
        "feedback_by_outcome": by_outcome,
        "feedback_distinct_writes": distinct_writes,  # distinct 条目账（join 分子）
        "outcome_landed": landed,
        "join_drift": distinct_writes - landed,     # 持续>0=链路丢信号（死回路证据；purge 删除条目会计入，看 l3 与 process 配平）
        "polarity_nonnull": pj.get("nn", 0),        # S2 join 键非空率分子
        "live_total": pj.get("total", 0),
        "auto_track_eligible": eligible,            # 消费门槛面（polarity≥0.5∧adopt≥2，与 AUTO_SQL 同谓词）
        "l3_fb_signals": len(fb_l3),
        "l3_fb_signals_window": sum(1 for e in fb_l3 if _safe_epoch(e) >= cutoff),
        "probe_status": classify_probe(distinct_writes, landed, eligible),
        "note": "探针只证活性不证疗效（SLM 459/459）；no_writes→查宿主采集侧",
    }


def _safe_epoch(e: dict) -> float:
    try:
        v = float(e.get("ts_epoch", 0) or 0)
        return v if v == v else 0.0              # NaN→0（与 hard_queries.safe_epoch 同语义，不动其文件）
    except (TypeError, ValueError):
        return 0.0


def injected_two_state(conn, days=7) -> dict:
    """曝光/消费两态计数 + orphan 机械对照审查（S3：曝光不得进正向消费信号）。"""
    d = window_days(days)
    win = f"now() - interval '{d} days'"
    kinds = {r["kind"]: r["c"] for r in db.fetch_all(
        conn, f"SELECT kind, count(*) c FROM access_events WHERE ts > {win} GROUP BY kind")}
    orphans = db.fetch_all(
        conn, """SELECT m.id::text AS id, m.bank, m.adopt_count, m.ttl_state, m.created_at
                 FROM memories m WHERE m.adopt_count > 0 AND NOT EXISTS
                 (SELECT 1 FROM access_events ae
                   WHERE ae.memory_id = m.id AND ae.kind='adopted')
                 ORDER BY m.created_at DESC LIMIT 5""")
    orphan_total = (db.fetch_one(
        conn, "SELECT count(*) c FROM memories m WHERE m.adopt_count > 0 AND NOT EXISTS "
              "(SELECT 1 FROM access_events ae WHERE ae.memory_id = m.id AND ae.kind='adopted')")
        or {"c": 0})["c"]
    return {
        "window_days": d,
        "events_by_kind": kinds,
        "exposure_events": kinds.get("recall_hit", 0),        # 曝光态（被召回展示）
        "consumption_events": kinds.get("adopted", 0),        # 消费态（宿主显式采纳）
        "injected_kind_rows_all_time": (db.fetch_one(
            conn, "SELECT count(*) c FROM access_events WHERE kind='injected'")
            or {"c": 0})["c"],                                 # schema 预留 kind：无人写=纯遥测登记
        "orphan_adopt_rows": orphan_total,                     # adopt_count>0 却无 adopted 事件
        "orphan_samples": orphans,                             # 供对照审查人工核验（Top5）
        "review_conclusion": "pass" if orphan_total == 0 else "review_samples",
        "note": "正信号路径（adopt_count/EMA/core-block 门槛/upgrade_adopted）只认 adopted 事件与显式 "
                "feedback；曝光延寿（last_accessed/复活窗）为拍板设计例外不计正信号。orphan>0≠自动定罪："
                "已知良性来源=consolidate 合并求和继承（事件留原条，consolidate.py:183）+评测批事件清理，"
                "samples 供逐条核验（2026-09-20 现网 8 行已全数溯源为该类）",
    }


def pinned_share(conn) -> dict:
    """pinned 占库存比（K2 量化护栏；分母=现行非 retired 行）。"""
    rows = db.fetch_all(
        conn, "SELECT bank, count(*) total, count(*) FILTER (WHERE pinned) pinned "
              "FROM memories WHERE is_current AND ttl_state <> 'retired' "
              "GROUP BY bank ORDER BY count(*) FILTER (WHERE pinned) DESC, count(*) DESC")
    total = sum(r["total"] for r in rows)
    pinned = sum(r["pinned"] for r in rows)
    return {
        "live_total": total,
        "pinned_total": pinned,
        "ratio": round(pinned / total, 4) if total else 0.0,
        "by_bank": [{"bank": r["bank"], "total": r["total"], "pinned": r["pinned"],
                     "ratio": round(r["pinned"] / r["total"], 4) if r["total"] else 0.0}
                    for r in rows],
        "note": "只观测不改豁免行为（P4 拍板）；pinned 轨另排除 archived（core_block PINNED_SQL 谓词）",
    }


def core_block_staleness(last_fetch: float | None, started_at: float, now: float | None = None) -> dict:
    """core block staleness：距最近一次注入面拉取（GET /v1/core-block）的时长。纯内存读数。"""
    now = time.time() if now is None else now
    if last_fetch is None:
        return {"ever_fetched_since_startup": False,
                "process_uptime_s": round(now - started_at, 1),
                "note": "本进程启动以来未收到 core-block 拉取（重启归零为进程内语义）"}
    return {"ever_fetched_since_startup": True,
            "last_fetch_iso": datetime.fromtimestamp(last_fetch, tz=timezone.utc)
                              .isoformat(timespec="seconds"),
            "staleness_s": round(now - last_fetch, 1)}
