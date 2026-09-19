"""W3 可插拔重排单测（2026-09-19，执行-W3-reranker）。

覆盖（验收判据前三线，全部 fake 化零 GPU/零 DB 依赖）：
① 关=零开销：config.RERANK_ENABLED 缺省 False + build_reranker()→None +
   recall(reranker=None) 与无参调用结果逐字节一致（重排段完全跳过）。
② 开=顺序可变 + score_parts.rerank 透出：注入 fake reranker（P(yes) 反转相关性），
   top5 顺序必须变化、每条被精排候选带 rerank∈[0,1]、乘法融合公式精确核对。
③ 模型失败降级同 degraded 模式：score() 抛异常 → 保留重排前顺序 +
   degraded=True + failed_routes.rerank 登记（增强路非主路，不 503，禁静默）。
④ top20 预算（#23）：>20 候选时第 21+ 条不打 rerank 分量。
运行：/usr/bin/python3.12 -m pytest tests/test_w3_reranker.py -v
"""
import sys
import uuid
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from memory_engine import config, recall as recall_mod  # noqa: E402


# ---------- fake 基建：注入三级 fake，零 GPU 零 DB ----------

class FakeEmbedder:
    def embed_queries(self, qs):
        return [[0.0] * config.EMBED_DIM] * len(qs)

    def embed_documents(self, ds):
        return [[0.0] * config.EMBED_DIM] * len(ds)


class FakePool:
    """recall 只在 hydrate 时取连接——fake 直接吐出预置 meta。"""

    class _Ctx:
        def __init__(self, meta):
            self.meta = meta

        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False

    def __init__(self, meta):
        self._meta = meta

    def connection(self, timeout: float = 5.0):
        return FakePool._Ctx(self._meta)


def make_meta(n=30):
    ids = [uuid.uuid4() for _ in range(n)]
    meta = {}
    for i, mid in enumerate(ids):
        meta[mid] = {
            "id": mid, "bank": "hermes", "domain": "d", "trigger_term": "t",
            "title": f"title-{i}", "body": f"body-{i} 内容", "body_ptr": None,
            "tags": [], "owner": "main", "visibility": "public",
            "ttl_state": "active", "staleness": "fresh", "priority": 5,
            "verify_status": "unverified", "source_ref": None, "source_tier": "user",
            "contains_pii": False, "memory_type": "episodic",
            "outcome": None, "polarity": None,
            "created_at": None, "updated_at": None,
        }
    return ids, meta


def install_fakes(monkeypatch, ids, meta):
    """三主路各给全量候选（rank 序=ids 序），graph 路关（W_GRAPH=0，隔离重排变量）。"""
    rows = [{"id": mid} for mid in ids]

    def fake_db(self, conn, bank, vis_sql, vis_params, *a, **k):
        return list(rows)

    from memory_engine import db as db_mod
    monkeypatch.setattr(db_mod, "route_vector", fake_db)
    monkeypatch.setattr(db_mod, "route_fts", fake_db)
    monkeypatch.setattr(db_mod, "route_time", fake_db)
    monkeypatch.setattr(recall_mod.db, "hydrate",
                        lambda conn, ids, _m=meta: {k: v for k, v in _m.items() if k in set(ids)})
    monkeypatch.setattr(config, "W_GRAPH", 0)
    return rows


class StubReranker:
    """契约同 Qwen3Reranker：score(query, documents)→P(yes) 列表（同序）。"""

    def __init__(self, probs=None, raise_exc=False):
        self._probs = probs
        self._raise = raise_exc
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query, documents):
        self.calls.append((query, list(documents)))
        if self._raise:
            raise RuntimeError("boom: rerank inference failed")
        if self._probs is not None:
            assert len(self._probs) == len(documents)
            return list(self._probs)
        return [0.5] * len(documents)


# ---------- ① 关=零开销（存量行为零变化） ----------

def test_switch_default_off():
    assert config.RERANK_ENABLED is False, "开关缺省必须=关（存量零变化拍板）"


def test_build_reranker_none_when_off(monkeypatch):
    from memory_engine import reranker as rr_mod
    monkeypatch.setattr(config, "RERANK_ENABLED", False)
    assert rr_mod.build_reranker() is None, "关=工厂返回 None → recall 重排段完全跳过"


def test_off_recall_byte_identical(monkeypatch):
    """reranker=None 与不传参（旧调用面）结果逐字节一致 + 不新增 score_parts 键。"""
    monkeypatch.setattr(config, "RERANK_FLOOR", 0.2)
    ids, meta = make_meta(25)
    install_fakes(monkeypatch, ids, meta)
    pool = FakePool(meta)
    base = recall_mod.recall(pool, FakeEmbedder(), "q", None, "main", 10, {})
    explicit_none = recall_mod.recall(pool, FakeEmbedder(), "q", None, "main", 10, {},
                                      reranker=None)
    def strip_ms(d):  # took_ms=墙钟耗时，两次调用必然有微差——零变化断言比静态载荷
        import copy
        c = copy.deepcopy(d)
        c.pop("took_ms", None)
        return c
    assert json_dumps(strip_ms(base)) == json_dumps(strip_ms(explicit_none)), \
        "关态：显式 None 与旧调用面必须零差异"
    for r in base["results"]:
        assert "rerank" not in r["score_parts"], "关态 score_parts 不得出现 rerank 键"


# ---------- ② 开=顺序可变 + rerank 分量透出 ----------

def test_on_reorders_and_exposes_rerank_component(monkeypatch):
    """fake P(yes) 与 RRF 序反相关 → top 顺序必须翻转，且每条带 rerank 分量。"""
    monkeypatch.setattr(config, "RERANK_FLOOR", 0.2)
    ids, meta = make_meta(25)
    install_fakes(monkeypatch, ids, meta)
    n = config.RERANK_TOP_N
    probs = [round((i + 1) / n, 6) for i in range(n)]        # 越靠后越相关 → 反转
    stub = StubReranker(probs=probs)
    res = recall_mod.recall(FakePool(meta), FakeEmbedder(), "q", None, "main", 5, {},
                            reranker=stub)
    off = recall_mod.recall(FakePool(meta), FakeEmbedder(), "q", None, "main", 5, {},
                            reranker=None)
    assert [r["id"] for r in res["results"]] != [r["id"] for r in off["results"]], \
        "开态：反转相关性 P(yes) 必须改变 top5 顺序"
    assert res["results"][0]["id"] == str(ids[n - 1]), "最高 P(yes) 候选应排第一"
    top = {r["id"]: r for r in res["results"]}
    for mid, p in zip(ids[:n], probs):
        r = top.get(str(mid))
        if r is not None:
            assert r["score_parts"]["rerank"] == round(p, 6), "rerank=P(yes) 精确透出"
    assert stub.calls and stub.calls[0][0] == "q", "score() 收到原 query"


def test_multiplicative_blend_formula(monkeypatch):
    """final' = final × (floor+(1−floor)·p) 精确核对（floor=0.2, p=0）。"""
    monkeypatch.setattr(config, "RERANK_FLOOR", 0.2)
    ids, meta = make_meta(3)
    install_fakes(monkeypatch, ids, meta)
    stub = StubReranker(probs=[0.0, 0.0, 0.0])
    res = recall_mod.recall(FakePool(meta), FakeEmbedder(), "q", None, "main", 3, {},
                            reranker=stub)
    ref = recall_mod.recall(FakePool(meta), FakeEmbedder(), "q", None, "main", 3, {},
                            reranker=None)
    ref_by_id = {r["id"]: r["score"] for r in ref["results"]}
    for r in res["results"]:
        expect = round(ref_by_id[r["id"]] * 0.2, 6)
        assert r["score"] == expect, "floor 压低公式：p=0 → final×0.2"
        assert r["score_parts"]["rerank"] == 0.0


# ---------- ③ 失败降级同 degraded 模式 ----------

def test_score_failure_degrades_same_as_routes(monkeypatch):
    """score() 抛异常 → 结果=重排前顺序 + degraded=True + failed_routes.rerank（不 503、不吞错）。"""
    ids, meta = make_meta(10)
    install_fakes(monkeypatch, ids, meta)
    stub = StubReranker(raise_exc=True)
    res = recall_mod.recall(FakePool(meta), FakeEmbedder(), "q", None, "main", 5, {},
                            reranker=stub)
    off = recall_mod.recall(FakePool(meta), FakeEmbedder(), "q", None, "main", 5, {},
                            reranker=None)
    assert [r["id"] for r in res["results"]] == [r["id"] for r in off["results"]], \
        "重排失败必须保留 RRF 融合顺序"
    assert res["degraded"] is True and res["failed_routes"].get("rerank"), \
        "降级语义与 vector/fts/graph 路同构：degraded+failed_routes.rerank"


# ---------- ④ #23 复杂度预算：只精排 top20 ----------

def test_only_top20_reranked(monkeypatch):
    ids, meta = make_meta(30)
    install_fakes(monkeypatch, ids, meta)
    n = config.RERANK_TOP_N
    stub = StubReranker(probs=[0.9] * n)
    res = recall_mod.recall(FakePool(meta), FakeEmbedder(), "q", None, "main", 100, {},
                            reranker=stub)
    assert len(stub.calls[0][1]) == n, f"每次召回最多精排 top{n} 候选（延迟预算）"
    reranked = [r for r in res["results"] if "rerank" in r["score_parts"]]
    assert len(reranked) == n, f"仅 top{n} 带 rerank 分量，其余候选零改动"


def json_dumps(obj):
    import json
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


# ---------- ⑤ token 预算制打包（纯 fake，无 GPU） ----------

def test_token_budget_packing(monkeypatch):
    """短文档合并成大批、长文档拆小批：N×批内最长 ≤ 预算，且结果序=输入序。"""
    from memory_engine import reranker as rr_mod
    stub = rr_mod.Qwen3Reranker.__new__(rr_mod.Qwen3Reranker)
    stub.model = object()                                   # loaded 探测用
    stub._lock = __import__("threading").Lock()
    stub._prefix_tokens, stub._suffix_tokens = [1, 2], [3, 4]

    class Tok:                                              # 1 字符 = 1 token（兼容 **kwargs）
        def encode(self, text, *a, **k):
            return list(range(len(text)))
    stub.tokenizer = Tok()
    monkeypatch.setattr(config, "RERANK_BATCH_TOKEN_BUDGET", 256)  # score() 有 max(256,·) 下限
    monkeypatch.setattr(config, "RERANK_BATCH", 8)
    batches = []

    def fake_batch(self, pairs):
        batches.append(len(pairs))
        return [0.5] * len(pairs)
    monkeypatch.setattr(rr_mod.Qwen3Reranker, "_score_batch", fake_batch)
    docs = ["s" * 10, "s" * 10, "s" * 10, "s" * 10, "s" * 60, "s" * 3]
    out = stub.score("q", docs)
    assert len(out) == len(docs) and batches and sum(batches) == len(docs)
    assert all(b <= config.RERANK_BATCH for b in batches)
    # 贪心推演（单条长=正文+前后缀 4；预算 256 生效）：[14,14,14,14,64,7]
    #   批1: 14×4=56≤256，第5条使 max=64×5=320>256 → n=4；批2: 64×2=128≤256 → n=2
    assert sum(batches) == len(docs) and all(1 <= b <= config.RERANK_BATCH for b in batches)
    assert batches == [4, 2], f"预算制打包应短条并批、长条压批大小：期望 [4,2] 实际 {batches}"
