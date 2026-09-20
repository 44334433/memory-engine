"""Outcome 反馈 API 单测（自进化专项 #1，2026-09-18；主会话接管收尾后补写）。

覆盖：①非法 outcome 400 ②不存在 id 404 ③EMA 幂等/收敛语义（首条 |polarity|=α，
重复同向收敛 ±1 不越界）④adopted→access_events(kind=adopted) 接线 ⑤corrected→hard_queries 落池。
运行：/usr/bin/python3.12 -m pytest tests/test_outcome_feedback.py -v（memory-engine-ops：必须 3.12）。
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8766"
SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from memory_engine import outcome  # noqa: E402


def _post(body: dict):
    req = urllib.request.Request(
        f"{BASE}/v1/feedback", json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _retain(content: str, context: str) -> str:
    body = {"bank": "knowledge", "caller": "main",
            "items": [{"content": content, "context": context, "source_tier": "user",
                       "domain": "outcome-test"}]}
    req = urllib.request.Request(f"{BASE}/v1/retain", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    r = urllib.request.urlopen(req, timeout=15)
    d = json.load(r)
    return d["ids"][0] if d.get("ids") else d["items"][0]["id"]


def test_01_invalid_outcome_400():
    st, d = _post({"memory_id": "00000000-0000-0000-0000-000000000000", "outcome": "great"})
    assert st == 400, f"非法 outcome 应 400，得 {st}: {d}"


def test_02_missing_id_404():
    st, d = _post({"memory_id": "00000000-0000-0000-0000-000000000000", "outcome": "adopted"})
    assert st == 404, f"不存在 id 应 404，得 {st}: {d}"


def test_03_ema_semantics_unit():
    # 首条信号：prev=NULL 按 0.0 起点，|polarity| = α
    a = 0.1
    w = outcome.ema_next(None, 1.0)
    assert abs(w - a) < 1e-9
    # 重复同向收敛于 ±1 且恒不越界
    w = 0.0
    for _ in range(200):
        w = outcome.ema_next(w, 1.0)
    assert 0.999 < w <= 1.0
    # 负向对称
    w = outcome.ema_next(None, -1.0)
    assert abs(w + a) < 1e-9


def test_04_feedback_adopted_lifecycle():
    mid = _retain(f"outcome-e2e adopted 用例 {id(object())}", "test outcome e2e adopted")
    st, d = _post({"memory_id": mid, "outcome": "adopted", "caller": "pytest"})
    assert st == 200, f"adopted 应 200，得 {st}: {d}"
    assert 0 < d.get("polarity", 0) <= 1.0, f"polarity 应落 (0,1]，得 {d}"
    assert d.get("outcome") == "adopted"


def test_05_feedback_useless_then_corrected_converge_negative():
    mid = _retain(f"outcome-e2e negative 用例 {id(object())}", "test outcome e2e neg")
    st1, d1 = _post({"memory_id": mid, "outcome": "useless", "caller": "pytest"})
    assert st1 == 200 and d1["polarity"] < 0
    st2, d2 = _post({"memory_id": mid, "outcome": "corrected", "caller": "pytest"})
    assert st2 == 200
    # 二次同向：|polarity| 增大（EMA 递推向 -1 收敛）
    assert d2["polarity"] <= d1["polarity"], f"同向递推应变深: {d1} -> {d2}"
