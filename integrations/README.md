# integrations — LangChain 适配器（可选生态层）

外部反馈短板 #5（生态接入）落地件。独立目录、不进 daemon、不被 `src/memory_engine`
反向 import——与 provider 插件同一纪律：HTTP 是唯一通道。

## 装什么

```bash
pip install requests langchain-core   # langchain-core 可选：缺席时 shim 兜底照样能跑
```

## 两个类

| 类 | 基类 | 映射 |
|---|---|---|
| `MemoryEngineMemory` | `BaseMemory`（langchain-core 1.x 已移除该模块 → 自动回落 shim） | `save_context` → `POST /v1/retain`；`load_memory_variables` → `POST /v1/recall`；`clear` → `GET /v1/memories?domain=` + 逐条 `DELETE` |
| `MemoryEngineRetriever` | `langchain_core.retrievers.BaseRetriever` | `_get_relevant_documents` → `POST /v1/recall` → `Document(page_content=body, metadata=可审计字段)` |

会话隔离 = 服务端 `domain="langchain:<session_id>"`（三主路统一过滤，零新端点）。
`source_tier` 缺省 `user`（用户直输语义，不走外部降权轨）；`tenant_id`/`agent_id`
显式传才带上——**不传零行为**，与服务端多租户开关缺省关语义对齐（开态服务端强制
默认租户，显式值优先）。

## 最小示例

```bash
python3 integrations/example_langchain_memory.py http://127.0.0.1:8766
```

```python
from integrations.langchain_memory import MemoryEngineRetriever

retriever = MemoryEngineRetriever(bank="knowledge", top_k=5)
docs = retriever.invoke("用户的部署偏好是什么？")   # langchain Runnable 接口
```

## 测试（hermetic，零网络）

```bash
/usr/bin/python3.12 -m pytest tests/test_langchain_adapter.py -v   # mock HTTP；无 langchain 环境同绿
```
