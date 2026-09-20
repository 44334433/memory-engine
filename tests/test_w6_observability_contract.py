"""W6 借鉴增强四项合同测试（2026-09-20 拍板「a」；形态参照 test_w1_core_block/test_w2_memory_type）。

纯逻辑（无库）：①classify_probe 四态判定 ②core_block_staleness 两态 ③norm_group/
norm_retrieved_ids E1/M1 归一 ④feedback 计数器快照拷贝语义 ⑤window_days 夹取。
活体（daemon 可达才跑，skip 不伪绿；BASE 走 MEMORY_ENGINE_PORT env=8767 隔离实例可跑）：
⑥GET /v1/metrics 四节形状 + pinned 比率值域 ⑦POST /v1/feedback 带 group+retrieved_ids
（E1/M1 新字段）→200 且组查询面命中 ⑧旧 payload（不带新字段）→200 向后兼容。
真库零残留纪律：活体用例条目尾部 DELETE ?purge=true（changelog 账本保留=append-only 设计）。
运行：/usr/bin/python3.12 -m pytest tests/test_w6_observability_contract.py -v
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from memory_engine import api_feedback, outcome, observability as obs  # noqa: E402

BASE = f"http://127.0.0.1:{int(os.environ.get('MEMORY_ENGINE_PORT', '8766'))}"  # 默认=现值；env 覆盖供隔离实例


def _get(path: str, timeout: int = 15):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as r:
        return r.status, json.load(r)


def _post(path: str, body: dict, timeout: int = 20):
    req = urllib.request.Request(f"{BASE}{path}", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _daemon_up() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/v1/health", timeout=2) as r:
            return json.loads(r.read() or b"{}").get("db") is True
    except Exception:
        return False


live = pytest.mark.skipif(not _daemon_up(), reason="daemon 不可达（skip 不伪绿）")


# ---------- ① classify_probe 四态（活性探针判定纯函数） ----------

def test_classify_probe_states():
    assert obs.classify_probe(0, 0, 0) == "no_writes"            # 写侧断=宿主采集侧问题
    assert obs.classify_probe(5, 0, 0) == "write_only"           # 有账无落列=写入链断（死回路）
    assert obs.classify_probe(5, 5, 0) == "joined_no_consume"    # 落列未达消费门槛
    assert obs.classify_probe(5, 5, 1) == "alive"                # 链路全活
    assert obs.classify_probe(5, 3, 1) == "alive"                # 只证活性不证疗效：差值另有 join_drift 透出


# ---------- ② core_block_staleness 两态 ----------

def test_core_block_staleness_two_states():
    t0 = 1_800_000_000.0
    never = obs.core_block_staleness(None, t0 - 100, now=t0)
    assert never["ever_fetched_since_startup"] is False
    assert never["process_uptime_s"] == 100.0
    fresh = obs.core_block_staleness(t0 - 42.5, t0 - 100, now=t0)
    assert fresh["ever_fetched_since_startup"] is True
    assert fresh["staleness_s"] == 42.5
    assert fresh["last_fetch_iso"].endswith("+00:00")


# ---------- ③ E1/M1 归一纯函数 ----------

def test_norm_group():
    assert outcome.norm_group(None) is None
    assert outcome.norm_group("   ") is None
    assert outcome.norm_group("  g1  ") == "g1"
    assert len(outcome.norm_group("x" * 500)) == outcome.GROUP_MAX_LEN


def test_norm_retrieved_ids():
    assert outcome.norm_retrieved_ids(None) is None
    assert outcome.norm_retrieved_ids([]) is None
    u = uuid.uuid4()
    assert outcome.norm_retrieved_ids([u]) == [str(u)]
    big = [str(uuid.uuid4()) for _ in range(500)]
    out = outcome.norm_retrieved_ids(big)
    assert len(out) == outcome.RETRIEVED_IDS_CAP


# ---------- ④ 计数器快照 ----------

def test_counters_snapshot_is_copy_and_bump():
    before = api_feedback.counters_snapshot()
    snap = api_feedback.counters_snapshot()
    snap["applied"] += 999                       # 改拷贝不得污染真值
    assert api_feedback.counters_snapshot()["applied"] == before["applied"]
    api_feedback._bump("l3_skipped_no_query")
    assert api_feedback.counters_snapshot()["l3_skipped_no_query"] == \
        before["l3_skipped_no_query"] + 1
    assert set(before) >= {"applied", "rejected_bad_outcome", "not_found",
                           "l3_skipped_no_query", "l3_skipped_pool", "l3_recorded",
                           "with_group", "with_retrieved_ids"}


# ---------- ⑤ window_days 夹取（SQL 注入面守卫） ----------

def test_window_days_clamp():
    assert obs.window_days(7) == 7
    assert obs.window_days(0) == 1
    assert obs.window_days(-5) == 1
    assert obs.window_days(10_000) == 365
    assert obs.window_days("abc") == 7
    assert obs.window_days(None) == 7


# ---------- ⑥~⑧ 活体合同 ----------

@live
def test_live_metrics_shape():
    st, d = _get("/v1/metrics?days=7")
    assert st == 200
    assert {"version", "window_days", "uptime_s", "feedback_path",
            "injected_two_state", "pinned_exemption", "process_counters"} <= set(d)
    fp = d["feedback_path"]
    assert fp["probe_status"] in ("no_writes", "write_only", "joined_no_consume", "alive")
    assert fp["join_drift"] == fp["feedback_distinct_writes"] - fp["outcome_landed"]
    assert fp["feedback_writes"] >= fp["feedback_distinct_writes"] >= fp["outcome_landed"]
    pin = d["pinned_exemption"]["share"]
    assert 0.0 <= pin["ratio"] <= 1.0
    assert pin["live_total"] >= pin["pinned_total"] >= 0
    assert pin["live_total"] == sum(b["total"] for b in pin["by_bank"])
    stale = d["pinned_exemption"]["core_block_staleness"]
    assert "ever_fetched_since_startup" in stale
    inj = d["injected_two_state"]
    assert {"exposure_events", "consumption_events", "orphan_adopt_rows",
            "orphan_samples", "review_conclusion"} <= set(inj)
    assert inj["review_conclusion"] in ("pass", "review_samples")
    assert len(inj["orphan_samples"]) <= 5
    assert d["process_counters"]["scope"] == "process_since_startup"


@live
def test_live_feedback_group_and_retrieved_ids():
    """E1+M1 新字段全链路：提交带组+召回集 → 响应透出 → 组聚合/明细查询命中。"""
    marker = uuid.uuid4().hex[:12]
    st, r = _post("/v1/retain", {"bank": "knowledge", "caller": "main",
                                 "items": [{"content": f"w6 观测合同测试条目 {marker}",
                                            "context": "test_w6 契约回归，可清理",
                                            "source_tier": "user", "domain": "w6-obs"}]})
    assert st == 200 and r["ids"], f"retain 失败 {st}: {r}"
    mid = r["ids"][0]
    group = f"w6-obs-{marker}"
    other = str(uuid.uuid4())
    try:
        st, d = _post("/v1/feedback", {"memory_id": mid, "outcome": "adopted",
                                       "caller": "w6-test", "group": group,
                                       "retrieved_ids": [mid, other]})
        assert st == 200, f"新字段反馈应 200，得 {st}: {d}"
        assert d["group"] == group and d["retrieved_ids_count"] == 2
        # E1 聚合查询
        st, agg = _get(f"/v1/feedback/groups?days=1")
        assert st == 200 and any(g["group"] == group for g in agg["groups"]), \
            f"组聚合列表应含 {group}: {agg}"
        row = next(g for g in agg["groups"] if g["group"] == group)
        assert row["n"] >= 1 and row["adopted"] >= 1 and row["distinct_memories"] >= 1
        # E1 明细 + M1 retrieved ids 回读
        st, det = _get(f"/v1/feedback/groups?group={group}")
        assert st == 200 and det["items"], f"组明细应命中: {det}"
        item = det["items"][0]
        assert item["memory_id"] == mid and item["outcome"] == "adopted"
        assert str(mid) in item["retrieved_ids"] and other in item["retrieved_ids"]
        # 旧 payload 向后兼容（不带新字段）
        st, legacy = _post("/v1/feedback", {"memory_id": mid, "outcome": "useless",
                                            "caller": "w6-legacy"})
        assert st == 200, f"旧 payload 必须仍 200，得 {st}: {legacy}"
        assert legacy["group"] is None and legacy["retrieved_ids_count"] == 0
        assert legacy["polarity"] < legacy["polarity_prev"], "useless 负向 EMA 演化不回归"
    finally:
        req = urllib.request.Request(f"{BASE}/v1/memories/{mid}?purge=true", method="DELETE")
        with urllib.request.urlopen(req, timeout=15) as r:
            assert r.status == 200, "清理必须成功（真库零残留纪律）"


@live
def test_live_core_block_staleness_roundtrip():
    """P4 staleness：core-block 拉取一次后 metrics 侧 staleness 应转为已拉取态。"""
    st, _ = _get("/v1/core-block?budget_chars=200")
    assert st == 200
    st, d = _get("/v1/metrics")
    stale = d["pinned_exemption"]["core_block_staleness"]
    assert stale["ever_fetched_since_startup"] is True
    assert 0 <= stale["staleness_s"] < 120
