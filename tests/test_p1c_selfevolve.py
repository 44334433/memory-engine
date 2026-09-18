"""P1 第三批新增用例（2026-09-16）：L2 信号接线 1 + L3 失败回流漏斗 1 + L1 配对检验 1。

L2：PG 临时 schema 跑真实 SQL（生命周期规则直调隔离断言）；
L3：tmp_path 文件漏斗（去重/上限/提案幂等）；
L1：G07 配对检验数学 + 快照版本化 + 固化写回（离线，tmp 隔离）。
"""
import json
import re
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from memory_engine import config, db
from memory_engine import hard_queries as hq
from memory_engine import lifecycle as lc
from memory_engine.db import PgPool

sys_path_scripts = str(Path(__file__).resolve().parent.parent / "scripts")


# —————————————————— ① L2 信号接线（PG 临时 schema，真实 SQL） ——————————————————

@pytest.fixture(scope="module")
def pg():
    try:
        conn = psycopg.connect(config.PG_DSN, autocommit=True, connect_timeout=3)
    except Exception as e:  # noqa: BLE001 —— PG 不可达=skip（不伪绿）
        pytest.skip(f"PG 不可达（{config.PG_DSN.split('@')[-1]}）：{e}")
    schema = f"test_p1c_{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(f"SET search_path TO {schema}, public")
        cur.execute("SET timezone TO 'UTC'")
        cur.execute("""
            CREATE TABLE memories (
              id uuid PRIMARY KEY,
              seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
              bank text NOT NULL DEFAULT 'hermes', domain text NOT NULL DEFAULT 'general',
              title text NOT NULL DEFAULT 't', body text NOT NULL DEFAULT 'b',
              owner text NOT NULL DEFAULT 'main', visibility text NOT NULL DEFAULT 'agent',
              priority int NOT NULL DEFAULT 3,
              ttl_state text NOT NULL DEFAULT 'active', ttl_expires_at timestamptz,
              staleness text NOT NULL DEFAULT 'fresh', verify_status text NOT NULL DEFAULT 'unverified',
              source_tier text NOT NULL DEFAULT 'agent',
              memory_type text NOT NULL DEFAULT 'episodic',   -- W2：lifecycle 分型衰减 CASE 引用（镜像须补列）
              access_count int NOT NULL DEFAULT 0, adopt_count int NOT NULL DEFAULT 0,
              created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
              last_accessed_at timestamptz)""")
        cur.execute("""CREATE TABLE access_events (
              id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
              memory_id uuid NOT NULL, ts timestamptz NOT NULL DEFAULT now(),
              kind text NOT NULL, caller text, query text)""")
        cur.execute("""CREATE TABLE changelog (
              seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
              ts timestamptz NOT NULL DEFAULT now(), op text NOT NULL, memory_id uuid, detail jsonb)""")
    yield conn, schema
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA {schema} CASCADE")
    conn.close()


def _seed(pg, state: str, created_days_ago: int) -> str:
    conn, _ = pg
    mid = str(uuid.uuid4())
    db.execute(conn, "INSERT INTO memories (id, ttl_state, created_at, updated_at) "
                     "VALUES (%s, %s, now() - (%s || ' days')::interval, now())",
               (mid, state, str(created_days_ago)))
    return mid


def _event(pg, mid: str, kind: str, days_ago: int) -> None:
    conn, _ = pg
    db.execute(conn, "INSERT INTO access_events (memory_id, kind, ts, caller) "
                     "VALUES (%s, %s, now() - (%s || ' days')::interval, 'p1c-test')",
               (mid, kind, str(days_ago)))


def _state_of(pg, mid: str) -> str:
    conn, _ = pg
    return db.fetch_one(conn, "SELECT ttl_state s FROM memories WHERE id=%s", (mid,))["s"]


def _reasons(pg, mid: str) -> list[str]:
    conn, _ = pg
    return [r["detail"]["reason"] for r in db.fetch_all(
        conn, "SELECT detail FROM changelog WHERE op='lifecycle' AND memory_id=%s", (mid,))]


def test_l2_adopt_upgrade_and_zero_access_decay(pg):
    """L2 用进废退双轨：adopted≤30d 强信号升级 decaying→active；
    连续 90d 零访问（任意 kind 锚定）→ trial/active 提前 decaying；拍板参数不动。"""
    # —— 轨 1：adopted 信号升级 ——
    m_up = _seed(pg, "decaying", 200)          # decaying + adopted 10d 前 → 升级 active
    _event(pg, m_up, "adopted", 10)
    m_stay = _seed(pg, "decaying", 200)        # decaying + 仅 40d 前 adopted（超 30d 窗）→ 不动
    _event(pg, m_stay, "adopted", 40)
    upgraded = lc.upgrade_adopted(conn=pg[0])
    assert m_up in [str(i) for i in upgraded] and m_stay not in [str(i) for i in upgraded]
    assert _state_of(pg, m_up) == "active" and _state_of(pg, m_stay) == "decaying"
    assert f"l2_adopt_upgrade_{config.L2_ADOPT_WINDOW_DAYS}d" in _reasons(pg, m_up)
    # recall_hit 3d 拍板复活窗不被本规则替代（revive_decaying 判据原样存在）
    m_rev = _seed(pg, "decaying", 200)
    _event(pg, m_rev, "recall_hit", 2)
    revived = lc.revive_decaying(conn=pg[0])
    assert m_rev in [str(i) for i in revived] and _state_of(pg, m_rev) == "active"

    # —— 轨 2：连续 90d 零访问 → 提前 decaying（锚=最近任意 kind 访问，无访问回退 created_at）——
    m_act_zero = _seed(pg, "active", 100)      # active，从未访问（100d 前），创建>90d → 衰减
    m_act_fresh = _seed(pg, "active", 100)     # active，10d 前有 recall_hit → 不衰减
    _event(pg, m_act_fresh, "recall_hit", 10)
    m_act_old = _seed(pg, "active", 100)       # active，最近访问在 95d 前 → 衰减（kind 泛化锚定）
    _event(pg, m_act_old, "recall_hit", 95)
    m_tri_never = _seed(pg, "trial", 95)       # trial，从未访问，创建>90d → 衰减
    m_tri_young = _seed(pg, "trial", 10)       # trial，从未访问但只入库 10d → 不衰减（连续零访问<90d）
    m_tri_other_kind = _seed(pg, "trial", 95)  # trial，10d 前有任意 kind 访问 → 不衰减
    _event(pg, m_tri_other_kind, "fetch", 10)
    decayed = lc.decay_zero_access(conn=pg[0])
    decayed = [str(i) for i in decayed]
    assert m_act_zero in decayed and m_act_old in decayed and m_tri_never in decayed
    assert m_act_fresh not in decayed and m_tri_young not in decayed and m_tri_other_kind not in decayed
    assert _state_of(pg, m_act_zero) == "decaying" and _state_of(pg, m_tri_young) == "trial"
    # W2 分型后 zero-access 规则 reason 带 _type_scaled 后缀（episodic 系数=1.0，90d 窗口不变——断言同步）
    assert f"l2_zero_access_{config.L2_ZERO_ACCESS_DAYS}d_type_scaled" in _reasons(pg, m_act_zero)
    assert f"l2_zero_access_{config.L2_ZERO_ACCESS_DAYS}d_type_scaled" in _reasons(pg, m_tri_never)

    # —— 延寿观测计数（read-only）：active+adopted≤30d 可见 ——
    m_renew = _seed(pg, "active", 100)
    _event(pg, m_renew, "adopted", 5)
    assert lc.adopt_renew_count(conn=pg[0]) >= 2      # m_up + m_renew（同 schema 计数）

    # —— scan() 摘要含新步骤且全步骤无 error（规则接线进入统一巡扫）——
    out = lc.scan(conn=pg[0])
    for step in ("decaying_to_active_adopt", "trial_to_decaying_zero_access",
                 "active_to_decaying_zero_access"):
        assert step in out["transitions"] and "error" not in out["transitions"][step], step
    assert "adopt_renew_observed" in out


# —————————————————— ② L3 失败回流漏斗（tmp 文件，离线） ——————————————————

@pytest.fixture()
def hq_dirs(tmp_path, monkeypatch):
    state = tmp_path / "state"
    auto = tmp_path / "autotune"
    monkeypatch.setattr(config, "STATE_DIR", state)
    monkeypatch.setattr(config, "AUTOTUNE_DIR", auto)
    monkeypatch.setattr(hq, "hard_queries_path", lambda: Path(state) / "hard_queries.jsonl")
    monkeypatch.setattr(hq, "_analysis_marker", lambda: Path(auto) / ".last_hardq_analysis")
    return state, auto


def test_l3_hard_query_funnel(hq_dirs):
    """L3 漏斗：落池→TTL 去重→上限 500→跨天聚类提案（幂等）→节流。"""
    state, auto = hq_dirs
    # 落池 + 短 query 拒收
    rec = hq.record_hard_query("完全找不到的这个东西xyz", "main", None, "hermes", "zero_hit")
    assert rec and rec["query"] == "完全找不到的这个东西xyz"
    assert hq.record_hard_query("ab", "main", None, None, "zero_hit") is None      # <4 字符
    # TTL 去重：同 query 归一化变体跳重
    assert hq.record_hard_query("完全找不到的这个东西XYZ", "main", None, "hermes", "zero_hit") is None
    assert hq.record_hard_query("  完全找不到的这个东西xyz  ", "main", None, "hermes", "low_score") is None
    entries = hq.read_all()
    assert len(entries) == 1 and entries[0]["reason"] == "zero_hit"
    # 上限 500：写入 505 条不同 query → 只留最新 500（防膨胀丢最旧）
    for i in range(504):
        hq.record_hard_query(f"长查询词表序号{i}号需要足够长度", "cron", 0.004, "knowledge", "low_score")
    entries = hq.read_all()
    assert len(entries) == config.HARD_QUERY_CAP == 500
    assert all("完全找不到" not in e["query"] for e in entries)                    # 最旧被挤出
    # 周期分析：跨≥2 天 且 ≥3 次 → 提案；未达阈 → 无
    day1 = {"ts": "2026-09-14T10:00:00+00:00", "ts_epoch": 0.0,
            "key": hq.qkey("同一个失败查询反复出现"), "query": "同一个失败查询反复出现",
            "caller": "main", "bank": None, "top1_score": None, "reason": "zero_hit"}
    day2 = dict(day1, ts="2026-09-15T10:00:00+00:00")
    day3 = dict(day1, ts="2026-09-16T10:00:00+00:00", reason="low_score")
    single = dict(day1, key=hq.qkey("只有一天的单次查询"), query="只有一天的单次查询",
                  ts="2026-09-16T11:00:00+00:00")
    with hq._lock:
        hq._write_all(hq.read_all() + [day1, day2, day3, single])
    out = hq.analyze_hard_queries(force=True)
    assert len(out["proposals"]) == 1
    prop = json.loads(Path(out["proposals"][0]).read_text(encoding="utf-8"))
    assert prop["kind"] == "hard_query_cluster" and prop["hits"] == 3
    assert prop["active_days"] == ["2026-09-14", "2026-09-15", "2026-09-16"]
    assert prop["status"] == "pending_review"
    # 幂等：重跑不重产同一簇提案；节流：非 force 且刚分析过 → skipped
    out2 = hq.analyze_hard_queries(force=True)
    assert out2["proposals"] == []
    out3 = hq.analyze_hard_queries()
    assert out3.get("skipped") == "throttled"


# —————————————————— ③ L1 配对逐题检验（G07 数学 + 快照 + 固化） ——————————————————

def test_l1_paired_gate_and_solidify(tmp_path, monkeypatch):
    """McNemar 精确值 / delta+p 双条件闸 / 快照版本化 / 固化写回+校验 / pending 回滚语义。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "param_autotune", Path(sys_path_scripts) / "param_autotune.py")
    pat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pat)

    # McNemar 精确值：b=c=0→1；b=0,c=8→2/256=0.0078；b=1,c=8→2*(1+9)/512=0.039
    assert pat.mcnemar_exact_p(0, 0) == 1.0
    assert abs(pat.mcnemar_exact_p(0, 8) - 0.0078125) < 1e-9
    assert abs(pat.mcnemar_exact_p(1, 8) - 20 / 512) < 1e-9

    n = 36
    base = [i < 20 for i in range(n)]                       # 基线 20/36 ≈ 0.5556
    # delta 达标(+2 题=+5.6pp)但不显著（b=2,c=4, p≈0.69）→ 闸拒绝（G07 双条件）
    mut_delta_only = [(2 <= i < 24) for i in range(n)]
    g1 = pat.paired_gate(base, mut_delta_only)
    assert g1["delta"] >= 0.05 and not g1["gate_pass"] and abs(g1["mcnemar_p"] - 44 / 64) < 1e-9
    # 全部不一致对都是改进（b=0, c=8）→ 双条件通过
    mut_good = [(i < 20) or (28 <= i < 36) for i in range(n)]
    g2 = pat.paired_gate(base, mut_good)
    assert g2["gate_pass"] and g2["b_only_baseline"] == 0 and g2["c_only_mutant"] == 8
    assert abs(g2["p5_mutant"] - round(28 / 36, 4)) < 1e-9

    # 快照版本化 + 固化写回（tmp 隔离）
    auto = tmp_path / "autotune"
    monkeypatch.setattr(config, "AUTOTUNE_DIR", auto)
    snap1 = pat.write_snapshot(pat.capture_params(), source="baseline")
    snap2 = pat.write_snapshot({"RRF_K": 50, "W_VEC": 1.0, "W_FTS": 0.8, "W_TIME": 0.4,
                                "W_GRAPH": 0.5, "TIER_WEIGHTS": {"user": 1.0, "agent": 1.0,
                                                                 "web": 0.9, "cron": 0.95}},
                               source="solidified")
    assert pat.snapshot_versions() == [1, 2]
    assert pat.latest_snapshot()["version"] == 2
    assert snap1.name == "params-v1.json" and snap2.name == "params-v2.json"

    cfg = tmp_path / "config.py"
    cfg.write_text('RRF_K = 60\n'
                   'TIER_WEIGHTS = {\n'
                   '    "user": 1.0,\n'
                   '    "agent": 1.0,\n'
                   '    "web": float(os.environ.get("MEMORY_ENGINE_TIER_WEIGHT_WEB", "0.85")),\n'
                   '    "cron": float(os.environ.get("MEMORY_ENGINE_TIER_WEIGHT_CRON", "0.9")),\n'
                   '}\n', encoding="utf-8")
    monkeypatch.setattr(pat, "_config_targets", lambda: [cfg])
    changed = pat.solidify_config({"RRF_K": 50, "TIER_WEIGHTS": {"web": 0.9, "cron": 0.95}})
    assert changed == [str(cfg)]
    text = cfg.read_text(encoding="utf-8")
    assert "RRF_K = 50" in text
    web_line = next(l for l in text.splitlines() if "TIER_WEIGHT_WEB" in l)
    cron_line = next(l for l in text.splitlines() if "TIER_WEIGHT_CRON" in l)
    assert '"0.9"' in web_line and '"0.95"' in cron_line

    # pending 连续两轮语义：一轮通过挂起；换 mutant=连续性断裂自动撤销（回滚）
    pat.write_pending({"mutation": {"RRF_K": 50}, "pass_rounds": 1})
    assert pat.read_pending()["pass_rounds"] == 1
    pat.write_pending(None)
    assert pat.read_pending() is None
    # 台账追加可读
    pat.ledger_append({"mode": "unit", "decision": "unit_test"})
    lines = (auto / "ledger.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[-1])["decision"] == "unit_test"
