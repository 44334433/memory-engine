"""memory-engine ↔ LangChain 适配器（外部反馈短板#5：生态接入）。

双实现（独立可选包，不进 daemon——铁律）：
- ``MemoryEngineMemory(BaseMemory)``：会话级记忆。``save_context`` 把一轮对话写入引擎
  （POST /v1/retain），``load_memory_variables`` 按会话召回（POST /v1/recall）注入提示词
  变量 ``history``。会话隔离=服务端 ``domain="langchain:<session_id>"``。
- ``MemoryEngineRetriever(BaseRetriever)``：query → ``Document`` 列表，供 RAG/检索链直挂。

依赖姿态（拍板：依赖仅 langchain-core 抽象类 + requests 直连 8766）：
- 运行期：``requests``（HTTP 唯一通道，与引擎侧「禁 import 旁路」同律）+ langchain-core 的
  ``BaseRetriever``/``Document``。
- ``BaseMemory`` 在 langchain-core 1.x 已随 memory 模块移除：导入链
  langchain_core.memory → langchain.memory（0.x）→ 内置 shim。shim 态模块仍可
  import/实例化/驱动，单测在纯 stdlib+requests 环境全绿（hermetic mock HTTP）。
- 字段声明在每个具体类上（不依赖 mixin 继承）：pydantic v1/v2 基类都按类注解收集字段，
  shim 基类走自带 __init__——一套代码三种基类形态通用。

字段与服务端契约对齐（api_core.py RetainItem/RecallRequest）：``context`` 必填（写入质量闸）；
``source_tier`` 缺省 ``user``（用户直输语义）；``tenant_id``/``agent_id`` 透传——None 键剔除
=关态零行为（MULTI_TENANT=1 时服务端强制默认租户、显式值优先，语义见 config 注释）。

最小示例 example_langchain_memory.py；单测 tests/test_langchain_adapter.py（mock HTTP）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import requests

log = logging.getLogger("memory-engine.langchain")

# —— 抽象类解析（langchain-core 1.x 已无 memory 模块 → 逐级回落到 shim） ——
try:  # langchain-core 0.x
    from langchain_core.memory import BaseMemory  # type: ignore
except ImportError:
    try:  # langchain 0.x
        from langchain.memory import BaseMemory  # type: ignore
    except ImportError:
        class BaseMemory:  # shim：无 langchain 环境仍可 import/实例化
            """Minimal stand-in for langchain's BaseMemory (abstract surface only)."""


try:
    from langchain_core.retrievers import BaseRetriever
    from langchain_core.documents import Document
except ImportError:
    class BaseRetriever:  # shim
        """Minimal stand-in for langchain_core BaseRetriever."""

        def get_relevant_documents(self, query: str, **kwargs: Any) -> list:
            return self._get_relevant_documents(query, **kwargs)

        def _get_relevant_documents(self, query: str, **kwargs: Any) -> list:
            raise NotImplementedError

    class Document:  # shim（字段面同 langchain_core.documents.Document）
        def __init__(self, page_content: str = "", metadata: Optional[dict] = None):
            self.page_content = page_content
            self.metadata = metadata or {}

        def __repr__(self) -> str:
            return (f"Document(page_content={self.page_content[:40]!r}, "
                    f"metadata={self.metadata!r})")

DEFAULT_BASE_URL = "http://127.0.0.1:8766"
_META_KEYS = ("id", "score", "title", "bank", "domain", "tags", "memory_type",
              "source_tier", "source_ref", "created_at", "ttl_state")


def _is_pydantic(obj) -> bool:
    """基类是否 pydantic 模型（v1 __fields__ / v2 model_fields 双探）。"""
    return any(hasattr(c, "model_fields") or hasattr(c, "__fields__")
               for c in type(obj).__mro__[1:])


def _init(self, **kwargs: Any) -> None:
    """pydantic 基类 → 交给模型校验；shim 基类 → kwargs 直赋（类属性即缺省值）。"""
    if _is_pydantic(self):
        super(type(self), self).__init__(**kwargs)
    else:
        for k, v in kwargs.items():
            setattr(self, k, v)


def _post(self, path: str, payload: dict) -> dict:
    resp = requests.post(f"{self.base_url.rstrip('/')}{path}", json=payload,
                         timeout=self.timeout_s)
    resp.raise_for_status()          # 失败显式上抛（不吞错；引擎「禁吞禁静默」同律）
    return resp.json()


def _request(self, method: str, path: str, **kw) -> requests.Response:
    resp = requests.request(method, f"{self.base_url.rstrip('/')}{path}",
                            timeout=self.timeout_s, **kw)
    resp.raise_for_status()
    return resp


def _scope(self) -> dict:
    """租户/宿主透传（None 剔除=关态零行为，服务端单宿主全量语义不变）。"""
    out = {}
    if getattr(self, "tenant_id", None):
        out["tenant_id"] = self.tenant_id
    if getattr(self, "agent_id", None):
        out["agent_id"] = self.agent_id
    return out


class MemoryEngineMemory(BaseMemory):
    """会话记忆：save_context 写入 / load_memory_variables 召回（domain 隔离会话）。"""

    base_url: str = DEFAULT_BASE_URL
    timeout_s: float = 10.0
    bank: str = "hermes"
    caller: str = "langchain"
    top_k: int = 5
    tenant_id: Optional[str] = None
    agent_id: Optional[str] = None
    session_id: str = "default"       # 会话标识 → domain="langchain:<session_id>"
    memory_key: str = "history"       # 注入提示词的变量名
    input_key: str = "input"
    output_key: str = "output"
    human_prefix: str = "Human"
    ai_prefix: str = "AI"
    return_messages: bool = False
    source_tier: str = "user"          # 用户直输语义（投毒闸四级来源，非外部降权轨）

    def __init__(self, **kwargs: Any) -> None:
        _init(self, **kwargs)

    @property
    def memory_variables(self) -> list:
        return [self.memory_key]

    @property
    def _domain(self) -> str:
        return f"langchain:{self.session_id}"

    def save_context(self, inputs: dict, outputs: dict) -> None:
        human = str(inputs.get(self.input_key) or "").strip()
        ai = str(outputs.get(self.output_key) or "").strip()
        if not human and not ai:
            return
        item = {
            "content": f"{self.human_prefix}: {human}\n{self.ai_prefix}: {ai}".strip(),
            "context": f"LangChain conversation memory (session={self.session_id})",
            "domain": self._domain,
            "tags": ["langchain", f"session:{self.session_id}"],
            "source_tier": self.source_tier,
            "source_type": "conversation",
            **_scope(self),
        }
        res = _post(self, "/v1/retain", {"bank": self.bank, "caller": self.caller,
                                         "items": [item]})
        if res.get("dedup_skipped"):
            log.debug("save_context dedup-skipped（重复轮次不重复入库，非错误）")

    def load_memory_variables(self, inputs: dict) -> dict:
        query = str(inputs.get(self.input_key) or "").strip()
        if not query:
            return {self.memory_key: [] if self.return_messages else ""}
        res = _post(self, "/v1/recall", {"query": query, "bank": self.bank,
                                         "caller": self.caller, "top_k": self.top_k,
                                         "filters": {"domain": self._domain, **_scope(self)}})
        turns = [t for t in ((r.get("body") or "").strip() for r in res.get("results", [])) if t]
        if not self.return_messages:
            return {self.memory_key: "\n".join(f"- {t}" for t in turns)}
        try:
            from langchain_core.messages import AIMessage, HumanMessage
        except ImportError:
            return {self.memory_key: "\n".join(f"- {t}" for t in turns)}
        msgs: list = []
        for t in turns:
            hp, ap = f"{self.human_prefix}: ", f"{self.ai_prefix}: "
            if t.startswith(hp) and ("\n" + ap) in t:      # save_context 写入格式可逆解析
                h, _, a = t.partition("\n" + ap)
                msgs += [HumanMessage(h[len(hp):].strip()), AIMessage(a.strip())]
            else:
                msgs.append(HumanMessage(t))
        return {self.memory_key: msgs}

    def clear(self) -> None:
        """清空本会话：按 domain 列表 → 逐条 DELETE（默认软删=retired；引擎保留历史可审计）。"""
        page = 200
        while True:
            resp = _request(self, "GET", "/v1/memories",
                            params={"domain": self._domain, "limit": page, "offset": 0})
            items = resp.json().get("items", [])
            if not items:
                return
            for it in items:
                _request(self, "DELETE", f"/v1/memories/{it['id']}")
            if len(items) < page:
                return


class MemoryEngineRetriever(BaseRetriever):
    """query → Document（page_content=记忆正文；metadata=可审计字段，含 score/score 链路）。"""

    base_url: str = DEFAULT_BASE_URL
    timeout_s: float = 10.0
    bank: str = "hermes"
    caller: str = "langchain"
    top_k: int = 5
    tenant_id: Optional[str] = None
    agent_id: Optional[str] = None
    extra_filters: dict = {}    # 服务端 filters 透传（memory_type/as_of/date_range/…）

    def __init__(self, **kwargs: Any) -> None:
        _init(self, **kwargs)

    def _get_relevant_documents(self, query: str, *, callbacks: Any = None,
                                **kwargs: Any) -> list:
        res = _post(self, "/v1/recall", {"query": query, "bank": self.bank,
                                         "caller": self.caller,
                                         "top_k": kwargs.pop("top_k", None) or self.top_k,
                                         "filters": {**self.extra_filters, **_scope(self)}})
        docs = []
        for r in res.get("results", []):
            meta = {k: r.get(k) for k in _META_KEYS if r.get(k) is not None}
            docs.append(Document(page_content=(r.get("body") or "").strip(), metadata=meta))
        return docs
