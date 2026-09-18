"""W2 latency probe: measure recall P50/P95 against live daemon.
Usage: python3 scripts/w2_latency_probe.py [label]
Prints JSON one-liner. Fails loudly on non-200 (reads body — 4xx≠success rule).
"""
import http.client
import json
import statistics
import sys
import time

QS = [
    "记忆引擎 召回", "memory type 分类", "系统 配置", "Python 测试", "数据库 迁移",
    "用户 偏好", "项目 结构", "错误 处理", "缓存 策略", "网络 代理",
] * 3


def probe(body: dict) -> dict:
    lat = []
    for q in QS:
        payload = json.dumps({**body, "query": q}).encode()
        c = http.client.HTTPConnection("127.0.0.1", 8766, timeout=15)
        t0 = time.perf_counter()
        c.request("POST", "/v1/recall", payload, {"Content-Type": "application/json"})
        r = c.getresponse()
        raw = r.read()
        dt = (time.perf_counter() - t0) * 1000
        if r.status != 200:
            print(json.dumps({"error": f"HTTP {r.status}", "body": raw[:400].decode("utf-8", "replace")}))
            sys.exit(1)
        lat.append(dt)
        c.close()
    lat.sort()
    n = len(lat)
    return {"n": n, "p50_ms": round(statistics.median(lat), 1),
            "p95_ms": round(lat[max(0, int(n * 0.95) - 1)], 1),
            "max_ms": round(lat[-1], 1)}


if __name__ == "__main__":
    label = sys.argv[1] if len(sys.argv) > 1 else "plain"
    extra = {}
    if len(sys.argv) > 2:
        extra = json.loads(sys.argv[2])
    out = {"label": label, **probe(extra)}
    print(json.dumps(out, ensure_ascii=False))
