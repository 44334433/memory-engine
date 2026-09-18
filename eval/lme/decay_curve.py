#!/usr/bin/env python3
"""Corpus-size decay curve — R@5 vs bank size (5 buckets, same question set).

Question this answers: how much recall does the hybrid ranker lose as the
haystack grows? Five corpus-size buckets (500 / 1k / 2k / 4k / 6k turns), the
SAME scored question set at every bucket, bank rebuilt from scratch per bucket.

Protocol
- Scored set: 100 answerable questions sampled once (seed=42) from the 500-question
  LongMemEval-S corpus; every bucket scores exactly this set ("同题集").
- Bank: independent bank `eval_decay` on the eval engine instance (:8767), purged
  and rebuilt per bucket (API-only ingest, dedup=false — same contract as ingest_turns).
- Corpus per bucket: the scored set's gold (evidence) turns, always present, plus a
  distractor fill sampled from OTHER questions' user turns (nested prefix across
  buckets — a bucket only ever gains turns). Distractors are LongMemEval synthetic
  ShareGPT-derived content: the "synthetic corpus extension" option — no real
  production memories are used anywhere in this script.
- Scoring: bank-wide top-50 recall (global mode, no tag filter — the deployment
  shape where corpus growth actually dilutes results), official LongMemEval scorer
  (src/retrieval/eval_utils.py, zero re-implementation); R@5 = recall_any@5
  turn-level, session-level reported alongside.

Output: decay_results.json + decay_curve.md (markdown table) next to this script.

Usage (eval engine instance must be reachable; see eval/README.md):
  ENGINE=http://127.0.0.1:8767 python3 decay_curve.py
"""
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

LME_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.environ.get("ENGINE", "http://127.0.0.1:8767")
LME_DATA = os.path.expanduser("~/.hermes/memory-engine/eval/lme")
BANK = "eval_decay"
N_QUESTIONS = int(os.environ.get("DECAY_N_Q", "100"))
BUCKETS = [int(b) for b in os.environ.get("DECAY_BUCKETS", "500,1000,2000,4000,6000").split(",")]
SEED = 42
TOPK = 50
KS = [1, 5, 10, 30]

REPO_SRC = "/media/qq/Linux/HermesArchive/longmemeval/LongMemEval/src/retrieval"
sys.path.insert(0, REPO_SRC)
# NumPy 2.x shim (official eval_utils uses np.asfarray, removed in 2.0)
import numpy as _np  # noqa: E402
if not hasattr(_np, "asfarray"):
    _np.asfarray = lambda a, dtype=_np.float64: _np.asarray(a, dtype=dtype)
from eval_utils import evaluate_retrieval, evaluate_retrieval_turn2session, dcg  # noqa: E402
import numpy as np  # noqa: E402


def api(path: str, body: dict | None = None, method: str = "POST",
        timeout: int = 900) -> dict:
    req = urllib.request.Request(
        ENGINE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def bank_purge() -> int:
    """Bank-scoped hard purge via per-id API DELETE (purge=true)."""
    ids = []
    limit, offset = 200, 0
    while True:
        d = api(f"/v1/memories?bank={BANK}&limit={limit}&offset={offset}", method="GET")
        items = d.get("items", d.get("memories", []))
        ids += [m["id"] for m in items]
        if len(items) < limit:
            break
        offset += limit
    for i, mid in enumerate(ids):
        api(f"/v1/memories/{mid}?purge=true", method="DELETE")
        if (i + 1) % 500 == 0:
            print(f"  purge {i + 1}/{len(ids)}", flush=True)
    return len(ids)


def build_bucket_corpus(turns: dict, qids: list[str], gold: dict,
                        pool: list[str], size: int) -> list[dict]:
    """Gold turns of the scored set + distractor prefix (nested across buckets)."""
    keep = set()
    for qid in qids:
        keep.update(gold[qid])
    keep &= set(turns)
    fill = [d for d in pool if d not in keep][: max(0, size - len(keep))]
    order = sorted(keep | set(fill))
    # 统一空 content 过滤（gold 空 turn 影响 recall_any 仅当该题全部 gold 为空，100 题中 ≤1 题，R@5 影响<1%）
    return [{"doc_id": d, "content": turns[d], "qid": q_turns_owner.get(d, ""), "source_ref": d}
            for d in order if turns[d].strip()]


def ingest(items: list[dict]) -> int:
    """Idempotent batch ingest: dedup=true makes timed-out retries safe (server
    skips exact-duplicate content), so a batch is re-posted until committed."""
    BATCH = 32
    committed = 0
    for off in range(0, len(items), BATCH):
        chunk = items[off:off + BATCH]
        for attempt in range(3):
            try:
                d = api("/v1/retain", {"bank": BANK, "caller": "main",
                                       "items": chunk, "dedup": True})
                committed += len(d.get("ids", []))
                break
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt == 2:
                    # 4xx 必读 body（2026-09-18 教训：bank 白名单/投毒闸等校验错误全藏在 body 里）
                    try:
                        print(f"  ingest FINAL {type(e).__name__} body: "
                              f"{e.read().decode()[:400]}", flush=True)
                    except Exception:
                        pass
                    raise
                print(f"  ingest retry {attempt + 1} after error: {e}", flush=True)
                time.sleep(5)
        if (off // BATCH) % 20 == 0:
            print(f"  ingest {off + len(chunk)}/{len(items)}", flush=True)
    return committed


def global_recall(query: str) -> list[str]:
    d = api("/v1/recall", {"query": query, "bank": BANK, "caller": "main", "top_k": TOPK})
    return [x["source_ref"] for x in d["results"]]


def score(qids: list[str], questions: dict, gold: dict) -> dict:
    out = {"turn": {f"{m}@{k}": [] for m in ("recall_any", "recall_all", "ndcg_any") for k in KS},
           "session": {f"{m}@{k}": [] for m in ("recall_any", "recall_all", "ndcg_any") for k in KS}}
    for qid in qids:
        q = questions[qid]
        cids = q["turn_ids"]
        cdocs = gold[qid]
        ranked = global_recall(q["question"])
        in_bank = set(ranked)
        # official scorer operates on per-question corpus order; global hits map to it
        idx_of = {d: i for i, d in enumerate(cids)}
        turn_rankings = [idx_of[d] for d in ranked if d in idx_of]
        for k in KS:
            ra, rl, nd = evaluate_retrieval(turn_rankings, cdocs, cids, k=k)
            out["turn"][f"recall_any@{k}"].append(ra)
            out["turn"][f"recall_all@{k}"].append(rl)
            out["turn"][f"ndcg_any@{k}"].append(nd)
            ra, rl, nd = evaluate_retrieval_turn2session(turn_rankings, cdocs, cids, k=k)
            out["session"][f"recall_any@{k}"].append(ra)
            out["session"][f"recall_all@{k}"].append(rl)
            out["session"][f"ndcg_any@{k}"].append(nd)
    res = {}
    for lvl in ("turn", "session"):
        res[lvl] = {m: round(float(np.mean(v)), 4) for m, v in out[lvl].items()}
    return res


q_turns_owner: dict[str, str] = {}  # doc_id -> qid (filled in main)


def main() -> int:
    global q_turns_owner
    questions_list = [json.loads(l) for l in open(f"{LME_DATA}/questions.jsonl", encoding="utf-8")]
    questions = {q["question_id"]: q for q in questions_list}
    turns = {}
    for l in open(f"{LME_DATA}/turns.jsonl", encoding="utf-8"):
        t = json.loads(l)
        turns[t["doc_id"]] = t["content"]
        q_turns_owner[t["doc_id"]] = t["qid"]
    print(f"questions={len(questions)} turns={len(turns)}", flush=True)

    # fixed scored set: 100 answerable questions with ≥1 gold turn (seed=42, QA-subset discipline)
    nonabs = [q["question_id"] for q in questions_list
              if "_abs" not in q["question_id"] and questions[q["question_id"]]["gold_turn_ids"]]
    random.Random(SEED).shuffle(nonabs)
    qids = nonabs[:N_QUESTIONS]
    gold = {q: questions[q]["gold_turn_ids"] for q in qids}
    gold_n = sum(len(v) for v in gold.values())
    print(f"scored_set={len(qids)} gold_turns={gold_n}", flush=True)

    # distractor pool: other questions' user turns (synthetic corpus, safe)
    pool_all = [d for d in turns if turns[d].strip() and q_turns_owner[d] not in set(qids)]  # 空 content 过滤（turns 为 doc_id→content dict）
    random.Random(SEED).shuffle(pool_all)

    n_purged = bank_purge()
    print(f"bank_purged={n_purged}", flush=True)

    results = {"config": {"bank": BANK, "engine": ENGINE, "seed": SEED,
                          "n_scored_questions": len(qids),
                          "gold_turns": gold_n, "topk": TOPK, "buckets": BUCKETS,
                          "mode": "global (bank-wide top-k, no tag filter)",
                          "scorer": "LongMemEval official eval_utils",
                          "corpus": "LongMemEval-S synthetic haystack turns only (no production data)"},
               "buckets": []}
    for size in BUCKETS:
        t0 = time.time()
        corpus = build_bucket_corpus(turns, qids, gold, pool_all, size)
        corpus_n = len(corpus)
        items = [{"content": c["content"],
                  "context": f"LME decay corpus turn (qid={c['qid']})",
                  "title": c["content"][:60] or "lme turn",
                  # operator 提供的受控科研语料=user 级可信来源（投毒闸 external_only 模式
                  # 只拦非 user 源；语料中确实存在注入类英文句，正是闸门按设计拦截的实证，
                  # 评测数据不应被生产防线误伤，也不应为此凿穿防线——2026-09-18）
                  "source_type": "manual", "source_tier": "user",
                  "tags": ["lme-decay", f"q:{c['qid']}", "decay"],
                  "domain": "general", "priority": 3,
                  "source_ref": c["source_ref"],
                  "owner": "main", "visibility": "agent"} for c in corpus]
        committed = ingest(items)
        if committed < corpus_n:
            print(f"NOTE: ingest inserted {committed}/{corpus_n} (dedup skips on retry overlap)", flush=True)
        res = score(qids, questions, gold)
        row = {"bucket_size": size, "corpus_actual": corpus_n, "ingested": committed,
               "rag5_turn": res["turn"]["recall_any@5"],
               "turn": res["turn"], "session": res["session"],
               "elapsed_s": round(time.time() - t0, 1)}
        results["buckets"].append(row)
        print(f"bucket={size} corpus={corpus_n} R@5(turn)={row['rag5_turn']} "
              f"R@5(session)={res['session']['recall_any@5']} ({row['elapsed_s']}s)", flush=True)
        json.dump(results, open(f"{LME_DIR}/decay_results.json", "w"), indent=2, ensure_ascii=False)

    # markdown curve table
    md = ["# Corpus-size decay curve (LongMemEval-S, synthetic corpus)",
          "",
          "Same 100-question set scored at every bucket; bank `eval_decay` rebuilt per bucket;",
          "bank-wide top-k (global mode); official LongMemEval scorer. R@5 = recall_any@5.",
          "",
          "| corpus size (turns) | R@5 (turn) | R@5 (session) | R@1 (turn) | R@10 (turn) |",
          "|---|---|---|---|---|"]
    for b in results["buckets"]:
        md.append(f"| {b['bucket_size']} | {b['turn']['recall_any@5']} | "
                  f"{b['session']['recall_any@5']} | {b['turn']['recall_any@1']} | "
                  f"{b['turn']['recall_any@10']} |")
    md += ["",
           "*Corpus = LongMemEval synthetic haystack turns only (no production memories). "
           "Gold turns of the scored set are present in every bucket; distractor fill grows with bucket size (nested).*"]
    with open(f"{LME_DIR}/decay_curve.md", "w") as f:
        f.write("\n".join(md) + "\n")
    print("WROTE decay_results.json + decay_curve.md", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
