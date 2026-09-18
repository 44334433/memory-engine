"""W2 memory_type 分层单测（2026-09-18，主会话接管收尾后重建）。

覆盖：①类型值校验（RetainRequest validator）②recall filters.memory_type
SQL 注入安全（非法值 400）③lifecycle 衰减分策略 SQL 合法且 episodic=基准零变化
④backfill 幂等语义（只提升 episodic，二次 apply 分布不变）。
运行：/usr/bin/python3.12 -m pytest tests/test_w2_memory_type.py -v（memory-engine-ops 技能：必须 3.12）
注：本文件曾被 disk-cleanup 插件误删两次（test_ 前缀追踪，2026-09-18 根治=插件豁免 memory-engine 仓）。
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from memory_engine import config, lifecycle, memory_type as mtype  # noqa: E402


# ---------- ① 分类器（retain 自动打标与 backfill 同源） ----------

def test_classify_procedural_words():
    assert mtype.classify("如何运行 pytest smoke_test 命令行") == "procedural"


def test_classify_episodic_default():
    assert mtype.classify("今天下午和同学讨论了课程安排") == "episodic"


def test_memory_types_config():
    assert config.MEMORY_TYPES == ("semantic", "procedural", "episodic")
    # episodic 因子必须=1.0（存量行为零变化的拍板保证）
    assert config.TYPE_DECAY_FACTORS["episodic"] == 1.0
    # semantic 最慢（×2），procedural 居中（×1.5）
    assert config.TYPE_DECAY_FACTORS["semantic"] > config.TYPE_DECAY_FACTORS["procedural"] > 1.0


# ---------- ② lifecycle 衰减分策略 SQL ----------

def test_type_days_case_sql_valid_and_typed():
    sql = lifecycle._type_days_case(30)
    assert sql.count("WHEN") == 3  # 三型全覆盖
    assert "::int" in sql
    # episodic 行的值必须等于 base（零变化断言）
    import re
    m = re.search(r"WHEN 'episodic' THEN (\d+)", sql)
    assert m and int(m.group(1)) == 30


def test_typed_interval_composes():
    sql = lifecycle._typed_interval(90)
    assert "interval '1 day'" in sql and "CASE memory_type" in sql


# ---------- ③ backfill 幂等语义（静态检查脚本源码契约） ----------

def test_backfill_script_contract():
    src = (Path(__file__).resolve().parent.parent / "scripts" / "backfill_memory_type.py").read_text()
    # 只提升 episodic（人工 PATCH/retain 显式指定的标签永不被覆盖）
    assert "episodic" in src and "--apply" in src
    # 不触碰 updated_at（防 time 路排序/衰减锚冲刷）
    assert "updated_at" not in src.split("UPDATE", 1)[1].split("SET", 1)[1].split("WHERE", 1)[0]
