#!/usr/bin/env python3
"""LongMemEval-S 检索评测（官方口径复现 + 本引擎实测）。

官方协议（LongMemEval@9e0b455f）：
- 语料：每题 haystack 的 user turns，doc_id=<session_id>_<n>（answer session 无证据 turn → noans_）
- gold：含 'answer' 的 doc_id；query=question 原文
- 指标：k∈{1,3,5,10,30,50}，recall_any@k / recall_all@k / ndcg_any@k（turn 级 + turn2session 级）
- 汇总：跳过 '_abs' 弃答题；跳过无 has_answer 证据的题（官方 averaging 同款条件）
- 计分函数：直接 import 官方 src/retrieval/eval_utils.py（零重实现）

三种被测 ranker（同一语料、同一 gold、同一计分）：
  flat_engine   本引擎 /v1/recall，tags 过滤 q:<qid>（等价官方 flat per-question 索引）
  bm25_official 官方 rank_bm25.BM25Okapi 原码路径（同口径基线复现，可对论文数字）
  global_engine 本引擎全库召回（122K turns 无过滤，真实部署形态；k=全库 top-k 语义）
"""
import json
import os
import sys
import time
import urllib.request

LME_DIR = os.environ.get("LME_DIR", os.path.dirname(os.path.abspath(__file__)))  # run_eval.sh 可重定向（缺省=本目录，行为不变）
REPO_SRC = os.environ.get("LME_SCORER_SRC", os.path.expanduser("~/LongMemEval/src/retrieval"))
ENGINE = os.environ.get("LME_ENGINE", "http://127.0.0.1:8767")
TOPK = 50
KS = [1, 3, 5, 10, 30, 50]

sys.path.insert(0, REPO_SRC)
# NumPy 2.x 兼容 shim（官方 eval_utils 使用 np.asfarray，2.0 已移除；不改官方文件，行为等价）
import numpy as _np
if not hasattr(_np, "asfarray"):
    _np.asfarray = lambda a, dtype=_np.float64: _np.asarray(a, dtype=dtype)
from eval_utils import evaluate_retrieval, evaluate_retrieval_turn2session, dcg  # noqa: E402  官方计分

import numpy as np  # noqa: E402
from rank_bm25 import BM25Okapi  # noqa: E402


def engine_recall(query: str, qid: str | None, topk: int = TOPK) -> list[str]:
    """返回按引擎分序的 source_ref(doc_id) 列表；qid 非 None 时按题过滤（flat）。"""
    body = {"query": query, "bank": "hermes-sessions", "caller": "main", "top_k": topk}
    if qid is not None:
        body["filters"] = {"tags": [f"q:{qid}"]}
    req = urllib.request.Request(ENGINE + "/v1/recall",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read())
    return [x["source_ref"] for x in d["results"]]


def agg_metrics(ranker, questions, corpus, gold, q_corpus_turns):
    """ranker(q) -> 有序 doc_id 列表（可为部分序）。汇总官方三指标。"""
    out = {"turn": {f"{m}@{k}": [] for m in ("recall_any", "recall_all", "ndcg_any") for k in KS},
           "session": {f"{m}@{k}": [] for m in ("recall_any", "recall_all", "ndcg_any") for k in KS}}
    skipped_abs, skipped_notarget, used = 0, 0, 0
    for q in questions:
        qid = q["question_id"]
        if "_abs" in qid:
            skipped_abs += 1
            continue
        cids = q_corpus_turns[qid]
        cdocs = gold[qid]
        if not cdocs:
            skipped_notarget += 1
            continue
        ranked = ranker(q, qid, cids)
        idx_of = {d: i for i, d in enumerate(cids)}
        # turn-level: rankings 为 cids 索引序列（ranked 必为 cids 子集序列）
        turn_rankings = [idx_of[d] for d in ranked if d in idx_of]
        for k in KS:
            ra, rl, nd = evaluate_retrieval(turn_rankings, cdocs, cids, k=k)
            out["turn"][f"recall_any@{k}"].append(ra)
            out["turn"][f"recall_all@{k}"].append(rl)
            out["turn"][f"ndcg_any@{k}"].append(nd)
            # turn2session（官方函数，内部扩 k 至 k 个唯一 session）
            ra, rl, nd = evaluate_retrieval_turn2session(turn_rankings, cdocs, cids, k=k)
            out["session"][f"recall_any@{k}"].append(ra)
            out["session"][f"recall_all@{k}"].append(rl)
            out["session"][f"ndcg_any@{k}"].append(nd)
        used += 1
    res = {"used_questions": used, "skipped_abs": skipped_abs, "skipped_notarget": skipped_notarget}
    for lvl in ("turn", "session"):
        res[lvl] = {m: round(float(np.mean(v)), 4) for m, v in out[lvl].items()}
    return res


def agg_metrics_global(questions, gold, bank_top_fn):
    """global：top-k 取全库序列前 k（外题 distractor 占位不剔除），语义=部署时取 k 条记忆。"""
    out = {"turn": {f"{m}@{k}": [] for m in ("recall_any", "recall_all", "ndcg_any") for k in KS},
           "session": {f"{m}@{k}": [] for m in ("recall_any", "recall_all", "ndcg_any") for k in KS}}
    skipped_abs, skipped_notarget, used = 0, 0, 0
    for q in questions:
        qid = q["question_id"]
        if "_abs" in qid:
            skipped_abs += 1
            continue
        cdocs = gold[qid]
        if not cdocs:
            skipped_notarget += 1
            continue
        cids = q["turn_ids"]
        bank_ranked = bank_top_fn(q, qid)          # 全库 top-TOPK doc_ids（引擎分序）
        gold_set = set(cdocs)
        strip = lambda d: "_".join(d.split("_")[:-1])
        gold_sess = set(strip(d) for d in cdocs)
        for k in KS:
            topk = bank_ranked[:k]
            # turn 级
            rels = [1 if d in gold_set else 0 for d in topk]
            ideal = sorted([1 if c in gold_set else 0 for c in cids], reverse=True)[:k]
            idcg = dcg(ideal, k)
            out["turn"][f"recall_any@{k}"].append(float(any(rels)))
            out["turn"][f"recall_all@{k}"].append(float(all(g in set(topk) for g in cdocs)))
            out["turn"][f"ndcg_any@{k}"].append(round(dcg(rels, k) / idcg, 6) if idcg else 0.0)
            # session 级（doc→session 去重后按首现序）
            seen, sess_rel = set(), []
            for d in topk:
                s = strip(d)
                if s not in seen:
                    seen.add(s)
                    sess_rel.append(1 if s in gold_sess else 0)
            gold_sess_all = set(strip(c) for c in cids if c in gold_set)
            sess_corpus = []
            for c in cids:
                s = strip(c)
                if s not in sess_corpus:
                    sess_corpus.append(s)
            ideal_s = sorted([1 if s in gold_sess_all else 0 for s in sess_corpus], reverse=True)[:k]
            idcg_s = dcg(ideal_s, k)
            out["session"][f"recall_any@{k}"].append(float(any(sess_rel)))
            out["session"][f"recall_all@{k}"].append(float(all(g in set(strip(x) for x in topk) for g in gold_sess)))
            out["session"][f"ndcg_any@{k}"].append(round(dcg(sess_rel, k) / idcg_s, 6) if idcg_s else 0.0)
        used += 1
    res = {"used_questions": used, "skipped_abs": skipped_abs, "skipped_notarget": skipped_notarget}
    for lvl in ("turn", "session"):
        res[lvl] = {m: round(float(np.mean(v)), 4) for m, v in out[lvl].items()}
    return res


def main() -> int:
    questions = [json.loads(l) for l in open(f"{LME_DIR}/questions.jsonl", encoding="utf-8")]
    corpus = {}
    for l in open(f"{LME_DIR}/turns.jsonl", encoding="utf-8"):
        t = json.loads(l)
        corpus[t["doc_id"]] = t["content"]
    gold = {q["question_id"]: q["gold_turn_ids"] for q in questions}
    q_corpus_turns = {q["question_id"]: q["turn_ids"] for q in questions}
    print(f"questions={len(questions)} corpus={len(corpus)}", flush=True)

    # ---- 1) flat_engine ----
    t0 = time.time()
    flat_cache = {}
    for i, q in enumerate(questions):
        if "_abs" in q["question_id"]:
            continue
        flat_cache[q["question_id"]] = engine_recall(q["question"], q["question_id"])
        if (i + 1) % 100 == 0:
            print(f"flat recall {i+1}/{len(questions)} {time.time()-t0:.0f}s", flush=True)

    def flat_ranker(q, qid, cids):
        return flat_cache[qid]
    res_flat = agg_metrics(flat_ranker, questions, corpus, gold, q_corpus_turns)
    res_flat["ranker"] = "memory-engine flat (tags filter, 3-way RRF)"
    print("flat_engine:", json.dumps(res_flat["session"], ensure_ascii=False)[:200], flush=True)

    # ---- 2) global_engine（复用 flat 无过滤请求，单独打分）----
    g0 = time.time()
    global_cache = {}
    for i, q in enumerate(questions):
        if "_abs" in q["question_id"]:
            continue
        global_cache[q["question_id"]] = engine_recall(q["question"], None)
        if (i + 1) % 100 == 0:
            print(f"global recall {i+1}/{len(questions)} {time.time()-g0:.0f}s", flush=True)

    def global_fn(q, qid):
        return global_cache[qid]
    res_global = agg_metrics_global(questions, gold, global_fn)
    res_global["ranker"] = "memory-engine global (no filter, bank-wide top-k)"
    print("global_engine session:", json.dumps(res_global["session"], ensure_ascii=False)[:200], flush=True)

    # ---- 3) bm25_official（官方原码路径）----
    def bm25_ranker(q, qid, cids):
        cids = q["turn_ids"]
        toks = [corpus[d].split(" ") for d in cids]
        bm = BM25Okapi(toks)
        scores = bm.get_scores(q["question"].split(" "))
        order = np.argsort(scores)[::-1]
        return [cids[i] for i in order]
    res_bm25 = agg_metrics(bm25_ranker, questions, corpus, gold, q_corpus_turns)
    res_bm25["ranker"] = "BM25Okapi official path (turn granularity)"
    print("bm25_official session:", json.dumps(res_bm25["session"], ensure_ascii=False)[:200], flush=True)

    out = {"config": {"topk": TOPK, "ks": KS, "engine": ENGINE,
                      "dataset": "longmemeval_s (hf sha 2ec2a55, cleaned 2025-09-19)",
                      "official_repo_commit": "9e0b455f4ef0e2ab8f2e582289761153549043fc"},
           "flat_engine": res_flat, "global_engine": res_global, "bm25_official": res_bm25}
    with open(f"{LME_DIR}/lme_retrieval_results.json", "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print("WROTE lme_retrieval_results.json", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
