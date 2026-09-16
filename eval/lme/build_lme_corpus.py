#!/usr/bin/env python3
"""LongMemEval-S 语料构建（严格对齐官方 run_retrieval.process_item_flat_index turn 粒度口径）。

官方口径（LongMemEval@9e0b455 src/retrieval/run_retrieval.py）：
- 语料 doc = 每个 user turn，doc id = <session_id>_<turn_no(1-based)>
- answer session（session_id 含 'answer'）的 user turn：has_answer=True → 保留 answer_ 前缀；
  has_answer=False → 前缀改写为 noans_
- 非 answer session 的 user turn：id 原样（不含 answer 字样）
- gold = corpus_ids 中含 'answer' 的 doc
- query = entry['question']；指标对 k∈{1,3,5,10,30,50} 取 recall_any/recall_all/ndcg_any

产出：
  ~/.hermes/memory-engine/eval/lme/questions.jsonl  每题一行（含 turn_ids 供评测）
  ~/.hermes/memory-engine/eval/lme/turns.jsonl      每个 user turn 一行（供灌库）
"""
import json
import os
import sys

SRC = "/media/qq/Linux/HermesArchive/longmemeval/hf-cleaned/longmemeval_s_cleaned.json"
OUT_DIR = os.path.expanduser("~/.hermes/memory-engine/eval/lme")
os.makedirs(OUT_DIR, exist_ok=True)


def main() -> int:
    data = json.load(open(SRC))
    print(f"loaded {len(data)} questions")

    stats = {
        "n_questions": len(data),
        "n_abs": 0,
        "qtypes": {},
        "n_sessions_total": 0,
        "n_turns_total": 0,
        "n_user_turns": 0,
        "n_gold_turns": 0,
        "n_noans_flagged": 0,
        "turns_per_q": [],
        "chars_user_turns": 0,
        "max_user_turn_chars": 0,
    }
    n_q = 0
    with open(f"{OUT_DIR}/questions.jsonl", "w") as fq, open(f"{OUT_DIR}/turns.jsonl", "w") as ft:
        for entry in data:
            qid = entry["question_id"]
            stats["n_abs"] += 1 if "_abs" in qid else 0
            stats["qtypes"][entry["question_type"]] = stats["qtypes"].get(entry["question_type"], 0) + 1

            turn_ids, gold_turn_ids = [], []
            for sess_id, sess in zip(entry["haystack_session_ids"], entry["haystack_sessions"]):
                stats["n_sessions_total"] += 1
                is_ans_sess = "answer" in sess_id
                sess_date = None
                for s_id, d in zip(entry["haystack_session_ids"], entry["haystack_dates"]):
                    if s_id == sess_id:
                        sess_date = d
                        break
                for i, turn in enumerate(sess):
                    stats["n_turns_total"] += 1
                    if turn["role"] != "user":
                        continue
                    stats["n_user_turns"] += 1
                    if is_ans_sess:
                        assert "has_answer" in turn, f"{qid} answer session turn lacks has_answer"
                        if turn["has_answer"]:
                            doc_id = f"{sess_id}_{i+1}"
                            gold_turn_ids.append(doc_id)
                            stats["n_gold_turns"] += 1
                        else:
                            doc_id = (sess_id + "_" + str(i + 1)).replace("answer", "noans")
                            stats["n_noans_flagged"] += 1
                    else:
                        doc_id = f"{sess_id}_{i+1}"
                    turn_ids.append(doc_id)
                    c = turn["content"]
                    stats["chars_user_turns"] += len(c)
                    stats["max_user_turn_chars"] = max(stats["max_user_turn_chars"], len(c))
                    ft.write(json.dumps({
                        "doc_id": doc_id,
                        "qid": qid,
                        "session_id": sess_id,
                        "is_gold": doc_id in gold_turn_ids,
                        "session_date": sess_date,
                        "content": c,
                    }, ensure_ascii=False) + "\n")
            stats["turns_per_q"].append(len(turn_ids))
            fq.write(json.dumps({
                "question_id": qid,
                "question_type": entry["question_type"],
                "question": entry["question"],
                "answer": entry["answer"],
                "question_date": entry["question_date"],
                "answer_session_ids": entry["answer_session_ids"],
                "haystack_session_ids": entry["haystack_session_ids"],
                "turn_ids": turn_ids,
                "gold_turn_ids": gold_turn_ids,
                "n_gold_sessions": len(entry["answer_session_ids"]),
            }, ensure_ascii=False) + "\n")
            n_q += 1
            if n_q % 100 == 0:
                print(f"processed {n_q}")

    tpq = stats.pop("turns_per_q")
    stats["turns_per_q_avg"] = round(sum(tpq) / len(tpq), 1)
    stats["turns_per_q_max"] = max(tpq)
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    json.dump(stats, open(f"{OUT_DIR}/corpus_stats.json", "w"), indent=2, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
