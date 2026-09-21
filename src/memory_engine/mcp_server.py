"""memory-engine MCP server —— stdio 传输的 REST 桥（2026-09-21 拍板立项，backlog 7dd74d9f）。

让 MCP 宿主（Claude Desktop / Cursor / Hermes 等）通过标准 tool 协议读写记忆引擎。

架构纪律（对齐 sdk/python 先例）：本进程只做「MCP 工具调用 → 引擎 REST API」的翻译，
绝不直连数据库——引擎侧的写入质量闸、投毒闸（注入扫描/四级来源/低信任入场）、
判重、access_events 审计链全部复用，不存在旁路。

依赖：官方 MCP Python SDK（`pip install mcp`，mcp>=2 用 MCPServer，mcp 1.x 回退
FastMCP——两代 API 类名兼容）；HTTP 走标准库 urllib，零新增第三方依赖。

环境变量：
  MEMORY_ENGINE_BASE     引擎 REST base URL，缺省 http://127.0.0.1:8766
  MEMORY_ENGINE_TIMEOUT  单次 HTTP 超时秒数，缺省 30（retain 走 GPU 嵌入需数秒）
  MEMORY_ENGINE_CALLER   引擎侧 caller 身份，缺省 main（单宿主全可见）。引擎可见性模型
                         （recall._vis_sql）：非 main caller 视为独立宿主，只能看
                         owner==caller 或 visibility=public 的条目——多宿主 RLS 时代
                         按宿主改名即可自动隔离，零代码变更。

启动：PYTHONPATH=src python -m memory_engine.mcp_server（stdio，由宿主拉起）

安全边界：stdio 进程由宿主 fork，处于本机信任边界内——六工具全开、无认证。
若未来改为 HTTP 远程暴露（streamable-http），必须先另加认证层，不在本批范围。

fail-open：任何工具内部异常（daemon 不可达/引擎 4xx/JSON 意外）都被顶层包装器
捕获并转成 "ERROR: ..." 文本返回宿主，进程绝不因单次调用崩溃。
"""
from __future__ import annotations

import functools
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Optional

try:  # mcp >= 2.x：FastMCP 更名 MCPServer（官方 SDK 选型结论见执行文档）
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x 兼容
    from mcp.server.fastmcp import FastMCP as _Server

INSTRUCTIONS = (
    "memory-engine 记忆库：retain 写入（带上下文质量闸与投毒闸）、recall 语义+全文混合检索、"
    "feedback 反馈回收、memories 明细与列表、metrics 健康观测。写记忆必须给 context（来源与理由）。"
)

server = _Server("memory-engine", instructions=INSTRUCTIONS)

# 与引擎 config 对齐的枚举镜像（api_core/api_feedback 为真源；漂移后果=引擎 422/400 兜住）
SOURCE_TIERS = ("user", "agent", "web", "cron")
OUTCOMES = ("adopted", "corrected", "useless")
RECALL_K_MAX = 100          # 引擎 recall 侧 same clamp（max(1,min(top_k,100))）
LIST_LIMIT_MAX = 200        # 引擎 /v1/memories limit 上限
_BODY_EXCERPT = 500         # recall 结果正文截断（宿主 token 预算纪律，全文走 memory_get）


def _base() -> str:
    return os.environ.get("MEMORY_ENGINE_BASE", "http://127.0.0.1:8766").rstrip("/")


def _timeout() -> float:
    try:
        return float(os.environ.get("MEMORY_ENGINE_TIMEOUT", "30"))
    except ValueError:
        return 30.0


def _caller() -> str:
    return os.environ.get("MEMORY_ENGINE_CALLER", "main")


class EngineError(RuntimeError):
    """引擎侧 HTTP 错误（带状态码与 detail），由 _failsafe 统一转 ERROR 文本。"""


def _http(method: str, path: str, payload: dict | None = None,
          params: dict | None = None) -> dict | list:
    """一次 REST 调用；非 2xx 抛 EngineError（含引擎 detail），网络错误抛 EngineError。"""
    url = _base() + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            body = json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        raise EngineError(f"engine {method} {path} → HTTP {e.code}: {_http_err_detail(e)}") from None
    except urllib.error.URLError as e:
        raise EngineError(f"memory-engine daemon 不可达（{url}）: {e.reason}；"
                          f"确认 daemon 已启动或设 MEMORY_ENGINE_BASE") from None
    except OSError as e:
        raise EngineError(f"memory-engine HTTP 失败（{url}）: {e}") from None
    return body


def _http_err_detail(e: urllib.error.HTTPError) -> str:
    """提取 FastAPI detail（422 校验列表压成一行），失败则回退原始 body 截断。"""
    try:
        raw = json.loads(e.read() or b"{}")
        detail = raw.get("detail", raw)
        if isinstance(detail, list):  # pydantic 422 错误列表
            detail = "; ".join(f"{'.'.join(str(x) for x in d.get('loc', []))}: {d.get('msg')}"
                               for d in detail)
        return str(detail)[:300]
    except Exception:
        return f"HTTP {e.code}"


def _failsafe(fn):
    """顶层兜底：工具异常→ERROR 文本返回宿主，不崩进程（fail-open）。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 —— fail-open 是本服务器的显式设计
            return f"ERROR: {fn.__name__} failed: {exc}"
    return wrapper


def _trim_row(row: dict, excerpt: int = _BODY_EXCERPT) -> dict:
    """列表/召回用的紧凑投影：保留决策字段，正文截断，全文由 memory_get 取。"""
    body = row.get("body") or ""
    out = {k: row.get(k) for k in
           ("id", "score", "title", "bank", "domain", "tags", "ttl_state",
            "staleness", "verify_status", "memory_type", "source_tier",
            "priority", "created_at", "updated_at", "access_count")}
    if "outcome" in row:
        out["outcome"] = row.get("outcome")
    out["body_excerpt"] = body[:excerpt]
    if len(body) > excerpt:
        out["truncated"] = True
    return {k: v for k, v in out.items() if v is not None}


def _ok(res) -> str:
    return json.dumps(res, ensure_ascii=False, default=str)


@server.tool()
@_failsafe
def memory_retain(content: str, bank: str, context: str,
                  tags: Optional[list[str]] = None,
                  source_tier: str = "agent") -> str:
    """写入一条长期记忆到 memory-engine（POST /v1/retain）。

    何时用：宿主学到了值得跨会话保留的事实/偏好/流程结论时。content 是记忆正文，
    context 必填——说明这条记忆的来源与为什么值得存（引擎写入质量闸会拒绝空 context）。
    bank 是知识库名（如 hermes / knowledge / hermes-docs，缺省集合由引擎定义，非法值会 422 报错并列出可选值）。
    source_tier 标注证据等级：user=用户亲述（最高信任）、agent=宿主自行提炼（默认）、
    web/cron=外部来源（默认低信任 trial 入场，内容会被注入扫描，检出提示词注入模式将拒收）。
    引擎自带内容判重：全等/高相似重复会被跳过并在 dedup_skipped 中体现，不会报错。
    返回 JSON：{"ids": [...], "dedup_skipped": n, "took_ms": ...}。"""
    if not content or not content.strip():
        return "ERROR: memory_retain 参数 content 不能为空"
    if not context or not context.strip():
        return "ERROR: memory_retain 参数 context 必填（说明来源与保存理由，引擎质量闸拒绝无上下文写入）"
    if source_tier not in SOURCE_TIERS:
        return f"ERROR: source_tier 必须为 {SOURCE_TIERS} 之一，收到 {source_tier!r}"
    item = {"content": content, "context": context, "source_tier": source_tier}
    if tags:
        item["tags"] = list(tags)
    return _ok(_http("POST", "/v1/retain",
                     {"bank": bank, "caller": _caller(), "items": [item]}))


@server.tool()
@_failsafe
def memory_recall(query: str, k: int = 10, bank: Optional[str] = None) -> str:
    """混合检索记忆（向量+全文 RRF，POST /v1/recall）——回答历史相关问题时的首选工具。

    query 是自然语言问题或关键词；k 为返回条数（1-100，默认 10）；bank 缺省=None 跨全部库，
    指定则只查单库。score 是综合分（含生命周期/时效/来源权重），不是余弦相似度。
    命中条的正文按 500 字符截断（省宿主上下文），需要全文用 memory_get(id)。
    返回 JSON：{"results": [{"id","score","title","body_excerpt",...}], "degraded": 可选}；
    degraded=true 表示部分检索路失败、结果可能不全（可重试）。"""
    if not query or not query.strip():
        return "ERROR: memory_recall 参数 query 不能为空"
    if not isinstance(k, int) or k < 1 or k > RECALL_K_MAX:
        return f"ERROR: memory_recall 参数 k 必须在 1-{RECALL_K_MAX}，收到 {k!r}"
    res = _http("POST", "/v1/recall",
                {"query": query, "top_k": k, "bank": bank, "caller": _caller()})
    if not isinstance(res, dict):
        return _ok(res)
    out = {"results": [_trim_row(r) for r in res.get("results", [])]}
    for key in ("degraded", "failed_routes", "took_ms", "route_counts"):
        if key in res:
            out[key] = res[key]
    out["count"] = len(out["results"])
    return _ok(out)


@server.tool()
@_failsafe
def memory_feedback(memory_id: str, outcome: str,
                    reason: Optional[str] = None) -> str:
    """对一次召回结果提交使用反馈（POST /v1/feedback）——记忆质量的回收闭环。

    何时用：宿主采纳了某条记忆（adopted）、发现它需要修正（corrected）、
    或它对本问题无用（useless）。outcome 三选一；memory_id 来自 memory_recall 结果。
    reason 可选：自由文本说明（引擎侧作为归因 query 字段入库；corrected/useless 时
    会进 L3 困难样本池用于改进检索，adopted 时仅作审计）。
    反馈会影响后续排序权重（outcome/polarity 计分）。返回 JSON：{"id","outcome","polarity",...}。"""
    if outcome not in OUTCOMES:
        return f"ERROR: memory_feedback 参数 outcome 必须为 {OUTCOMES} 之一，收到 {outcome!r}"
    try:
        uuid.UUID(str(memory_id))
    except (ValueError, TypeError, AttributeError):
        return f"ERROR: memory_feedback 参数 memory_id 必须是合法 UUID，收到 {memory_id!r}"
    payload: dict = {"memory_id": str(memory_id), "outcome": outcome, "caller": _caller()}
    if reason:
        payload["query"] = reason
    return _ok(_http("POST", "/v1/feedback", payload))


@server.tool()
@_failsafe
def memory_get(memory_id: str) -> str:
    """按 id 取一条记忆的完整明细（GET /v1/memories/{id}）——含全文 body、标签、
    生命周期状态、审计字段。id 来自 memory_recall / memory_search_list；不存在返回
    HTTP 404 的 ERROR 文本。"""
    try:
        uuid.UUID(str(memory_id))
    except (ValueError, TypeError, AttributeError):
        return f"ERROR: memory_get 参数 memory_id 必须是合法 UUID，收到 {memory_id!r}"
    return _ok(_http("GET", f"/v1/memories/{urllib.parse.quote(str(memory_id))}"))


@server.tool()
@_failsafe
def memory_search_list(bank: Optional[str] = None, days: Optional[int] = None,
                       limit: int = 20) -> str:
    """按条件浏览记忆列表（GET /v1/memories，非语义检索）——用于盘点/审计，
    查「最近存了什么」；找「与某问题相关的」请用 memory_recall。
    bank 可选限库；days 可选（1-365）只看最近 N 天创建的；limit 默认 20（上限 200）。
    按创建时间倒序返回 JSON：{"count", "items": [{"id","title","bank","created_at",
    "body_excerpt",...}]}，正文截断，全文走 memory_get。"""
    if days is not None and (not isinstance(days, int) or days < 1 or days > 365):
        return f"ERROR: memory_search_list 参数 days 必须在 1-365，收到 {days!r}"
    if not isinstance(limit, int) or limit < 1 or limit > LIST_LIMIT_MAX:
        return f"ERROR: memory_search_list 参数 limit 必须在 1-{LIST_LIMIT_MAX}，收到 {limit!r}"
    # 引擎无 days 过滤参数：多取后按 created_at 客户端过滤（200 条内窗口近似精确）
    fetch = min(LIST_LIMIT_MAX, limit * (4 if days else 1))
    res = _http("GET", "/v1/memories", params={"bank": bank, "limit": fetch, "offset": 0})
    items = res.get("items", []) if isinstance(res, dict) else []
    if days:
        import datetime as _dt
        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)
        kept = []
        for it in items:
            created = it.get("created_at")
            try:
                dt = _dt.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                if dt >= cutoff:
                    kept.append(it)
            except (ValueError, TypeError):
                kept.append(it)  # 时间解析失败不误删，宁多勿漏
        items = kept[:limit]
    else:
        items = items[:limit]
    return _ok({"count": len(items), "items": [_trim_row(it, 200) for it in items]})


@server.tool()
@_failsafe
def engine_metrics(days: Optional[int] = None) -> str:
    """引擎健康与观测指标（GET /v1/metrics，纯读）——反馈通路活性探针、注入/消费两态计数、
    pinned 占比、进程内计数器、uptime。用于运维巡检或写入前确认引擎状态。
    days 可选统计窗口（1-365，缺省=引擎默认 7 天）。异常时结果里会带 degraded 类字段。"""
    if days is not None and (not isinstance(days, int) or days < 1 or days > 365):
        return f"ERROR: engine_metrics 参数 days 必须在 1-365，收到 {days!r}"
    return _ok(_http("GET", "/v1/metrics", params={"days": days}))


def main() -> None:
    """stdio 服务入口（宿主拉起的阻塞循环）；一切日志走 stderr，stdout 归协议。"""
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
