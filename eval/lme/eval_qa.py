#!/usr/bin/env python3
"""LongMemEval-S QA 评测（本地 LLM 生成+判定，官方 judge prompt 逐字复用）。

口径声明（写进报告）：
- 判定：官方 evaluate_qa.get_anscheck_prompt 四题型+弃答模板逐字复用；temp=0；max_tokens=10；label='yes' in resp。
- 生成：官方非 CoT RAG 模板（History Chats + Current Date + Question + Answer）；top-5 flat 检索；
  turn 扩展为 round（user turn + 紧随 assistant 回复），nl 格式 "Role: content"。
- 模型：本地 llama-server（生成=判定同模型）→ 数字仅同模型内部自比（RAG vs no-retrieval Δ），
  严禁与官方 GPT-4o-judged 榜单数字直接对比。
- 子样本：非弃答题中固定种子抽样（seed=42, N=100），清单落盘可复现。
- 全量口径（2026-09-17）：LME_QA_N=470 = 全部 470 条非弃答题（500 题中 30 条 _abs 弃答题
  无 QA 可评）；同 seed 下前 100 条与 n=100 子样本完全重合（超集，可对照）。
- 断点续跑：LME_QA_RESUME=1 时从 qa_log.jsonl 已完成 qid 继续（470 题约 3-4h，防中断重跑）。
"""
import json
import os
import random
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone

LME_DIR = os.path.expanduser("~/.hermes/memory-engine/eval/lme")
ENGINE = os.environ.get("ENGINE", "http://127.0.0.1:8767")
LLM = os.environ.get("LLM", "http://127.0.0.1:8769/v1/chat/completions")
N_SAMPLE = int(os.environ.get("LME_QA_N", "100"))
RESUME = os.environ.get("LME_QA_RESUME", "") == "1"
SEED = 42
TOPK = 5
DATE_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2}).*?(\d{2}):(\d{2})")


def to_iso(s):
    m = DATE_RE.search(s or "")
    if not m:
        return ""
    y, mo, d, h, mi = map(int, m.groups())
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).isoformat()


def llm_chat(prompt: str, max_tokens: int) -> str:
    body = json.dumps({"messages": [{"role": "user", "content": prompt}],
                       "temperature": 0, "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(LLM, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"]["content"].strip()


# —— 官方 judge 模板（evaluate_qa.py 逐字复制，勿改动） ——
def get_anscheck_prompt(task, question, answer, response, abstention=False):
    if not abstention:
        if task in ['single-session-user', 'single-session-assistant', 'multi-session']:
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        elif task == 'temporal-reasoning':
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        elif task == 'knowledge-update':
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        elif task == 'single-session-preference':
            template = "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
            prompt = template.format(question, answer, response)
        else:
            raise NotImplementedError
    else:
        template = "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only."
        prompt = template.format(question, answer, response)
    return prompt


def engine_top5(question: str, qid: str) -> list[str]:
    body = json.dumps({"query": question, "bank": "hermes-sessions", "caller": "main",
                       "top_k": TOPK, "filters": {"tags": [f"q:{qid}"]}}).encode()
    req = urllib.request.Request(ENGINE + "/v1/recall", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read())
    return [x["source_ref"] for x in d["results"]]


def round_of(session, turn_idx):
    """官方 flat-turn round 扩展：user turn + 紧随 assistant 回复。"""
    r = [session[turn_idx]]
    if turn_idx + 1 < len(session) and session[turn_idx + 1]["role"] == "assistant":
        r.append(session[turn_idx + 1])
    return r


def main() -> int:
    questions = [json.loads(l) for l in open(f"{LME_DIR}/questions.jsonl", encoding="utf-8")]
    nonabs = [q for q in questions if "_abs" not in q["question_id"]]
    random.Random(SEED).shuffle(nonabs)
    sample = nonabs[:N_SAMPLE]
    json.dump([q["question_id"] for q in sample],
              open(f"{LME_DIR}/qa_sample_ids.json", "w"), indent=1)
    print(f"sample={len(sample)} (seed={SEED})", flush=True)

    # 原始数据（重建 round：需要 assistant 回复）
    done: dict[str, dict] = {}
    if RESUME and os.path.exists(f"{LME_DIR}/qa_log.jsonl"):
        for l in open(f"{LME_DIR}/qa_log.jsonl", encoding="utf-8"):
            try:
                r = json.loads(l)
                done[r["question_id"]] = r
            except Exception:
                pass
        print(f"resume: {len(done)} already done", flush=True)
    else:
        for f in (f"{LME_DIR}/qa_log.jsonl",):
            if os.path.exists(f):
                os.remove(f)
    data = json.load(open("/media/qq/Linux/HermesArchive/longmemeval/hf-cleaned/longmemeval_s_cleaned.json"))
    by_qid = {e["question_id"]: e for e in data}

    results = {"rag": [], "norag": []}
    t0 = time.time()
    n_new = 0
    for i, q in enumerate(sample):
        if q["question_id"] in done:
            r = done[q["question_id"]]
            results["rag"].append({"qid": q["question_id"], "label": r["rag_label"]})
            results["norag"].append({"qid": q["question_id"], "label": r["norag_label"]})
            continue
        n_new += 1
        entry = by_qid[q["question_id"]]
        sess_map = {sid: s for sid, s in zip(entry["haystack_session_ids"], entry["haystack_sessions"])}
        date_map = {sid: d for sid, d in zip(entry["haystack_session_ids"], entry["haystack_dates"])}

        # RAG 上下文（top-5 round）
        top5 = engine_top5(q["question"], q["question_id"])
        chunks = []
        for doc_id in top5:
            sid, tn = doc_id.rsplit("_", 1)
            sid = sid.replace("noans_", "answer_") if sid.startswith("noans_") else sid
            if sid not in sess_map:
                continue
            r = round_of(sess_map[sid], int(tn) - 1)
            lines = [f"{t['role']}: {t['content']}" for t in r]
            chunks.append(f"Session Date: {date_map.get(sid, '')}\n" + "\n".join(lines))
        history = "\n\n".join(chunks) if chunks else "(no relevant history found)"
        rag_prompt = ("I will give you several history chats between you and a user. "
                      "Please answer the question based on the relevant chat history.\n\n\n"
                      f"History Chats:\n\n{history}\n\nCurrent Date: {q['question_date']}\n"
                      f"Question: {q['question']}\nAnswer:")
        norag_prompt = (f"Current Date: {q['question_date']}\nQuestion: {q['question']}\nAnswer:")

        rec = {"question_id": q["question_id"], "qtype": q["question_type"],
               "question": q["question"], "answer": q["answer"], "top5": top5}
        for mode, p in (("rag", rag_prompt), ("norag", norag_prompt)):
            try:
                hyp = llm_chat(p, 256)
            except Exception as e:
                hyp = f"__LLM_ERROR__ {e}"
            rec[mode] = hyp
            jp = get_anscheck_prompt(q["question_type"], q["question"], q["answer"], hyp,
                                     abstention=False)
            try:
                verdict = llm_chat(jp, 10)
            except Exception as e:
                verdict = f"__LLM_ERROR__ {e}"
            rec[mode + "_verdict"] = verdict
            rec[mode + "_label"] = ("yes" in verdict.lower()) and ("__LLM_ERROR__" not in verdict)
        results["rag"].append({"qid": rec["question_id"], "label": rec["rag_label"]})
        results["norag"].append({"qid": rec["question_id"], "label": rec["norag_label"]})
        json.dump(rec, open(f"{LME_DIR}/qa_log.jsonl", "a"), ensure_ascii=False)
        if (i + 1) % 10 == 0:
            acc_r = sum(x["label"] for x in results["rag"]) / len(results["rag"])
            acc_n = sum(x["label"] for x in results["norag"]) / len(results["norag"])
            print(f"{i+1}/{len(sample)} rag={acc_r:.3f} norag={acc_n:.3f} ({time.time()-t0:.0f}s)", flush=True)

    def summary(mode):
        labels = results[mode]
        by_type = {}
        recs = {r["qid"]: r for r in
                (json.loads(l) for l in open(f"{LME_DIR}/qa_log.jsonl", encoding="utf-8"))}
        for x in labels:
            by_type.setdefault(recs[x["qid"]]["qtype"], []).append(x["label"])
        return {"n": len(labels),
                "accuracy": round(sum(x["label"] for x in labels) / len(labels), 4),
                "by_type": {k: round(sum(v) / len(v), 4) for k, v in by_type.items()}}

    out = {"config": {"n": len(sample), "requested": N_SAMPLE, "new_this_run": n_new,
                      "seed": SEED, "topk": TOPK,
                      "gen_model": "local llama-server (see report)",
                      "judge": "official get_anscheck_prompt, same local model",
                      "note": "同模型自比口径；禁止与官方 GPT-4o-judged 数字直接对比"},
           "rag": summary("rag"), "norag": summary("norag")}
    out["delta_rag_minus_norag"] = round(out["rag"]["accuracy"] - out["norag"]["accuracy"], 4)
    json.dump(out, open(f"{LME_DIR}/lme_qa_results.json", "w"), indent=2, ensure_ascii=False)
    print(json.dumps(out, indent=2, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
