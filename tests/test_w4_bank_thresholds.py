"""W4 per-bank adaptive thresholds (dedup cos / decay scale), 2026-09-19.

覆盖：①config 查表（登记 bank 各自默认 / 未登记回退全局=现行为 / env 追加覆盖）
②lifecycle SQL CASE（bank scale × 分型窗；空映射与 W2 逐字符相等=存量等价）
③PG 行为实证（临时 schema 真跑 decay_trials：三 bank 同数据不同窗→判据分化；
  回退 bank 行为与 W2 一致）——p1c 同款镜像表模式，零依赖 daemon。
④HTTP 实测分化（MEMORY_ENGINE_W4_LIVE=1 显式开启，否则 skip 记原因不伪绿：
  生产 daemon 需带 W4 代码重启后才有真值；实证批=隔离实例 8767）。
Demo pair X/Y measured cos=0.9722 (local embedder, 2026-09-19):
  hermes(0.95)→skip、knowledge(0.98)→retain、未登记 bank(回退 0.97)→skip。
运行：/usr/bin/python3.12 -m pytest tests/test_w4_bank_thresholds.py -v
"""
import importlib
import json
import os
import urllib.error
import urllib.request
import uuid

import psycopg
import pytest

from memory_engine import config
from memory_engine import lifecycle as lc


# —————————————————— ① config 查表与回退 ——————————————————

def test_dedup_cos_registered_bank_defaults():
    # 登记 bank 各自默认（实测值，变更点唯一=config.BANK_DEDUP_COS）
    assert config.dedup_cos_for("hermes") == 0.95
    assert config.dedup_cos_for("reflection") == 0.95
    assert config.dedup_cos_for("knowledge") == 0.98
    # 两 bank 阈值分化存在（完成判据的配置面）
    assert config.dedup_cos_for("hermes") != config.dedup_cos_for("knowledge")
    assert config.dedup_cos_for("hermes") != config.DEDUP_SIM


def test_dedup_cos_unregistered_fallback_current():
    # 未登记 bank → 回退全局默认 = 现行为（hermes-docs/eval_* 即此类）
    for b in ("hermes-docs", "eval_decay", "any-future-bank", ""):
        assert config.dedup_cos_for(b) == config.DEDUP_SIM == 0.97


def test_decay_scale_registered_and_fallback():
    assert config.decay_scale_for("hermes") == 1.5
    assert config.decay_scale_for("knowledge") == 1.5
    assert config.decay_scale_for("hermes-sessions") == 0.5
    # 未登记（含 reflection=实测居中未偏移）→ 1.0 = W2 现行为
    assert config.decay_scale_for("reflection") == 1.0
    assert config.decay_scale_for("hermes-docs") == 1.0
    assert config.decay_scale_for("nobody") == 1.0


def test_env_bank_map_override_and_merge(monkeypatch):
    # env 追加/覆盖（隔离实例实测通道）；还原用二次 reload（模块对象原地刷新）
    monkeypatch.setenv("MEMORY_ENGINE_BANK_DEDUP_COS", "w4x=0.90, knowledge=0.99")
    monkeypatch.setenv("MEMORY_ENGINE_BANK_DECAY_SCALE", "w4x=0.5")
    try:
        importlib.reload(config)
        assert config.dedup_cos_for("w4x") == 0.90            # 新 bank 经 env 登记
        assert config.dedup_cos_for("knowledge") == 0.99      # env 覆盖生产默认
        assert config.dedup_cos_for("hermes") == 0.95         # 未覆盖项保留
        assert config.decay_scale_for("w4x") == 0.5
    finally:
        monkeypatch.delenv("MEMORY_ENGINE_BANK_DEDUP_COS")
        monkeypatch.delenv("MEMORY_ENGINE_BANK_DECAY_SCALE")
        importlib.reload(config)
    assert config.dedup_cos_for("knowledge") == 0.98          # 还原无残留


# —————————————————— ② SQL CASE 结构与存量等价 ——————————————————

def test_type_days_case_single_arg_identical_to_w2():
    # 单参调用逐字符=旧 W2 实现（签名兼容基座；等价公式独立重算）
    f = config.TYPE_DECAY_FACTORS
    expect = ("(CASE memory_type "
              f"WHEN 'semantic' THEN {round(30 * f['semantic'])} "
              f"WHEN 'procedural' THEN {round(30 * f['procedural'])} "
              f"WHEN 'episodic' THEN {round(30 * f['episodic'])} "
              "ELSE 30 END)::int")
    assert lc._type_days_case(30) == expect


def test_bank_case_empty_map_char_identical_to_w2(monkeypatch):
    # 存量等价核心断言：BANK_DECAY_SCALE 清空 → 输出与 W2 逐字符相等
    monkeypatch.setattr(config, "BANK_DECAY_SCALE", {})
    for base in (30, 90, 180):
        assert lc._bank_type_days_case(base) == lc._type_days_case(base)
        assert "WHEN bank" not in lc._bank_type_days_case(base)


def test_bank_case_all_one_map_char_identical_to_w2(monkeypatch):
    # scale=1.0 的登记项不生成冗余分支（等价且 SQL 不膨胀）
    monkeypatch.setattr(config, "BANK_DECAY_SCALE", {"hermes": 1.0})
    assert lc._bank_type_days_case(30) == lc._type_days_case(30)


def test_bank_case_differentiated_values():
    sql = lc._bank_type_days_case(30)
    # 生产映射：hermes ×1.5 / knowledge ×1.5 / hermes-sessions ×0.5 各有分支
    assert "WHEN bank = 'hermes'" in sql and "WHEN bank = 'hermes-sessions'" in sql
    f = config.TYPE_DECAY_FACTORS
    # hermes 分支 episodic=round(30×1.5)=45、semantic=round(30×1.5×2)=90
    import re
    hermes = re.search(r"WHEN bank = 'hermes' THEN \(CASE memory_type (.*?) END\)::int", sql)
    assert hermes and "WHEN 'episodic' THEN 45" in hermes.group(1) \
        and "WHEN 'semantic' THEN 90" in hermes.group(1)
    sess = re.search(r"WHEN bank = 'hermes-sessions' THEN \(CASE memory_type (.*?) END\)::int", sql)
    assert sess and "WHEN 'episodic' THEN 15" in sess.group(1)
    # ELSE=未登记回退分支，逐字符=W2 分型 CASE（现行为），外层以默认分支收尾
    assert sql.endswith("ELSE " + lc._type_days_case(30) + " END)::int")
    assert f"WHEN 'episodic' THEN {round(30 * f['episodic'])}" in sql  # 回退内 episodic=30


# —————————————————— ③ PG 行为实证（临时 schema 真跑 decay_trials） ——————————————————

@pytest.fixture(scope="module")
def pg():
    try:
        conn = psycopg.connect(config.PG_DSN, autocommit=True, connect_timeout=3)
    except Exception as e:  # noqa: BLE001 —— PG 不可达=skip（不伪绿）
        pytest.skip(f"PG 不可达（{config.PG_DSN.split('@')[-1]}）：{e}")
    schema = f"test_w4_{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(f"SET search_path TO {schema}, public")
        cur.execute("SET timezone TO 'UTC'")
        # 镜像表（p1c 同款最小列集，含 W4 需要的 bank/memory_type/created_at/ttl_state）
        cur.execute("""
            CREATE TABLE memories (
              id uuid PRIMARY KEY,
              seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
              bank text NOT NULL DEFAULT 'hermes',
              ttl_state text NOT NULL DEFAULT 'active',
              memory_type text NOT NULL DEFAULT 'episodic',
              ttl_expires_at timestamptz,
              created_at timestamptz NOT NULL DEFAULT now(),
              updated_at timestamptz NOT NULL DEFAULT now())""")
        cur.execute("""CREATE TABLE access_events (
              id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
              memory_id uuid NOT NULL, ts timestamptz NOT NULL DEFAULT now(),
              kind text NOT NULL)""")
        cur.execute("""CREATE TABLE changelog (
              seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
              ts timestamptz NOT NULL DEFAULT now(), op text NOT NULL,
              memory_id uuid, detail jsonb)""")
    yield conn, schema
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA {schema} CASCADE")
    conn.close()


def _seed_trial(pg, bank: str, days_ago: int, memory_type: str = "episodic") -> str:
    conn, _ = pg
    mid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO memories (id, bank, ttl_state, memory_type, created_at) "
        "VALUES (%s, %s, 'trial', %s, now() - (%s || ' days')::interval)",
        (mid, bank, memory_type, str(days_ago)))
    return mid


def _states(pg):
    conn, _ = pg
    return {str(r[0]): r[1] for r in conn.execute(
        "SELECT id, ttl_state FROM memories").fetchall()}


@pytest.fixture()
def clean(pg):
    """行为用例间隔离：module-scope schema 内三表逐用例清空（防上一用例遗留 trial 行互污）。"""
    conn, _schema = pg
    conn.execute("TRUNCATE memories, access_events, changelog")
    return pg


def test_decay_trials_bank_scale_differentiates(clean):
    """同一数据分布，bank 级 scale 使四条判据真实分化（PG 实跑，非字符串断言）。"""
    pg = clean
    ids = {
        "hermes_40d": _seed_trial(pg, "hermes", 40),            # 窗 30×1.5=45 → 保留
        "knowledge_sem_80d": _seed_trial(pg, "knowledge", 80, "semantic"),  # 30×1.5×2=90 → 保留
        "knowledge_epi_40d": _seed_trial(pg, "knowledge", 40),  # 45 → 保留
        "sessions_20d": _seed_trial(pg, "hermes-sessions", 20),  # 30×0.5=15 → 衰减
        "docs_40d_fallback": _seed_trial(pg, "hermes-docs", 40),  # 回退 30 → 衰减（=W2 现行为）
    }
    decayed = {str(i) for i in lc.decay_trials(pg[0])}
    st = _states(pg)
    assert decayed == {ids["sessions_20d"], ids["docs_40d_fallback"]}, \
        f"应仅 sessions(15d窗) 与回退 docs(30d窗) 衰减，实际 {decayed} / {st}"
    assert st[ids["hermes_40d"]] == "trial"          # 45d 窗未到（旧 30d 窗此处必衰减→分化的反面）
    assert st[ids["knowledge_sem_80d"]] == "trial"   # 90d 窗未到
    assert st[ids["knowledge_epi_40d"]] == "trial"
    # changelog 留痕（转换可审计）
    n = pg[0].execute("SELECT count(*) FROM changelog WHERE op='lifecycle'").fetchone()[0]
    assert n == 2


def test_decay_trials_empty_map_equals_legacy(clean, monkeypatch):
    """存量等价：scale 映射清空后同构数据回到 W2 判定（40d 全衰减、20d 保留）。"""
    pg = clean
    monkeypatch.setattr(config, "BANK_DECAY_SCALE", {})
    ids = {
        "hermes_40d": _seed_trial(pg, "hermes", 40),
        "sessions_20d": _seed_trial(pg, "hermes-sessions", 20),
        "docs_40d": _seed_trial(pg, "hermes-docs", 40),
    }
    decayed = {str(i) for i in lc.decay_trials(pg[0])}
    assert decayed == {ids["hermes_40d"], ids["docs_40d"]}   # 一律 30d 基准窗
    assert _states(pg)[ids["sessions_20d"]] == "trial"


# —————————————————— ④ 集中变更点守卫（源码级） ——————————————————

def test_single_change_point_guards():
    """集中变更点守卫：阈值字面量只准活在 config 的映射区。"""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent / "src/memory_engine"
    api = (root / "api_core.py").read_text()
    assert "config.dedup_cos_for(req.bank)" in api
    assert "config.DEDUP_SIM" not in api, "判重阈值全局常量不得再出现在 api_core（唯一入口=查表函数）"
    assert not re.search(r"0\.9[578]\d*", api), "api_core 不得出现 cos 阈值字面量"
    lif = (root / "lifecycle.py").read_text()
    assert "config.BANK_DECAY_SCALE" in lif, "lifecycle 判据必须经 config 映射读取"
    assert not re.search(r"scale\s*\*\s*1\.[25]", lif), "lifecycle 不得写死 scale 数值"


# —————————————————— ⑤ HTTP 实测分化（显式 gate，默认 skip 不伪绿） ——————————————————

W4_X = ("W4演示甲（合成数据）：光伏板巡检SLAM项目的三轮验收在九月完成，整体通过；"
        "打光模组遗留两处轻微缺陷，计划下月闭环，需复拍一组数据。")
W4_Y = ("W4演示乙（合成数据）：光伏板巡检SLAM项目九月通过三轮整体验收，"
        "打光模组遗留两处轻微缺陷待下月闭环，验证需再拍一批数据。")
# 2026-09-19 本机 embedder（与 daemon 同模型同权重）实测 cos(X,Y)=0.9722：
# hermes(0.95)→skip、knowledge(0.98)→retain；旧全局 0.97→两边同 skip（分化只来自 bank 级映射）。
W4_BAND_NOTE = "cos(X,Y)=0.9722 ∈ [0.95,0.98)"


def _http(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data, {"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _live_gate(live_server):
    # 显式开关：MEMORY_ENGINE_W4_LIVE=1 才跑（daemon 须已运行 W4 代码；生产重启交接期默认 skip，不伪绿不假红）
    if os.environ.get("MEMORY_ENGINE_W4_LIVE") != "1":
        pytest.skip("W4 live HTTP proof needs MEMORY_ENGINE_W4_LIVE=1 and a "
                    "daemon running W4 code (batch evidence: isolated instance run)")
    return live_server


@pytest.mark.parametrize("bank,expect_skip", [
    ("hermes", True),      # 登记 0.95：带内近重复应被收编
    ("knowledge", False),  # 登记 0.98：同一条必须放行
])
def test_dedup_bank_threshold_live_differentiation(live_server, bank, expect_skip):
    """同一内容对 X/Y，hermes 跳过、knowledge 放行＝阈值分化的 HTTP 端到端实证。"""
    base = _live_gate(live_server)
    tag = int(uuid.uuid4().int % 10**9)
    ctx = f"W4 自适应阈值实测（用后即删 {tag}）"
    created = []
    try:
        st, rx = _http(base, "POST", "/v1/retain", {
            "bank": bank, "caller": "w4-test",
            "items": [{"content": W4_X + f" 标记{tag}", "context": ctx, "source_tier": "user",
                       "domain": "w4-test"}]})
        assert st == 200 and rx.get("ids"), f"X 写入应成功：{st} {rx}"
        created += rx["ids"]
        st, ry = _http(base, "POST", "/v1/retain", {
            "bank": bank, "caller": "w4-test",
            "items": [{"content": W4_Y + f" 标记{tag}", "context": ctx, "source_tier": "user",
                       "domain": "w4-test"}]})
        assert st == 200, f"Y 写入应 200：{st} {ry}"
        created += ry.get("ids", [])
        skipped = ry.get("dedup_skipped", 0)
        if expect_skip:
            assert skipped >= 1 and not ry.get("ids"), \
                f"{bank}@{config.dedup_cos_for(bank)} 应判重跳过（{W4_BAND_NOTE}），实际 {ry}"
        else:
            assert skipped == 0 and ry.get("ids"), \
                f"{bank}@{config.dedup_cos_for(bank)} 应放行（{W4_BAND_NOTE}），实际 {ry}"
    finally:
        for cid in created:
            _http(base, "DELETE", f"/v1/memories/{cid}?purge=true")


def test_dedup_exact_hash_path_unaffected(live_server):
    """回归护栏：同内容重写（hash 判重）任何 bank 都跳，不受 bank 级 cos 影响。"""
    base = _live_gate(live_server)
    tag = int(uuid.uuid4().int % 10**9)
    body = {"bank": "knowledge", "caller": "w4-test",
            "items": [{"content": f"W4精确重复回归 {tag}", "context": "用后即删",
                       "source_tier": "user", "domain": "w4-test"}]}
    st, r1 = _http(base, "POST", "/v1/retain", body)
    assert st == 200 and r1.get("ids")
    try:
        st, r2 = _http(base, "POST", "/v1/retain", body)
        assert st == 200 and r2.get("dedup_skipped") == 1 and not r2.get("ids"), f"{r2}"
    finally:
        for cid in r1.get("ids", []):
            _http(base, "DELETE", f"/v1/memories/{cid}?purge=true")
