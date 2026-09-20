"""⑪TTL env 化单测（2026-09-20，可信度建设批）：零 GPU/零 PG/零 LLM，纯 config+lifecycle。

断言两态：
1. 关态（无 env）= 零行为：默认值逐位=改造前硬编码现值（30/90/180），
   且 lifecycle._EXPIRES_SQL 渲染出的 SQL 与改造前字面量逐字节一致；
2. 开态：MEMORY_ENGINE_TTL_* env 覆盖生效（config 重导入语义，与其余 MEMORY_ENGINE_* 一致）。
"""
import importlib
import os

from memory_engine import config, lifecycle

TTL_ENV_KEYS = ("MEMORY_ENGINE_TTL_TRIAL_DAYS",
                "MEMORY_ENGINE_TTL_DECAY_DAYS",
                "MEMORY_ENGINE_TTL_ARCHIVE_DAYS")


def _reload_config():
    importlib.reload(config)


def _cleanup():
    for k in TTL_ENV_KEYS:
        os.environ.pop(k, None)
    _reload_config()


def test_ttl_defaults_equal_legacy_literals():
    """关态零行为：env 全不设时，三窗=改造前硬编码现值。"""
    saved = {k: os.environ.pop(k, None) for k in TTL_ENV_KEYS}
    try:
        _reload_config()
        assert config.TRIAL_DECAY_DAYS == 30
        assert config.ACTIVE_DECAY_DAYS == 90
        assert config.DECAY_ARCHIVE_DAYS == 180
    finally:
        os.environ.update({k: v for k, v in saved.items() if v is not None})
        _reload_config()


def test_ttl_expires_sql_byte_identical_when_off():
    """关态零行为（SQL 级）：_EXPIRES_SQL 渲染与改造前字面量逐字节一致。

    改造前：trial="now() + interval '30 days'"，decaying="now() + interval '180 days'"，
    candidate 走 {d}=CANDIDATE_DAYS(6)。渲染路径=_transact 的 .format(d,t,k)。
    """
    saved = {k: os.environ.pop(k, None) for k in TTL_ENV_KEYS}
    try:
        _reload_config()
        fmt = dict(d=config.CANDIDATE_DAYS,
                   t=config.TRIAL_DECAY_DAYS,
                   k=config.DECAY_ARCHIVE_DAYS)
        assert lifecycle._EXPIRES_SQL["trial"].format(**fmt) == "now() + interval '30 days'"
        assert lifecycle._EXPIRES_SQL["decaying"].format(**fmt) == "now() + interval '180 days'"
        assert lifecycle._EXPIRES_SQL["candidate"].format(**fmt) == "now() + interval '6 days'"
        assert lifecycle._EXPIRES_SQL["active"] == "NULL"
        assert lifecycle._EXPIRES_SQL["archived"] == "NULL"
    finally:
        os.environ.update({k: v for k, v in saved.items() if v is not None})
        _reload_config()


def test_ttl_env_override_changes_windows_and_sql():
    """开态：三 env 覆盖后转移窗与 ttl_expires_at 渲染同步变化。"""
    try:
        os.environ.update({"MEMORY_ENGINE_TTL_TRIAL_DAYS": "45",
                           "MEMORY_ENGINE_TTL_DECAY_DAYS": "120",
                           "MEMORY_ENGINE_TTL_ARCHIVE_DAYS": "365"})
        _reload_config()
        assert config.TRIAL_DECAY_DAYS == 45
        assert config.ACTIVE_DECAY_DAYS == 120
        assert config.DECAY_ARCHIVE_DAYS == 365
        fmt = dict(d=config.CANDIDATE_DAYS,
                   t=config.TRIAL_DECAY_DAYS,
                   k=config.DECAY_ARCHIVE_DAYS)
        assert lifecycle._EXPIRES_SQL["trial"].format(**fmt) == "now() + interval '45 days'"
        assert lifecycle._EXPIRES_SQL["decaying"].format(**fmt) == "now() + interval '365 days'"
    finally:
        _cleanup()


def test_ttl_transition_reason_strings_follow_config():
    """关态零行为（文案级）：changelog reason 由 config 插值，默认=旧字面值。

    旧实现 reason=f"no_signal_{30}d_type_scaled" 等；现走 config 同值→同串。
    """
    saved = {k: os.environ.pop(k, None) for k in TTL_ENV_KEYS}
    try:
        _reload_config()
        assert f"no_signal_{config.TRIAL_DECAY_DAYS}d_type_scaled" == "no_signal_30d_type_scaled"
        assert f"no_trigger_{config.ACTIVE_DECAY_DAYS}d_type_scaled" == "no_trigger_90d_type_scaled"
        assert f"no_signal_{config.DECAY_ARCHIVE_DAYS}d_type_scaled" == "no_signal_180d_type_scaled"
    finally:
        os.environ.update({k: v for k, v in saved.items() if v is not None})
        _reload_config()
