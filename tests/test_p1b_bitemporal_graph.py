"""P1 第二批新增用例（2026-09-16）：迁移幂等 1 + 时间截断 1 + 图召回 1 跳 1 + tenant 过滤 1
+ contradicts 不动 memories 1。

PG 用例在专用临时 schema 跑**真实迁移 SQL / 生成列 / 递归 CTE**（不碰生产数据，结束即删）；
离线用例假 pool/conn 直调生产代码路径（与 P0/P1 一批套件同风格）。
"""
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from memory_engine import config, db
from memory_engine import recall as recall_mod
from memory_engine.api_core import RetainItem

MIGRATION_002 = (Path(__file__).resolve().parent.parent
                 / "scripts" / "migrations" / "002_bitemporal_graph_multihost.sql")


def _fake_pool(conn):
    return SimpleNamespace(connection=lambda: nullcontext(conn))


def _fake_request(eng):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=eng)))


def _meta(mid: str) -> dict:
    return {"id": mid, "seq": 1, "priority": 3, "ttl_state": "active", "verify_status": "unverified",
            "staleness": "fresh", "source_tier": "agent", "title": "t", "body": "b", "body_ptr": None,
            "bank": "hermes", "domain": "d", "tags": [], "owner": "main", "visibility": "agent",
            "trigger_term": None, "source_ref": None, "contains_pii": None,
            "created_at": None, "updated_at": None, "memory_type": "episodic"}   # W2：recall 结果透出类型列


class _OkEmbedder:
    device = "cuda"

    def embed_queries(self, qs):
        return [[0.0] * 1024]

    def embed_documents(self, ts):
        return [[0.0] * 1024]


def _mem_fields(**over) -> dict:
    """insert_memory 全字段构造器（生产写入路径，供 PG 用例造数）。"""
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


# —————————————————— PG 临时 schema 夹具（模块级，真实迁移 + 最小镜像表） ——————————————————

@pytest.fixture(scope="module")
def pg():
    try:
        conn = psycopg.connect(config.PG_DSN, autocommit=True, connect_timeout=3)
    except Exception as e:  # noqa: BLE001 —— PG 不可达=skip（不伪绿）
        pytest.skip(f"PG 不可达（{config.PG_DSN.split('@')[-1]}）：{e}")
    schema = f"test_p1b_{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(f"SET search_path TO {schema}, public")   # vector 扩展类型在 public
        cur.execute("SET timezone TO 'UTC'")                  # 时间断言统一 UTC 口径
        # memories 最小镜像（列覆盖迁移 002 + RETAIN_SQL + 用例所需；约束放宽仅保 NOT NULL 骨架）
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
              memory_type text NOT NULL DEFAULT 'episodic',   -- W2：RETAIN_SQL index 27（镜像须随生产 SQL 补列）
              pinned boolean NOT NULL DEFAULT false,          -- W1：RETAIN_SQL index 28
              superseded_by uuid,                             -- 008 图谱深度批：supersede_memory 写谱系指针（镜像随生产补列）
              created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
              search_text text GENERATED ALWAYS AS (title || ' ' || body) STORED)""")
        cur.execute("CREATE TABLE changelog (seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
                    "ts timestamptz NOT NULL DEFAULT now(), op text NOT NULL, memory_id uuid, detail jsonb)")
        cur.execute("CREATE TABLE engine_meta (key text PRIMARY KEY, value jsonb NOT NULL)")
        cur.execute("INSERT INTO engine_meta(key, value) VALUES ('schema_ver', '1'::jsonb)")
        # 迁移前种子行（created_at 固定，验证 valid_at 回填=created_at）
        cur.execute("INSERT INTO memories (id, created_at) VALUES (%s, '2026-01-01T00:00:00+00:00')",
                    (uuid.uuid4(),))
        cur.execute("INSERT INTO memories (id, created_at) VALUES (%s, '2026-02-01T00:00:00+00:00')",
                    (uuid.uuid4(),))
        # 首次应用迁移 002（生产同款 SQL，事务内整体应用）
        with conn.transaction():
            cur.execute(MIGRATION_002.read_text(encoding="utf-8"))
    yield conn, schema
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA {schema} CASCADE")
    conn.close()


# —————————————————— ① 迁移幂等（PG 真实迁移 SQL 二次执行） ——————————————————

def test_migration_002_idempotent(pg):
    """迁移 002 重跑零副作用：对象齐全、种子回填 valid_at=created_at、行数不变、schema_ver=2。"""
    conn, schema = pg
    sql = MIGRATION_002.read_text(encoding="utf-8")
    with conn.transaction():          # 第二次应用（夹具已应用过一次）——幂等即「重跑无错误无副作用」
        conn.execute(sql)
    cols = {r["column_name"] for r in db.fetch_all(
        conn, "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name='memories'",
        (schema,))}
    assert {"valid_at", "invalid_at", "is_current", "tenant_id", "agent_id"} <= cols
    tables = {r["table_name"] for r in db.fetch_all(
        conn, "SELECT table_name FROM information_schema.tables WHERE table_schema=%s", (schema,))}
    assert {"entities", "edges"} <= tables
    idx = {r["indexname"] for r in db.fetch_all(
        conn, "SELECT indexname FROM pg_indexes WHERE schemaname=%s", (schema,))}
    assert {"idx_mem_current", "idx_mem_tenant", "idx_mem_agent", "uq_edge_mem", "uq_edge_ent",
            "uq_entity_name", "uq_mem_dedup"} <= idx
    rows = db.fetch_all(
        conn, "SELECT id, created_at, valid_at, invalid_at, is_current "
              "FROM memories ORDER BY created_at")
    assert len(rows) == 2                                   # 重跑不增删行
    for r in rows:
        assert r["valid_at"] == r["created_at"]             # 回填：事件时间缺省=created_at
        assert r["invalid_at"] is None and r["is_current"] is True
    sv = db.fetch_one(conn, "SELECT value FROM engine_meta WHERE key='schema_ver'")
    assert sv["value"] == 2


# —————————————————— ② 时间截断（双时序 supersede 原语） ——————————————————

def test_supersede_time_truncation(pg):
    """矛盾更新=旧条 invalid_at=新条 valid_at（同瞬截断）+新条 INSERT 重存；
    旧行内容不可变、绝不 DELETE；重复 supersede 幂等 no-op。"""
    conn, _ = pg
    old = db.insert_memory(conn, **_mem_fields(body="旧版本事实：项目X 用方案A"))
    va = "2026-09-10T00:00:00+00:00"
    res = db.supersede_memory(conn, old["id"], _mem_fields(body="新版本事实：项目X 用方案B", valid_at=va))
    assert res is not None and res["new_id"] != str(old["id"])
    old_row = db.fetch_one(conn, "SELECT * FROM memories WHERE id=%s", (old["id"],))
    new_row = db.fetch_one(conn, "SELECT * FROM memories WHERE id=%s", (res["new_id"],))
    # 时间截断：两侧同一时刻
    assert old_row["invalid_at"].isoformat() == "2026-09-10T00:00:00+00:00"
    assert new_row["valid_at"] == old_row["invalid_at"]
    # 旧行内容不可变（唯一写动作=invalid_at），新行重存
    assert old_row["body"] == "旧版本事实：项目X 用方案A" and old_row["title"] == "t"
    assert old_row["is_current"] is False and new_row["is_current"] is True
    assert new_row["body"] == "新版本事实：项目X 用方案B" and new_row["valid_at"].isoformat() == va
    # 重复 supersede：幂等 no-op（旧行 invalid_at 不被复写、不产生新版本）
    again = db.supersede_memory(conn, old["id"], _mem_fields(body="第三次版本", valid_at=va))
    assert again is None
    assert db.fetch_one(conn, "SELECT count(*) c FROM memories")["c"] == 4   # 夹具 2 种子 + 旧 + 新（无第三次）
    old_row = db.fetch_one(conn, "SELECT invalid_at FROM memories WHERE id=%s", (old["id"],))
    assert old_row["invalid_at"].isoformat() == va


# —————————————————— ③ 图召回 1 跳（真实 CTE + RRF 融合） ——————————————————

def test_graph_recall_1hop(pg, monkeypatch):
    conn, _ = pg
    m1, m2, m3 = (uuid.uuid4() for _ in range(3))
    for mid, ttl in ((m1, "active"), (m2, "active"), (m3, "retired")):
        db.insert_memory(conn, **_mem_fields(id=mid, ttl_state=ttl, title=f"记忆{str(mid)[:4]}"))
    assert db.insert_edge(conn, m1, dst_mid=m2, etype="related", source="weak_graph:domain")
    assert db.insert_edge(conn, m1, dst_mid=m3, etype="related", source="weak_graph:domain")
    # 真实递归 CTE：1 跳拉回 m2；retired 的 m3 不拉回；种子 m1 自身不返回
    rows = db.graph_expand(conn, [m1], config.GRAPH_HOPS, "TRUE", [], config.GRAPH_MAX_NEIGHBORS)
    ids = [str(r["id"]) for r in rows]
    assert str(m2) in ids and str(m3) not in ids and str(m1) not in ids
    assert rows[0]["hop"] == 1
    # 融合层：图路作为第四路进 RRF（W_GRAPH=0.5 observe 期），score_parts.graph / routes.graph 透出
    monkeypatch.setattr(db, "route_vector", lambda *a, **k: [])
    monkeypatch.setattr(db, "route_fts", lambda *a, **k: [{"id": str(m1)}])
    monkeypatch.setattr(db, "route_time", lambda *a, **k: [])
    monkeypatch.setattr(db, "graph_expand", lambda *a, **k: [{"id": str(m2), "hop": 1}])
    meta = {str(m1): _meta(str(m1)), str(m2): _meta(str(m2))}
    monkeypatch.setattr(db, "hydrate", lambda c, ids_: {i: meta[i] for i in ids_ if i in meta})
    res = recall_mod.recall(_fake_pool(SimpleNamespace()), _OkEmbedder(), "q", None, "main", 5, {})
    by_id = {r["id"]: r for r in res["results"]}
    assert str(m2) in by_id and res["routes"]["graph"] == 1
    assert by_id[str(m2)]["score_parts"]["routes"]["graph"] == 1
    assert by_id[str(m2)]["score_parts"]["graph"] == round(config.W_GRAPH / (config.RRF_K + 1), 6)
    # 图路失败=显式降级不炸主召回（failed_routes.graph 登记，503 语义不掺入）

    def _boom(*a, **k):
        raise RuntimeError("edges 表挂了")
    monkeypatch.setattr(db, "graph_expand", _boom)
    res2 = recall_mod.recall(_fake_pool(SimpleNamespace()), _OkEmbedder(), "q", None, "main", 5, {})
    assert "graph" in res2["failed_routes"] and res2["degraded"] is True
    assert {r["id"] for r in res2["results"]} >= {str(m1)}   # 主路结果不受影响


# —————————————————— ④ tenant/agent 过滤（离线：SQL 构造 + 入库管道） ——————————————————

def test_tenant_agent_filter_and_retain_plumbing(monkeypatch):
    # 召回过滤：传了才拼条件，且默认叠加 is_current（历史版本不召回）
    sql, params = recall_mod._filters_sql({"tenant_id": "t1", "agent_id": "a1"})
    assert "AND tenant_id = %s" in sql and "AND agent_id = %s" in sql
    assert params[:2] == ["t1", "a1"] and "AND is_current" in sql
    sql0, params0 = recall_mod._filters_sql({})
    assert "tenant_id" not in sql0 and "agent_id" not in sql0 and params0 == []
    recall_mod.validate_filters({"tenant_id": "t1"})        # 校验器不误伤多宿主过滤键
    # 入库管道：RetainItem → insert_memory 参数末位（不回退 P0 断言位）
    item = RetainItem(content="多宿主记忆", context="ctx", tenant_id="t1", agent_id="a1")
    assert item.tenant_id == "t1" and item.agent_id == "a1"
    captured = []

    class _Cur:
        def execute(self, sql_, params_):
            captured.append((sql_, params_))

        def fetchone(self):
            return ("11111111-1111-1111-1111-111111111111", 42)

    class _FakeConn:
        def transaction(self):
            return nullcontext()

        def cursor(self):
            return nullcontext(_Cur())

    db.insert_memory(_FakeConn(), **_mem_fields(tenant_id="t1", agent_id="a1"))
    retain_sql, retain_params = captured[0]                 # 首条 = RETAIN_SQL（次条 = changelog）
    assert "tenant_id, agent_id" in retain_sql
    # P1 二批位序契约：tenant/agent=index 25/26（W2 memory_type=27、W1 pinned=28 只追加末位，不动既有位）
    assert list(retain_params[25:27]) == ["t1", "a1"]
    db.insert_memory(_FakeConn(), **_mem_fields())          # 单宿主不传 → NULL（不分区）
    retain_sql2, retain_params2 = captured[2]
    assert "tenant_id, agent_id" in retain_sql2
    assert list(retain_params2[25:27]) == [None, None]


# —————————————————— ⑤ contradicts 边只记不动 memories（G15 observe-only） ——————————————————

def test_contradicts_edge_never_touches_memories(pg):
    conn, _ = pg
    mx, my = uuid.uuid4(), uuid.uuid4()
    for mid in (mx, my):
        db.insert_memory(conn, **_mem_fields(id=mid, body=f"互相矛盾的陈述 {str(mid)[:4]}"))
    snap = {r["id"]: r for r in db.fetch_all(
        conn, "SELECT id, body, title, invalid_at, is_current, updated_at FROM memories WHERE id IN (%s,%s)",
        (mx, my))}
    eid = db.insert_edge(conn, mx, dst_mid=my, etype="contradicts", source="llm_extract")
    assert eid    # 边已记录
    # memories 逐字段未动（observe-only：不置 invalid_at、不改内容、不删除）
    after = {r["id"]: r for r in db.fetch_all(
        conn, "SELECT id, body, title, invalid_at, is_current, updated_at FROM memories WHERE id IN (%s,%s)",
        (mx, my))}
    assert after == snap
    # 三元组唯一去重：重复插入不产生第二条
    assert db.insert_edge(conn, mx, dst_mid=my, etype="contradicts", source="llm_extract") is None
    assert db.fetch_one(conn, "SELECT count(*) c FROM edges WHERE src_mid=%s AND etype='contradicts'",
                        (mx,))["c"] == 1
    # 边类型白名单：非法 etype 拒绝
    with pytest.raises(ValueError):
        db.insert_edge(conn, mx, dst_mid=my, etype="junk", source="x")
    # G15 约束写死代码（observe-only 升 enforce 前置=金标 precision>=0.7 + 30 天抽检）
    src = Path(db.__file__).read_text(encoding="utf-8")
    assert "observe-only" in src and "precision>=0.7" in src
    assert config.CONTRADICTION_ENFORCE_PRECONDITION == "gold_edge_precision>=0.7 AND 30d_spotcheck_passed"
