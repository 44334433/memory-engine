"""双引擎对照评测（切主验收闸）：同题集分测 本引擎 vs 旧版基线 recall。

语料对齐：旧库 L1 全量以 dedup=false 移植进本引擎（source_ref=record_id，
tags=['eval-corpus']，owner='main' 模拟生产召回面），两引擎同题同 gold。
数据集不随仓库分发（含真实生产内容）；本脚本公开评测方法。
指标：P@5（gold 进 top5 比率）、MRR@10（gold 排名倒数均值，出 top10 记 0）。
产出：eval/compare_result.json（逐题+汇总）
运行：python3 eval/run_compare.py（需引擎 daemon 与基线服务在跑）
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from memory_engine import config  # noqa: E402

_EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
EVAL = os.environ.get("MEMORY_EVAL_OUT", os.path.join(_EVAL_DIR, "memory_recall_eval.jsonl"))
OUT = os.path.join(_EVAL_DIR, "compare_result.json")
BASELINE_DB = os.environ.get("MEMORY_EVAL_LEGACY_DB", "")          # 旧版记忆库 sqlite（必填）
BASELINE_URL = os.environ.get(
    "MEMORY_EVAL_BASELINE_URL", "http://127.0.0.1:8420/v3/atomic/search")
TOPK = 10


def http(url, method="GET", body=None, headers=None, timeout=30):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def baseline_key() -> str:
    """基线服务鉴权键：env LLM_GATEWAY_API_KEY > $MEMORY_ENGINE_HOME/.env > 仓库 .env。"""
    v = os.environ.get("LLM_GATEWAY_API_KEY")
    if v:
        return v.strip()
    candidates = [
        os.path.join(os.environ.get("MEMORY_ENGINE_HOME",
                                    os.path.expanduser("~/hermes-data")), ".env"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
    ]
    for path in candidates:
        try:
            for line in open(path, encoding="utf-8"):
                if line.startswith("LLM_GATEWAY_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    raise RuntimeError("LLM_GATEWAY_API_KEY 未找到（env 或 .env 文件，见 .env.example）")


def ingest_corpus() -> dict:
    """旧库 L1 全量 → 本引擎（幂等：已存在 eval 语料则跳过）。"""
    if not BASELINE_DB or not os.path.exists(BASELINE_DB):
        print("FATAL: 设置 MEMORY_EVAL_LEGACY_DB 指向旧版记忆库 sqlite", file=sys.stderr)
        sys.exit(2)
    with config_pool_connection() as conn:
        n = conn.execute("SELECT count(*) FROM memories WHERE source_type='eval'").fetchone()[0]
    if n >= 800:
        return {"skipped": True, "existing": n}
    td = sqlite3.connect(f"file:{BASELINE_DB}?mode=ro", uri=True)
    td.row_factory = sqlite3.Row
    rows = td.execute("SELECT record_id, content, scene_name, type, priority, created_time "
                      "FROM l1_records").fetchall()
    ids, skipped = [], 0
    batch = []
    for r in rows:
        batch.append({"content": r["content"], "context": (r["scene_name"] or r["type"] or "eval")[:300],
                      "title": (r["scene_name"] or r["content"][:40])[:80],
                      "tags": ["eval-corpus"], "owner": "main", "visibility": "agent",
                      "source_type": "eval", "source_ref": r["record_id"],
                      "priority": min(5, max(1, int(r["priority"] / 20) or 1)),
                      "original_date": (r["created_time"] or None)})
        if len(batch) >= 32:
            out = http(f"{BASE}/v1/retain", "POST",
                       {"bank": "hermes", "caller": "eval", "dedup": False, "items": batch},
                       timeout=120)
            ids += out["ids"]; skipped += out["dedup_skipped"]; batch = []
    if batch:
        out = http(f"{BASE}/v1/retain", "POST",
                   {"bank": "hermes", "caller": "eval", "dedup": False, "items": batch}, timeout=120)
        ids += out["ids"]; skipped += out["dedup_skipped"]
    return {"skipped": False, "ingested": len(ids), "skipped_dup": skipped}


def config_pool_connection():
    from memory_engine.db import PgPool
    pool = PgPool(config.PG_DSN, 1, 1)
    return _PoolCtx(pool)


class _PoolCtx:
    def __init__(self, pool):
        self.pool = pool

    def __enter__(self):
        self.cm = self.pool.connection()
        return self.cm.__enter__()

    def __exit__(self, *a):
        r = self.cm.__exit__(*a)
        self.pool.close()
        return r


def recall_engine(q):
    out = http(f"{BASE}/v1/recall", "POST",
               {"query": q, "bank": "hermes", "caller": "main", "top_k": TOPK}, timeout=60)
    # gold 对齐键 = source_ref（移植时保留的旧库 record_id，引擎自有 id 是 UUIDv7）
    return [(r.get("source_ref") or r["id"], r["score"]) for r in out["results"]], out.get("took_ms")


def recall_baseline(q, key):
    out = http(BASELINE_URL, "POST",
               {"team_id": "default", "agent_id": "default", "user_id": "default",
                "query": q, "limit": TOPK},
               headers={"Authorization": f"Bearer {key}"}, timeout=60)
    items = (out.get("data") or {}).get("items") or []
    return [(r["id"], r.get("score", 0)) for r in items], None


def score(ranked, gold, scene_ids=None):
    """主口径=exact gold；次口径=同场景兄弟记录（「当时需要记忆X」的更真实相关性）。"""
    ids = [i for i, _ in ranked]
    if gold in ids:
        rank = ids.index(gold) + 1
        out = {"hit@5": rank <= 5, "rank": rank, "rr": 1.0 / rank}
    else:
        out = {"hit@5": False, "rank": None, "rr": 0.0}
    if scene_ids:
        scene_ranks = [ids.index(i) + 1 for i in ids if i in scene_ids]
        best = min(scene_ranks) if scene_ranks else None
        out["scene_rank"] = best
        out["scene_rr"] = 1.0 / best if best else 0.0
        out["scene_hit@5"] = bool(best and best <= 5)
    return out


def main() -> int:
    global BASE
    BASE = f"http://{config.HOST}:{config.PORT}"
    ing = ingest_corpus()
    print("ingest:", ing)
    key = baseline_key()

    # 同场景分组（次级相关性口径：gold 的 scene 兄弟记录也算相关）
    td = sqlite3.connect(f"file:{BASELINE_DB}?mode=ro", uri=True)
    scene_of = dict(td.execute("SELECT record_id, scene_name FROM l1_records").fetchall())
    scenes = {}
    for rid, sc in scene_of.items():
        scenes.setdefault(sc, set()).add(rid)

    questions = [json.loads(l) for l in open(EVAL)]
    per_q, t_engine, t_baseline = [], [], []
    for qd in questions:
        q, gold = qd["query"], qd["gold_id"]
        re_, te = recall_engine(q)
        rb, tb = recall_baseline(q, key)
        t_engine.append(te or 0)
        if tb is not None:
            t_baseline.append(tb)
        sib = scenes.get(scene_of.get(gold), {gold})
        se, sb = score(re_, gold, sib), score(rb, gold, sib)
        per_q.append({"qid": qd["qid"], "query": q[:60], "gold": gold,
                      "engine": se, "baseline": sb})
        mark = "E" if se["rr"] > sb["rr"] else ("=" if se["rr"] == sb["rr"] else "B")
        print(f"{qd['qid']} {mark} engine(rank={se['rank']},scene={se['scene_rank']}) "
              f"baseline(rank={sb['rank']},scene={sb['scene_rank']}) :: {q[:40]}")

    summary = {
        "n_questions": len(per_q),
        "session_verified_rate": round(sum(1 for q in questions if q["session_verified"]) / len(questions), 3),
        "primary_exact_gold": {
            "engine_P@5": round(sum(1 for p in per_q if p["engine"]["hit@5"]) / len(per_q), 3),
            "baseline_P@5": round(sum(1 for p in per_q if p["baseline"]["hit@5"]) / len(per_q), 3),
            "engine_MRR@10": round(sum(p["engine"]["rr"] for p in per_q) / len(per_q), 3),
            "baseline_MRR@10": round(sum(p["baseline"]["rr"] for p in per_q) / len(per_q), 3),
        },
        "secondary_same_scene": {
            "engine_P@5": round(sum(1 for p in per_q if p["engine"]["scene_hit@5"]) / len(per_q), 3),
            "baseline_P@5": round(sum(1 for p in per_q if p["baseline"]["scene_hit@5"]) / len(per_q), 3),
            "engine_MRR@10": round(sum(p["engine"]["scene_rr"] for p in per_q) / len(per_q), 3),
            "baseline_MRR@10": round(sum(p["baseline"]["scene_rr"] for p in per_q) / len(per_q), 3),
        },
        "engine_recall_p50_ms": sorted(t_engine)[len(t_engine) // 2] if t_engine else None,
        "corpus": {"ingested": ing},
        "gate": None,
    }
    pe, pb = summary["primary_exact_gold"], summary["secondary_same_scene"]
    gap = pe["engine_MRR@10"] - pe["baseline_MRR@10"]
    summary["gate"] = ("PASS（对齐或优于基线）" if (pe["engine_P@5"] >= pe["baseline_P@5"]
                                                and gap >= -0.05) else "FAIL（低于基线）")
    result = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "summary": summary, "per_query": per_q}
    json.dump(result, open(OUT, "w"), ensure_ascii=False, indent=2, default=str)
    print("\n=== 汇总（主口径=exact gold / 次口径=同场景）===")
    print(f"P@5    engine={pe['engine_P@5']}/{pb['engine_P@5']}  baseline={pe['baseline_P@5']}/{pb['baseline_P@5']}")
    print(f"MRR@10 engine={pe['engine_MRR@10']}/{pb['engine_MRR@10']}  baseline={pe['baseline_MRR@10']}/{pb['baseline_MRR@10']}")
    print(f"session_verified={summary['session_verified_rate']}  gap(MRR exact)={gap:+.3f}  "
          f"engine_P50={summary['engine_recall_p50_ms']}ms")
    print("gate:", summary["gate"], "->", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
