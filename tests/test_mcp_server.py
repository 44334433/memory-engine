"""MCP server 合同测试（2026-09-21 MCP server 批）。

hermetic：_http/urlopen 全部 mock，不打真 daemon；真链路验收走 stdio 冒烟
（scripts 见执行文档 执行-MCPserver-2026-09-21.md）。mcp 未安装时整文件 skip
（CI 基础依赖不含 mcp，本机 Hermes venv 含）。
"""
import asyncio
import io
import json
import urllib.error

import pytest

pytest.importorskip("mcp", reason="CI 基础镜像未装 mcp SDK 时 skip（不伪绿，本机全跑）")

from memory_engine import mcp_server  # noqa: E402

EXPECTED_TOOLS = {"memory_retain", "memory_recall", "memory_feedback",
                  "memory_get", "memory_search_list", "engine_metrics"}


def _tools():
    return asyncio.run(mcp_server.server.list_tools())


@pytest.fixture
def spy(monkeypatch):
    """拦 _http：记录调用并返回 canned 响应。"""
    calls = []
    responses = {"default": {"ok": True}}

    def fake(method, path, payload=None, params=None):
        calls.append({"method": method, "path": path,
                      "payload": payload, "params": params})
        for key, val in responses.items():
            if key != "default" and path.startswith(key):
                return val
        return responses["default"]

    monkeypatch.setattr(mcp_server, "_http", fake)
    fake.calls = calls
    fake.responses = responses
    return fake


# —— ① 工具映射 ——

def test_six_tools_registered():
    assert {t.name for t in _tools()} == EXPECTED_TOOLS


def test_tool_descriptions_carry_semantics():
    """MCP 宿主 LLM 靠 docstring 选工具：每个工具描述必须非空且有信息量。"""
    for t in _tools():
        assert t.description and len(t.description) >= 40, f"{t.name} 缺语义描述"


def test_retain_endpoint_mapping(spy):
    out = json.loads(mcp_server.memory_retain(
        "内容A", "hermes", "来自 MCP 测试", tags=["x", "y"], source_tier="user"))
    assert out == {"ok": True}
    c = spy.calls[0]
    assert c["method"] == "POST" and c["path"] == "/v1/retain"
    item = c["payload"]["items"][0]
    assert item["content"] == "内容A" and item["context"] == "来自 MCP 测试"
    assert item["tags"] == ["x", "y"] and item["source_tier"] == "user"
    assert c["payload"]["bank"] == "hermes" and c["payload"]["caller"] == "main"  # 单宿主缺省全可见


def test_recall_endpoint_and_body_trim(spy):
    spy.responses["/v1/recall"] = {"results": [
        {"id": "1", "score": 0.9, "title": "T", "body": "B" * 600, "bank": "hermes"}]}
    out = json.loads(mcp_server.memory_recall("问题", k=5, bank="knowledge"))
    c = spy.calls[0]
    assert c["method"] == "POST" and c["path"] == "/v1/recall"
    assert c["payload"]["top_k"] == 5 and c["payload"]["bank"] == "knowledge"
    r = out["results"][0]
    assert len(r["body_excerpt"]) == mcp_server._BODY_EXCERPT and r.get("truncated") is True
    assert out["count"] == 1


def test_feedback_and_get_mapping(spy):
    mid = "01a0b1c3-f982-7a38-a29b-3bbfdf197e90"
    mcp_server.memory_feedback(mid, "useless", reason="过时了")
    c = spy.calls[0]
    assert c["method"] == "POST" and c["path"] == "/v1/feedback"
    assert c["payload"]["memory_id"] == mid and c["payload"]["outcome"] == "useless"
    assert c["payload"]["query"] == "过时了"  # reason→引擎归因字段（L3 信号源）
    mcp_server.memory_get(mid)
    assert spy.calls[1]["method"] == "GET" and spy.calls[1]["path"] == f"/v1/memories/{mid}"


def test_search_list_days_filter_and_metrics_params(spy):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(days=1)).isoformat()
    stale = (now - timedelta(days=30)).isoformat()
    spy.responses["/v1/memories"] = {"items": [
        {"id": "a", "created_at": fresh, "body": "新"},
        {"id": "b", "created_at": stale, "body": "旧"}]}
    out = json.loads(mcp_server.memory_search_list(bank="hermes", days=7, limit=10))
    assert [i["id"] for i in out["items"]] == ["a"] and out["count"] == 1
    spy.calls.clear()
    spy.responses["/v1/metrics"] = {"uptime_s": 1}
    mcp_server.engine_metrics(days=3)
    assert spy.calls[0]["path"] == "/v1/metrics"
    assert spy.calls[0]["params"]["days"] == 3


# —— ② 参数校验（本地闸：不发 HTTP） ——

def test_param_validation_no_http(spy):
    bad_calls = [
        (mcp_server.memory_retain, ("", "hermes", "ctx")),
        (mcp_server.memory_retain, ("c", "hermes", "  ")),
        (mcp_server.memory_retain, ("c", "hermes", "ctx", None, "bogus")),
        (mcp_server.memory_recall, ("",)),
        (mcp_server.memory_recall, ("q", 0)),
        (mcp_server.memory_recall, ("q", 101)),
        (mcp_server.memory_feedback, ("not-a-uuid", "adopted")),
        (mcp_server.memory_feedback, ("z" * 32, "adopted")),  # 非 hex UUID
        (mcp_server.memory_feedback, ("0" * 32, "maybe")),
        (mcp_server.memory_get, ("nope",)),
        (mcp_server.memory_search_list, (None, 0, 20)),
        (mcp_server.memory_search_list, (None, None, 9999)),
        (mcp_server.engine_metrics, (400,)),
    ]
    for fn, args in bad_calls:
        out = fn(*args)
        assert out.startswith("ERROR:"), f"{fn.__name__}{args} 未被本地校验拒绝: {out[:60]}"
    assert spy.calls == [], "校验失败路径不应发 HTTP"


# —— ③ fail-open（顶层兜底：异常→ERROR 文本，不抛不崩） ——

def test_fail_open_connection_error(monkeypatch):
    def refuse(*a, **kw):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(mcp_server.urllib.request, "urlopen", refuse)
    out = mcp_server.memory_recall("任何查询", 3)
    assert out.startswith("ERROR:") and "不可达" in out  # detail 带下一步动作


def test_fail_open_engine_422_detail(monkeypatch):
    body = json.dumps({"detail": "bank 必须为 ('hermes',) 之一"}).encode()
    err = urllib.error.HTTPError("u", 422, "Unprocessable", {}, io.BytesIO(body))
    monkeypatch.setattr(mcp_server.urllib.request, "urlopen",
                        lambda *a, **kw: (_ for _ in ()).throw(err))
    out = mcp_server.memory_retain("c", "no-such-bank", "ctx")
    assert out.startswith("ERROR:") and "422" in out and "bank" in out


def test_fail_open_unexpected_internal_error(monkeypatch):
    monkeypatch.setattr(mcp_server, "_http",
                        lambda *a, **kw: json.loads("not-json{{"))
    out = mcp_server.engine_metrics()
    assert out.startswith("ERROR:")  # 任意内部异常也不许逃出工具边界


# —— ④ 环境变量口径 ——

def test_base_env(monkeypatch):
    monkeypatch.delenv("MEMORY_ENGINE_BASE", raising=False)
    assert mcp_server._base() == "http://127.0.0.1:8766"
    monkeypatch.setenv("MEMORY_ENGINE_BASE", "http://example.test:9/")
    assert mcp_server._base() == "http://example.test:9"  # 尾斜杠归一


def test_caller_env_override(monkeypatch, spy):
    """多宿主时代：MEMORY_ENGINE_CALLER 改名即隔离（引擎 _vis_sql 强制 owner/public 边界）。"""
    monkeypatch.setenv("MEMORY_ENGINE_CALLER", "claude-desktop")
    mcp_server.memory_recall("q", 3)
    mcp_server.memory_feedback("0" * 32, "adopted")
    assert spy.calls[0]["payload"]["caller"] == "claude-desktop"
    assert spy.calls[1]["payload"]["caller"] == "claude-desktop"


def test_returns_json_text(spy):
    spy.responses["/v1/metrics"] = {"反馈": "中文原样"}
    out = mcp_server.engine_metrics()
    assert json.loads(out)["反馈"] == "中文原样"  # ensure_ascii=False 保中文
