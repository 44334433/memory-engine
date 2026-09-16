#!/usr/bin/env python3
"""flat_engine 分题型 session 级指标（复用 eval_retrieval 实现与官方计分）。"""
import json
import os
import sys

LME_DIR = os.path.expanduser("~/.hermes/memory-engine/eval/lme")
sys.path.insert(0, LME_DIR)
sys.path.insert(0, "/media/qq/Linux/HermesArchive/longmemeval/LongMemEval/src/retrieval")
import eval_retrieval as ER  # noqa: E402
import numpy as np  # noqa: E402


def main() -> int:
    questions = [json.loads(l) for l in open(f"{LME_DIR}/questions.jsonl", encoding="utf-8")]
    corpus = {}
    for l in open(f"{LME_DIR}/turns.jsonl", encoding="utf-8"):
        t = json.loads(l)
        corpus[t["doc_id"]] = t["content"]
    gold = {q["question_id"]: q["gold_turn_ids"] for q in questions}
    q_corpus_turns = {q["question_id"]: q["turn_ids"] for q in questions}

    by_type = {}
    for q in questions:
        if "_abs" in q["question_id"]:
            continue
        cids = q_corpus_turns[q["question_id"]]
        cdocs = gold[q["question_id"]]
        if not cdocs:
            continue
        ranked = ER.engine_recall(q["question"], q["question_id"])
        idx_of = {d: i for i, d in enumerate(cids)}
        tr = [idx_of[d] for d in ranked if d in idx_of]
        ra, rl, nd = ER.evaluate_retrieval_turn2session(tr, cdocs, cids, k=5)
        _, rl10, _ = ER.evaluate_retrieval_turn2session(tr, cdocs, cids, k=10)
        by_type.setdefault(q["question_type"], []).append((ra, rl, nd, rl10))

    table = {}
    for t, vals in sorted(by_type.items()):
        table[t] = {"n": len(vals),
                    "recall_all@5": round(float(np.mean([v[1] for v in vals])), 4),
                    "recall_all@10": round(float(np.mean([v[3] for v in vals])), 4),
                    "ndcg_any@5": round(float(np.mean([v[2] for v in vals])), 4)}
    json.dump(table, open(f"{LME_DIR}/flat_by_type.json", "w"), indent=2, ensure_ascii=False)
    print(json.dumps(table, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
