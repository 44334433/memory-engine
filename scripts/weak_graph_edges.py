#!/usr/bin/env python3
"""弱图边生成（P1 第二批 ③）：domain/owner/tags 同值 → candidate related 边。

设计口径：
- 「弱图」= 用现有结构化字段同值关系建候选关联边（无 LLM 成本），入 edges 表供第四路图召回拉回；
- 限流：组内每条记忆最多连 K 个近邻 peer（按 updated_at DESC 取近邻），每组最多参与 GROUP_CAP 条
  （防止 general 大组 O(n²) 爆边）；全局 --max-edges 硬上限；
- 去重：脚本内 pair 集合去重 + DB 层 uq_edge_* 现行三元组唯一索引 ON CONFLICT DO NOTHING
  （重跑幂等，只补缺失边）；
- 边属性：etype=related，source=weak_graph:<dim>，valid_at=src 记忆 valid_at（事件时间对齐）；
- 遍历为无向（图召回 CTE 双向拉边），故只存单向 edge 即可达双向。

用法（默认 dry-run）：
  python3 scripts/weak_graph_edges.py                # 统计将生成的边
  python3 scripts/weak_graph_edges.py --apply        # 实际写入 edges 表
  python3 scripts/weak_graph_edges.py --apply --dims domain,tags --k 3 --max-edges 30000
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memory_engine import config, db  # noqa: E402
import psycopg  # noqa: E402

DIMS = ("domain", "owner", "tags")

# 现行、未被治理淘汰的记忆才参与建边（archived/retired/历史版本不拉边）
POOL_SQL = """
SELECT id, {key} AS key, valid_at
FROM memories
WHERE is_current AND ttl_state NOT IN ('archived', 'retired') AND {key} IS NOT NULL
ORDER BY updated_at DESC
"""


def _rows_for_dim(conn, dim: str) -> list[dict]:
    if dim == "tags":
        # jsonb 数组展开：一个 tag 一组
        return db.fetch_all(conn, """
            SELECT m.id, t.tag AS key, m.valid_at
            FROM memories m, jsonb_array_elements_text(m.tags) AS t(tag)
            WHERE m.is_current AND m.ttl_state NOT IN ('archived', 'retired')
            ORDER BY m.updated_at DESC""")
    return db.fetch_all(conn, POOL_SQL.format(key=dim))


def build_pairs(rows: list[dict], k: int, group_cap: int) -> list[tuple]:
    """组内近邻配对：每条记忆连同组最近更新的 K 个 peer（去重成 pair 集）。"""
    groups: dict = {}
    for r in rows:
        groups.setdefault(r["key"], []).append(r)
    pairs: set = set()
    for members in groups.values():
        members = members[:group_cap]           # 大组截断（限流）
        for i, a in enumerate(members):
            for b in members[i + 1: i + 1 + k]:  # 近邻窗口
                if a["id"] == b["id"]:
                    continue
                # 无向边存单向（src=组内更新更晚者→peer），遍历侧双向可达
                pairs.add((str(a["id"]), str(b["id"]), str(a["valid_at"] or b["valid_at"])))
    return sorted(pairs)


def main() -> int:
    ap = argparse.ArgumentParser(description="弱图 candidate related 边生成（dry-run 默认）")
    ap.add_argument("--apply", action="store_true", help="实际写入 edges 表（默认 dry-run）")
    ap.add_argument("--dims", default="domain,owner,tags", help="参与维度（逗号分隔）")
    ap.add_argument("--k", type=int, default=config.WEAK_GRAPH_K, help="组内每记忆连边数上限")
    ap.add_argument("--group-cap", type=int, default=config.WEAK_GRAPH_GROUP_CAP, help="每组参与成员上限")
    ap.add_argument("--max-edges", type=int, default=50000, help="单次运行写入边数硬上限（限流）")
    ap.add_argument("--batch", type=int, default=2000, help="每事务批大小")
    args = ap.parse_args()

    dims = [d.strip() for d in args.dims.split(",") if d.strip() in DIMS]
    if not dims:
        print(f"--dims 仅允许 {DIMS}")
        return 2

    with psycopg.connect(config.PG_DSN, autocommit=True) as conn:
        stats, total_written = {}, 0
        for dim in dims:
            rows = _rows_for_dim(conn, dim)
            pairs = build_pairs(rows, args.k, args.group_cap)
            stats[dim] = {"members": len(rows), "pairs": len(pairs)}
            print(f"[{dim}] members={len(rows)} candidate_pairs={len(pairs)}")
            if not args.apply or not pairs:
                continue
            budget = max(0, args.max_edges - total_written)   # 全局限流：单次运行写入上限
            todo = pairs[:budget]
            written = 0
            for i in range(0, len(todo), args.batch):
                chunk = todo[i: i + args.batch]
                with conn.transaction():
                    for src, dst, vat in chunk:
                        # None=三元组已存在（ON CONFLICT 幂等，重跑只补缺失边）
                        if db.insert_edge(conn, src, dst_mid=dst, etype="related",
                                          valid_at=vat, source=f"weak_graph:{dim}"):
                            written += 1
                print(f"  ... {min(i + len(chunk), len(todo))}/{len(todo)} new_edges={written}")
            stats[dim]["written"] = written
            total_written += written
        print(json.dumps({"apply": args.apply, "total_written": total_written, "stats": stats},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
