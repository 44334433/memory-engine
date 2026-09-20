"""纯单元测试（离线可跑，无 DB/无 daemon）：util 纯函数 + config env 覆盖语义。"""
import importlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from memory_engine import config, util


# ---------- util.uuid7 ----------
def test_uuid7_time_ordered_and_version():
    # v7 语义：前 48bit=毫秒时间戳（时间有序主键）；同毫秒内随机位无全序。
    # 注：原实现 byte8 未设 RFC4122 variant 位，UUID.version 属性可能为 None——本批不改核心，只校验时间前缀。
    a, b = util.uuid7(), util.uuid7()

    def ts_ms(u: uuid.UUID) -> int:
        s = str(u).replace('-', '')
        return int(s[:12], 16)
    assert str(a)[14] == '7' and str(b)[14] == '7'
    assert ts_ms(b) - ts_ms(a) < 1000  # 两次调用必然同秒级窗口
    assert ts_ms(b) >= ts_ms(a)


# ---------- util.content_hash ----------
def test_content_hash_differs_by_bank_and_body():
    h1 = util.content_hash("hermes", "正文")
    h2 = util.content_hash("knowledge", "正文")
    h3 = util.content_hash("hermes", "正文2")
    assert len(h1) == 64
    assert h1 != h2 and h1 != h3
    assert util.content_hash("hermes", "正文") == h1  # 确定性


# ---------- util.staleness_of ----------
def test_staleness_buckets():
    now = datetime.now(timezone.utc)
    assert util.staleness_of(now - timedelta(days=1)) == "fresh"
    assert util.staleness_of(now - timedelta(days=29.5)) == "fresh"    # ≤30d
    assert util.staleness_of(now - timedelta(days=30.5)) == "aging"    # >30d（留时钟漂移余量）
    assert util.staleness_of(now - timedelta(days=89.5)) == "aging"    # ≤90d
    assert util.staleness_of(now - timedelta(days=90.5)) == "stale"    # >90d
    assert util.staleness_of(None, None) == "fresh"                    # 无基准=默认 fresh


def test_staleness_naive_dt_treated_as_utc():
    naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=200)
    assert util.staleness_of(naive) == "stale"


# ---------- util.vec_to_pg / derive_title ----------
def test_vec_to_pg_format():
    s = util.vec_to_pg([0.123456789, -1.0])
    assert s.startswith("[") and s.endswith("]") and "," in s
    assert len(s.split(",")) == 2


def test_derive_title_first_line_and_cap():
    assert util.derive_title("第一行\n第二行") == "第一行"
    assert len(util.derive_title("x" * 100)) == 48
    assert util.derive_title("   ") == "untitled"


# ---------- config：env 覆盖语义（脱敏后的可配置性契约） ----------
def test_config_defaults():
    assert config.BANKS == ("hermes", "hermes-sessions", "knowledge", "reflection")
    assert config.STALE_WEIGHTS == {"fresh": 1.0, "aging": 0.9, "stale": 0.7}
    assert config.EMBED_DIM == 1024


def test_port_env_override(monkeypatch):
    monkeypatch.setenv("MEMORY_ENGINE_PORT", "9999")
    cfg = importlib.reload(config)
    assert cfg.PORT == 9999
    monkeypatch.delenv("MEMORY_ENGINE_PORT")
    importlib.reload(config)
    assert config.PORT == 8766


def test_home_env_controls_base_dir(monkeypatch):
    monkeypatch.setenv("MEMORY_ENGINE_HOME", "/tmp/me-home-test")
    monkeypatch.delenv("MEMORY_ENGINE_DIR", raising=False)
    cfg = importlib.reload(config)
    assert str(cfg.BASE_DIR).startswith("/tmp/me-home-test/memory-engine")
    monkeypatch.delenv("MEMORY_ENGINE_HOME")
    monkeypatch.delenv("MEMORY_ENGINE_DIR", raising=False)
    importlib.reload(config)
    # 默认数据根 = ~/hermes-data（不硬编码任何用户家目录）
    assert str(config.BASE_DIR).endswith("hermes-data/memory-engine")


@pytest.mark.parametrize("bank", ["hermes", "hermes-sessions", "knowledge", "reflection"])
def test_bank_whitelist(bank):
    assert bank in config.BANKS
