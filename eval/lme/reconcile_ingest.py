#!/usr/bin/env python3
"""灌库完成后的权威对账：turns.jsonl doc_id 集合 vs DB source_ref 集合，输出缺失清单。

用法：python3 reconcile_ingest.py
"""
import json
import os
import subprocess

LME_DIR = os.path.expanduser("~/.hermes/memory-engine/eval/lme")


def main() -> int:
    doc_ids = []
    for l in open(f"{LME_DIR}/turns.jsonl", encoding="utf-8"):
        doc_ids.append(json.loads(l)["doc_id"])
    want = set(doc_ids)
    out = subprocess.run(
        ["psql", "-h", "127.0.0.1", "-p", "5434", "-U", "memengine", "-d", "memengine_lme", "-tAc",
         "select source_ref from memories where source_type='eval' and bank='hermes-sessions';"],
        capture_output=True, text=True)
    have = set(x for x in out.stdout.strip().split("\n") if x)
    missing = want - have
    extra = have - want
    # 缺失条目中是否有 gold（影响 recall 上限）
    gold_miss = []
    for l in open(f"{LME_DIR}/questions.jsonl", encoding="utf-8"):
        q = json.loads(l)
        hit = [d for d in q["gold_turn_ids"] if d in missing]
        if hit:
            gold_miss.append({"qid": q["question_id"], "gold_missing": hit})
    result = {"n_expected": len(want), "n_in_db": len(have), "n_missing": len(missing),
              "missing": sorted(missing), "n_extra": len(extra), "extra": sorted(extra)[:10],
              "gold_questions_affected": gold_miss}
    json.dump(result, open(f"{LME_DIR}/ingest_reconcile.json", "w"), indent=2, ensure_ascii=False)
    print(json.dumps({k: v for k, v in result.items() if k not in ("missing", "extra")},
                     indent=2, ensure_ascii=False))
    print("missing list:", sorted(missing)[:10])
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
