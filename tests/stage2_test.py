"""阶段2 自测：生命周期全链路 / consolidate 去重合并 / CLI 全命令 / 健康四真 / WAL。

可重复执行：测试条目以 tags=['stage2-selftest'] 标记并在结束时 purge（changelog 留痕不删）。
时间流逝用数据回拨模拟（统一 DB 时钟，scan 无时钟参数）。
运行：/usr/bin/python3 tests/stage2_test.py  → tests/last_stage2.json
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
try:
    import psycopg                                # noqa: E402
except ImportError:  # 独立仓最小环境（pytest 采集阶段不连库）
    psycopg = None                                # noqa: E402
from memory_engine import config                  # noqa: E402

BASE = f"http://{config.HOST}:{config.PORT}"
CLI = os.environ.get("MEMORY_ENGINE_CLI", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "deploy", "memory-engine.sh"))
RESULTS: dict[str, dict] = {}


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS[name] = {"ok": bool(ok), "detail": str(detail)[:300]}
    print(("PASS " if ok else "FAIL ") + name + (" | " + str(detail)[:200] if detail else ""))


def http(method: str, path: str, body: dict | None = None, timeout: float = 60) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def sql(query: str, params: tuple = ()) -> list[dict]:
    with psycopg.connect(config.PG_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            if cur.description is None:
                return []
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def retain_one(bank: str, content: str, context: str, source_ref: str) -> str:
    out = http("POST", "/v1/retain", {
        "bank": bank, "caller": "stage2-test",
        "items": [{"content": content, "context": context, "tags": ["stage2-selftest"],
                   "source_type": "manual", "source_ref": source_ref, "priority": 3,
                   "source_tier": "user"}]})  # P0 投毒闸批：本测试验证 user 来源 candidate 候选期链路，显式声明
    assert out["ids"], f"retain failed: {out}"
    return out["ids"][0]


def cli(*args: str) -> tuple[int, str]:
    r = subprocess.run(["bash", CLI, *args], capture_output=True, text=True, timeout=180)
    return r.returncode, r.stdout + r.stderr


def wait_consolidate(op_id: str, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        op = http("GET", f"/v1/consolidate/{op_id}")
        if op.get("status") in ("done", "failed"):
            return op
        time.sleep(0.5)
    return {"status": "timeout"}


def main() -> int:
    marker = f"阶段2自测 {datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    ids: list[str] = []

    # 0. 预清理：上轮崩溃残留的测试条目（防语义判重拦截本轮写入）
    stale = sql("DELETE FROM memories WHERE tags @> '[\"stage2-selftest\"]'::jsonb RETURNING id")
    if stale:
        print(f"pre-clean: purged {len(stale)} leftover test entries")

    # 0. 健康四真
    h = http("GET", "/v1/health")
    check("health.four_true", h["status"] == "ok" and h["db"] and h["model_loaded"]
          and h["warm"] and h["ready"], f"status={h['status']} v={h['version']}")
    lc = http("GET", "/v1/lifecycle")
    check("lifecycle.thread_alive", lc["thread_alive"] is True)
    check("lifecycle.wal_visible", lc["wal"].get("files", 0) >= 1, lc["wal"])

    # 1. retain → candidate 入场（候选期 6d）
    c1 = retain_one("knowledge", f"{marker} 主条目：pgvector HNSW 部分索引按 bank 裁剪扫描面",
                    "阶段2生命周期自测", "stage2/entry-main")
    ids.append(c1)
    row = http("GET", f"/v1/memories/{c1}")
    check("retain.candidate_entry", row["ttl_state"] == "candidate", row["ttl_state"])

    # 2. 信号累积：3 次 recall 触发 + 2 次 adopt
    q = f"{marker} 主条目：pgvector HNSW 部分索引按 bank 裁剪扫描面"  # 携带 marker 唯一串→确定性命中
    for _ in range(3):
        http("POST", "/v1/recall", {"query": q, "bank": "knowledge", "caller": "stage2-test"})
    http("POST", f"/v1/memories/{c1}/adopt", {"caller": "stage2-test"})
    http("POST", f"/v1/memories/{c1}/adopt", {"caller": "stage2-test"})
    # 转正信号直读 access_events 断言入账（candidates 预览 LIMIT 100 截断，大库下不保证含新条目，
    # 故预览只验接口形状；转正闸语义由后续 run 的 candidate_to_trial + trial→active 转换断言覆盖）
    sig_rows = sql("SELECT kind, count(*)::int AS n FROM access_events "
                   "WHERE memory_id=%s AND ts > now() - interval '30 days' GROUP BY kind", (c1,))
    sig = {r["kind"]: r["n"] for r in sig_rows}
    check("gate.signals_recorded", sig.get("recall_hit", 0) >= 3 and sig.get("adopted", 0) >= 2, str(sig))
    prev = http("GET", "/v1/lifecycle/candidates")
    check("gate.preview_shape", all(k in prev for k in ("promote", "decay", "archive")),
          str(list(prev.keys())))

    # 3. 候选期未满：run 不转 trial
    run1 = http("POST", "/v1/lifecycle/run", {"dry_run": False})
    state = http("GET", f"/v1/memories/{c1}")["ttl_state"]
    check("lifecycle.candidate_period_holds", state == "candidate", state)

    # 4. 数据回拨 7d → 同一轮 scan 连续流转 candidate→trial→（转正闸 2/3=0.667≥0.6）→active
    sql("UPDATE memories SET created_at = created_at - interval '7 days' WHERE id=%s", (c1,))
    run2 = http("POST", "/v1/lifecycle/run", {"dry_run": False})
    to_trial = run2["transitions"]["candidate_to_trial"]["ids"]
    check("lifecycle.candidate_to_trial", c1 in to_trial,
          str(run1["transitions"]["candidate_to_trial"]["count"]))
    state = http("GET", f"/v1/memories/{c1}")["ttl_state"]
    check("lifecycle.trial_then_promoted_same_scan", state == "active", state)

    # 5. 负例：3 触发 0 采纳 → 采纳率 0 < 60% 不得转正
    c2 = retain_one("knowledge", f"{marker} 负例条目：无采纳只有触发",
                    "阶段2生命周期自测-负例", "stage2/entry-neg")
    ids.append(c2)
    for _ in range(3):
        http("POST", "/v1/recall", {"query": "无采纳只有触发 负例条目", "bank": "knowledge",
                                    "caller": "stage2-test"})
    sql("UPDATE memories SET created_at = created_at - interval '7 days' WHERE id=%s", (c2,))
    http("POST", "/v1/lifecycle/run", {"dry_run": False})
    s2 = http("GET", f"/v1/memories/{c2}")["ttl_state"]
    check("gate.reject_low_adopt_rate", s2 == "trial", s2)

    # 6. 正例已同轮转正；changelog 断言 admission_gate 细节
    http("POST", "/v1/lifecycle/run", {"dry_run": False})
    s1 = http("GET", f"/v1/memories/{c1}")["ttl_state"]
    cl = sql("SELECT detail FROM changelog WHERE op='lifecycle' AND memory_id=%s "
             "ORDER BY seq DESC LIMIT 3", (c1,))
    check("lifecycle.promoted", s1 == "active", s1)
    check("changelog.admission_gate", any(d["detail"].get("reason") == "admission_gate" for d in cl),
          json.dumps([d["detail"] for d in cl], ensure_ascii=False)[:200])

    # 7. 无触发 → decaying（召回降权 0.7 见 recall.life_factor）；3d 内命中 → 复活
    #    W4（2026-09-19）：窗口=ACTIVE_DECAY_DAYS×类型系数×bank scale（knowledge=1.5），
    #    回拨量按条目实际 memory_type 现算（集中变更点=config，本脚本不另拍数字）。
    c1_mt = http("GET", f"/v1/memories/{c1}").get("memory_type") or "episodic"
    c1_win = lambda base: int(round(base * config.TYPE_DECAY_FACTORS.get(c1_mt, 1.0)
                                    * config.decay_scale_for("knowledge")))
    sql(f"UPDATE memories SET created_at = created_at - interval '{c1_win(config.ACTIVE_DECAY_DAYS) + 1} days' WHERE id=%s", (c1,))
    sql(f"UPDATE access_events SET ts = ts - interval '{c1_win(config.ACTIVE_DECAY_DAYS) + 1} days' WHERE memory_id=%s", (c1,))
    run3 = http("POST", "/v1/lifecycle/run", {"dry_run": False})
    check("lifecycle.decay_90d", c1 in run3["transitions"]["active_to_decaying"]["ids"],
          str(run3["transitions"]["active_to_decaying"]))
    rec = http("POST", "/v1/recall", {"query": q, "bank": "knowledge", "caller": "stage2-test"})
    hit = next((r for r in rec["results"] if r["id"] == c1), None)
    check("recall.decaying_reachable", hit is not None, "decaying 条目仍可召回(降权)")
    run4 = http("POST", "/v1/lifecycle/run", {"dry_run": False})
    check("lifecycle.revive", c1 in run4["transitions"]["decaying_to_active"]["ids"],
          str(run4["transitions"]["decaying_to_active"]))

    # 8. 归档视界无信号 → archived（hidden 不删：行在、recall 不可见）
    #    同轮 scan 内 active→decaying 后 decaying→archived 会连续触发，故两轮取并集断言
    #    W4：回拨=绝对锚定 now()，取 max(decay 窗, archive 窗)+2（§7 的相对回拨一并覆盖）
    d8 = max(c1_win(config.ACTIVE_DECAY_DAYS), c1_win(config.DECAY_ARCHIVE_DAYS)) + 2
    sql(f"UPDATE memories SET created_at = now() - interval '{d8} days' WHERE id=%s", (c1,))
    sql(f"UPDATE access_events SET ts = ts - interval '{d8} days' WHERE memory_id=%s", (c1,))
    runA = http("POST", "/v1/lifecycle/run", {"dry_run": False})
    runB = http("POST", "/v1/lifecycle/run", {"dry_run": False})
    archived_ids = (set(runA["transitions"]["decaying_to_archived"]["ids"])
                    | set(runB["transitions"]["decaying_to_archived"]["ids"]))
    decayed_ids = (set(runA["transitions"]["active_to_decaying"]["ids"])
                   | set(runB["transitions"]["active_to_decaying"]["ids"]))
    check("lifecycle.archive_180d", c1 in archived_ids or (c1 in decayed_ids and c1 in archived_ids),
          f"A={ {k: v['count'] for k, v in runA['transitions'].items()} } "
          f"B={ {k: v['count'] for k, v in runB['transitions'].items()} }")
    still = http("GET", f"/v1/memories/{c1}")
    rec2 = http("POST", "/v1/recall", {"query": q, "bank": "knowledge", "caller": "stage2-test"})
    check("archive.hidden_not_deleted",
          still["ttl_state"] == "archived" and all(r["id"] != c1 for r in rec2["results"]),
          f"state={still['ttl_state']} recall_hits={sum(1 for r in rec2['results'] if r['id']==c1)}")

    # 9. 人工干预转换 + 404/422 防御
    tr = http("POST", "/v1/lifecycle/transition",
              {"memory_id": c2, "action": "promote", "reason": "stage2-selftest"})
    check("transition.manual", tr["to"] == "active", tr)
    code = 0
    try:
        http("POST", "/v1/lifecycle/transition", {"memory_id": str(uuid.uuid4()), "action": "promote"})
    except urllib.error.HTTPError as e:
        code = e.code
    check("transition.404_guard", code == 404, f"code={code}")

    # 10. consolidate：首条 dedup=on 写入，近似变体 dedup=false 强制入库 → 聚合合并 + source_ref 链
    base_content = f"{marker} 整合测试：记忆引擎生命周期由状态机驱动，包含候选试用转正衰减归档五态"
    variants = [base_content,
                base_content + " 另注：转正闸为触发≥3且采纳率≥60%。",
                base_content + " 补充：归档态 hidden 不删除。"]
    vid = [retain_one("knowledge", variants[0], "阶段2整合自测", "stage2/consolidate-src-0")]
    dup = http("POST", "/v1/retain", {
        "bank": "knowledge", "caller": "stage2-test", "dedup": False,
        "items": [{"content": v, "context": "阶段2整合自测", "tags": ["stage2-selftest"],
                   "source_type": "manual", "source_ref": f"stage2/consolidate-dup-{i}",
                   "priority": 3} for i, v in enumerate(variants[1:], start=1)]})
    vid.extend(dup["ids"])
    ids.extend(vid)
    check("retain.dedup_bypass", len(dup["ids"]) == 2, str(dup))
    dry = wait_consolidate(http("POST", "/v1/consolidate",
                                {"days": 1, "sim": 0.85, "dry_run": True})["operation_id"])
    check("consolidate.dry_run_groups", dry.get("progress", {}).get("groups", 0) >= 1,
          json.dumps(dry.get("progress", {}), ensure_ascii=False))
    op = wait_consolidate(http("POST", "/v1/consolidate",
                               {"days": 1, "sim": 0.85, "dry_run": False})["operation_id"])
    check("consolidate.done", op.get("status") == "done", json.dumps(op.get("progress", {})))
    canon, merged_away = None, []
    for m in vid:
        st = http("GET", f"/v1/memories/{m}")["ttl_state"]
        if st != "archived":
            canon = m
        else:
            merged_away.append(m)
    check("consolidate.merged_away_archived", canon is not None and len(merged_away) == len(vid) - 1,
          f"canon={canon} away={merged_away}")
    csrc = http("GET", f"/v1/memories/{canon}")["source_ref"] if canon else ""
    chain_ok = all(x in json.dumps(csrc, ensure_ascii=False) for x in merged_away) if canon else False
    check("consolidate.source_ref_chain", chain_ok, str(csrc)[:200])
    rec3 = http("POST", "/v1/recall", {"query": "生命周期 状态机 候选 试用 转正",
                                       "bank": "knowledge", "caller": "stage2-test"})
    got_ids = [r["id"] for r in rec3["results"]]
    check("consolidate.recall_dedup_visible",
          canon in got_ids and not any(m in got_ids for m in merged_away),
          f"top={got_ids[:5]}")

    # 11. CLI 全命令实测
    rc, out = cli("stats")
    check("cli.stats", rc == 0 and '"states"' in out, f"rc={rc}")
    rc, out = cli("list", "--limit", "3")
    check("cli.list", rc == 0 and "total=" in out, f"rc={rc} {out[:80]}")
    rc, out = cli("search", "生命周期 状态机", "--bank", "knowledge", "--top-k", "3")
    check("cli.search", rc == 0 and "hits=" in out, f"rc={rc} {out[:80]}")
    rc, out = cli("get", canon or c1)
    check("cli.get", rc == 0 and '"id"' in out, f"rc={rc}")
    rc, out = cli("adopt", canon or c1)
    check("cli.adopt", rc == 0 and '"adopt_count"' in out, f"rc={rc} {out[:60]}")
    rc, out = cli("retrain", canon or c1)
    check("cli.retrain", rc == 0 and '"reembedded": true' in out, f"rc={rc} {out[:60]}")
    rc, out = cli("lifecycle", "run", "--dry-run")
    check("cli.lifecycle_run_dry", rc == 0 and "pending" in out, f"rc={rc}")
    rc, out = cli("lifecycle", "history", "--limit", "5")
    check("cli.lifecycle_history", rc == 0 and '"items"' in out, f"rc={rc}")
    ctmp = retain_one("knowledge", f"{marker} CLI删除目标条目", "阶段2CLI自测", "stage2/cli-del")
    ids.append(ctmp)
    rc, out = cli("delete", ctmp)
    check("cli.delete_soft", rc == 0 and '"state": "retired"' in out, f"rc={rc} {out[:60]}")
    rc, out = cli("delete", ctmp, "--force")
    check("cli.delete_force", rc == 0 and '"state": "deleted"' in out, f"rc={rc} {out[:60]}")
    ids.remove(ctmp)
    rc, out = cli("transition", c2, "archive", "--reason", "cli-selftest")
    check("cli.transition", rc == 0 and '"to": "archived"' in out, f"rc={rc} {out[:60]}")

    # 12. WAL 滚动清理（1 段 < keep=168，应零删除并记录 engine_meta）
    from memory_engine import lifecycle as lcmod
    w = lcmod.wal_cleanup()
    meta = sql("SELECT value FROM engine_meta WHERE key='last_wal_cleanup'")
    check("wal.cleanup_recorded", bool(w["deleted"] == 0 and meta and meta[0]["value"]["ts"] == w["ts"]),
          json.dumps(w, ensure_ascii=False))

    # 13. 清理测试条目（changelog 留痕）
    for m in set(ids):
        try:
            http("DELETE", f"/v1/memories/{m}?purge=true")
        except Exception:
            pass

    failed = [k for k, v in RESULTS.items() if not v["ok"]]
    out = {"ts": datetime.now(timezone.utc).isoformat(), "port": config.PORT,
           "total": len(RESULTS), "failed": len(failed), "checks": RESULTS}
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_stage2.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n=== stage2: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS, failed={failed} ===")
    return 1 if failed else 0


if __name__ == "__main__":
    import urllib.error
    sys.exit(main())
