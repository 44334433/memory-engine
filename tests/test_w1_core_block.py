"""W1 核心记忆块（core block）单测（2026-09-18，拍板顺序 W2 之后）。

纯逻辑（无库）：①rank 排序 pinned 轨优先/auto 轨分数降序 ②render_block 预算截断
（≤budget 不变量、尾部按分数弃入、条目截尾「…」）③pinned 优先 ④空库=空块
⑤auto_score 单调/带内拉伸 ⑥config 默认值（1500 对齐 freshness 闸）⑦SQL 契约
（is_current/ttl_state/vis 占位）⑧存量零影响静态守卫（recall/lifecycle 不 import
core_block、不消费 pinned）。
活体（daemon 可达才跑，skip 不伪绿）：端点形状、pinned/unpin 切换生效、零副作用
（core-block 读取不推 access_count——防自激回路）、预算极端值、存量行为不变。
运行：/usr/bin/python3.12 -m pytest tests/test_w1_core_block.py -v（memory-engine-ops：必须 3.12）
真库零残留纪律：活体用例条目尾部 DELETE ?purge=true。
"""
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from memory_engine import config, core_block as cb  # noqa: E402


def _row(mid=None, pinned=False, bank="knowledge", mtype="semantic", priority=3, seq=1,
         title="t", body="b" * 50, polarity: float | None = 0.8, adopt=3, access=10):
    return {"id": mid or str(uuid.uuid4()), "bank": bank, "memory_type": mtype,
            "priority": priority, "seq": seq, "title": title, "body": body,
            "polarity": polarity, "adopt_count": adopt, "access_count": access,
            "pinned": pinned}


# ---------- ① rank 排序 ----------

def test_rank_pinned_track_first_regardless_of_score():
    low_pinned = _row(pinned=True, polarity=None, adopt=0, access=0, seq=1)
    high_auto = _row(pinned=False, polarity=1.0, adopt=9, access=99, seq=2)
    ordered = cb.rank([high_auto, low_pinned])
    assert [r["pinned"] for r in ordered] == [True, False]


def test_rank_auto_ordered_by_score_then_seq():
    a = _row(polarity=0.6, adopt=2, access=1, seq=10)
    b = _row(polarity=1.0, adopt=8, access=50, seq=11)
    ordered = cb.rank([a, b])
    assert [r["seq"] for r in ordered] == [11, 10]


def test_rank_pinned_track_order_priority_then_seq():
    p1 = _row(pinned=True, priority=3, seq=5)
    p2 = _row(pinned=True, priority=5, seq=4)   # 高优先级但更早
    ordered = cb.rank([p1, p2])
    assert [r["seq"] for r in ordered] == [4, 5]  # priority 降序主导，seq 只做同优先级 tiebreak


# ---------- ② 预算闸 / ③ pinned 优先 / ④ 空库 ----------

def test_render_budget_invariant_hard():
    rows = [_row(body="正文" * 200, seq=i) for i in range(10)]
    out = cb.render_block(rows, 300)
    assert len(out["text"]) <= 300
    assert out["used_chars"] == len(out["text"])
    assert out["dropped_count"] > 0
    # 截断条目带「…」标记或整条弃入；ids 只含实际入块条目
    assert len(out["ids"]) == 10 - out["dropped_count"]


def test_render_pinned_survives_small_budget_drops_auto_first():
    pinned = _row(pinned=True, body="钉住条目正文" * 3, seq=1, title="钉")
    autos = [_row(pinned=False, body="自动条目正文" * 3, seq=10 + i, polarity=1.0, adopt=9)
             for i in range(5)]
    out = cb.render_block(autos + [pinned], 200)
    assert out["ids"][0] == pinned["id"]              # pinned 最先入块
    assert len(out["text"]) <= 200
    assert out["dropped_count"] >= 1                  # 低分 auto 从尾部弃入
    assert pinned["id"] in out["text"] or "钉" in out["text"]


def test_render_order_follows_score_within_budget():
    lo = _row(polarity=0.5, adopt=2, access=0, seq=1, title="低分")
    hi = _row(polarity=1.0, adopt=9, access=40, seq=2, title="高分")
    out = cb.render_block([lo, hi], 1500)
    assert out["ids"].index(hi["id"]) < out["ids"].index(lo["id"])


def test_render_empty_rows_returns_empty_block():
    out = cb.render_block([], 1500)
    assert out == {"text": "", "ids": [], "used_chars": 0, "dropped_count": 0}


def test_render_tiny_budget_never_exceeds():
    rows = [_row(body="x" * 100, seq=1), _row(body="y" * 100, seq=2, pinned=True)]
    for budget in (1, 5, 40, cb.ENTRY_MIN_TAIL):
        out = cb.render_block(rows, budget)
        assert len(out["text"]) <= budget


# ---------- ⑤ auto_score ----------

def test_auto_score_monotonic_polarity_and_capped():
    s_lo = cb.auto_score(_row(polarity=0.55, adopt=2, access=1))
    s_hi = cb.auto_score(_row(polarity=1.0, adopt=9, access=99))
    assert s_hi > s_lo
    s_10 = cb.auto_score(_row(polarity=1.0, adopt=10, access=50))
    s_100 = cb.auto_score(_row(polarity=1.0, adopt=100, access=10 ** 6))
    assert abs(s_100 - s_10) < 1e-9                   # 封顶：刷计数不涨分（防旁路）
    assert cb.auto_score(_row(polarity=0.5, adopt=10, access=50)) == pytest.approx(0.5)  # 带底 pn=0，adopt/access 封顶=0.3+0.2
    assert 0.0 <= cb.auto_score(_row(polarity=None, adopt=0, access=0)) <= 1.0


# ---------- ⑥ config 默认 ----------

def test_config_defaults_aligned_with_gates():
    assert config.CORE_BLOCK_BUDGET_CHARS == 1500     # 对齐 freshness MAX_INJECT_CHARS
    assert config.CORE_AUTO_TYPES == ("semantic", "procedural")
    assert config.CORE_MIN_POLARITY > 0 and config.CORE_MIN_ADOPT >= 2
    assert abs(config.CORE_W_POLARITY + config.CORE_W_ADOPT + config.CORE_W_ACCESS - 1.0) < 1e-9


# ---------- ⑦ SQL 契约 + ⑧ 存量零影响守卫 ----------

def test_sql_contract_filters():
    for sql in (cb.PINNED_SQL, cb.AUTO_SQL):
        assert "is_current" in sql and "ttl_state NOT IN ('retired','archived')" in sql
        assert "{vis}" in sql and "LIMIT %s" in sql
    assert "memory_type = ANY(%s)" in cb.AUTO_SQL and "polarity >= %s" in cb.AUTO_SQL
    assert "pinned AND" in cb.PINNED_SQL


def test_existing_paths_do_not_consume_pinned():
    """存量零变化铁律：检索/衰减代码不 import core_block、不消费 pinned（grep 源码级守卫）。"""
    for rel in ("memory_engine/recall.py", "memory_engine/lifecycle.py",
                "memory_engine/outcome.py"):
        src = (SRC / rel).read_text(encoding="utf-8")
        assert "core_block" not in src, f"{rel} 不应依赖 core_block"
        assert "pinned" not in src, f"{rel} 不应消费 pinned 列"


# ============ 活体用例（daemon 可达才跑） ============

BASE = "http://127.0.0.1:8766"


def _req(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _daemon_up() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/v1/health", timeout=2) as r:
            return json.load(r).get("db") is True
    except Exception:
        return False


pytestmark_live = pytest.mark.skipif(not _daemon_up(), reason="daemon 不可达（skip 不伪绿）")


@pytestmark_live
def test_live_endpoint_shape_and_budget_gate():
    st, d = _req("GET", "/v1/core-block")
    assert st == 200
    for k in ("text", "ids", "budget_chars", "used_chars", "dropped_count",
              "pinned_candidates", "auto_candidates", "truncated", "took_ms"):
        assert k in d, f"响应缺字段 {k}"
    assert d["budget_chars"] == 1500 and len(d["text"]) <= 1500
    assert isinstance(d["ids"], list) and isinstance(d["text"], str)
    st, d2 = _req("GET", "/v1/core-block?budget_chars=80")
    assert st == 200 and len(d2["text"]) <= 80


@pytestmark_live
def test_live_bad_budget_422():
    st, _ = _req("GET", "/v1/core-block?budget_chars=0")
    assert st == 422                                    # Query ge=1 校验（FastAPI 422）


@pytestmark_live
def test_live_pinned_roundtrip_and_zero_side_effect():
    marker = f"w1coreblock-{uuid.uuid4().hex[:10]}"
    body = f"核心记忆块活体验证条目 {marker}：" + "内容填充。" * 40
    st, r = _req("POST", "/v1/retain", {"bank": "knowledge", "caller": "main", "items": [
        {"content": body, "context": "W1 core-block 活体测试（用后即删）",
         "source_tier": "user", "memory_type": "semantic", "domain": "w1-test"}]})
    assert st == 200
    mid = (r.get("ids") or [None])[0]
    assert mid, f"retain 未产生新行（判重命中？）: {r}"
    try:
        # 1) 新条目零信号 → 不在块（存量行为不变：无 pinned 不参与、auto 门槛未过）
        _, d0 = _req("GET", "/v1/core-block")
        assert mid not in d0["ids"]
        _, mem = _req("GET", f"/v1/memories/{mid}")
        assert mem.get("pinned") is False              # 默认 false=存量语义
        # 2) pin → 入块、原文条目级（非摘要）
        st, p1 = _req("PATCH", f"/v1/memories/{mid}", {"pinned": True})
        assert st == 200 and p1.get("pinned") is True
        _, d1 = _req("GET", "/v1/core-block")
        assert mid in d1["ids"] and marker in d1["text"]
        acc_after_pin = d1 and _req("GET", f"/v1/memories/{mid}")[1]["access_count"]
        # 3) 零副作用：再读 3 次，access_count/adopt_count 不涨（防自激回路）
        for _ in range(3):
            _req("GET", "/v1/core-block")
        _, mem2 = _req("GET", f"/v1/memories/{mid}")
        assert mem2["access_count"] == acc_after_pin and mem2["adopt_count"] == 0
        # 4) 非 pinned 字段 PATCH 不清钉（存量 PATCH 语义零变化）
        st, p2 = _req("PATCH", f"/v1/memories/{mid}", {"title": "改名不清钉"})
        assert st == 200 and p2.get("pinned") is True
        # 5) unpin → 出块
        st, p3 = _req("PATCH", f"/v1/memories/{mid}", {"pinned": False})
        assert st == 200 and p3.get("pinned") is False
        _, d2 = _req("GET", "/v1/core-block")
        assert mid not in d2["ids"]
    finally:
        _req("DELETE", f"/v1/memories/{mid}?purge=true")   # 真库零残留


@pytestmark_live
def test_live_core_block_p95_lt_50ms():
    lat = []
    for _ in range(30):
        t0 = time.perf_counter()
        st, _ = _req("GET", "/v1/core-block")
        lat.append((time.perf_counter() - t0) * 1000)
        assert st == 200
    lat.sort()
    p95 = lat[int(len(lat) * 0.95) - 1]
    print(f"\ncore-block p50={statistics.median(lat):.1f}ms p95={p95:.1f}ms max={lat[-1]:.1f}ms")
    assert p95 < 50, f"P95={p95:.1f}ms 超验收线 50ms（含 HTTP 环回开销）"
