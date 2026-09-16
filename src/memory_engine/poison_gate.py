"""投毒闸（2026-09-16 P0 批拍板，蓝图外追加层）：retain 入口来源强制 + 注入模式扫描。

威胁模型：agent/web/cron 回投内容可携带恶意指令入库，每次 recall 自动注入=持久化跨会话投毒。

- source_tier 四级（user/agent/web/cron），缺省=agent（缺元数据按不信任处理，禁缺省=user）；
- 外部来源（非 user 直输）默认 trial 低信任入场（复用 TTL 状态机现成态，蓝图 §6）；
- 注入扫描=指令式祈使/越权语句样本库起步，命中即拒（422 带命中样本，不吞不静默）；
  废弃条件（写死）：误报率实测 >30% 时收紧扫描范围为仅外部来源
  （MEMORY_ENGINE_INJECTION_SCAN=external_only），样本库本体不动；
- contains_pii 为预留字段：本批只入库不判级，判级逻辑待 PII 闸拍板。
"""
import re

from . import config

# 指令式祈使/越权语句样本库（起步集：高置信样本，宁缺勿滥——误报>30% 触发收紧条件，见模块 docstring）
INJECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    # —— 指令式祈使：推翻/无视既有指令 ——
    (r"ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?|messages?)",
     "en:ignore-previous-instructions"),
    (r"disregard\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?)",
     "en:disregard-previous"),
    (r"忽略((之前|以上|上面|先前|所有|全部|任何)的?)+(系统)?(指令|提示词?|规则|设定|约束)",
     "zh:ignore-previous-instructions"),
    (r"(无视|推翻|覆盖)((之前|以上|上面|所有|系统)的?)+(指令|规则|设定)",
     "zh:override-instructions"),
    # —— 越权：冒充系统权威 / 角色劫持 ——
    (r"(系统|管理员|开发者|运营方)(指令|通知|要求)(：|:)", "zh:fake-authority"),
    (r"you\s+are\s+now\s+(a|an|no\s+longer)", "en:role-hijack"),
    # —— 数据外泄：套取系统提示词/内部规则 ——
    (r"(reveal|print|show|output)\s+(me\s+)?(your\s+)?(system\s+prompt|initial\s+instructions?|hidden\s+rules?)",
     "en:prompt-exfiltration"),
    (r"(输出|打印|泄露|吐出)(你的|系统的)?(系统提示词|初始指令|隐藏规则)", "zh:prompt-exfiltration"),
    (r"(必须|立即|马上)(执行|遵照|obey)(以下|如下|这条|下面)(的)?(指令|命令|要求)", "zh:must-execute"),
    (r"jailbreak\s*(prompt|mode|template)|越狱(模式|提示词|模板)", "ex:jailbreak"),
)

_COMPILED = tuple((re.compile(p, re.IGNORECASE), label) for p, label in INJECTION_PATTERNS)


def normalize_tier(v: str | None) -> str:
    """来源四级强制：None/空串缺省=agent；非法值 ValueError（API 层映射 422）。"""
    tier = (v or "agent").strip().lower()
    if tier not in config.SOURCE_TIERS:
        raise ValueError(f"source_tier 必须为 {config.SOURCE_TIERS} 之一（缺省 agent）")
    return tier


def is_external(tier: str) -> bool:
    """外部来源=非用户直输（agent/web/cron），均可夹带注入载荷。"""
    return tier in config.EXTERNAL_SOURCE_TIERS


def entry_state(tier: str) -> str:
    """外部来源默认 trial 低信任入场；user 走常规 candidate 候选期。"""
    return config.EXTERNAL_ENTRY_STATE if is_external(tier) else "candidate"


def should_scan(tier: str) -> bool:
    """扫描范围：all=全量起步（当前拍板）；external_only=误报>30% 废弃条件触发后的收紧态。"""
    return config.INJECTION_SCAN_SCOPE == "all" or is_external(tier)


def scan_injection(text: str) -> list[str]:
    """返回命中的模式标签列表（空列表=未命中）。只检测，处置（拒绝入库）由调用方拍板。"""
    if not text:
        return []
    return [label for rx, label in _COMPILED if rx.search(text)]
