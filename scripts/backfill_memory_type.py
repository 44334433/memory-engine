"""W2 存量 memory_type 回填打标（2026-09-18）。

启发式唯一真源 = src/memory_engine/memory_type.py（与 retain 自动打标同源）。
规则（任务口径）：含代码/命令/规则/coding 词 → procedural；事实/定义 → semantic；
其余 → episodic。procedural 优先于 semantic（描述操作规程的文本常同时含定义词）。

用法：
  python3 scripts/backfill_memory_type.py             # dry-run（默认）：只出分布报告，零写入
  python3 scripts/backfill_memory_type.py --apply     # 落库：仅提升 memory_type='episodic' 的行

安全语义：
- 只从 episodic（缺省值）提升 → 人工 PATCH 或 retain 显式指定的标签永不被覆盖；
- UPDATE 不触碰 updated_at（防止回填把 time 路排序/衰减锚整体冲刷）；
- 幂等：二次 --apply 分布不变（episodic 之外的行不在 UPDATE 集合）；
- 不写逐行 changelog：整库级维护动作，逐行 op 会洪水化 changelog/新鲜度游标；
  汇总摘要写 engine_meta(key='memory_type_backfill') + 报告 JSON 落 state/。
"""
import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from memory_engine import config, db, memory_type as mtype  # noqa: E402
import psycopg  # noqa: E402

BATCH = 2000


def scan_and_report(conn) -> tuple[dict, list]:
    """全量扫一遍：返回 (分布报告, 需更新的 [(id, memory_type)])。只读，零写入。"""
    dist, by_rule, by_type_bank = Counter(), Counter(), Counter()
    samples: dict = {}
    updates: list = []
    cursor_seq, n = 0, 0
    while True:
        rows = db.fetch_all(
            conn,
            "SELECT id, seq, bank, title, body FROM memories WHERE seq > %s ORDER BY seq LIMIT %s",
            (cursor_seq, BATCH))
        if not rows:
            break
        for r in rows:
            n += 1
            cursor_seq = int(r["seq"])
            label, rule = mtype.classify_with_reason(f"{r['title'] or ''}\n{r['body'] or ''}")
            dist[label] += 1
            by_rule[rule] += 1
            by_type_bank[f"{label}/{r['bank']}"] += 1
            key = f"{label}/{rule}"
            if len(samples.setdefault(key, [])) < 3:
                samples[key].append({"id": str(r["id"]), "bank": r["bank"],
                                     "title": (r["title"] or "")[:80]})
            if label != config.MEMORY_TYPE_DEFAULT:
                updates.append((str(r["id"]), label))
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "total_scanned": n,
        "distribution": dict(dist),
        "by_rule": dict(by_rule),
        "by_type_bank": dict(by_type_bank),
        "samples": samples,
    }, updates


def main() -> int:
    ap = argparse.ArgumentParser(description="W2 memory_type 存量回填（dry-run 默认）")
    ap.add_argument("--apply", action="store_true", help="落库（默认 dry-run 只出报告）")
    args = ap.parse_args()

    t0 = time.perf_counter()
    with psycopg.connect(config.PG_DSN, autocommit=True) as conn:
        report, updates = scan_and_report(conn)
        report["would_update"] = len(updates)
        if args.apply and updates:
            with conn.cursor() as cur:
                # 仅提升仍为 episodic 的行（双保险：分类快照与落库之间若有并发 retain/PATCH 也不覆盖）
                cur.executemany(
                    "UPDATE memories SET memory_type = %s "
                    "WHERE id = %s::uuid AND memory_type = 'episodic'",
                    [(label, mid) for mid, label in updates])
            report["final_distribution"] = {
                r["memory_type"]: r["c"] for r in db.fetch_all(
                    conn, "SELECT memory_type, count(*) c FROM memories GROUP BY memory_type")}
            db.execute(
                conn, "INSERT INTO engine_meta(key,value) VALUES ('memory_type_backfill',%s::jsonb) "
                      "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (json.dumps(report, ensure_ascii=False, default=str),))
        report["applied"] = bool(args.apply)
        report["took_s"] = round(time.perf_counter() - t0, 1)

    out = Path(os.environ.get("MEMORY_ENGINE_STATE_DIR",
                              str(Path(__file__).resolve().parent.parent / "state"))) / \
        "memory_type_backfill_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in
                      ("ts", "total_scanned", "distribution", "by_rule", "would_update",
                       "applied", "took_s", "final_distribution") if k in report},
                     ensure_ascii=False, indent=2))
    print(f"报告落盘: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
