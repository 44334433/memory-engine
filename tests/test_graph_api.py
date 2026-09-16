"""图谱只读 API 用例（2026-09-17 P0 附件批）：GET /v1/graph 真库两态 + 结构不变量。

仿 test_p1c_selfevolve 的活体验证纪律（conftest.live_server 探活，daemon 不可达即 skip
不伪绿）；走 httpx 打真 daemon（127.0.0.1:8766）真库——graph 全程只读 GET，零写入零残留。
两态：①无过滤全图（形状/计数自洽/端点闭合/非空）②bank 过滤诱导子图（过滤语义收敛）。
"""
import uuid

import httpx
import pytest


@pytest.fixture(scope="module")
def client(live_server):
    with httpx.Client(base_url=live_server, timeout=30.0) as c:
        yield c


def test_graph_unfiltered_structure_and_invariants(client):
    """态①：无过滤全图——响应形状/计数自洽/边端点闭合于节点集/节点字段完备。"""
    r = client.get("/v1/graph")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"nodes", "edges", "counts"}
    assert set(body["counts"]) == {"nodes", "edges", "total_edges", "truncated"}
    assert body["counts"]["nodes"] == len(body["nodes"])
    assert body["counts"]["edges"] == len(body["edges"])
    assert body["counts"]["truncated"] is (body["counts"]["total_edges"] > len(body["edges"]))
    ids = {n["id"] for n in body["nodes"]}
    for e in body["edges"]:
        assert e["source"] in ids and e["target"] in ids          # 无孤立端点噪声
        assert e["relation"] in ("related", "causal", "parent_child", "contradicts")
        assert e["weight"] == 1.0                                 # P0 常数占位（无权重列）
    for n in body["nodes"]:
        assert n["type"] in ("memory", "entity")
        if n["type"] == "memory":
            assert set(n) == {"id", "label", "type", "bank", "domain"}
        else:
            assert n["bank"] is None and n["domain"] is None      # 实体全局共享，不隶属单 bank
    # 真库有弱图存量（3470+ 边）→ 全图非空
    assert body["nodes"] and body["edges"]


def test_graph_bank_filter_induced_subgraph(client):
    """态②：bank 过滤——记忆节点全部收敛到该 bank；诱导子图边数 ≤ 全图且非空。"""
    full = client.get("/v1/graph").json()
    r = client.get("/v1/graph", params={"bank": "hermes"})
    assert r.status_code == 200, r.text
    filtered = r.json()
    assert filtered["counts"]["total_edges"] <= full["counts"]["total_edges"]
    assert filtered["counts"]["total_edges"] > 0
    for n in filtered["nodes"]:
        if n["type"] == "memory":
            assert n["bank"] == "hermes"
    ids = {n["id"] for n in filtered["nodes"]}
    for e in filtered["edges"]:
        assert e["source"] in ids and e["target"] in ids


def test_graph_nonexistent_bank_empty_graph(client):
    """坏 bank=空图：200 + 0 边 0 点、truncated=False（不报错不裸 500）。"""
    r = client.get("/v1/graph", params={"bank": f"__no_such_{uuid.uuid4().hex[:8]}__"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["nodes"] == [] and body["edges"] == []
    assert body["counts"]["total_edges"] == 0 and body["counts"]["truncated"] is False


def test_graph_limit_clamp(client):
    """limit 裁剪：limit=3 → 返回 ≤3 边且 total_edges 兜底计数不缩水；下界钳到 1。"""
    r = client.get("/v1/graph", params={"limit": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["edges"]) <= 3
    assert body["counts"]["total_edges"] >= len(body["edges"])
    assert body["counts"]["truncated"] is True
    r0 = client.get("/v1/graph", params={"limit": 0})   # max(1, min(0, MAX)) → 1
    assert r0.status_code == 200 and len(r0.json()["edges"]) <= 1
