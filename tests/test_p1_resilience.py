"""P1 第一批新增用例（2026-09-16）：降级 2 + 原子化 1 + UNIQUE 1 + 可插拔 1 + 降权 1。

离线可跑（无 daemon/CUDA/PG 依赖）：假 pool/conn + monkeypatch 隔离，直调生产代码路径
（与 P0 套件同风格）。核心召回打分语义不改，只验证降级/降权分支与事务/兜底结构。
"""
import importlib
import uuid
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import BackgroundTasks, HTTPException

from memory_engine import api_core, api_memories, config, db, util
from memory_engine import recall as recall_mod
from memory_engine.api_core import RecallRequest, RetainItem, RetainRequest
from memory_engine.api_memories import PatchRequest
from memory_engine.embedder import OpenAICompatProvider, Qwen3EmbeddingProvider, build_embedder


def _fake_pool(conn=None):
    return SimpleNamespace(connection=lambda: nullcontext(conn or SimpleNamespace()))


def _fake_request(eng):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=eng)))


def _meta(mid: str, source_tier: str = "agent") -> dict:
    return {"id": mid, "seq": 1, "priority": 3, "ttl_state": "active",
            "verify_status": "unverified", "staleness": "fresh", "source_tier": source_tier,
            "title": "t", "body": "b", "body_ptr": None, "bank": "hermes", "domain": "d",
            "tags": [], "owner": "main", "visibility": "agent", "trigger_term": None,
            "source_ref": None, "contains_pii": None, "created_at": None, "updated_at": None}


class _BoomEmbedder:
    device = "cuda"

    def embed_queries(self, qs):
        raise RuntimeError("CUDA OOM (drill)")

    def embed_documents(self, ts):
        raise RuntimeError("CUDA OOM (drill)")


# ---------- 降级（P1 批 2 例） ----------

def test_embed_failure_degrades_to_fts_only(monkeypatch):
    """嵌入路失败→200 显式降级：矢量路跳过，纯 FTS+时序路出结果，degraded=true+failed_routes 透出。"""
    rows_fts = [{"id": "m-1"}, {"id": "m-2"}]
    monkeypatch.setattr(db, "route_vector",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("嵌入失败时矢量路不应被调用")))
    monkeypatch.setattr(db, "route_fts", lambda *a, **k: rows_fts)
    monkeypatch.setattr(db, "route_time", lambda *a, **k: [{"id": "m-3"}])
    meta = {"m-1": _meta("m-1"), "m-2": _meta("m-2"), "m-3": _meta("m-3")}
    monkeypatch.setattr(db, "hydrate", lambda conn, ids: {i: meta[i] for i in ids if i in meta})

    res = recall_mod.recall(_fake_pool(), _BoomEmbedder(), "查询", None, "main", 5, {})
    assert res["degraded"] is True
    assert "vector" in res["failed_routes"] and "embed" in res["failed_routes"]["vector"]
    assert res["routes"] == {"vector": 0, "fts": 2, "time": 1}
    ids = {r["id"] for r in res["results"]}
    assert {"m-1", "m-2", "m-3"} <= ids
    assert res["results"][0]["score_parts"]["tier_weight"] == 1.0


def test_embed_and_all_routes_failure_still_503(monkeypatch):
    """嵌入失败叠加 FTS/时序路全失败=全路失败→RecallRouteError→API 层 503（P1：503 唯一入口）。"""
    monkeypatch.setattr(db, "route_fts",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pg down")))
    monkeypatch.setattr(db, "route_time",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pg down")))
    with pytest.raises(recall_mod.RecallRouteError) as ei:
        recall_mod.recall(_fake_pool(), _BoomEmbedder(), "q", None, "main", 5, {})
    assert set(ei.value.failed_routes) == {"vector", "fts", "time"}

    eng = SimpleNamespace(db=_fake_pool(), embedder=_BoomEmbedder())
    monkeypatch.setattr(recall_mod, "recall",
                        lambda *a, **k: (_ for _ in ()).throw(ei.value))
    with pytest.raises(HTTPException) as api_ei:
        api_core.recall(RecallRequest(query="x"), _fake_request(eng), BackgroundTasks())
    assert api_ei.value.status_code == 503
    d = api_ei.value.detail
    assert d["degraded"] is True and d["retryable"] is True
    assert set(d["failed_routes"]) == {"vector", "fts", "time"}


# ---------- 原子化（P1 批 1 例） ----------

class _TxTrackingConn:
    """假 conn：记录语句与事务作用域；changelog INSERT 可注入失败以验证回滚。"""

    def __init__(self, fail_changelog: bool = False):
        self.fail_changelog = fail_changelog
        self.tx_depth = 0
        self.in_tx_sql: list[str] = []
        self.out_tx_sql: list[str] = []
        self.rolled_back = False
        self.committed = False

    @contextmanager
    def transaction(self):
        self.tx_depth += 1
        try:
            yield self
        except Exception:
            self.rolled_back = True
            raise
        else:
            self.committed = True
        finally:
            self.tx_depth -= 1

    @contextmanager
    def cursor(self):
        yield self

    def execute(self, sql, params=()):
        u = " ".join(sql.split()).upper()
        in_tx = self.tx_depth > 0
        if in_tx:
            kind = "changelog" if "CHANGELOG" in u else ("memories_update" if u.startswith("UPDATE") else u[:20])
            if self.fail_changelog and kind == "changelog":
                raise RuntimeError("changelog insert boom")
            self.in_tx_sql.append(kind)
        else:
            self.out_tx_sql.append(u[:40])
        if u.startswith("SELECT BANK"):
            self.description = [SimpleNamespace(name="bank"), SimpleNamespace(name="title"),
                                SimpleNamespace(name="body"), SimpleNamespace(name="original_date")]
            self._rows = [("hermes", "old", "old body", None)]
        elif u.startswith("UPDATE MEMORIES"):
            assert in_tx, "UPDATE memories 必须在事务内（P1 原子化）"
            self.description = [SimpleNamespace(name="id"), SimpleNamespace(name="bank"),
                                SimpleNamespace(name="title"), SimpleNamespace(name="body")]
            self._rows = [(uuid.uuid4(), "hermes", "old", "new body")]
        elif u.startswith("SELECT"):
            self.description = None
            self._rows = []
        else:
            self.description = None
            self._rows = []

    def fetchone(self):
        rows = getattr(self, "_rows", [])
        return rows[0] if rows else None

    def fetchall(self):
        return list(getattr(self, "_rows", []))


def test_patch_update_and_changelog_same_transaction(monkeypatch):
    """patch 的 memories UPDATE + changelog INSERT 同事务：changelog 失败→整体回滚，不缺账。"""
    conn = _TxTrackingConn()
    eng = SimpleNamespace(
        db=_fake_pool(conn),
        embedder=SimpleNamespace(embed_documents=lambda texts: [[0.0] * 1024]))
    row = api_memories.patch_memory(uuid.uuid4(), PatchRequest(body="new body"), _fake_request(eng))
    assert row["body"] == "new body"
    assert conn.rolled_back is False and conn.committed is True
    assert conn.in_tx_sql == ["memories_update", "changelog"]      # 同事务且 UPDATE 在前
    assert conn.out_tx_sql and conn.out_tx_sql[0].startswith("SELECT BANK")  # 读旧值在事务外

    conn2 = _TxTrackingConn(fail_changelog=True)
    eng2 = SimpleNamespace(
        db=_fake_pool(conn2),
        embedder=SimpleNamespace(embed_documents=lambda texts: [[0.0] * 1024]))
    with pytest.raises(RuntimeError, match="changelog insert boom"):
        api_memories.patch_memory(uuid.uuid4(), PatchRequest(body="new body"), _fake_request(eng2))
    assert conn2.rolled_back is True and conn2.committed is False   # 账本失败→UPDATE 一并回滚


# ---------- 判重 UNIQUE 兜底（P1 批 1 例） ----------

def test_retain_unique_conflict_returns_existing(monkeypatch):
    """并发竞态撞 UNIQUE(bank, dedup_key)→不 500：返回既有条目 id+dedup_existing，键=sha256(body+US+context)。"""
    existing_id = uuid.uuid4()

    class _SelConn:
        def __init__(self):
            self.description = None
            self._rows = []

        def connection(self):  # 兼容 pool_conn(eng) 用法
            return nullcontext(self)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        @contextmanager
        def cursor(self):
            yield self

        def execute(self, sql, params=()):
            u = " ".join(sql.split()).upper()
            if u.startswith("SELECT") and "DEDUP_KEY" in u:
                self.description = [SimpleNamespace(name="id"), SimpleNamespace(name="seq")]
                self._rows = [(existing_id, 42)]
            else:
                self.description = None
                self._rows = []

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return list(self._rows)

    seen = {}

    def fake_insert(conn, **f):
        seen["dedup_key"] = f["dedup_key"]
        raise psycopg.errors.UniqueViolation("uq_mem_dedup conflict (race drill)")

    monkeypatch.setattr(db, "insert_memory", fake_insert)
    conn = _SelConn()
    eng = SimpleNamespace(
        db=conn,
        embedder=SimpleNamespace(embed_documents=lambda texts: [[0.0] * 1024]))
    req = RetainRequest(bank="hermes", items=[
        RetainItem(content="P1 判重兜底", context="P1 部署验证 ctx")])
    res = api_core.retain(req, _fake_request(eng), BackgroundTasks())
    assert res["ids"] == [] and res["dedup_skipped"] == 1
    assert res["dedup_existing"] == [str(existing_id)]
    assert seen["dedup_key"] == util.dedup_hash("P1 判重兜底", "P1 部署验证 ctx")
    assert len(seen["dedup_key"]) == 64


# ---------- 可插拔（P1 批 1 例） ----------

def test_embed_provider_pluggable(monkeypatch):
    """EMBED_PROVIDER 工厂：qwen3=本地实现 / openai_compat=远程(走 LLM_GATEWAY_API_KEY) / 未知值拒绝。"""
    monkeypatch.setenv("MEMORY_ENGINE_EMBED_PROVIDER", "openai_compat")
    monkeypatch.setenv("LLM_GATEWAY_API_KEY", "k-p1-test")
    importlib.reload(config)
    p = build_embedder()
    assert isinstance(p, OpenAICompatProvider)
    assert p.device == "remote" and p.api_key == "k-p1-test" and p.loaded is False
    p.load()                                   # 远程 provider：load 无本地加载动作
    assert p.loaded is True

    monkeypatch.setenv("MEMORY_ENGINE_EMBED_PROVIDER", "qwen3")
    importlib.reload(config)
    q = build_embedder()
    assert isinstance(q, Qwen3EmbeddingProvider) and q.loaded is False

    monkeypatch.setenv("MEMORY_ENGINE_EMBED_PROVIDER", "junk-provider")
    importlib.reload(config)
    with pytest.raises(ValueError):
        build_embedder()

    monkeypatch.delenv("MEMORY_ENGINE_EMBED_PROVIDER")
    monkeypatch.delenv("LLM_GATEWAY_API_KEY")
    importlib.reload(config)
    assert config.EMBED_PROVIDER == "qwen3"
    assert config.TIER_WEIGHTS == {"user": 1.0, "agent": 1.0, "web": 0.85, "cron": 0.9}
    assert config.EMBED_VER == 1


# ---------- source_tier 降权（P1 批 1 例） ----------

def test_tier_downweight_in_fusion(monkeypatch):
    """web=0.85/cron=0.9 乘入 final（user/agent 不降权）；score_parts.tier_weight 透出降权因子。"""
    assert config.TIER_WEIGHTS["web"] == 0.85 and config.TIER_WEIGHTS["cron"] == 0.9
    assert config.TIER_WEIGHTS["user"] == 1.0 and config.TIER_WEIGHTS["agent"] == 1.0

    class _OkEmbedder:
        device = "cuda"

        def embed_queries(self, qs):
            return [[0.0] * 1024]

        def embed_documents(self, ts):
            return [[0.0] * 1024]

    monkeypatch.setattr(db, "route_vector", lambda *a, **k: [])
    monkeypatch.setattr(db, "route_fts", lambda *a, **k: [{"id": "m-web"}])
    monkeypatch.setattr(db, "route_time", lambda *a, **k: [{"id": "m-agent"}])
    meta = {"m-web": _meta("m-web", source_tier="web"), "m-agent": _meta("m-agent", source_tier="agent")}
    monkeypatch.setattr(db, "hydrate", lambda conn, ids: {i: meta[i] for i in ids if i in meta})

    res = recall_mod.recall(_fake_pool(), _OkEmbedder(), "q", None, "main", 5, {})
    assert res["degraded"] is False and res["failed_routes"] == {}
    by_id = {r["id"]: r for r in res["results"]}
    assert by_id["m-agent"]["score_parts"]["tier_weight"] == 1.0
    assert by_id["m-web"]["score_parts"]["tier_weight"] == 0.85
    # 同 pri/life/stale 下：score 比 = rrf 比 × 0.85（秩不同 rrf 不同，比值法秩无关）
    ratio = by_id["m-web"]["score"] / by_id["m-agent"]["score"]
    expect = (by_id["m-web"]["score_parts"]["rrf"] / by_id["m-agent"]["score_parts"]["rrf"]) * 0.85
    assert abs(ratio - expect) < 1e-3
