"""多跳图谱 + 实体消歧 + 时序边失效（2026-09-21 拍板插队批）新增用例。

覆盖三件：
  件1 多跳：逐跳 BFS 环防 visited、每跳 top-K、衰减 ×0.5/跳、graph_hops 参数（钳制/0=关）、
      持久边权乘积传播（迁移 010）；
  件2 消歧：同名多 etype 实体按 query 嵌入 vs 邻域文档嵌入均值选边（真 PG + 真向量），
      margin 内不罚、开关关闭零行为；
  件3 时序：图遍历 as_of（边窗+节点窗）、「当时的图」、audit 候选 SQL（T1 retired /
      T2 superseded 端点）。

风格对齐 test_p1b：PG 用例走专用临时 schema 真迁移/真 SQL/真向量；离线用例 monkeypatch
生产路径（假 pool），不碰生产数据。
"""
import importlib.util
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from memory_engine import config, db
from memory_engine import recall as recall_mod
from memory_engine.util import vec_to_pg

ROOT = Path(__file__).resolve().parent.parent
MIGRATION_002 = ROOT / "scripts" / "migrations" / "002_bitemporal_graph_multihost.sql"
MIGRATION_010 = ROOT / "scripts" / "migrations" / "010_edge_weight.sql"


def _fake_pool(conn):
    return SimpleNamespace(connection=lambda: nullcontext(conn))


def _mem_fields(**over) -> dict:
    f = dict(
        id=uuid.uuid4(), bank="hermes", domain="general", trigger_term=None, title="t",
        body="b", body_ptr=None, tags=[], owner="main", visibility="agent",
        source_type="manual", source_ref=None, priority=3, ttl_state="active",
        ttl_expires_days=30, source_tier="agent", contains_pii=None,
        original_date=None, staleness="fresh", embed_model="m", embed_dim=1024,
        content_hash="h-" + uuid.uuid4().hex, embedding=None, dedup_key=None,
        valid_at=None, tenant_id=None, agent_id=None,
    )
    f.update(over)
    return f


def _e(i: int, dim: int = 1024) -> list[float]:
    """第 i 个标准基向量（消歧用例的可控嵌入源）。"""
    v = [0.0] * dim
    v[i] = 1.0
    return v


class _VecEmbedder:
    """返回固定 query 向量的假嵌入器（真实维度，避免零向量 cos=NULL 干扰断言）。"""
    device = "cpu"

    def __init__(self, qvec):
        self.qvec = qvec

    def embed_queries(self, qs):
        return [self.qvec]

    def embed_documents(self, ts):
        return [[0.0] * 1024 for _ in ts]


def _pin_routes(monkeypatch, seed_ids):
    """三路命中钉死=指定种子（vector 承接全部种子，fts/time 清空；graph 留真实 SQL）。"""
    rows = [{"id": i} for i in seed_ids]
    monkeypatch.setattr(db, "route_vector", lambda *a, **k: list(rows))
    monkeypatch.setattr(db, "route_fts", lambda *a, **k: [])
    monkeypatch.setattr(db, "route_time", lambda *a, **k: [])


# —————————————————— PG 临时 schema（真迁移 002+010，含 outcome/polarity 镜像列） ——————————————————

@pytest.fixture(scope="module")
def pg():
    try:
        conn = psycopg.connect(config.PG_DSN, autocommit=True, connect_timeout=3)
    except Exception as e:  # noqa: BLE001 —— PG 不可达=skip（不伪绿）
        pytest.skip(f"PG 不可达：{e}")
    schema = f"test_mhdt_{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(f"SET search_path TO {schema}, public")
        cur.execute("SET timezone TO 'UTC'")
        cur.execute("""
            CREATE TABLE memories (
              id uuid PRIMARY KEY,
              seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
              bank text NOT NULL DEFAULT 'hermes', domain text NOT NULL DEFAULT 'general',
              trigger_term text, title text NOT NULL DEFAULT 't', body text NOT NULL DEFAULT 'b',
              body_ptr text, tags jsonb NOT NULL DEFAULT '[]', owner text NOT NULL DEFAULT 'main',
              visibility text NOT NULL DEFAULT 'agent', source_type text NOT NULL DEFAULT 'manual',
              source_ref text, priority int NOT NULL DEFAULT 3,
              ttl_state text NOT NULL DEFAULT 'active', ttl_expires_at timestamptz,
              staleness text NOT NULL DEFAULT 'fresh', verify_status text NOT NULL DEFAULT 'unverified',
              embed_model text NOT NULL DEFAULT 'm', embed_dim int NOT NULL DEFAULT 1024,
              embed_ver int NOT NULL DEFAULT 1, content_hash text NOT NULL DEFAULT 'h',
              embedding vector(1024), source_tier text NOT NULL DEFAULT 'agent',
              contains_pii boolean, dedup_key text, original_date timestamptz,
              memory_type text NOT NULL DEFAULT 'episodic',
              pinned boolean NOT NULL DEFAULT false,
              outcome text, polarity double precision,      -- hydrate 透出列（006 同构）
              superseded_by uuid,                           -- 008 同构（supersede_memory 写谱系指针）
              access_count int NOT NULL DEFAULT 0, adopt_count int NOT NULL DEFAULT 0,   -- hydrate 全列
              created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
              search_text text GENERATED ALWAYS AS (title || ' ' || body) STORED)""")
        cur.execute("CREATE TABLE changelog (seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
                    "ts timestamptz NOT NULL DEFAULT now(), op text NOT NULL, memory_id uuid, detail jsonb)")
        cur.execute("CREATE TABLE engine_meta (key text PRIMARY KEY, value jsonb NOT NULL)")
        cur.execute("INSERT INTO engine_meta(key, value) VALUES ('schema_ver', '1'::jsonb)")
        with conn.transaction():
            cur.execute(MIGRATION_002.read_text(encoding="utf-8"))
            cur.execute(MIGRATION_010.read_text(encoding="utf-8"))
    yield conn, schema
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA {schema} CASCADE")
    conn.close()


# —————————————————— 件1：多跳 BFS（真实 SQL） ——————————————————

def test_two_hop_mem_path(pg):
    """A记忆→B记忆→C记忆 二跳路径：hops=2 拉到 C（hop=2），hops=1 只到 B。"""
    conn, _ = pg
    a, b, c = (uuid.uuid4() for _ in range(3))
    for m in (a, b, c):
        db.insert_memory(conn, **_mem_fields(id=m, title=f"链{str(m)[:4]}"))
    assert db.insert_edge(conn, a, dst_mid=b, etype="related", source="test")
    assert db.insert_edge(conn, b, dst_mid=c, etype="causal", source="test")
    rows1 = db.graph_expand(conn, [a], 1, "TRUE", [], 30)
    ids1 = {str(r["id"]): r["hop"] for r in rows1}
    assert ids1 == {str(b): 1}
    rows2 = db.graph_expand(conn, [a], 2, "TRUE", [], 30)
    ids2 = {str(r["id"]): r["hop"] for r in rows2}
    assert ids2 == {str(b): 1, str(c): 2}


def test_two_hop_entity_bridge_recall_returns_b(pg, monkeypatch):
    """验收线②：A 记忆→实体→B 记忆 经完整 recall() 返回 B（实体桥含 via 归因）。"""
    conn, _ = pg
    a, b = uuid.uuid4(), uuid.uuid4()
    ent = db.upsert_entity(conn, f"桥接实体{uuid.uuid4().hex[:6]}", "concept")
    db.insert_memory(conn, **_mem_fields(id=a, title="熔断插件配置"))
    db.insert_memory(conn, **_mem_fields(id=b, title="exit 2 allowlist 语义"))
    db.insert_edge(conn, a, entity_id=uuid.UUID(ent), etype="related", source="test")
    db.insert_edge(conn, b, entity_id=uuid.UUID(ent), etype="related", source="test")
    _pin_routes(monkeypatch, [a])          # 三路只留 A 作种子（B 必须经图路到达）
    res = recall_mod.recall(_fake_pool(conn), _VecEmbedder(_e(0)), "q", None, "main", 10, {},
                            graph_hops=1)
    by_id = {r["id"]: r for r in res["results"]}
    assert str(b) in by_id, "同实体一跳桥接必须把 B 拉回"
    gp = by_id[str(b)]["score_parts"]
    assert gp["graph"] > 0 and gp["routes"]["graph"] >= 1
    assert res["graph_meta"]["disambig_collisions"] == 0   # 无重名→不触发消歧
    res2 = recall_mod.recall(_fake_pool(conn), _VecEmbedder(_e(0)), "q", None, "main", 10, {},
                             graph_hops=0)
    assert "graph" not in res2["routes"] and res2["graph_meta"]["hops"] == 0


def test_cycle_visited_dedup(pg):
    """防环+访问去重：A→B→C→A 环（双向语义下 C/D 亦可反向前驱），每节点恰一次、
    种子不回拉、有限步终止（visited 排除复访边 c→a/d→a）。"""
    conn, _ = pg
    a, b, c, d = (uuid.uuid4() for _ in range(4))
    for m in (a, b, c, d):
        db.insert_memory(conn, **_mem_fields(id=m))
    for x, y in ((a, b), (b, c), (c, a), (c, d), (d, a)):
        db.insert_edge(conn, x, dst_mid=y, etype="related", source="test")
    rows = db.graph_expand(conn, [a], 3, "TRUE", [], 30)
    ids = [str(r["id"]) for r in rows]
    assert len(ids) == len(set(ids)) and str(a) not in ids
    assert set(ids) == {str(b), str(c), str(d)}
    hops = {str(r["id"]): r["hop"] for r in rows}
    assert hops == {str(b): 1, str(c): 1, str(d): 1}   # 无向语义：c/d 经 c→a、d→a 反向边一跳可达


def test_per_hop_topk_and_weight_product(pg):
    """每跳 top-K 截断（按边权乘积降序）+ 持久边权乘积传播（0.8×0.5=0.4）。"""
    conn, _ = pg
    s = uuid.uuid4()
    db.insert_memory(conn, **_mem_fields(id=s))
    nbrs = []
    for w in (0.9, 0.8, 0.7, 0.6, 0.5):
        m = uuid.uuid4()
        db.insert_memory(conn, **_mem_fields(id=m))
        assert db.insert_edge(conn, s, dst_mid=m, etype="related", source="test", weight=w)
        nbrs.append((str(m), w))
    rows = db.graph_expand(conn, [s], 1, "TRUE", [], 30, per_hop_k=2)
    assert len(rows) == 2
    assert {str(r["id"]) for r in rows} == {nbrs[0][0], nbrs[1][0]}   # 权重最高两个存活
    assert sorted((r["w"] for r in rows), reverse=True) == [0.9, 0.8]
    # 边权乘积：桥两跳各 0.8×0.5
    e = db.upsert_entity(conn, f"权积{uuid.uuid4().hex[:6]}", "tool")
    far = uuid.uuid4()
    db.insert_memory(conn, **_mem_fields(id=far))
    db.insert_edge(conn, s, entity_id=uuid.UUID(e), etype="related", source="test", weight=0.8)
    db.insert_edge(conn, far, entity_id=uuid.UUID(e), etype="related", source="test", weight=0.5)
    rows2 = db.graph_expand(conn, [s], 2, "TRUE", [], 30)
    hit = [r for r in rows2 if str(r["id"]) == str(far)]
    assert len(hit) == 1 and abs(hit[0]["w"] - 0.4) < 1e-9 and hit[0]["via"] == [e]


def test_hop_decay_in_graph_component(monkeypatch):
    """衰减：hop=2 的 graph 分量 = W/(RRF_K+rank)×0.5（hermetic——fake graph_expand 行）。"""
    monkeypatch.setattr(db, "route_vector", lambda *a, **k: [{"id": "m1"}])
    monkeypatch.setattr(db, "route_fts", lambda *a, **k: [])
    monkeypatch.setattr(db, "route_time", lambda *a, **k: [])
    monkeypatch.setattr(db, "graph_expand",
                        lambda *a, **k: [{"id": "m2", "hop": 1, "via": [], "w": 1.0},
                                         {"id": "m3", "hop": 2, "via": [], "w": 1.0}])
    meta = {i: {"id": i, "seq": 1, "priority": 3, "ttl_state": "active", "verify_status": "unverified",
                "staleness": "fresh", "source_tier": "agent", "title": "t", "body": "b",
                "body_ptr": None, "bank": "hermes", "domain": "d", "tags": [], "owner": "main",
                "visibility": "agent", "trigger_term": None, "source_ref": None,
                "contains_pii": None, "created_at": None, "updated_at": None,
                "memory_type": "episodic", "outcome": None, "polarity": None}
            for i in ("m1", "m2", "m3")}
    monkeypatch.setattr(db, "hydrate", lambda c, ids_: {i: meta[i] for i in ids_ if i in meta})
    monkeypatch.setattr(recall_mod, "_graph_seeds", lambda *a: ["m1"])
    res = recall_mod.recall(_fake_pool(SimpleNamespace()), _OkEmbedder(), "q", None, "main", 10, {})
    gp = {r["id"]: r["score_parts"] for r in res["results"]}
    g1 = gp["m2"]["graph"]
    g2 = gp["m3"]["graph"]
    r1, r2 = gp["m2"]["routes"]["graph"], gp["m3"]["routes"]["graph"]
    assert abs(g1 - round(config.W_GRAPH / (config.RRF_K + r1), 6)) < 1e-9
    assert abs(g2 - round(config.W_GRAPH / (config.RRF_K + r2) * config.GRAPH_HOP_DECAY, 6)) < 1e-9


class _OkEmbedder:
    device = "cuda"

    def embed_queries(self, qs):
        return [[0.1] * 1024]

    def embed_documents(self, ts):
        return [[0.1] * 1024]


def test_graph_hops_param_clamp_and_passthrough(monkeypatch):
    """graph_hops：>上限钳到 GRAPH_HOPS_MAX；API 层越界（含负数）=400 带原因。"""
    from fastapi import HTTPException
    from memory_engine import api_core
    seen = {}
    seed = uuid.uuid4()
    monkeypatch.setattr(db, "route_vector", lambda *a, **k: [{"id": seed}])
    monkeypatch.setattr(db, "route_fts", lambda *a, **k: [])
    monkeypatch.setattr(db, "route_time", lambda *a, **k: [])
    monkeypatch.setattr(db, "hydrate", lambda c, ids_: {})   # 图/邻全空→无条目可回灌，绕开假 conn

    def _g(c, seeds, hops, *a, **k):
        seen["hops"] = hops
        return []
    monkeypatch.setattr(db, "graph_expand", _g)
    pool = _fake_pool(SimpleNamespace())
    recall_mod.recall(pool, _OkEmbedder(), "q", None, "main", 5, {}, graph_hops=99)
    assert seen["hops"] == config.GRAPH_HOPS_MAX
    recall_mod.recall(pool, _OkEmbedder(), "q", None, "main", 5, {}, graph_hops=1)
    assert seen["hops"] == 1
    eng = SimpleNamespace(db=pool, embedder=_OkEmbedder(), reranker=None)
    req = _FakeRequest(eng)
    for bad in (5, -1):
        with pytest.raises(HTTPException) as ei:
            api_core.recall(api_core.RecallRequest(query="q", graph_hops=bad), req, _FakeBG())
        assert ei.value.status_code == 400 and "graph_hops" in str(ei.value.detail)
    ok = api_core.RecallRequest(query="q", graph_hops=3)
    assert ok.graph_hops == 3


class _FakeBG:
    def add_task(self, *a, **k):
        pass


def _FakeRequest(eng):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=eng)))


# —————————————————— 件2：实体消歧（真 PG + 真向量几何） ——————————————————

def _build_collision(pg):
    """造两个同名不同 etype 实体（org/tool），各桥接一组记忆；返回构件句柄。"""
    conn, _ = pg
    name = f"飞书{uuid.uuid4().hex[:6]}"          # 同名（小写归一后 collision）
    s, n_org, n_tool = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ent_org = uuid.UUID(db.upsert_entity(conn, name, "org"))
    ent_tool = uuid.UUID(db.upsert_entity(conn, name, "tool"))
    db.insert_memory(conn, **_mem_fields(id=s, embedding=vec_to_pg(_e(0))))
    db.insert_memory(conn, **_mem_fields(id=n_org, embedding=vec_to_pg(_e(1))))
    db.insert_memory(conn, **_mem_fields(id=n_tool, embedding=vec_to_pg(_e(2))))
    db.insert_edge(conn, s, entity_id=ent_org, etype="related", source="test")
    db.insert_edge(conn, n_org, entity_id=ent_org, etype="related", source="test")
    db.insert_edge(conn, s, entity_id=ent_tool, etype="related", source="test")
    db.insert_edge(conn, n_tool, entity_id=ent_tool, etype="related", source="test")
    return conn, s, n_org, n_tool, ent_org, ent_tool


def test_disambiguation_picks_right_edge(pg, monkeypatch):
    """验收线③：q 嵌入贴近 org 邻域（均值含 e1）→ tool 桥邻居 graph 分量被罚、org 桥不罚。"""
    conn, s, n_org, n_tool, ent_org, ent_tool = _build_collision(pg)
    q = _e(0)
    q[0], q[1] = 1.0, 2.0                       # closer to mean(e0,e1) than mean(e0,e2)
    _pin_routes(monkeypatch, [s])
    res = recall_mod.recall(_fake_pool(conn), _VecEmbedder(q), "q", None, "main", 10, {},
                            graph_hops=1)
    gp = {r["id"]: r["score_parts"] for r in res["results"]}
    assert str(n_org) in gp and str(n_tool) in gp, "两条同名实体桥都必须先被拉到"
    ratio = gp[str(n_org)]["graph"] / gp[str(n_tool)]["graph"]
    assert ratio > 2.0, f"败方应被罚至 ~0.3×，实测 org/tool 分量比 {ratio:.3f}"
    assert res["graph_meta"]["disambig_collisions"] == 1
    assert res["graph_meta"]["disambig_penalized"] == 1


def test_disambiguation_margin_and_off(pg, monkeypatch):
    """cos 差 ≤ margin=真歧义不罚；GRAPH_DISAMBIG=off 时零消歧行为。"""
    conn, s, n_org, n_tool, ent_org, ent_tool = _build_collision(pg)
    rows_g = [{"id": n_org, "hop": 1, "via": [str(ent_org)], "w": 1.0},
              {"id": n_tool, "hop": 1, "via": [str(ent_tool)], "w": 1.0}]
    q_amb = _e(0)
    q_amb[1], q_amb[2] = 1.0, 1.0               # 与两邻域均值等距
    factors, n_coll = recall_mod._graph_disambiguate(conn, q_amb, rows_g, None)
    assert n_coll == 1 and factors == {}, "等距=真歧义，宁保守不罚"
    q_org = _e(0)
    q_org[1] = 2.0
    factors2, _ = recall_mod._graph_disambiguate(conn, q_org, rows_g, None)
    assert factors2.get(n_tool) == config.GRAPH_DISAMBIG_PENALTY
    monkeypatch.setattr(config, "GRAPH_DISAMBIG", False)
    factors3, n3 = recall_mod._graph_disambiguate(conn, q_org, rows_g, None)
    assert factors3 == {} and n3 == 0           # 开关关闭=零行为


# —————————————————— 件3：as_of 时序边失效（真实 SQL） ——————————————————

def _mk_temporal(pg, tag):
    """S→X（旧边 2026-01..2026-06 失效）、S→Y（现行边）两时态；记忆 valid_at 同 2026-01。"""
    conn, _ = pg
    s, x, y = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for m in (s, x, y):
        db.insert_memory(conn, **_mem_fields(id=m, valid_at=t0, title=f"{tag}{str(m)[:4]}"))
    e1 = db.insert_edge(conn, s, dst_mid=x, etype="related", valid_at=t0, source="test")
    assert db.insert_edge(conn, s, dst_mid=y, etype="related", valid_at=t0, source="test")
    db.execute(conn, "UPDATE edges SET invalid_at=%s::timestamptz WHERE id=%s",
               ("2026-06-01T00:00:00+00:00", e1))
    return conn, s, x, y


def test_graph_asof_time_windows(pg):
    """验收线④：缺省只召回现行边（X 被失效边隔离）；as_of=窗口内「当时的图」X 复现；
    as_of=窗口外/早于 valid_at 均不含 X；现行边 Y 全程在场（窗内）。"""
    conn, s, x, y = _mk_temporal(pg, "asof")
    now_default = db.graph_expand(conn, [s], 1, "TRUE", [], 30)
    ids = {str(r["id"]) for r in now_default}
    assert ids == {str(y)}, "invalid_at 已置位的历史边缺省不得召回"
    t_in = datetime(2026, 3, 1, tzinfo=timezone.utc)
    as_in = db.graph_expand(conn, [s], 1, "TRUE", [], 30, as_of=t_in)
    assert {str(r["id"]) for r in as_in} == {str(x), str(y)}
    t_out = datetime(2026, 7, 1, tzinfo=timezone.utc)
    as_out = {str(r["id"]) for r in db.graph_expand(conn, [s], 1, "TRUE", [], 30, as_of=t_out)}
    assert as_out == {str(y)}
    t_pre = datetime(2025, 1, 1, tzinfo=timezone.utc)
    as_pre = db.graph_expand(conn, [s], 1, "TRUE", [], 30, as_of=t_pre)
    assert as_pre == [], "边 valid_at 之前=图还不存在，全空（当时的图诚实快照）"


def test_recall_asof_reaches_graph_route(pg, monkeypatch):
    """filters.as_of 贯穿到图遍历（此前图路完全无视 as_of=本批补的实缺口）。"""
    conn, s, x, y = _mk_temporal(pg, "prop")
    seen = {}
    real = db.graph_expand

    def _spy(c, seeds, hops, vis_sql, vis_params, limit, **kw):
        seen.update(kw)
        return real(c, seeds, hops, vis_sql, vis_params, limit, **kw)
    monkeypatch.setattr(db, "graph_expand", _spy)
    _pin_routes(monkeypatch, [s])
    res = recall_mod.recall(_fake_pool(conn), _VecEmbedder(_e(0)), "q", None, "main", 10,
                            {"as_of": "2026-03-01T00:00:00+00:00"})
    assert seen.get("as_of") == datetime(2026, 3, 1, tzinfo=timezone.utc)
    ids = {r["id"] for r in res["results"]}
    assert str(x) in ids, "as_of=窗内历史查询必须经图路带回已失效边的目标"
    # 校验器兜底（既有行为不回退）
    with pytest.raises(ValueError):
        recall_mod.validate_filters({"as_of": "not-a-date"})


def test_migration_010_idempotent(pg):
    """迁移 010 重跑零副作用：weight 列/三伴生索引在位，既有边权重默认 1.0。"""
    conn, schema = pg
    sql = MIGRATION_010.read_text(encoding="utf-8")
    with conn.transaction():
        conn.execute(sql)
    cols = {r["column_name"] for r in db.fetch_all(
        conn, "SELECT column_name FROM information_schema.columns "
              "WHERE table_schema=%s AND table_name='edges'", (schema,))}
    assert "weight" in cols
    idx = {r["indexname"] for r in db.fetch_all(
        conn, "SELECT indexname FROM pg_indexes WHERE schemaname=%s", (schema,))}
    assert {"idx_edges_src_all", "idx_edges_dst_all", "idx_edges_entity_all"} <= idx
    m1, m2 = uuid.uuid4(), uuid.uuid4()
    db.insert_memory(conn, **_mem_fields(id=m1))
    db.insert_memory(conn, **_mem_fields(id=m2))
    eid = db.insert_edge(conn, m1, dst_mid=m2, etype="related", source="wcheck010")
    row = db.fetch_one(conn, "SELECT weight FROM edges WHERE id=%s", (eid,))
    assert row["weight"] == 1.0                    # 缺省边权=1.0（零行为）
    with pytest.raises(ValueError):
        db.insert_edge(conn, uuid.uuid4(), dst_mid=uuid.uuid4(), weight=0)   # 防呆：weight>0


def test_edge_temporal_audit_candidates(pg):
    """回填评估 SQL：T1 端点 retired、T2 端点 superseded 各命中；proposed_invalid_at 取端点失效时刻；
    健康边（双现行）不入清单（只读评估，绝不 UPDATE）。"""
    conn, _ = pg
    retired_src = uuid.uuid4()
    db.insert_memory(conn, **_mem_fields(id=retired_src, ttl_state="retired",
                                         valid_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    live = uuid.uuid4()
    db.insert_memory(conn, **_mem_fields(id=live))
    db.insert_edge(conn, retired_src, dst_mid=live, etype="related", source="test")
    old = db.insert_memory(conn, **_mem_fields(body="旧版"))
    assert db.supersede_memory(conn, old["id"], _mem_fields(body="新版")) is not None
    db.execute(conn, "INSERT INTO edges(id, src_mid, dst_mid, etype, source) "
                     "VALUES (%s,%s,%s,'related','test')",
               (str(uuid.uuid4()), old["id"], live))
    spec = importlib.util.spec_from_file_location("eta", ROOT / "scripts" / "edge_temporal_audit.py")
    eta = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eta)
    rows = db.fetch_all(conn, eta.CANDIDATE_SQL)
    reasons = {r["reason"] for r in rows}
    assert {"T1_retired_endpoint", "T2_superseded_endpoint"} <= reasons
    t2 = [r for r in rows if r["reason"] == "T2_superseded_endpoint"][0]
    old_row = db.fetch_one(conn, "SELECT invalid_at FROM memories WHERE id=%s", (old["id"],))
    assert t2["proposed_invalid_at"] == old_row["invalid_at"]
    healthy = db.fetch_one(
        conn, "SELECT count(*) c FROM edges e WHERE e.invalid_at IS NULL AND ("
              "EXISTS (SELECT 1 FROM memories m WHERE m.id=e.src_mid AND NOT m.is_current) OR "
              "EXISTS (SELECT 1 FROM memories m WHERE m.id=e.dst_mid AND NOT m.is_current) OR "
              "EXISTS (SELECT 1 FROM memories m WHERE m.id=e.src_mid AND m.ttl_state='retired') OR "
              "EXISTS (SELECT 1 FROM memories m WHERE m.id=e.dst_mid AND m.ttl_state='retired'))")
    assert healthy["c"] == len(rows)   # 候选集闭合=评估 SQL 判定面无漏报/误报
