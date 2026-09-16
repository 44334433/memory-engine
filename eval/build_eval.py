"""评测集构建（方法脚本）：从旧版记忆库 L1 语料抽「当时需要记忆X」场景题。

数据集说明：题目与 gold 均源自真实生产记忆内容，评测数据集因含真实生产内容不公开；
本脚本公开的是构建方法论。复现者可用自有语料按同构方法构造评测集（≥30 题）。

方法：
1. 来源=旧版记忆库 L1 records（每条含当时场景上下文），按场景多样性每场景取 1 条 gold；
2. 交叉验证：session_key → 会话库 sessions 在档（真实会话历史证明），产出 session_verified 字段；
3. 每题 = {query: 当时场景上下文, gold: 该场景下真实被记住的记忆(record_id 反查)}。
产出：eval/memory_recall_eval.jsonl（不随仓库分发）

运行：MEMORY_EVAL_LEGACY_DB=... MEMORY_EVAL_STATE_DB=... python3 eval/build_eval.py
"""
import json
import os
import sqlite3
import sys

LEGACY_DB = os.environ.get("MEMORY_EVAL_LEGACY_DB", "")   # 旧版记忆库 sqlite（必填）
STATE_DB = os.environ.get("MEMORY_EVAL_STATE_DB", "")     # 会话库 sqlite（可选，交叉验证用）
OUT = os.environ.get("MEMORY_EVAL_OUT", "eval/memory_recall_eval.jsonl")
N_QUESTIONS = 36          # ≥30 题量线
MIN_CONTENT = 40          # gold 内容最短长度（过滤碎片段）


def main() -> int:
    if not LEGACY_DB or not os.path.exists(LEGACY_DB):
        print("FATAL: 设置 MEMORY_EVAL_LEGACY_DB 指向旧版记忆库 sqlite（l1_records 表）", file=sys.stderr)
        return 2
    td = sqlite3.connect(f"file:{LEGACY_DB}?mode=ro", uri=True)
    td.row_factory = sqlite3.Row
    rows = td.execute(
        """SELECT record_id, content, type, priority, scene_name, session_key,
                  created_time, timestamp_str
           FROM l1_records WHERE length(content) >= ? AND scene_name <> ''
           ORDER BY priority DESC, created_time DESC""",
        (MIN_CONTENT,)).fetchall()
    print(f"candidate records: {len(rows)}")

    # 每 scene 取 1 条 gold（多样性：不同场景不重复）；scene 名需与 gold 内容有区分度
    seen_scenes, picked = set(), []
    for r in rows:
        scene = (r["scene_name"] or "").strip()
        if not scene or scene in seen_scenes or len(scene) < 8:
            continue
        # 泄漏防护：scene 文本几乎原样出现在 content 里则换下一题仍可，gold 本身不剔除
        seen_scenes.add(scene)
        picked.append(dict(r))
        if len(picked) >= N_QUESTIONS:
            break

    # 交叉验证：session_key 在会话库 sessions 在档（真实会话历史证明）
    verified = 0
    if STATE_DB and os.path.exists(STATE_DB):
        st = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
        for p in picked:
            row = st.execute("SELECT id, title FROM sessions WHERE id=?",
                             (p["session_key"],)).fetchone()
            p["session_verified"] = bool(row)
            p["session_title"] = (row[1][:60] if row and row[1] else None)
            verified += bool(row)
        st.close()
    else:
        for p in picked:
            p["session_verified"] = False
            p["session_title"] = None
    print(f"session cross-verified: {verified}/{len(picked)}")

    with open(OUT, "w") as f:
        for i, p in enumerate(picked):
            item = {
                "qid": f"q{i:03d}",
                "query": p["scene_name"].strip(),          # 真实上下文（当时需要记忆X的场景）
                "gold_id": p["record_id"],                  # 对应记忆（旧库 record_id，双引擎同源）
                "gold_excerpt": p["content"][:120],
                "gold_type": p["type"],
                "source_ref": p["record_id"],
                "session_key": p["session_key"],
                "session_verified": p["session_verified"],
                "session_title": p["session_title"],
                "created_time": p["created_time"],
            }
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"eval set written: {OUT} ({len(picked)} questions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
