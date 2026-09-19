#!/usr/bin/env python3
"""W3 实测对比：同库真实查询，rerank off(off) vs on(8791, cpu)。
输出：每查询 top5 顺序 diff + P(yes) 透出 + 双态延迟（P50/P95，各 7 次预热1次）。
只读 HTTP，不写库。"""
import http.client
import json
import statistics

QUERIES = [
    ("异构记忆竞争 检索质量 重排", "hermes-docs"),
    ("memory-engine 网关端口 8766 拍板", None),
    ("飞书回复末尾 Next 行机制", "hermes"),
    ("GPU 显存 OOM 预算 禁擦线分配", None),
    ("Hindsight 记忆引擎 四层 bank 架构", "hermes-docs"),
]


def recall(port, query, bank, top_k=100):
    body = json.dumps({"query": query, "bank": bank, "caller": "main",
                       "top_k": top_k, "filters": {}}).encode()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    t0 = __import__("time").perf_counter()
    c.request("POST", "/v1/recall", body, {"Content-Type": "application/json"})
    r = c.getresponse()
    d = json.loads(r.read().decode())
    ms = (__import__("time").perf_counter() - t0) * 1000
    c.close()
    return r.status, ms, d


out = {"queries": []}
for q, bank in QUERIES:
    recall(8766, q, bank)                      # 预热
    off_ms, on_ms = [], []
    for _ in range(7):
        s0, m0, d0 = recall(8766, q, bank)
        s1, m1, d1 = recall(8791, q, bank)
        assert s0 == 200 and s1 == 200, (q, s0, s1)
        off_ms.append(m0)
        on_ms.append(m1)
    ids0 = [r["id"] for r in d0["results"][:5]]
    ids1 = [r["id"] for r in d1["results"][:5]]
    moved = sum(1 for a, b in zip(ids0, ids1) if a != b) + abs(len(ids0) - len(ids1))
    assert "rerank" not in (d1.get("failed_routes") or {}), f"开态 rerank 路失败: {d1.get('failed_routes')}"
    assert not d1.get("degraded"), f"开态非重排故障不应 degraded: {d1}"
    n_all = sum(1 for r in d1["results"] if "rerank" in r["score_parts"])
    assert n_all >= 1, f"开态结果必须出现 rerank 分量: {[r['score_parts'].get('rerank') for r in d1['results'][:5]]}"
    rr = [r["score_parts"].get("rerank") for r in d1["results"][:5]]
    assert not any("rerank" in r["score_parts"] for r in d0["results"]), "关态泄漏 rerank 分量"
    out["queries"].append({
        "query": q, "bank": bank,
        "top5_same_order": ids0 == ids1, "top5_moved_slots": moved,
        "rerank_p_top5": rr,
        "latency_off_ms_p50": round(statistics.median(off_ms), 1),
        "latency_on_ms_p50": round(statistics.median(on_ms), 1),
        "latency_off_ms_p95": round(sorted(on_ms[:0] + off_ms)[-1], 1),
        "latency_on_ms_p95": round(sorted(on_ms)[-1], 1),
        "delta_p50_ms": round(statistics.median(on_ms) - statistics.median(off_ms), 1),
    })
print(json.dumps(out, ensure_ascii=False, indent=1))
