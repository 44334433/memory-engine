"""memory_type（W2 分层，2026-09-18）：三类记忆 + 内容启发式打标唯一真源。

三类语义（蓝图口径）：
  semantic    事实/定义/原理——跨会话稳定知识（衰减最慢，×TYPE_DECAY_FACTORS）
  procedural  规程/规则/代码操作——可执行做法（衰减次慢）
  episodic    事件/会话流水——一次性上下文（正常衰减；存量与缺省值）

classify() 是**唯一启发式真源**：retain 缺省自动打标与存量回填脚本共用，
规则顺序 procedural → semantic → episodic（代码词与定义词同时命中时按可操作的
procedural 处理——规则优先于描述）。启发式允许人工 PATCH memory_type 纠偏，
回填脚本只从 episodic 提升、绝不覆盖人工/已分类标签。
"""
import re

from . import config

# (类型, 规则名, 模式)——顺序即优先级；规则名进 dry-run 分布报告（可追溯）
_RULES: list[tuple[str, str, re.Pattern]] = [
    ("procedural", "code_block", re.compile(r"```|\bdef \w+\(|\bclass \w+[:（(]|\bimport \w+")),
    ("procedural", "command", re.compile(
        r"\b(git|curl|pip|apt|docker|systemctl|psql|pytest|grep|sed|awk|npm|make)\s+\S|"
        r"\bpython3?\s+-m\b")),
    ("procedural", "rule_word", re.compile(
        r"coding|代码|脚本|命令行?用法|规则|流程|步骤|SOP|规范|手册|做法|禁止|必须|前置条件")),
    ("semantic", "definition", re.compile(
        r"(?<!自)定义|是指|指的是|含义|概念|原理|定律|定理|means\b|defined as|refers to")),
    ("semantic", "fact_word", re.compile(
        r"事实[:：]|架构[:：]|口径[:：]|属于.{1,12}(类别|类型|种类)")),
]


def classify(text: str | None) -> str:
    """启发式分类，恒返回 config.MEMORY_TYPES 之一（未命中=episodic）。"""
    return classify_with_reason(text)[0]


def classify_with_reason(text: str | None) -> tuple[str, str]:
    """返回 (memory_type, rule)；rule='default' 表示未命中启发式。"""
    t = text or ""
    for mtype, rule, pat in _RULES:
        if pat.search(t):
            return mtype, rule
    return config.MEMORY_TYPE_DEFAULT, "default"
