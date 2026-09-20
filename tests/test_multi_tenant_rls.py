"""多租户预备层单测（2026-09-19 拍板变更批：触发条件提前，用户显式拍板）。

覆盖：
① 关态零行为：config.MULTI_TENANT 缺省 False；_tenant_forced_filters(filters) 原对象直通
  （同一性断言=不拷贝不注入）；recall() 三路由捕获 SQL 不含 tenant_id；开/关两态召回结果
  逐字段一致（fake 路由下等价逐字节）。
② 开态强制：注入 filters.tenant_id=config.TENANT_ID；调用方显式 tenant_id 优先；
  不改动调用方原 dict。
③ 迁移文件契约：008_rls.sql 对 memories/entities/edges 三表 ENABLE+FORCE RLS、
  DROP POLICY IF EXISTS+CREATE POLICY（幂等式）、策略以 current_setting('app.tenant_id', true) 判会话租户。
④ 影子库实跑（可选，默认 skip）：设 MEMORY_ENGINE_RLS_TEST_DSN=<影子库 DSN> 时真实 PG 执行——
  迁移双跑幂等、未设 app.tenant_id 会话全量直通、设租户后三表只见本租户、WITH CHECK 拒跨租户写。
  本仓验收实证=2026-09-19 影子集群（initdb 独立实例）跑通，命令见 ④ docstring。
运行：/usr/bin/python3.12 -m pytest tests/test_multi_tenant_rls.py -v
"""
import importlib
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from memory_engine import config, recall as recall_mod  # noqa: E402
from memory_engine import db as db_mod  # noqa: E402

MIGRATION = ROOT / "scripts" / "migrations" / "008_rls.sql"


# ---------- fake 基建（复用 test_w3_reranker 模式：零 GPU/零 DB） ----------

class FakeEmbedder:
    def embed_queries(self, qs):
        return [[0.0] * config.EMBED_DIM] * len(qs)

    def embed_documents(self, ds):
        return [[0.0] * config.EMBED_DIM] * len(ds)


class FakePool:
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


def make_meta(n=5):
    ids = [uuid.uuid4() for _ in range(n)]
    meta = {}
    for i, mid in enumerate(ids):
        meta[mid] = {
            "id": mid, "bank": "hermes", "domain": "d", "trigger_term": "t",
            "title": f"title-{i}", "body": f"body-{i}", "body_ptr": None,
            "tags": [], "owner": "main", "visibility": "public",
            "ttl_state": "active", "staleness": "fresh", "priority": 5,
            "verify_status": "unverified", "source_ref": None, "source_tier": "user",
            "contains_pii": False, "memory_type": "episodic",
            "outcome": None, "polarity": None,
            "created_at": None, "updated_at": None,
        }
    return ids, meta


def install_route_capture(monkeypatch, ids, meta):
    """三主路捕获 (extra_sql, extra_params) 并返回全量候选；graph 路关（隔离变量）。"""
    captured = []
    rows = [{"id": mid} for mid in ids]

    def fake_route(self_or_conn, *a, **k):
        # db.route_*(conn, bank, vis_sql, vis_params, q/…, limit, extra_sql, extra_params)
        # 经 monkeypatch 以 unbound 函数注入 → 首参=conn
        args = a
        extra_sql, extra_params = args[-2], args[-1]
        captured.append((extra_sql, tuple(extra_params)))
        return list(rows)

    monkeypatch.setattr(db_mod, "route_vector", fake_route)
    monkeypatch.setattr(db_mod, "route_fts", fake_route)
    monkeypatch.setattr(db_mod, "route_time", fake_route)
    monkeypatch.setattr(recall_mod.db, "hydrate",
                        lambda conn, ids, _m=meta: {k: v for k, v in _m.items() if k in set(ids)})
    monkeypatch.setattr(config, "W_GRAPH", 0)
    return captured


# ---------- ① 关态零行为（缺省拍板） ----------

def test_switch_default_off():
    assert config.MULTI_TENANT is False, "MULTI_TENANT 缺省必须=关（存量零变化拍板）"
    assert config.TENANT_ID == "default"


def test_off_passthrough_identity():
    """关=原对象直通：不注入、不拷贝（零行为断言的最强形式）。"""
    monkey = importlib.import_module("memory_engine.recall")
    monkeypatch_val = config.MULTI_TENANT
    try:
        config.MULTI_TENANT = False
        f = {"domain": "d"}
        assert monkey._tenant_forced_filters(f) is f
        assert monkey._tenant_forced_filters(None) is None
    finally:
        config.MULTI_TENANT = monkeypatch_val


def test_off_recall_sql_no_tenant_clause(monkeypatch):
    monkeypatch.setattr(config, "MULTI_TENANT", False)
    ids, meta = make_meta()
    captured = install_route_capture(monkeypatch, ids, meta)
    res = recall_mod.recall(FakePool(meta), FakeEmbedder(), "q", None, "main", 5, {})
    assert res["results"], "fake 路由下应有结果（否则本断言失去意义）"
    assert captured, "三路由必须被调用"
    for extra_sql, _p in captured:
        assert "tenant_id" not in extra_sql, f"关态 SQL 不得出现 tenant 过滤: {extra_sql}"


def test_off_on_results_equal_when_no_tenant_rows(monkeypatch):
    """关态与开态（fake 路由只捕获不真过滤）输出逐字段一致：证明开关注入仅改 SQL 谓词，
    不改融合/评分/输出契约（开态真实过滤=④影子库实跑覆盖）。"""
    ids, meta = make_meta()
    monkeypatch.setattr(config, "MULTI_TENANT", False)
    install_route_capture(monkeypatch, ids, meta)
    off = recall_mod.recall(FakePool(meta), FakeEmbedder(), "q", None, "main", 5, {})
    monkeypatch.setattr(config, "MULTI_TENANT", True)
    cap_on = install_route_capture(monkeypatch, ids, meta)
    on = recall_mod.recall(FakePool(meta), FakeEmbedder(), "q", None, "main", 5, {})
    assert off["results"] == on["results"]
    for extra_sql, params in cap_on:
        assert "tenant_id = %s" in extra_sql and "default" in params


# ---------- ② 开态强制过滤 ----------

def test_on_injects_default_tenant(monkeypatch):
    monkeypatch.setattr(config, "MULTI_TENANT", True)
    monkeypatch.setattr(config, "TENANT_ID", "host-a")
    out = recall_mod._tenant_forced_filters({"domain": "d"})
    assert out == {"domain": "d", "tenant_id": "host-a"}
    src = {"domain": "d"}
    recall_mod._tenant_forced_filters(src)
    assert src == {"domain": "d"}, "不得改动调用方原 dict"


def test_on_respects_explicit_tenant(monkeypatch):
    monkeypatch.setattr(config, "MULTI_TENANT", True)
    monkeypatch.setattr(config, "TENANT_ID", "host-a")
    out = recall_mod._tenant_forced_filters({"tenant_id": "t9"})
    assert out["tenant_id"] == "t9", "调用方显式 tenant 优先（多宿主合法跨查）"
    out_none = recall_mod._tenant_forced_filters({"tenant_id": ""})
    assert out_none["tenant_id"] == "host-a", "空串视为未指定→强制默认"


# ---------- ③ 迁移文件契约 ----------

def test_migration_contract():
    sql = MIGRATION.read_text(encoding="utf-8")
    for table in ("memories", "entities", "edges"):
        assert "ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table} FORCE  ROW LEVEL SECURITY" in sql or \
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in sql, f"{table} 缺 FORCE（属主绕过）"
        assert f"DROP POLICY IF EXISTS tenant_isolation ON {table}" in sql, f"{table} 缺幂等 DROP"
    assert "current_setting('app.tenant_id', true)" in sql
    assert sql.count("CREATE POLICY tenant_isolation ON") == 3
    assert "coalesce(" in sql, "缺省未设租户会话必须直通（关态零行为的 DB 侧保证）"


# ---------- ④ 影子库实跑（env 门控，默认 skip；真实 PG 验证策略语义） ----------
# RLS_TEST_DSN=影子库「管理连接」（表属主/超级用户）；应用连接用户名自动替换为 memapp
# （策略只对非豁免角色生效——FORCE 覆盖属主、超级用户天然豁免，语义分工见迁移文件注释 c)。
# 影子实例起法（本仓验收 2026-09-19 实证，零生产风险，端口 5444 避让生产 5433）：
#   /usr/lib/postgresql/18/bin/initdb -D /tmp/me-shadow -A trust
#   /usr/lib/postgresql/18/bin/pg_ctl -D /tmp/me-shadow \
#     -o "-p 5444 -k /tmp -c listen_addresses=127.0.0.1" -l /tmp/me-shadow.log start
#   psql -h 127.0.0.1 -p 5444 -d postgres -c "CREATE DATABASE memengine_shadow"
#   psql … -d memengine_shadow -f docker/00-extensions.sql -f schema.sql
#   MEMORY_ENGINE_PG_DSN='postgresql://postgres@127.0.0.1:5444/memengine_shadow' \
#     python3 scripts/migrate.py --apply
#   MEMORY_ENGINE_RLS_TEST_DSN='postgresql://qq@127.0.0.1:5444/memengine_shadow' \
#     /usr/bin/python3.12 -m pytest tests/test_multi_tenant_rls.py -v

RLS_DSN = os.environ.get("MEMORY_ENGINE_RLS_TEST_DSN", "")


@pytest.mark.skipif(not RLS_DSN, reason="未设 MEMORY_ENGINE_RLS_TEST_DSN（影子库实跑门控）")
def test_rls_shadow_db():
    import re
    import psycopg
    sql = MIGRATION.read_text(encoding="utf-8")
    tag = uuid.uuid4().hex[:8]
    ids = {"null": uuid.uuid4(), "t1": uuid.uuid4(), "t2": uuid.uuid4()}
    with psycopg.connect(RLS_DSN, autocommit=True) as adm:
        # 管理连接双跑=迁移幂等（含列/索引/策略重建）
        adm.execute(sql)
        adm.execute(sql)
        assert adm.execute(
            "SELECT count(*) FROM pg_policies WHERE tablename IN "
            "('memories','entities','edges') AND policyname='tenant_isolation'").fetchone()[0] == 3
        assert adm.execute(
            "SELECT count(*) FROM pg_class WHERE relname IN ('memories','entities','edges')"
            " AND relrowsecurity AND relforcerowsecurity").fetchone()[0] == 3, "缺 FORCE（属主会绕过策略）"
        adm.execute("DO $$ BEGIN CREATE ROLE memapp LOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$")
        adm.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON memories, entities, edges TO memapp")
    app_dsn = re.sub(r"://[^@]+@", "://memapp@", RLS_DSN)
    with psycopg.connect(app_dsn, autocommit=True) as conn:
        # 未设租户=全量直通（关态零行为的 DB 侧断言，含 NULL 租户行可读可写）
        for mid, tenant in (("null", None), ("t1", "t1"), ("t2", "t2")):
            conn.execute(
                "INSERT INTO memories(id, bank, title, body, source_type, embed_model,"
                " embed_dim, content_hash, tenant_id) "
                "VALUES (%s,'knowledge',%s,%s,'manual','test',1024,%s,%s)",
                (str(ids[mid]), f"t-{tag}", f"body {tag}", f"h-{tag}-{tenant}", tenant))
        assert conn.execute("SELECT count(*) FROM memories WHERE content_hash LIKE %s",
                            (f"h-{tag}%",)).fetchone()[0] == 3
        # 设租户=只见本租户（NULL 行不可见，对齐应用层 P1 二批「NULL 仅无过滤可见」语义）
        conn.execute("SET app.tenant_id='t1'")
        seen = conn.execute("SELECT id FROM memories WHERE content_hash LIKE %s",
                            (f"h-{tag}%",)).fetchall()
        assert len(seen) == 1 and seen[0][0] == ids["t1"], "t1 会话应只见 t1 行"
        # WITH CHECK：拒跨租户写
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO memories(id, bank, title, body, source_type, embed_model,"
                " embed_dim, content_hash, tenant_id) "
                "VALUES (%s,'knowledge',%s,%s,'manual','test',1024,%s,'t2')",
                (str(uuid.uuid4()), f"x-{tag}", f"xb-{tag}", f"hx-{tag}"))
        conn.execute("SET app.tenant_id='t2'")
        assert conn.execute("SELECT count(*) FROM entities WHERE tenant_id='t1'").fetchone()[0] == 0
        # 回 unset：清理
        conn.execute("RESET app.tenant_id")
        conn.execute("DELETE FROM edges WHERE src_mid = ANY(%s::uuid[])", (list(ids.values()),))
        conn.execute("DELETE FROM memories WHERE content_hash LIKE %s", (f"h-{tag}%",))
        assert conn.execute("SELECT count(*) FROM memories WHERE content_hash LIKE %s",
                            (f"h-{tag}%",)).fetchone()[0] == 0
