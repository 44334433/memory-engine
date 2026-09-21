#!/usr/bin/env python3
"""时序边失效回填评估（多跳批 件3，2026-09-21）：只读评估 + 变更清单；--apply 显式执行。

背景：edges 表 13,788 条现行边 invalid_at 全 NULL（时序字段存在未使用）。本脚本评估
「源记忆已 tombstone」的过期边并给出失效回填清单：

  T1 源/目标记忆已 retired（用户删除软 tombstone）→ invalid_at = 记忆的 invalid_at
     （superseded 时刻）或 updated_at（retire 落点）——retired 无删除时间列，updated_at
     为最接近事件时间（诚实近似，报告标注）。
  T2 端点记忆被 supersede（is_current=false 且未 retired，即版本已被取代）→ 边随旧版本
     失效，invalid_at = 该旧版本 invalid_at。

纪律（本批拍板）：默认 dry-run 只读评估，变更清单落 CSV 供回投确认；--apply 才写库。
写动作仅置位 invalid_at（且仅当仍为 NULL），绝不 DELETE 边——与记忆侧双时序铁律同构。
G15 不触碰：本脚本只处理 tombstone/supersede 两类确定性事件，绝不基于 contradicts 边推断失效。

用法：
  /usr/bin/python3.12 scripts/edge_temporal_audit.py                 # 只读评估（默认）
  /usr/bin/python3.12 scripts/edge_temporal_audit.py --out /tmp/edge_invalid_backfill.csv
  /usr/bin/python3.12 scripts/edge_temporal_audit.py --apply         # 确认后执行（另批）
"""
import argparse
import csv
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from memory_engine import config, db  # noqa: E402
import psycopg  # noqa: E402

# 候选过期边：端点 retired（T1）或端点 superseded 非现行（T2）；只取现行边（invalid_at IS NULL）
CANDIDATE_SQL = """
SELECT e.id AS edge_id, e.src_mid, e.dst_mid, e.entity_id, e.etype, e.source, e.valid_at,
       CASE
         WHEN ms.ttl_state='retired' OR md.ttl_state='retired' THEN 'T1_retired_endpoint'
         ELSE 'T2_superseded_endpoint'
       END AS reason,
       COALESCE(
         (CASE WHEN ms.ttl_state='retired' OR NOT ms.is_current
               THEN COALESCE(ms.invalid_at, ms.updated_at) END),
         (CASE WHEN md.ttl_state='retired' OR (md.id IS NOT NULL AND NOT md.is_current)
               THEN COALESCE(md.invalid_at, md.updated_at) END),
         now()
       ) AS proposed_invalid_at
FROM edges e
LEFT JOIN memories ms ON ms.id = e.src_mid
LEFT JOIN memories md ON md.id = e.dst_mid
WHERE e.invalid_at IS NULL
  AND ( ms.ttl_state='retired' OR NOT ms.is_current
     OR (md.id IS NOT NULL AND (md.ttl_state='retired' OR NOT md.is_current)) )
ORDER BY e.valid_at, e.id
"""

APPLY_SQL = ("UPDATE edges SET invalid_at = %s::timestamptz "
             "WHERE id = %s::uuid AND invalid_at IS NULL")


def main() -> int:
    ap = argparse.ArgumentParser(description="时序边失效回填评估（默认只读）")
    ap.add_argument("--apply", action="store_true",
                    help="实际置位 invalid_at（默认 dry-run 只出清单——本批拍板：变更需回投确认）")
    ap.add_argument("--out", default="/tmp/edge_invalid_backfill.csv",
                    help="变更清单 CSV 输出路径")
    args = ap.parse_args()

    conn = psycopg.connect(config.PG_DSN, autocommit=True, connect_timeout=3)
    total = db.fetch_one(conn, "SELECT count(*) c FROM edges")["c"]
    current = db.fetch_one(conn, "SELECT count(*) c FROM edges WHERE invalid_at IS NULL")["c"]
    already = total - current
    rows = db.fetch_all(conn, CANDIDATE_SQL)
    t1 = [r for r in rows if r["reason"] == "T1_retired_endpoint"]
    t2 = [r for r in rows if r["reason"] == "T2_superseded_endpoint"]

    print(f"edges 总数={total} 现行={current} 已失效={already}")
    print(f"候选过期边={len(rows)}（T1 端点retired={len(t1)} / T2 端点superseded={len(t2)}）")
    for r in rows[:20]:
        print(f"  {r['reason']} edge={r['edge_id']} etype={r['etype']} src={str(r['src_mid'])[:8]}"
              f" dst={str(r['dst_mid'])[:8] if r['dst_mid'] else 'ent'}"
              f" valid_at={r['valid_at']:%F} → invalid_at={r['proposed_invalid_at']:%F}")

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["edge_id", "reason", "src_mid", "dst_mid", "entity_id", "etype",
                    "source", "valid_at", "proposed_invalid_at"])
        for r in rows:
            w.writerow([r["edge_id"], r["reason"], r["src_mid"], r["dst_mid"], r["entity_id"],
                        r["etype"], r["source"], r["valid_at"].isoformat(),
                        r["proposed_invalid_at"].isoformat()])
    print(f"变更清单: {args.out}（{len(rows)} 行）")

    if not args.apply:
        print("DRY-RUN（只读评估）——加 --apply 且经回投确认后执行置位")
        return 0
    done = 0
    with conn.transaction():
        for r in rows:
            db.execute(conn, APPLY_SQL, (r["proposed_invalid_at"], r["edge_id"]))
            done += 1
    left = db.fetch_one(conn, "SELECT count(*) c FROM edges WHERE invalid_at IS NULL")["c"]
    print(f"APPLY 完成：提交置位 {done} 条；现行边 {current} → {left}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
