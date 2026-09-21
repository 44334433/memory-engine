"""图谱深度批用例（2026-09-19，backlog supersede-chain-ashof ①②）：
取代链多跳（链遍历正确 / max_hops 防环 / changelog 回填 / 断链旗标）+ 2 跳邻居
（BFS 遍历 / as-of 边过滤 / etype 收窄 / limit 护栏）+ 活体端点（真实链回放 / 404 / 422）。

仿 test_p1b 纪律：PG 用例在专用临时 schema 跑真实迁移 SQL + 真实递归 CTE（不碰生产数据，结束即删）；
活体用例走 conftest.live_server（daemon 不可达即 skip 不伪绿）。
"""
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import psycopg
import pytest

from memory_engine import config, db
from memory_engine.api_graph_depth import neighbors, walk_chain

MIGRATION_002 = (Path(__file__).resolve().parent.parent
                 / "scripts" / "migrations" / "002_bitemporal_graph_multihost.sql")
MIGRATION_008 = (Path(__file__).resolve().parent.parent
                 / "scripts" / "migrations" / "008_superseded_by.sql")
MIGRATION_010 = (Path(__file__).resolve().parent.parent
                 / "scripts" / "migrations" / "010_edge_weight.sql")


def _mem_fields(**over) -> dict:
    """insert_memory 全字段构造器（与 test_p1b 同款，供 PG 用例造数）。"""
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


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


# —————————————————— PG 临时 schema 夹具（002+008 真实迁移） ——————————————————

@pytest.fixture(scope="module")
def pg():
    try:
        conn = psycopg.connect(config.PG_DSN, autocommit=True, connect_timeout=3)
    except Exception as e:  # noqa: BLE001 —— PG 不可达=skip（不伪绿）
        pytest.skip(f"PG 不可达（{config.PG_DSN.split('@')[-1]}）：{e}")
    schema = f"test_gdepth_{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(f"SET search_path TO {schema}, public")   # vector 扩展类型在 public
        cur.execute("SET timezone TO 'UTC'")
        # memories 最小镜像（列覆盖 RETAIN_SQL + 迁移 002 所需；与 test_p1b 同构）
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
              created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
              search_text text GENERATED ALWAYS AS (title || ' ' || body) STORED)""")
        cur.execute("CREATE TABLE changelog (seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
                    "ts timestamptz NOT NULL DEFAULT now(), op text NOT NULL, memory_id uuid, detail jsonb)")
        cur.execute("CREATE TABLE engine_meta (key text PRIMARY KEY, value jsonb NOT NULL)")
        cur.execute("INSERT INTO engine_meta(key, value) VALUES ('schema_ver', '1'::jsonb)")
        with conn.transaction():
            cur.execute(MIGRATION_002.read_text(encoding="utf-8"))
            cur.execute(MIGRATION_008.read_text(encoding="utf-8"))
            cur.execute(MIGRATION_010.read_text(encoding="utf-8"))   # 多跳批：insert_edge 含 weight 列
    yield conn, schema
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA {schema} CASCADE")
    conn.close()


def _build_chain(conn, n: int, t0: datetime, step_min: int = 10) -> list[dict]:
    """用生产写路径 db.supersede_memory 造 n 代真链（a0→a1→…，valid_at 逐代 +step）。"""
    first = db.insert_memory(conn, **_mem_fields(body="v0", valid_at=t0))
    chain = [dict(id=str(first["id"]), valid_at=t0)]
    prev = first["id"]
    for i in range(1, n):
        va = t0 + timedelta(minutes=step_min * i)
        res = db.supersede_memory(conn, prev, _mem_fields(body=f"v{i}", valid_at=va))
        assert res is not None
        chain.append(dict(id=res["new_id"], valid_at=va))
        prev = uuid.UUID(res["new_id"])
    return chain


# —————————————————— ① 迁移 008：幂等 + changelog 回填 ——————————————————

def test_migration_008_idempotent_and_backfill(pg):
    conn, schema = pg
    cols = {r["column_name"] for r in db.fetch_all(
        conn, "SELECT column_name FROM information_schema.columns "
              "WHERE table_schema=%s AND table_name='memories'", (schema,))}
    assert "superseded_by" in cols
    idx = {r["indexname"] for r in db.fetch_all(
        conn, "SELECT indexname FROM pg_indexes WHERE schemaname=%s", (schema,))}
    assert "idx_mem_superseded_by" in idx
    # 回填：账在且目标行在 → 指针对；目标被 purge → 跳过不炸；一旧多账=取最新（seq DESC）
    a, b = uuid.uuid4(), uuid.uuid4()
    ghost = uuid.uuid4()
    for x in (a, b):
        db.insert_memory(conn, **_mem_fields(id=x))
    db.execute(conn, "INSERT INTO changelog(op, memory_id, detail) VALUES ('supersede', %s, %s::jsonb)",
               (a, json.dumps({"new_id": str(ghost)})))   # 旧账目标不存在（被 purge）
    db.execute(conn, "INSERT INTO changelog(op, memory_id, detail) VALUES ('supersede', %s, %s::jsonb)",
               (a, json.dumps({"new_id": str(b)})))       # 新账目标在 → 最新者胜
    db.execute(conn, "INSERT INTO changelog(op, memory_id, detail) VALUES ('supersede', %s, %s::jsonb)",
               (uuid.uuid4(), json.dumps({"new_id": str(uuid.uuid4())})))   # 旧行已 purge 的孤立账
    with conn.transaction():
        conn.execute(MIGRATION_008.read_text(encoding="utf-8"))   # 重跑（夹具已应用一次+新账）
    assert str(db.fetch_one(conn, "SELECT superseded_by FROM memories WHERE id=%s", (a,))["superseded_by"]) == str(b)
    n_links = db.fetch_one(
        conn, "SELECT count(*) c FROM memories WHERE superseded_by IS NOT NULL")["c"]
    with conn.transaction():
        conn.execute(MIGRATION_008.read_text(encoding="utf-8"))   # 三次执行=零副作用
    assert db.fetch_one(conn, "SELECT count(*) c FROM memories WHERE superseded_by IS NOT NULL")["c"] == n_links


# —————————————————— ② 写路径：supersede 即谱系（无需回填） ——————————————————

def test_supersede_memory_sets_superseded_by(pg):
    conn, _ = pg
    chain = _build_chain(conn, 3, _dt("2026-09-01T00:00:00+00:00"))
    ptrs = [db.fetch_one(conn, "SELECT superseded_by FROM memories WHERE id=%s", (uuid.UUID(c["id"]),))
            ["superseded_by"] for c in chain]
    assert [str(p) if p else None for p in ptrs] == [chain[1]["id"], chain[2]["id"], None]


# —————————————————— ③ 链遍历正确性（任意 seed，双时序窗口齐全） ——————————————————

def test_chain_traversal_any_seed(pg):
    conn, _ = pg
    chain = _build_chain(conn, 4, _dt("2026-09-02T00:00:00+00:00"))
    ids = [c["id"] for c in chain]
    for seed_i, expect in ((0, [0, 1, 2, 3]), (2, [-2, -1, 0, 1])):
        res = walk_chain(conn, uuid.UUID(ids[seed_i]), 5)
        assert [v["id"] for v in res["versions"]] == ids      # head→tail 全链有序
        assert res["head"] == ids[0] and res["tail"] == ids[-1]
        assert [v["hop_from_seed"] for v in res["versions"]] == expect
        assert not res["cycle_detected"] and not res["truncated_forward"] and not res["truncated_back"]
        # 时间窗：旧条 invalid_at = 后继 valid_at（时间截断），尾条现行无失效
        for v, nxt in zip(res["versions"], res["versions"][1:]):
            assert v["invalid_at"] is not None and v["invalid_at"] == nxt["valid_at"]
            assert not v["is_current"]
        assert res["versions"][-1]["invalid_at"] is None and res["versions"][-1]["is_current"]


# —————————————————— ④ max_hops 截断显式化 ——————————————————

def test_chain_max_hops_truncation_flags(pg):
    conn, _ = pg
    chain = _build_chain(conn, 6, _dt("2026-09-03T00:00:00+00:00"))
    res = walk_chain(conn, uuid.UUID(chain[0]["id"]), max_hops=2)
    assert len(res["versions"]) == 3                      # seed + 2 跳
    assert res["truncated_forward"] is True and res["truncated_back"] is False
    assert res["cycle_detected"] is False
    # 从链尾回看：截断方向翻转
    res2 = walk_chain(conn, uuid.UUID(chain[-1]["id"]), max_hops=1)
    assert len(res2["versions"]) == 2
    assert res2["truncated_back"] is True and res2["truncated_forward"] is False


# —————————————————— ⑤ 防环（手改数据造 3-环，有限步终止+显式旗标） ——————————————————

def test_chain_cycle_guard(pg):
    conn, _ = pg
    m = [uuid.uuid4() for _ in range(3)]
    for x in m:
        db.insert_memory(conn, **_mem_fields(id=x, body="环成员"))
    # 构造 a→b→c→a 非法环（生产写路径不可能产生；模拟脏数据，铁律=必须有限步终止）
    for src, dst in zip(m, m[1:] + m[:1]):
        db.execute(conn, "UPDATE memories SET superseded_by=%s WHERE id=%s", (dst, src))
    res = walk_chain(conn, m[0], max_hops=10)             # 无防环=无限递归/栈爆；有防环=正常返回
    assert res["cycle_detected"] is True
    assert {v["id"] for v in res["versions"]} == {str(x) for x in m}   # 每节点至多一次
    assert len(res["versions"]) == 3


# —————————————————— ⑥ 邻居遍历：2 跳 + 实体桥 + 环 ——————————————————

def test_neighbors_two_hop_entity_bridge(pg):
    conn, _ = pg
    m = {k: uuid.uuid4() for k in "abcd"}
    for k, x in m.items():
        db.insert_memory(conn, **_mem_fields(id=x, body=f"节点{k}"))
    ent = db.upsert_entity(conn, "共享概念X", "concept")
    db.insert_edge(conn, m["a"], dst_mid=m["b"], etype="related", source="test")
    db.insert_edge(conn, m["b"], dst_mid=m["c"], etype="causal", source="test")
    db.insert_edge(conn, m["a"], entity_id=ent, etype="related", source="test")
    db.insert_edge(conn, m["d"], entity_id=ent, etype="related", source="test")
    res = neighbors(conn, m["a"], "memory", hops=2)
    hop_of = {n["id"]: (n["hop"], n["type"]) for n in res["nodes"]}
    assert hop_of[str(m["b"])] == (1, "memory")
    assert hop_of[str(m["c"])] == (2, "memory")           # 记忆→记忆→记忆
    assert hop_of[ent] == (1, "entity")                   # 记忆→实体
    assert hop_of[str(m["d"])] == (2, "memory")           # 记忆→实体→记忆（实体桥）
    assert str(m["a"]) not in hop_of                      # 种子不回拉
    assert res["counts"]["nodes"] == len(res["nodes"]) == len(hop_of)
    # 树边闭合：每条边两端都在节点集+种子
    ids = set(hop_of) | {str(m["a"])}
    for e in res["edges"]:
        assert e["source"] in ids and e["target"] in ids
    # hops=1 收敛
    r1 = neighbors(conn, m["a"], "memory", hops=1)
    assert {n["id"] for n in r1["nodes"]} == {str(m["b"]), ent}
    # 环：a-b 双向重复引用 + b-a：seen 防重，每节点仍只返回一次
    db.insert_edge(conn, m["c"], dst_mid=m["a"], etype="related", source="test")   # 三角环
    r2 = neighbors(conn, m["a"], "memory", hops=3)
    assert len({n["id"] for n in r2["nodes"]}) == len(r2["nodes"])                 # 无重复节点
    assert r2["counts"]["visited"] <= 4                                             # 环未引爆遍历


# —————————————————— ⑦ as-of 边过滤（事件时间窗） ——————————————————

def test_neighbors_asof_edge_and_node_filter(pg):
    conn, _ = pg
    x, y, z = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    future = datetime.now(timezone.utc) + timedelta(days=1)
    # 节点自身也有事件时间窗：x 从 09-01 起、y 从 09-05 起、z 明天才生效（与边窗口配套）
    db.insert_memory(conn, **_mem_fields(id=x, body=f"asof{x.hex[:2]}", valid_at=_dt("2026-09-01T00:00:00+00:00")))
    db.insert_memory(conn, **_mem_fields(id=y, body=f"asof{y.hex[:2]}", valid_at=_dt("2026-09-05T00:00:00+00:00")))
    db.insert_memory(conn, **_mem_fields(id=z, body=f"asof{z.hex[:2]}", valid_at=future))
    # 边 x→y：valid 09-05，09-10 失效；边 x→z：明天才生效（未来边，现行口径不可见）
    db.insert_edge(conn, x, dst_mid=y, etype="related", source="test")
    db.execute(conn, "UPDATE edges SET valid_at=%s, invalid_at=%s WHERE src_mid=%s AND dst_mid=%s",
               (_dt("2026-09-05T00:00:00+00:00"), _dt("2026-09-10T00:00:00+00:00"), x, y))
    db.insert_edge(conn, x, dst_mid=z, etype="related", valid_at=future, source="test")
    # 缺省（现行口径=invalid_at IS NULL，与 /v1/graph 既有语义一致）：失效边隐藏，z 边现行可见
    assert {n["id"] for n in neighbors(conn, x, "memory", hops=1)["nodes"]} == {str(z)}
    # as-of 窗口内：只拉 x→y
    r = neighbors(conn, x, "memory", hops=1, as_of=_dt("2026-09-07T00:00:00+00:00"))
    assert {n["id"] for n in r["nodes"]} == {str(y)}
    # as-of 窗口外（边失效后）：拉空
    r2 = neighbors(conn, x, "memory", hops=1, as_of=_dt("2026-09-11T00:00:00+00:00"))
    assert {n["id"] for n in r2["nodes"]} == set()
    # 节点时窗：y 被取代后，as_of 早于其 valid_at → 不可见
    db.execute(conn, "UPDATE memories SET valid_at=%s WHERE id=%s",
               (_dt("2026-09-08T00:00:00+00:00"), y))
    r3 = neighbors(conn, x, "memory", hops=1, as_of=_dt("2026-09-07T00:00:00+00:00"))
    assert {n["id"] for n in r3["nodes"]} == set()


# —————————————————— ⑧ etype 收窄 + limit 护栏 ——————————————————

def test_neighbors_etype_and_limit_guards(pg):
    conn, _ = pg
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for k in (a, b, c):
        db.insert_memory(conn, **_mem_fields(id=k, body=f"guard{k.hex[:2]}"))
    db.insert_edge(conn, a, dst_mid=b, etype="related", source="test")
    db.insert_edge(conn, a, dst_mid=c, etype="causal", source="test")
    r = neighbors(conn, a, "memory", hops=1, etype="causal")
    assert {n["id"] for n in r["nodes"]} == {str(c)}
    r_lim = neighbors(conn, a, "memory", hops=1, limit=1)
    assert r_lim["counts"]["truncated"] is True and len(r_lim["nodes"]) == 1


# —————————————————— ⑨ 活体端点（真实链回放 + 参数校验；daemon 不可达 skip） ——————————————————

@pytest.fixture(scope="module")
def client(live_server):
    with httpx.Client(base_url=live_server, timeout=30.0) as c:
        yield c


def test_live_chain_real_lineage_replay(client):
    """真实数据链回放：库内既有 supersede 谱系（含 008 回填行）逐链 GET，断言结构不变量。"""
    r = client.get("/v1/memories", params={"limit": 5000})
    assert r.status_code == 200
    rows = r.json()
    rows = rows.get("items", rows) if isinstance(rows, dict) else rows
    linked = [x for x in rows if x.get("invalid_at")]
    if not linked:
        pytest.skip("库内无失效（被取代）版本行（fresh DB）；谱系回放仅活库跑")
    checked = 0
    for x in linked:
        rc = client.get(f"/v1/memories/{x['id']}/chain", params={"max_hops": 5})
        assert rc.status_code == 200, rc.text
        body = rc.json()
        assert body["length"] >= 1 and body["seed"] == x["id"]
        assert body["versions"][0]["id"] == body["head"]
        assert not body["cycle_detected"]
        hops = [v["hop_from_seed"] for v in body["versions"]]
        assert hops == sorted(hops) and 0 in hops          # 有序 + seed 在链上
        for v, nxt in zip(body["versions"], body["versions"][1:]):
            assert v["invalid_at"] == nxt["valid_at"]      # 时间窗首尾相接
        checked += 1
        if checked >= 5:
            break
    assert checked > 0


def test_live_chain_smoke_build_and_404(client):
    """活体端到端：retain→supersede×2→链回放→max_hops 截断→purge 清理（真库零残留）。"""
    st = client.post("/v1/retain", json={"bank": "hermes", "caller": "subagent:gdepth-test",
                                         "items": [{"content": f"链深冒烟 v1 {uuid.uuid4().hex[:6]}",
                                                    "context": "图谱深度批活体测试"}]})
    assert st.status_code == 200
    mid = st.json()["ids"][0]
    made = [mid]
    try:
        for i in (2, 3):
            sp = client.patch(f"/v1/memories/{mid}", json={"body": f"链深冒烟 v{i} {uuid.uuid4().hex[:6]}",
                                                           "supersede": True,
                                                           "valid_at": f"2026-09-{10+i}T00:00:00+00:00"})
            assert sp.status_code == 200, sp.text
            mid = sp.json()["id"]
            made.append(mid)
        rc = client.get(f"/v1/memories/{made[0]}/chain")
        assert rc.status_code == 200
        body = rc.json()
        assert body["length"] == 3 and [v["id"] for v in body["versions"]] == made
        assert body["versions"][-1]["is_current"] and body["versions"][-1]["invalid_at"] is None
        assert body["tail"] == made[-1]
        # max_hops 截断显式化（链头出发限 1 跳）
        rt = client.get(f"/v1/memories/{made[0]}/chain", params={"max_hops": 1})
        assert rt.status_code == 200 and rt.json()["length"] == 2
        assert rt.json()["truncated_forward"] is True
    finally:
        for cid in made:
            client.delete(f"/v1/memories/{cid}?purge=true")
    # 404 语义
    assert client.get(f"/v1/memories/{uuid.uuid4()}/chain").status_code == 404


def test_live_neighbors_invariants(client):
    """活体 2 跳：从全图挑一条真实边做种子，断言 BFS 不变量；as_of 远古=空窗。"""
    g = client.get("/v1/graph", params={"limit": 50}).json()
    mem_edges = [e for e in g["edges"] if e["relation"]]
    if not mem_edges:
        pytest.skip("库内无现行边（fresh DB 未跑抽取服务）；BFS 不变量仅活库回放")
    seed = mem_edges[0]["source"]
    r = client.get("/v1/graph/neighbors", params={"id": seed, "hops": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["seed"]["id"] == seed and body["hops"] == 2
    hops = [n["hop"] for n in body["nodes"]]
    assert hops and min(hops) >= 1 and max(hops) <= 2
    assert len({n["id"] for n in body["nodes"]}) == len(body["nodes"])   # 无重复（防环）
    ids = {n["id"] for n in body["nodes"]} | {seed}
    for e in body["edges"]:
        assert e["source"] in ids and e["target"] in ids
        assert e["hop"] <= 2
    # as-of 远古：图谱建于 2026-09，2020 时点应零边（边过滤语义）
    r2 = client.get("/v1/graph/neighbors", params={"id": seed, "hops": 2, "as_of": "2020-01-01T00:00:00+00:00"})
    assert r2.status_code == 200 and r2.json()["edges"] == []
    # 参数校验：坏 etype=422；不存在 seed=404；hops 钳制
    assert client.get("/v1/graph/neighbors", params={"id": seed, "etype": "bogus"}).status_code == 422
    assert client.get("/v1/graph/neighbors", params={"id": str(uuid.uuid4())}).status_code == 404
    assert client.get("/v1/graph/neighbors", params={"id": seed, "hops": 99}).status_code == 200
