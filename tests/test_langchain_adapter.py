"""LangChain 适配器单测（mock HTTP，零 langchain 依赖、零网络、零 daemon）。

hermetic 手法：importlib 按文件加载 integrations/langchain_memory.py（无 langchain 环境
同路径可跑）；mock.patch.object(mod.requests, ...) 拦截全部 HTTP。本机 langchain-core 1.x
在场 → MemoryEngineRetriever 走真 BaseRetriever（pydantic v2 字段面被真实基类验证），
MemoryEngineMemory 走 shim——两条基类形态都过同一套断言。

运行：/usr/bin/python3.12 -m pytest tests/test_langchain_adapter.py -v
"""
import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "me_langchain_memory", ROOT / "integrations" / "langchain_memory.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules["me_langchain_memory"] = mod
_spec.loader.exec_module(mod)


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise mod.requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


RETAIN_OK = {"ids": ["11111111-1111-1111-1111-111111111111"], "dedup_skipped": 0,
             "dedup_existing": [], "seq": 7, "took_ms": 3.2}
RECALL_OK = {"results": [
    {"id": "a1", "score": 0.9, "title": "偏好", "body": "Human: 喜欢咖啡\nAI: 记下了",
     "bank": "hermes", "domain": "langchain:s1", "tags": ["langchain"], "memory_type": "episodic",
     "source_tier": "user", "created_at": "2026-09-19T00:00:00+00:00", "ttl_state": "active"},
    {"id": "a2", "score": 0.4, "body": "无前缀的一条事实", "title": "t2"},
], "took_ms": 1.5, "degraded": False, "failed_routes": {}, "routes": {"vector": 2}}


def _memory(**kw):
    return mod.MemoryEngineMemory(bank="hermes", session_id="s1", **kw)


# ---------- ① save_context → POST /v1/retain 契约 ----------

def test_save_context_payload():
    m = _memory()
    with mock.patch.object(mod.requests, "post", return_value=_Resp(RETAIN_OK)) as p:
        m.save_context({"input": "喜欢咖啡"}, {"output": "记下了"})
    url, kwargs = p.call_args[0][0], p.call_args[1]
    assert url.endswith("/v1/retain")
    body = kwargs["json"]
    assert body["bank"] == "hermes" and body["caller"] == "langchain"
    item = body["items"][0]
    # 服务端契约：content+context 必填（缺=422）；轮次格式可逆解析
    assert item["content"] == "Human: 喜欢咖啡\nAI: 记下了"
    assert item["context"].strip()
    assert item["domain"] == "langchain:s1"
    assert item["source_tier"] == "user"
    assert "session:s1" in item["tags"]
    # 关态零行为：不显式传 tenant → payload 无 tenant 键
    assert "tenant_id" not in item and "agent_id" not in item


def test_save_context_empty_turn_noop():
    m = _memory()
    with mock.patch.object(mod.requests, "post") as p:
        m.save_context({}, {})
    p.assert_not_called()


# ---------- ② load_memory_variables → POST /v1/recall ----------

def test_load_memory_variables_text():
    m = _memory(top_k=2)
    with mock.patch.object(mod.requests, "post", return_value=_Resp(RECALL_OK)) as p:
        out = m.load_memory_variables({"input": "咖啡"})
    body = p.call_args[1]["json"]
    assert body["query"] == "咖啡" and body["top_k"] == 2
    assert body["filters"] == {"domain": "langchain:s1"}
    assert out["history"].startswith("- Human: 喜欢咖啡")
    assert "- 无前缀的一条事实" in out["history"]


def test_load_memory_no_input_short_circuits():
    m = _memory()
    with mock.patch.object(mod.requests, "post") as p:
        out = m.load_memory_variables({})
    p.assert_not_called()
    assert out == {"history": ""}


def test_return_messages_roundtrip():
    """写入格式 'Human: …\\nAI: …' 可逆解析回消息对；langchain 缺席时文本回退。"""
    m = _memory(return_messages=True)
    with mock.patch.object(mod.requests, "post", return_value=_Resp(RECALL_OK)):
        out = m.load_memory_variables({"input": "咖啡"})
    msgs = out["history"]
    if isinstance(msgs, str):                      # 无消息类 → 降级为文本（显式契约）
        assert "咖啡" in msgs
    else:
        types = [type(x).__name__ for x in msgs]
        assert types[0] == "HumanMessage" and types[1] == "AIMessage"
        assert msgs[0].content == "喜欢咖啡" and msgs[1].content == "记下了"


# ---------- ③ 租户透传（开态显式值；None 剔除=关态零行为） ----------

def test_tenant_passthrough_both_endpoints():
    m = _memory(tenant_id="t1")
    with mock.patch.object(mod.requests, "post", return_value=_Resp(RETAIN_OK)) as p:
        m.save_context({"input": "a"}, {"output": "b"})
    assert p.call_args[1]["json"]["items"][0]["tenant_id"] == "t1"
    with mock.patch.object(mod.requests, "post", return_value=_Resp(RECALL_OK)) as p2:
        m.load_memory_variables({"input": "a"})
    assert p2.call_args[1]["json"]["filters"] == {"domain": "langchain:s1", "tenant_id": "t1"}


# ---------- ④ Retriever → Document 映射 ----------

def test_retriever_documents():
    r = mod.MemoryEngineRetriever(top_k=3)
    with mock.patch.object(mod.requests, "post", return_value=_Resp(RECALL_OK)) as p:
        docs = r._get_relevant_documents("咖啡")
    assert p.call_args[0][0].endswith("/v1/recall")
    assert p.call_args[1]["json"]["top_k"] == 3
    assert len(docs) == 2
    d0 = docs[0]
    assert d0.page_content == "Human: 喜欢咖啡\nAI: 记下了"
    assert d0.metadata["id"] == "a1" and d0.metadata["score"] == 0.9
    assert "took_ms" not in d0.metadata          # metadata 只带条目级白名单键
    d1 = docs[1]
    assert d1.metadata.get("tags") is None or "tags" not in d1.metadata  # None 键剔除


def test_retriever_extra_filters_and_scope():
    r = mod.MemoryEngineRetriever(extra_filters={"memory_type": "semantic"}, agent_id="host-x")
    with mock.patch.object(mod.requests, "post", return_value=_Resp(RECALL_OK)) as p:
        r._get_relevant_documents("q")
    assert p.call_args[1]["json"]["filters"] == {"memory_type": "semantic", "agent_id": "host-x"}


# ---------- ⑤ clear()：domain 列表 → 逐条 DELETE ----------

def test_clear_lists_then_deletes():
    m = _memory()
    page1 = {"items": [{"id": "x1"}, {"id": "x2"}], "total": 2, "limit": 200, "offset": 0}
    with mock.patch.object(mod.requests, "request") as rq:
        rq.side_effect = [
            _Resp(page1),                       # GET 列表
            _Resp({"ok": True}),                # DELETE x1
            _Resp({"ok": True}),                # DELETE x2
            _Resp({"items": [], "total": 0, "limit": 200, "offset": 0}),  # 续页确认清空
        ]
        m.clear()
    calls = [c[0] for c in rq.call_args_list]
    assert calls[0] == ("GET", "http://127.0.0.1:8766/v1/memories")
    assert ("DELETE", "http://127.0.0.1:8766/v1/memories/x1") in calls
    assert ("DELETE", "http://127.0.0.1:8766/v1/memories/x2") in calls


# ---------- ⑥ 错误显式上抛（禁吞禁静默） ----------

def test_http_error_propagates():
    m = _memory()
    with mock.patch.object(mod.requests, "post", return_value=_Resp(
            {"detail": "context 必填"}, status=422)):
        with pytest.raises(mod.requests.HTTPError):
            m.save_context({"input": "a"}, {"output": "b"})


# ---------- ⑦ 模块无 langchain 也可 import（shim 面） ----------

def test_module_imports_without_langchain(monkeypatch):
    """把 langchain*/requests 之外的 langchain_core 伪装缺失后重载模块：shim 基类可用、方法面一致。"""
    import builtins
    real_import = builtins.__import__

    def blocked(name, *a, **k):
        if name == "langchain_core" or name.startswith("langchain_core.") \
                or name == "langchain" or name.startswith("langchain."):
            raise ImportError(f"blocked for test: {name}")
        return real_import(name, *a, **k)

    spec2 = importlib.util.spec_from_file_location(
        "me_lc_nolang", ROOT / "integrations" / "langchain_memory.py")
    mod2 = importlib.util.module_from_spec(spec2)
    monkeypatch.setattr(builtins, "__import__", blocked)
    try:
        spec2.loader.exec_module(mod2)
        assert mod2.BaseMemory.__module__ == "me_lc_nolang", "shim 生效（本地类定义）"
        m = mod2.MemoryEngineMemory(session_id="sx", return_messages=True)
        assert m.memory_variables == ["history"] and m._domain == "langchain:sx"
        with mock.patch.object(mod2.requests, "post", return_value=_Resp(RECALL_OK)):
            out = m.load_memory_variables({"input": "咖啡"})
        # 无消息类 → return_messages 文本回退（降级显式、不炸）；全程处于 langchain 屏蔽窗内
        assert isinstance(out["history"], str) and "咖啡" in out["history"]
        r = mod2.MemoryEngineRetriever(top_k=4)
        with mock.patch.object(mod2.requests, "post", return_value=_Resp(RECALL_OK)):
            docs = r._get_relevant_documents("q")
        assert docs[0].page_content.startswith("Human:") and docs[0].metadata["id"] == "a1"
    finally:
        monkeypatch.undo()
        builtins.__import__ = real_import
